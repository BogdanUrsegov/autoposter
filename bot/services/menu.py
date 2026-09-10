from __future__ import annotations

from decimal import Decimal

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, FSInputFile, Message, ReplyKeyboardRemove

from bot import texts as t
from bot.keyboards import admin_reply_kb, menu_kb
from bot.services.emoji import apply_text
from bot.loader import bot
from config import BASE_DIR, settings
from database import SessionLocal
from database.crud import count_referrals, get_setting, get_settings_map, get_user_by_tg

BANNER = BASE_DIR / "assets" / "menu_banner.jpg"


def _user_id(event: Message | CallbackQuery) -> int | None:
    return event.from_user.id if event.from_user else None


def ref_link(username: str, tg_id: int) -> str:
    return f"https://t.me/{username}?start=r{tg_id}"


async def bot_username() -> str:
    stored = ""
    async with SessionLocal() as session:
        stored = await get_setting(session, "bot_username", "")
    if stored:
        return stored
    me = await bot.get_me()
    return me.username or "bot"


async def build_menu_card(tg_id: int) -> tuple[str, object] | tuple[None, None]:
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, tg_id)
        if not user or user.blocked:
            return None, None
        cfg = await get_settings_map(session)
        refs = await count_referrals(session, user.id)
        stored = await get_setting(session, "bot_username", "")
    username = stored or await bot_username()
    link = ref_link(username, user.tg_id)
    caption = apply_text(t.MENU_CAPTION.format(
        title=t.MENU_TITLE,
        desc=t.MENU_DESC,
        balance=f"{Decimal(user.balance):.4f}",
        clicks=user.clicks_total,
        reward=cfg.get("click_reward", "0.25"),
        refs=refs,
        link=link,
        bonus=cfg.get("ref_bonus", "5"),
        threshold=cfg.get("ref_threshold", "10"),
        percent=cfg.get("ref_percent", "5"),
    ))
    kb = menu_kb(settings.is_admin(tg_id), link=link)
    return caption, kb


async def send_main_menu(
    event: Message | CallbackQuery,
    text: str | None = None,
    *,
    edit: bool = False,
) -> None:
    uid = _user_id(event)
    if not uid:
        return
    caption, kb = await build_menu_card(uid)
    if caption is None or kb is None:
        if isinstance(event, CallbackQuery):
            await event.answer(t.CLICK_BLOCKED, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(t.CLICK_BLOCKED)
        return

    if isinstance(event, CallbackQuery):
        msg = event.message
        if not msg:
            await event.answer()
            return
        if not edit:
            try:
                await event.answer()
            except Exception:
                pass
        if edit and getattr(msg, "photo", None):
            try:
                await msg.edit_caption(caption=caption, parse_mode="HTML", reply_markup=kb)
                return
            except TelegramBadRequest as err:
                if "not modified" in str(err).lower():
                    return
        chat_id = msg.chat.id
        bot_obj = event.bot
    else:
        chat_id = event.chat.id
        bot_obj = event.bot
        gone = await event.answer(
            "\u2060",
            reply_markup=admin_reply_kb() if settings.is_admin(uid) else ReplyKeyboardRemove(),
        )
        try:
            await gone.delete()
        except Exception:
            pass

    if BANNER.exists():
        await bot_obj.send_photo(
            chat_id,
            FSInputFile(BANNER),
            caption=caption,
            parse_mode="HTML",
            reply_markup=kb,
        )
        return
    await bot_obj.send_message(chat_id, caption, parse_mode="HTML", reply_markup=kb)
