from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from bot.keyboards import load_inline_markup
from bot.loader import bot
from bot.services.content import send_content
from bot.services.contests import contest_period_start
from bot import texts as t
from database import SessionLocal
from database.crud import get_user_by_tg, utcnow
from database.models import ChannelGreeting, Click, GreetingLead, GreetingPost, SavedChannel, User

logger = logging.getLogger(__name__)

DEFAULT_GREET = t.GREET_DEFAULT
_COPY_GONE = (
    "message to copy not found",
    "message can't be copied",
    "message_id_invalid",
    "chat not found",
    "chat_id is empty",
)
_inflight: set[int] = set()


def _payload_ready(row) -> bool:
    return bool(row.copy_chat_id and row.copy_message_id) or bool((row.text or "").strip() or row.media_file_id)


def _pack_payload(row) -> dict:
    delay = getattr(row, "delay_after_seconds", None)
    if delay is None:
        delay = getattr(row, "post_delay_seconds", 0)
    return {
        "copy_chat_id": row.copy_chat_id,
        "copy_message_id": row.copy_message_id,
        "extra_buttons": row.extra_buttons or "[]",
        "text": row.text or "",
        "parse_mode": row.parse_mode or "HTML",
        "media_type": row.media_type or "none",
        "media_file_id": row.media_file_id or "",
        "delay_after_seconds": max(0, int(delay or 0)),
    }


async def list_greeting_posts(session, greeting_id: int) -> list[GreetingPost]:
    return list(
        (
            await session.execute(
                select(GreetingPost)
                .where(GreetingPost.greeting_id == greeting_id)
                .order_by(GreetingPost.sort_order, GreetingPost.id)
            )
        )
        .scalars()
        .all()
    )


async def migrate_legacy_post(session, greeting: ChannelGreeting) -> None:
    posts = await list_greeting_posts(session, greeting.id)
    if posts:
        return
    if not _payload_ready(greeting):
        return
    session.add(
        GreetingPost(
            greeting_id=greeting.id,
            sort_order=0,
            copy_chat_id=greeting.copy_chat_id,
            copy_message_id=greeting.copy_message_id,
            extra_buttons=greeting.extra_buttons or "[]",
            text=greeting.text or "",
            parse_mode=greeting.parse_mode or "HTML",
            media_type=greeting.media_type or "none",
            media_file_id=greeting.media_file_id or "",
            delay_after_seconds=max(0, int(greeting.post_delay_seconds or 5)),
        )
    )
    await session.flush()


