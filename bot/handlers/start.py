from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select

from bot.keyboards import post_markup
from bot.loader import bot
from bot.services.content import send_content
from bot.services.contests import handle_contest_start
from bot.services.menu import send_main_menu
from bot.services.reminders import schedule_hour_remind
from bot.services.shows import cancel_shows, schedule_shows
from bot.services.webapp_menu import setup_admin_webapp_button
from bot import texts as t
from config import settings
from database import SessionLocal
from database.crud import active_ads, get_setting, get_user_by_tg
from database.models import Campaign, CampaignHit

router = Router()


def _payload_parts(payload: str | None) -> tuple[str | None, str | None, str | None, str | None]:
    if not payload:
        return None, None, None, None
    raw = payload.strip()
    if raw.startswith("g_"):
        return None, None, raw[2:], None
    if raw.startswith("b_"):
        return None, None, None, raw[2:]
    if raw.startswith("c_"):
        return raw[2:], None, None, None
    if raw.startswith("r") and raw[1:].isdigit():
        return None, raw[1:], None, None
    if raw.startswith("ref"):
        digits = raw[3:].lstrip("_")
        if digits.isdigit():
            return None, digits, None, None
    return raw, None, None, None


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    if not message.from_user:
        return

    campaign_code, ref_tg, contest_code, bait_code = _payload_parts(command.args)
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, message.from_user.id)
        if not user:
            return
        if user.blocked:
            await message.answer(t.START_BLOCKED)
            return

        is_unique = user.start_count == 0
        user.start_count += 1
        user_id = user.id

        if bait_code and bait_code.isdigit():
            from bot.services.combo import record_bait_hit

            await record_bait_hit(session, int(bait_code), user.id, is_unique)

        if campaign_code:
            campaign = (
                await session.execute(select(Campaign).where(Campaign.code == campaign_code))
            ).scalar_one_or_none()
            if campaign:
                session.add(
                    CampaignHit(
                        campaign_id=campaign.id,
                        user_id=user.id,
                        is_unique=is_unique,
                        is_premium=bool(user.is_premium),
                    )
                )
                if is_unique:
                    user.campaign_id = campaign.id

        if is_unique and ref_tg and int(ref_tg) != user.tg_id and user.referrer_id is None:
            referrer = await get_user_by_tg(session, int(ref_tg))
            if referrer and referrer.id != user.id:
                user.referrer_id = referrer.id

        ads = [] if contest_code else await active_ads(session)
        welcome = await get_setting(session, "welcome_text", t.WELCOME_MENU)
        pending_greet = bool(user.pending_greeting_id)
        await session.commit()

    if is_unique:
        schedule_hour_remind(message.from_user.id)

    if pending_greet:
        from bot.services.greetings import deliver_pending_post

        await deliver_pending_post(message.from_user.id)

    if contest_code:
        cancel_shows(message.from_user.id)
        await handle_contest_start(message, user_id, contest_code, is_unique)
        await schedule_shows(message.from_user.id)
        if settings.is_admin(message.from_user.id):
            await setup_admin_webapp_button(bot, message.from_user.id)
        return

    for ad in ads:
        markup = post_markup(ad.extra_buttons, ad.button_type, ad.button_text, ad.button_url)
        await send_content(
            bot,
            message.chat.id,
            ad.text,
            parse_mode=ad.parse_mode,
            media_type=ad.media_type,
            media_file_id=ad.media_file_id,
            media_path=ad.media_path,
            reply_markup=markup,
            copy_chat_id=ad.copy_chat_id,
            copy_message_id=ad.copy_message_id,
            replace_markup=bool(markup),
        )

    cancel_shows(message.from_user.id)
    await send_main_menu(message, welcome or t.WELCOME_MENU)
    await schedule_shows(message.from_user.id)
    if settings.is_admin(message.from_user.id):
        await setup_admin_webapp_button(bot, message.from_user.id)
