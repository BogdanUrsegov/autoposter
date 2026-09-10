from __future__ import annotations

import logging
from datetime import timezone
from decimal import Decimal

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from sqlalchemy import select

from bot import texts as t
from bot.keyboards import clicker_kb, post_markup, service_task_kb, task_kb
from bot.services.content import send_content
from bot.services.tasks import TaskItem, chat_from_url, chat_target, next_task
from bot.services.emoji import apply_text
from bot.services.menu import send_main_menu
from bot.loader import bot
from database import SessionLocal
from database.crud import (
    apply_referral_milestone,
    apply_referral_percent,
    get_settings_map,
    get_user_by_tg,
    today_str,
    utcnow,
)
from database.models import Click, Offer, OfferCompletion, User

router = Router()
logger = logging.getLogger(__name__)


def _dec(value: str, fallback: str) -> Decimal:
    try:
        return Decimal(value)
    except Exception:
        return Decimal(fallback)


async def _clicker_text(user: User, cfg: dict[str, str]) -> str:
    reward = cfg.get("click_reward", "0.25")
    cd = int(float(cfg.get("click_cooldown", "0") or 0))
    limit = int(float(cfg.get("click_daily_limit", "0") or 0))
    every = max(1, int(float(cfg.get("task_every_n", "5") or 5)))
    if user.clicks_today_date != today_str():
        used = 0
    else:
        used = user.clicks_today
    limit_line = t.CLICKER_LIMIT_NONE if limit <= 0 else t.CLICKER_LIMIT.format(used=used, limit=limit)
    cd_line = t.CLICKER_CD_NONE if cd <= 0 else t.CLICKER_CD.format(seconds=cd)
    until = every - (user.clicks_since_task % every)
    if user.pending_task_id or user.pending_service:
        until = 0
    return t.CLICKER.format(
        balance=f"{Decimal(user.balance):.2f}",
        reward=reward,
        limit_line=limit_line,
        cd_line=cd_line,
        until_task=until,
    )


async def _send_task(chat_id: int, item: TaskItem | Offer, user=None, cfg=None) -> None:
    if isinstance(item, TaskItem) and item.view_only:
        if item.promo_ad:
            ad = item.promo_ad
            markup = post_markup(ad.extra_buttons, ad.button_type, ad.button_text, ad.button_url)
            await send_content(
                bot,
                chat_id,
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
            return
        if item.promo_show:
            show = item.promo_show
            markup = post_markup(
                show.extra_buttons, show.button_type, show.button_text, show.button_url
            )
            await send_content(
                bot,
                chat_id,
                show.text,
                parse_mode=show.parse_mode,
                media_type=show.media_type,
                media_file_id=show.media_file_id,
                media_path=show.media_path,
                reply_markup=markup,
                copy_chat_id=show.copy_chat_id,
                copy_message_id=show.copy_message_id,
                replace_markup=bool(markup),
            )
            return
    if isinstance(item, TaskItem) and item.sponsor:
        sponsor = item.sponsor
        if (not sponsor.buttons) and user is not None and cfg is not None:
            from bot.services.providers import FETCHERS, take_one

            fn = FETCHERS.get(sponsor.service)
            if fn:
                fresh = take_one(await fn(user, cfg))
                if fresh:
                    sponsor = fresh
        await bot.send_message(
            chat_id,
            apply_text(sponsor.text or t.TASK_PROMPT),
            parse_mode="HTML",
            reply_markup=service_task_kb(sponsor.service, sponsor.buttons),
        )
        return
    offer = item.offer if isinstance(item, TaskItem) else item
    if not offer:
        return
    if isinstance(item, TaskItem) and item.view_only:
        markup = post_markup(
            getattr(offer, "extra_buttons", "[]"),
            "url" if offer.button_url else "none",
            offer.button_text or "",
            offer.button_url or "",
        )
        await send_content(
            bot,
            chat_id,
            offer.text or t.TASK_PROMPT,
            parse_mode=offer.parse_mode or "HTML",
            media_type=offer.media_type,
            media_file_id=offer.media_file_id,
            media_path=offer.media_path,
            reply_markup=markup,
            copy_chat_id=offer.copy_chat_id,
            copy_message_id=offer.copy_message_id,
            replace_markup=bool(markup),
        )
        return
    url = offer.button_url
    if not url and offer.chat_id:
        chat = str(offer.chat_id).lstrip("@")
        if not chat.lstrip("-").isdigit():
            url = f"https://t.me/{chat}"
    markup = task_kb(offer.id, url, offer.button_text or t.TASK_BTN_SUB)
    body = (offer.text or "").strip() or t.TASK_PROMPT
    sent = await send_content(
        bot,
        chat_id,
        body,
        parse_mode=offer.parse_mode or "HTML",
        media_type=offer.media_type,
        media_file_id=offer.media_file_id,
        media_path=offer.media_path,
        reply_markup=markup,
        copy_chat_id=offer.copy_chat_id,
        copy_message_id=offer.copy_message_id,
        replace_markup=True,
    )
    if sent and not (offer.text or "").strip():
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=sent.message_id,
                caption=apply_text(t.TASK_PROMPT),
                parse_mode="HTML",
                reply_markup=markup,
            )
        except TelegramBadRequest:
            pass