async def packed_greeting_posts(session, greeting: ChannelGreeting) -> list[dict]:
    await migrate_legacy_post(session, greeting)
    posts = await list_greeting_posts(session, greeting.id)
    packed = [_pack_payload(row) for row in posts if _payload_ready(row)]
    if not packed and _payload_ready(greeting):
        packed = [_pack_payload(greeting)]
    seen: set[tuple] = set()
    unique: list[dict] = []
    for payload in packed:
        key = (
            int(payload.get("copy_chat_id") or 0),
            int(payload.get("copy_message_id") or 0),
            payload.get("media_file_id") or "",
            (payload.get("text") or "")[:120],
            payload.get("extra_buttons") or "",
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(payload)
    rows = await list_greeting_posts(session, greeting.id)
    kept: set[tuple] = set()
    for row in rows:
        key = (
            int(row.copy_chat_id or 0),
            int(row.copy_message_id or 0),
            row.media_file_id or "",
            (row.text or "")[:120],
            row.extra_buttons or "",
        )
        if key in kept:
            await session.delete(row)
        else:
            kept.add(key)
    return unique


def greeting_has_post(row: ChannelGreeting, posts: list | None = None) -> bool:
    if posts:
        return True
    return _payload_ready(row)


async def get_or_create_greeting(session, channel_id: str) -> ChannelGreeting:
    row = (
        await session.execute(select(ChannelGreeting).where(ChannelGreeting.channel_id == str(channel_id)))
    ).scalar_one_or_none()
    if row:
        await migrate_legacy_post(session, row)
        return row
    row = ChannelGreeting(channel_id=str(channel_id), greet_text=DEFAULT_GREET, is_active=False)
    session.add(row)
    await session.flush()
    return row


async def send_greeting_payload(tg_id: int, payload: dict) -> bool:
    markup = load_inline_markup(payload.get("extra_buttons") or "[]")
    copy_chat_id = payload.get("copy_chat_id")
    copy_message_id = payload.get("copy_message_id")
    copied = False
    if copy_chat_id and copy_message_id:
        try:
            copy_kw: dict = {
                "chat_id": tg_id,
                "from_chat_id": copy_chat_id,
                "message_id": copy_message_id,
            }
            if markup:
                copy_kw["reply_markup"] = markup
            sent = await bot.copy_message(**copy_kw)
            copied = True
            if sent:
                # Same as the spammer: copy_message already applied the markup, so the
                # re-apply is redundant. Keyword args only — aiogram 3.7+ takes
                # business_connection_id first positionally.
                if markup and "reply_markup" not in copy_kw:
                    try:
                        await bot.edit_message_reply_markup(
                            chat_id=tg_id,
                            message_id=sent.message_id,
                            reply_markup=markup,
                        )
                    except Exception as exc:
                        logger.warning("greet markup edit failed %s: %s", tg_id, exc)
                return True
            return True
        except (TelegramNetworkError, TelegramRetryAfter, TimeoutError, asyncio.TimeoutError):
            logger.warning("greet copy network, skip fallback %s", tg_id)
            return copied
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            err = str(exc).lower()
            if copied or not any(token in err for token in _COPY_GONE):
                logger.warning("greet copy failed, skip fallback: %s", exc)
                return copied
            logger.warning("greet copy source gone, fallback send")
        except Exception:
            logger.exception("greet copy failed")
            return copied
    try:
        sent = await send_content(
            bot,
            tg_id,
            payload.get("text") or "",
            parse_mode=payload.get("parse_mode") or "HTML",
            media_type=payload.get("media_type") or "none",
            media_file_id=payload.get("media_file_id") or "",
            reply_markup=markup,
            replace_markup=bool(markup),
        )
        return bool(sent)
    except Exception:
        logger.exception("greet post failed")
        return False


async def send_greeting_post(tg_id: int, greeting: ChannelGreeting) -> bool:
    return await send_greeting_payload(tg_id, _pack_payload(greeting))


async def send_greeting_sequence(tg_id: int, posts: list[dict], delay: int | None = None) -> bool:
    any_ok = False
    fallback = max(0, int(delay or 0))
    last = len(posts) - 1
    for i, payload in enumerate(posts):
        if await send_greeting_payload(tg_id, payload):
            any_ok = True
        if i >= last:
            continue
        wait = payload.get("delay_after_seconds")
        if wait is None:
            wait = fallback
        wait = max(0, int(wait or 0))
        if wait:
            await asyncio.sleep(wait)
    return any_ok


async def _mark_posted(user_id: int, greeting_id: int, ok: bool) -> None:
    async with SessionLocal() as session:
        lead = (
            await session.execute(
                select(GreetingLead).where(
                    GreetingLead.greeting_id == greeting_id,
                    GreetingLead.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if lead:
            if ok:
                lead.posted = True
                lead.posted_at = utcnow()
            lead.replied = True
            if not lead.replied_at:
                lead.replied_at = utcnow()
        await session.commit()


async def handle_join_request(tg_id: int, channel_id: str, *, is_new: bool) -> str:
    async with SessionLocal() as session:
        greeting = (
            await session.execute(
                select(ChannelGreeting).where(
                    ChannelGreeting.channel_id == str(channel_id),
                    ChannelGreeting.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if not greeting:
            return "skip"
        posts = await packed_greeting_posts(session, greeting)
        if not posts:
            return "skip"
        user = await get_user_by_tg(session, tg_id)
        if not user:
            return "skip"
        lead = (
            await session.execute(
                select(GreetingLead).where(
                    GreetingLead.greeting_id == greeting.id,
                    GreetingLead.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if lead and not lead.failed:
            return "exists"
        if not lead:
            lead = GreetingLead(
                greeting_id=greeting.id,
                channel_id=str(channel_id),
                user_id=user.id,
                tg_id=tg_id,
                is_new=is_new,
            )
            session.add(lead)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                return "exists"
        text = greeting.greet_text or DEFAULT_GREET
        greeting_id = greeting.id
        lead_id = lead.id
        await session.commit()

    try:
        try:
            await bot.send_message(tg_id, text, parse_mode="HTML")
        except TelegramBadRequest:
            await bot.send_message(tg_id, text)
        ok = True
    except (TelegramForbiddenError, TelegramBadRequest):
        ok = False
    except Exception:
        logger.exception("greet dm failed")
        ok = False

    async with SessionLocal() as session:
        lead = await session.get(GreetingLead, lead_id)
        user = await get_user_by_tg(session, tg_id)
        if lead:
            if ok:
                lead.greeted = True
                lead.failed = False
                lead.greeted_at = utcnow()
            else:
                lead.failed = True
        if user and ok:
            user.pending_greeting_id = greeting_id
        await session.commit()
    return "ok" if ok else "fail"


async def deliver_pending_post(tg_id: int) -> bool:
    if tg_id in _inflight:
        return False
    _inflight.add(tg_id)
    posts: list[dict] = []
    delay = 0
    greeting_id = 0
    user_id = 0
    claimed = False
    try:
        async with SessionLocal() as session:
            user = (
                await session.execute(select(User).where(User.tg_id == tg_id).with_for_update())
            ).scalar_one_or_none()
            if not user or not user.pending_greeting_id:
                await session.commit()
            elif not await session.get(ChannelGreeting, user.pending_greeting_id):
                user.pending_greeting_id = None
                await session.commit()
            else:
                greeting = await session.get(ChannelGreeting, user.pending_greeting_id)
                packed = await packed_greeting_posts(session, greeting)
                lead = (
                    await session.execute(
                        select(GreetingLead)
                        .where(
                            GreetingLead.greeting_id == greeting.id,
                            GreetingLead.user_id == user.id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if not packed or (lead and lead.posted):
                    user.pending_greeting_id = None
                    await session.commit()
                else:
                    result = await session.execute(
                        update(User)
                        .where(User.id == user.id, User.pending_greeting_id == greeting.id)
                        .values(pending_greeting_id=None)
                    )
                    if result.rowcount:
                        posts = packed
                        delay = max(0, int(greeting.post_delay_seconds or 0))
                        greeting_id = greeting.id
                        user_id = user.id
                        if lead:
                            lead.replied = True
                            lead.replied_at = utcnow()
                            lead.posted = True
                        claimed = True
                    await session.commit()
    except Exception:
        _inflight.discard(tg_id)
        raise

    if not claimed:
        _inflight.discard(tg_id)
        return False

    async def _run() -> None:
        try:
            ok = await send_greeting_sequence(tg_id, posts, delay)
            await _mark_posted(user_id, greeting_id, ok)
        finally:
            _inflight.discard(tg_id)

    asyncio.create_task(_run())
    return True


async def greeting_stats(session, period: str = "day", channel_id: str | None = None) -> dict:
    start = contest_period_start(period)

    def since(column):
        if start is None:
            return True
        return column >= start

    async def _count(*extra):
        q = select(func.count()).select_from(GreetingLead).where(since(GreetingLead.created_at), *extra)
        if channel_id:
            q = q.where(GreetingLead.channel_id == str(channel_id))
        return int((await session.execute(q)).scalar() or 0)

    requests = await _count()
    greeted = await _count(GreetingLead.greeted.is_(True))
    failed = await _count(GreetingLead.failed.is_(True))
    replies = await _count(GreetingLead.replied.is_(True))
    posts = await _count(GreetingLead.posted.is_(True))
    new_users = await _count(GreetingLead.is_new.is_(True))
    old_users = max(0, requests - new_users)

    hit_users = (
        select(GreetingLead.user_id, func.min(GreetingLead.created_at).label("first_at"))
        .where(since(GreetingLead.created_at))
    )
    if channel_id:
        hit_users = hit_users.where(GreetingLead.channel_id == str(channel_id))
    hit_users = hit_users.group_by(GreetingLead.user_id).subquery()
    clicked = int(
        (
            await session.execute(
                select(func.count(func.distinct(Click.user_id)))
                .select_from(Click)
                .join(hit_users, Click.user_id == hit_users.c.user_id)
                .where(Click.created_at >= hit_users.c.first_at)
            )
        ).scalar()
        or 0
    )
    clicks_n = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Click)
                .join(hit_users, Click.user_id == hit_users.c.user_id)
                .where(Click.created_at >= hit_users.c.first_at)
            )
        ).scalar()
        or 0
    )
    visitors = int((await session.execute(select(func.count()).select_from(hit_users))).scalar() or 0)

    def pct(part: int, total: int) -> str:
        if not total:
            return "0%"
        return f"{round(part * 100 / total)}%"

    title = ""
    if channel_id:
        ch = (
            await session.execute(select(SavedChannel).where(SavedChannel.chat_id == str(channel_id)))
        ).scalar_one_or_none()
        title = (ch.username or ch.title or channel_id) if ch else str(channel_id)

    return {
        "period": period,
        "channel_id": channel_id,
        "title": title,
        "requests": requests,
        "greeted": greeted,
        "failed": failed,
        "replies": replies,
        "posts": posts,
        "new_users": new_users,
        "old_users": old_users,
        "clicked": clicked,
        "not_clicked": max(0, visitors - clicked),
        "clicks_n": clicks_n,
        "visitors": visitors,
        "reply_rate": pct(replies, greeted),
        "post_rate": pct(posts, replies),
        "click_rate": pct(clicked, visitors),
    }


def format_greeting_stats(data: dict) -> str:
    labels = {"day": "день", "week": "неделя", "month": "месяц"}
    period = labels.get(data.get("period") or "day", data.get("period") or "")
    who = data.get("title") or "все каналы"
    return "\n".join(
        [
            f"👋 <b>Стата приветки</b> · {who} · {period}",
            "",
            f"📥 <b>Заявок в канал</b> — <b>{data['requests']}</b>",
            f"👋 <b>Приветок отправлено</b> — <b>{data['greeted']}</b>",
            f"🚫 <b>Не смог написать</b> — <b>{data['failed']}</b>",
            f"💬 <b>Ответов</b> — <b>{data['replies']}</b> ({data['reply_rate']})",
            f"📨 <b>Постов отправлено</b> — <b>{data['posts']}</b> ({data['post_rate']})",
            f"🆕 <b>Новых в боте</b> — <b>{data['new_users']}</b>",
            f"♻ <b>Старых</b> — <b>{data['old_users']}</b>",
            "",
            "<b>Клики после приветки</b>",
            f"⚡ <b>Начали кликать</b> — <b>{data.get('clicked', 0)}</b> из <b>{data.get('visitors', 0)}</b> ({data['click_rate']})",
            f"😴 <b>Не кликали</b> — <b>{data.get('not_clicked', 0)}</b>",
            f"👆 <b>Кликов</b> — <b>{data.get('clicks_n', 0)}</b>",
        ]
    )