@router.callback_query(F.data == "go:clicker")
async def open_clicker(event: CallbackQuery) -> None:
    await send_main_menu(event, edit=True)
    try:
        await event.answer()
    except Exception:
        pass


@router.callback_query(F.data == "go:click")
async def do_click(event: CallbackQuery) -> None:
    user_tg = event.from_user
    if not user_tg:
        return
    if not event.message:
        await event.answer()
        return
    chat_id = event.message.chat.id
    need_task = False
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, user_tg.id)
        if not user or user.blocked:
            await event.answer(t.CLICK_BLOCKED, show_alert=True)
            return
        cfg = await get_settings_map(session)
        if user.pending_task_id or user.pending_service:
            item = await next_task(session, user)
            await session.commit()
            await event.answer(t.CLICK_TASK_LOCK, show_alert=True)
            if item:
                await _send_task(chat_id, item, user, cfg)
            return

        today = today_str()
        if user.clicks_today_date != today:
            user.clicks_today = 0
            user.clicks_today_date = today

        limit = int(float(cfg.get("click_daily_limit", "0") or 0))
        if limit > 0 and user.clicks_today >= limit:
            await event.answer(t.CLICK_LIMIT, show_alert=True)
            return

        cd = int(float(cfg.get("click_cooldown", "0") or 0))
        now = utcnow()
        if cd > 0 and user.last_click_at:
            last = user.last_click_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            elapsed = (now - last).total_seconds()
            if elapsed < cd:
                await event.answer(t.CLICK_CD.format(seconds=int(cd - elapsed) + 1), show_alert=True)
                return

        reward = _dec(cfg.get("click_reward", "0.25"), "0.25")
        every = max(1, int(float(cfg.get("task_every_n", "5") or 5)))
        percent = _dec(cfg.get("ref_percent", "5"), "5")
        threshold = _dec(cfg.get("ref_threshold", "10"), "10")
        bonus = _dec(cfg.get("ref_bonus", "5"), "5")

        user.balance = Decimal(user.balance) + reward
        user.total_earned = Decimal(user.total_earned) + reward
        user.clicks_total += 1
        user.clicks_today += 1
        user.clicks_since_task += 1
        user.last_click_at = now
        session.add(Click(user_id=user.id, reward=reward))
        await apply_referral_percent(session, user, reward, percent)
        await apply_referral_milestone(session, user, threshold, bonus)

        need_task = user.clicks_since_task >= every
        if need_task:
            user.clicks_since_task = 0

        await session.commit()
        await event.answer(t.CLICK_OK.format(reward=f"{reward:.2f}"))
        await send_main_menu(event, edit=True)

    if need_task:
        try:
            async with SessionLocal() as session:
                user = await get_user_by_tg(session, user_tg.id)
                if not user or user.blocked:
                    return
                cfg = await get_settings_map(session)
                item = await next_task(session, user)
                await session.commit()
            if item:
                await _send_task(chat_id, item, user, cfg)
        except Exception:
            logger.exception("task after click failed")


@router.callback_query(F.data.startswith("task:check:"))
async def check_task(callback: CallbackQuery) -> None:
    if not callback.from_user:
        return
    offer_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, callback.from_user.id)
        offer = await session.get(Offer, offer_id)
        if not user or not offer:
            await callback.answer("👀 Ещё не готово", show_alert=True)
            return
        ok = await _verify_offer(callback.bot, user, offer)
        if not ok:
            await callback.answer(t.TASK_NOT_DONE, show_alert=True)
            return
        exists = (
            await session.execute(
                select(OfferCompletion).where(
                    OfferCompletion.user_id == user.id, OfferCompletion.offer_id == offer.id
                )
            )
        ).scalar_one_or_none()
        if not exists:
            session.add(OfferCompletion(user_id=user.id, offer_id=offer.id))
        user.pending_task_id = None
        await session.commit()
    await callback.answer(t.TASK_DONE)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(t.TASK_DONE, reply_markup=clicker_kb())


@router.callback_query(F.data.startswith("task:svc:"))
async def check_service_task(callback: CallbackQuery) -> None:
    if not callback.from_user:
        return
    service = callback.data.split(":")[-1]
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, callback.from_user.id)
        if not user:
            await callback.answer("🙈 Не нашли тебя", show_alert=True)
            return
        cfg = await get_settings_map(session)
        from bot.services.providers import verify_sponsor

        ok = await verify_sponsor(service, user, cfg)
        if not ok:
            await callback.answer(t.TASK_NOT_DONE, show_alert=True)
            return
        user.pending_service = ""
        user.pending_service_payload = "{}"
        user.pending_task_id = None
        await session.commit()
    await callback.answer(t.TASK_DONE)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(t.TASK_DONE, reply_markup=clicker_kb())


async def _verify_offer(bot, user: User, offer: Offer) -> bool:
    chat = (offer.chat_id or "").strip() or chat_from_url(offer.button_url)
    if not chat:
        return False
    try:
        member = await bot.get_chat_member(chat_target(chat), user.tg_id)
        return member.status in ("member", "administrator", "creator", "restricted")
    except TelegramBadRequest:
        return False
    except Exception:
        return False
