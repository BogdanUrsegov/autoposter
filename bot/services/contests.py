from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import secrets
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from bot import texts as t
from bot.keyboards import clicker_kb, contest_op_kb, contest_own_kb, contest_status_kb, url_button
from bot.loader import bot
from bot.services.content import send_content
from bot.services.emoji import apply_text
from bot.services.providers import collect_sponsors, leftover_urls
from config import settings
from database import SessionLocal
from database.crud import get_setting, get_settings_map, get_user_by_tg, utcnow
from database.models import Click, Contest, ContestHit, ContestParticipant, ContestSub, User

logger = logging.getLogger(__name__)

def own_sponsor_buttons(contest: Contest) -> list[tuple[str, str]]:
    try:
        raw = json.loads(contest.own_sponsors or "[]")
    except json.JSONDecodeError:
        return []
    out: list[tuple[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("url"):
                out.append((str(item.get("title") or item.get("text") or t.TASK_BTN_SUB), str(item["url"])))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                out.append((str(item[0] or t.TASK_BTN_SUB), str(item[1])))
    return out


def contest_kind(contest: Contest) -> str:
    kind = (contest.sponsor_kind or "").strip()
    if kind in ("own", "service", "none"):
        return kind
    return "service" if int(contest.sponsors_count or 0) > 0 else "none"

SERVICE_TITLES = {
    "subgram": "SubGram",
    "tgrass": "Tgrass",
    "botohub": "BotoHub",
}


def contest_link(username: str, code: str) -> str:
    return f"https://t.me/{username}?start=g_{code}"


def parse_duration(raw: str) -> int | None:
    s = (raw or "").strip().lower().replace(" ", "")
    if not s:
        return None
    match = re.fullmatch(r"(\d+)([a-zа-я]*)", s)
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    if unit in ("", "м", "мин", "m", "min"):
        return value * 60
    if unit in ("с", "сек", "s", "sec"):
        return value
    if unit in ("ч", "час", "часа", "часов", "h", "hr", "hour", "hours"):
        return value * 3600
    if unit in ("д", "день", "дня", "дней", "d", "day", "days"):
        return value * 86400
    return None


def channel_target(raw: str):
    value = (raw or "").strip()
    value = value.replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "")
    value = value.split("?")[0].split("/")[0].strip()
    if not value:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    return value if value.startswith("@") else f"@{value.lstrip('@')}"


def mention_user(user: User | None) -> str:
    if not user:
        return "—"
    name = escape(user.first_name or "победитель")
    if user.username:
        return f"@{escape(user.username)}"
    return f'<a href="tg://user?id={user.tg_id}">{name}</a>'


def winners_block(users: list[User]) -> str:
    if not users:
        return "—"
    if len(users) == 1:
        return t.CONTEST_WINNER_ONE.format(winner=mention_user(users[0]))
    lines = [f"{i}. {mention_user(user)}" for i, user in enumerate(users, 1)]
    return t.CONTEST_WINNER_MANY.format(list="\n".join(lines))


async def participant_count(session, contest_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(ContestParticipant).where(
                    ContestParticipant.contest_id == contest_id
                )
            )
        ).scalar()
        or 0
    )


async def is_participant(session, contest_id: int, user_id: int) -> bool:
    row = (
        await session.execute(
            select(ContestParticipant.id).where(
                ContestParticipant.contest_id == contest_id,
                ContestParticipant.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    return row is not None


def _payload_urls(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return []
    urls = data.get("urls") or []
    return [str(u) for u in urls if u]


async def _notify_admins(text: str) -> None:
    for admin_id in settings.admin_id_list:
        try:
            await bot.send_message(admin_id, apply_text(text), parse_mode="HTML")
        except Exception:
            logger.warning("contest admin notify failed %s", admin_id)


def contest_join_label(button: str, count: int, contest: Contest) -> str:
    base = (button or "Участвовать").strip() or "Участвовать"
    return base[:64]


def contest_join_markup(contest: Contest, username: str, count: int) -> InlineKeyboardMarkup:
    return url_button(
        contest_join_label(contest.button_text, count, contest),
        contest_link(username, contest.code),
        colored=True,
    )


async def post_contest_to_channel(contest: Contest, username: str, count: int | None = None) -> int | None:
    chat = channel_target(contest.channel_id)
    if chat is None:
        return None
    n = 0 if count is None else max(0, int(count))
    markup = contest_join_markup(contest, username, n)
    body = contest.text or ""
    try:
        if contest.media_file_id and contest.media_type != "none" and len(body) > 1000:
            await send_content(
                bot,
                chat,
                "",
                parse_mode=contest.parse_mode or "HTML",
                media_type=contest.media_type or "photo",
                media_file_id=contest.media_file_id,
            )
            sent = await bot.send_message(chat, apply_text(body), parse_mode="HTML", reply_markup=markup)
            return sent.message_id if sent else None
        sent = await send_content(
            bot,
            chat,
            body,
            parse_mode=contest.parse_mode or "HTML",
            media_type=contest.media_type or "none",
            media_file_id=contest.media_file_id or "",
            reply_markup=markup,
            replace_markup=True,
        )
        return sent.message_id if sent else None
    except Exception:
        logger.exception("contest channel post failed")
        return None


async def restore_contest_post(contest_id: int) -> str:
    username = await bot_username_cached()
    async with SessionLocal() as session:
        contest = await session.get(Contest, contest_id)
        if not contest:
            return "missing"
        if not contest.channel_id:
            return "fail"
        count = await participant_count(session, contest.id)
        chat = channel_target(contest.channel_id)
        old_mid = contest.channel_message_id
        packed = contest
    if chat is not None and old_mid:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat,
                message_id=old_mid,
                reply_markup=contest_join_markup(packed, username, count),
            )
            return "alive"
        except TelegramBadRequest:
            pass
        except Exception:
            logger.warning("contest restore edit failed %s", contest_id)
    mid = await post_contest_to_channel(packed, username, count)
    if not mid:
        return "fail"
    async with SessionLocal() as session:
        row = await session.get(Contest, contest_id)
        if row:
            row.channel_message_id = mid
            await session.commit()
    return "ok"


async def clone_contest(contest_id: int) -> tuple[Contest | None, str]:
    username = await bot_username_cached()
    async with SessionLocal() as session:
        src = await session.get(Contest, contest_id)
        if not src:
            return None, "missing"
        code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10]
        now = utcnow()
        ends_at = None
        if src.end_type == "time":
            seconds = int(src.end_value or 0)
            if seconds <= 0 and src.started_at and src.ends_at:
                seconds = max(0, int((src.ends_at - src.started_at).total_seconds()))
            if seconds > 0:
                ends_at = now + timedelta(seconds=seconds)
        row = Contest(
            code=code,
            text=src.text,
            parse_mode=src.parse_mode or "HTML",
            media_type=src.media_type or "none",
            media_file_id=src.media_file_id or "",
            button_text=src.button_text or "Участвовать",
            sponsors_count=src.sponsors_count or 0,
            channel_id=src.channel_id or "",
            end_type=src.end_type or "participants",
            end_value=src.end_value or 0,
            winners_count=src.winners_count or 1,
            sponsor_kind=src.sponsor_kind or "service",
            own_sponsors=src.own_sponsors or "[]",
            status="active",
            started_at=now,
            ends_at=ends_at,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        packed = row
        new_id = row.id
    mid = await post_contest_to_channel(packed, username, 0)
    if mid:
        async with SessionLocal() as session:
            row = await session.get(Contest, new_id)
            if row:
                row.channel_message_id = mid
                await session.commit()
                packed = row
        return packed, "posted"
    return packed, "nopost"


async def send_contest_card(
    chat_id: int | str,
    contest: Contest,
    reply_markup: InlineKeyboardMarkup | None = None,
    extra_text: str = "",
) -> Message | None:
    body = contest.text or ""
    extra = apply_text(extra_text) if extra_text else ""
    media_type = contest.media_type or "none"
    file_id = contest.media_file_id or ""
    caption_ok = media_type != "none" and file_id and len(body) <= 1000
    if caption_ok:
        sent = await send_content(
            bot,
            chat_id,
            body,
            parse_mode=contest.parse_mode or "HTML",
            media_type=media_type,
            media_file_id=file_id,
            reply_markup=None if extra else reply_markup,
            replace_markup=not extra and bool(reply_markup),
        )
        if extra:
            return await bot.send_message(
                chat_id,
                extra,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        return sent
    if media_type != "none" and file_id:
        await send_content(
            bot,
            chat_id,
            "",
            parse_mode=contest.parse_mode or "HTML",
            media_type=media_type,
            media_file_id=file_id,
        )
    text = body
    if extra:
        text = f"{body}\n\n{extra}" if body.strip() else extra
    if not text.strip() and not reply_markup:
        return None
    return await bot.send_message(
        chat_id,
        apply_text(text or "‎"),
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def _enroll(session, contest: Contest, user: User) -> tuple[int, bool]:
    already = await is_participant(session, contest.id, user.id)
    if already:
        count = await participant_count(session, contest.id)
        return count, False
    session.add(ContestParticipant(contest_id=contest.id, user_id=user.id))
    user.pending_contest_id = None
    user.pending_contest_payload = "{}"
    await session.flush()
    count = await participant_count(session, contest.id)
    return count, True


async def update_channel_button(contest_id: int, count: int | None = None) -> None:
    username = await bot_username_cached()
    async with SessionLocal() as session:
        contest = await session.get(Contest, contest_id)
        if not contest or not contest.channel_id or not contest.channel_message_id:
            return
        if count is None:
            count = await participant_count(session, contest_id)
        markup = contest_join_markup(contest, username, count)
        chat = channel_target(contest.channel_id)
        message_id = contest.channel_message_id
    if chat is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat,
            message_id=message_id,
            reply_markup=markup,
        )
    except TelegramBadRequest:
        return
    except Exception:
        logger.warning("contest button update failed %s", contest_id)


async def record_contest_hit(session, contest_id: int, user_id: int, is_new: bool) -> None:
    exists = (
        await session.execute(
            select(ContestHit.id).where(
                ContestHit.contest_id == contest_id,
                ContestHit.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if exists:
        return
    session.add(ContestHit(contest_id=contest_id, user_id=user_id, is_new=bool(is_new)))


async def record_contest_subs(
    session,
    contest_id: int,
    user_id: int,
    done_urls: list[str],
    services: dict[str, str],
) -> None:
    if not done_urls:
        return
    existing = set(
        (
            await session.execute(
                select(ContestSub.url).where(
                    ContestSub.contest_id == contest_id,
                    ContestSub.user_id == user_id,
                    ContestSub.url.in_(done_urls),
                )
            )
        ).scalars().all()
    )
    for url in done_urls:
        if not url or url in existing:
            continue
        session.add(
            ContestSub(
                contest_id=contest_id,
                user_id=user_id,
                service=str(services.get(url) or ""),
                url=url[:512],
            )
        )


async def maybe_finish_by_participants(contest_id: int, count: int) -> None:
    async with SessionLocal() as session:
        contest = await session.get(Contest, contest_id)
        if not contest or contest.status != "active":
            return
        if contest.end_type != "participants":
            return
        if count < int(contest.end_value or 0):
            return
        await session.commit()
    await finish_contest(contest_id)


async def finish_contest(contest_id: int) -> bool:
    async with SessionLocal() as session:
        contest = await session.get(Contest, contest_id)
        if not contest or contest.status != "active":
            return False
        contest.status = "finished"
        contest.finished_at = utcnow()
        parts = list(
            (
                await session.execute(
                    select(ContestParticipant).where(ContestParticipant.contest_id == contest.id)
                )
            ).scalars().all()
        )
        count = len(parts)
        channel_id = contest.channel_id
        code = contest.code
        end_type = contest.end_type
        source = contest.source or "manual"
        winners: list[User] = []
        if end_type != "none":
            need = max(1, int(contest.winners_count or 1))
            if parts:
                picks = random.sample(parts, k=min(need, len(parts)))
                for pick in picks:
                    user = await session.get(User, pick.user_id)
                    if user:
                        winners.append(user)
            contest.winner_user_id = winners[0].id if winners else None
            contest.winner_ids = json.dumps([u.id for u in winners], ensure_ascii=False)
        else:
            contest.winner_user_id = None
            contest.winner_ids = "[]"
        await session.commit()
        winner_tgs = [u.tg_id for u in winners]
        block = winners_block(winners)

    if end_type == "none":
        await _notify_admins(f"🏁 Конкурс <code>{code}</code> остановлен без итогов.\nУчастников: <b>{count}</b>")
        return True

    if channel_id and source != "auto":
        chat = channel_target(channel_id)
        if chat is not None:
            if winners:
                body = t.CONTEST_CHANNEL_RESULTS.format(count=count, winners_block=block)
            else:
                body = t.CONTEST_CHANNEL_RESULTS_NONE
            try:
                await bot.send_message(chat, apply_text(body), parse_mode="HTML")
            except Exception:
                logger.exception("contest results post failed")

    for winner_tg in winner_tgs:
        try:
            await bot.send_message(
                winner_tg,
                apply_text(t.CONTEST_WINNER_DM),
                parse_mode="HTML",
                reply_markup=clicker_kb(),
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
        except Exception:
            logger.exception("contest winner dm failed")

    await _notify_admins(
        t.ADM_CONTEST_FINISH_ADMIN.format(code=code, count=count, winners_block=block)
    )
    return True


async def handle_contest_start(message: Message, user_id: int, code: str, is_new: bool = False) -> None:
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            await message.answer(apply_text(t.CONTEST_GONE), parse_mode="HTML")
            return
        contest = (
            await session.execute(select(Contest).where(Contest.code == code))
        ).scalar_one_or_none()
        if not contest:
            await message.answer(apply_text(t.CONTEST_GONE), parse_mode="HTML")
            return
        await record_contest_hit(session, contest.id, user.id, is_new)
        await session.commit()
        if contest.status != "active":
            await send_contest_card(message.chat.id, contest, extra_text=t.CONTEST_ENDED)
            return
        count = await participant_count(session, contest.id)
        if await is_participant(session, contest.id, user.id):
            await send_contest_card(
                message.chat.id,
                contest,
                reply_markup=contest_status_kb(),
                extra_text=t.CONTEST_ALREADY.format(count=count),
            )
            return
        cfg = await get_settings_map(session)
        kind = contest_kind(contest)
        contest_id = contest.id
        snap = {
            "text": contest.text,
            "parse_mode": contest.parse_mode,
            "media_type": contest.media_type,
            "media_file_id": contest.media_file_id,
        }
        buttons: list[tuple[str, str]] = []
        services: dict[str, str] = {}
        own_mode = False
        if kind == "own":
            buttons = own_sponsor_buttons(contest)
            own_mode = True
        elif kind == "service":
            need = int(contest.sponsors_count or 0)
            packed = await collect_sponsors(user, cfg, need) if need else []
            buttons = [(label, url) for label, url, _svc in packed]
            services = {url: svc for _label, url, svc in packed if url}
        if not buttons:
            count, added = await _enroll(session, contest, user)
            await session.commit()
            await send_contest_card(
                message.chat.id,
                contest,
                reply_markup=contest_status_kb(),
                extra_text=t.CONTEST_JOINED.format(count=count),
            )
            if added:
                await update_channel_button(contest_id, count)
                await maybe_finish_by_participants(contest_id, count)
            return
        if not own_mode:
            fresh = await session.get(User, user.id)
            if fresh:
                fresh.pending_contest_id = contest.id
                fresh.pending_contest_payload = json.dumps(
                    {
                        "urls": [url for _, url in buttons],
                        "buttons": [[label, url] for label, url in buttons],
                        "services": services,
                    },
                    ensure_ascii=False,
                )
        await session.commit()

    extra = apply_text(t.CONTEST_OWN if own_mode else t.CONTEST_OP)
    await send_content(
        bot,
        message.chat.id,
        snap["text"] or "",
        parse_mode=snap["parse_mode"] or "HTML",
        media_type=snap["media_type"] or "none",
        media_file_id=snap["media_file_id"] or "",
        replace_markup=False,
    )
    await bot.send_message(
        message.chat.id,
        extra,
        parse_mode="HTML",
        reply_markup=contest_own_kb(contest_id, buttons) if own_mode else contest_op_kb(contest_id, buttons),
    )


async def check_contest_op(tg_id: int, contest_id: int) -> tuple[str, Any]:
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, tg_id)
        contest = await session.get(Contest, contest_id)
        if not user or not contest:
            return "gone", None
        if contest.status != "active":
            user.pending_contest_id = None
            user.pending_contest_payload = "{}"
            await session.commit()
            return "ended", contest
        if await is_participant(session, contest.id, user.id):
            count = await participant_count(session, contest.id)
            return "already", (contest, count)
        urls = _payload_urls(user.pending_contest_payload)
        try:
            payload = json.loads(user.pending_contest_payload or "{}")
        except json.JSONDecodeError:
            payload = {}
        services = {str(k): str(v) for k, v in (payload.get("services") or {}).items() if k}
        cfg = await get_settings_map(session)
        left = await leftover_urls(user, cfg, urls) if urls else []
        done = [u for u in urls if u not in set(left)]
        await record_contest_subs(session, contest.id, user.id, done, services)
        if left:
            buttons = []
            labels = {u: t.TASK_BTN_SUB for u in left}
            for item in payload.get("buttons") or []:
                if isinstance(item, (list, tuple)) and len(item) == 2 and item[1] in labels:
                    labels[item[1]] = item[0] or t.TASK_BTN_SUB
            for url in left:
                buttons.append((labels.get(url) or t.TASK_BTN_SUB, url))
            user.pending_contest_payload = json.dumps(
                {
                    "urls": left,
                    "buttons": [[label, url] for label, url in buttons],
                    "services": {u: services[u] for u in left if u in services},
                },
                ensure_ascii=False,
            )
            await session.commit()
            return "left", (contest, buttons)
        count, added = await _enroll(session, contest, user)
        await session.commit()
    if added:
        await update_channel_button(contest_id, count)
        await maybe_finish_by_participants(contest_id, count)
    return "joined", (contest, count)


async def join_own_contest(tg_id: int, contest_id: int) -> tuple[str, Any]:
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, tg_id)
        contest = await session.get(Contest, contest_id)
        if not user or not contest:
            return "gone", None
        if contest.status != "active":
            return "ended", contest
        if await is_participant(session, contest.id, user.id):
            count = await participant_count(session, contest.id)
            return "already", (contest, count)
        count, added = await _enroll(session, contest, user)
        await session.commit()
    if added:
        await update_channel_button(contest_id, count)
        await maybe_finish_by_participants(contest_id, count)
    return "joined", (contest, count)


async def contest_watcher() -> None:
    await asyncio.sleep(5)
    while True:
        try:
            now = utcnow()
            async with SessionLocal() as session:
                rows = list(
                    (
                        await session.execute(
                            select(Contest.id).where(
                                Contest.status == "active",
                                Contest.end_type == "time",
                                Contest.ends_at.is_not(None),
                                Contest.ends_at <= now,
                            )
                        )
                    ).scalars().all()
                )
            for contest_id in rows:
                await finish_contest(contest_id)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("contest watcher")
        await asyncio.sleep(20)


async def bot_username_cached() -> str:
    async with SessionLocal() as session:
        stored = await get_setting(session, "bot_username", "")
    if stored:
        return stored
    me = await bot.get_me()
    return me.username or "bot"


def contest_period_start(period: str) -> datetime | None:
    msk = timezone(timedelta(hours=3))
    now = datetime.now(msk)
    today = datetime(now.year, now.month, now.day, tzinfo=msk)
    if period == "day":
        return today.astimezone(timezone.utc)
    if period == "week":
        return (today - timedelta(days=now.weekday())).astimezone(timezone.utc)
    if period == "month":
        return datetime(now.year, now.month, 1, tzinfo=msk).astimezone(timezone.utc)
    return None


async def contest_stats(session, period: str = "day", contest_id: int | None = None) -> dict:
    start = contest_period_start(period)

    def since(column):
        if start is None:
            return True
        return column >= start

    hits_q = select(func.count()).select_from(ContestHit).where(since(ContestHit.created_at))
    new_q = select(func.count()).select_from(ContestHit).where(since(ContestHit.created_at), ContestHit.is_new.is_(True))
    parts_q = select(func.count()).select_from(ContestParticipant).where(since(ContestParticipant.created_at))
    subs_q = select(ContestSub.service, func.count()).where(since(ContestSub.created_at)).group_by(ContestSub.service)
    if contest_id:
        hits_q = hits_q.where(ContestHit.contest_id == contest_id)
        new_q = new_q.where(ContestHit.contest_id == contest_id)
        parts_q = parts_q.where(ContestParticipant.contest_id == contest_id)
        subs_q = subs_q.where(ContestSub.contest_id == contest_id)

    hits = int((await session.execute(hits_q)).scalar() or 0)
    new_users = int((await session.execute(new_q)).scalar() or 0)
    old_users = max(0, hits - new_users)
    participants = int((await session.execute(parts_q)).scalar() or 0)
    by_service = {str(name or "other"): int(n) for name, n in (await session.execute(subs_q)).all()}
    subs_total = sum(by_service.values())

    hit_users = (
        select(ContestHit.user_id, func.min(ContestHit.created_at).label("first_at"))
        .where(since(ContestHit.created_at))
    )
    if contest_id:
        hit_users = hit_users.where(ContestHit.contest_id == contest_id)
    hit_users = hit_users.group_by(ContestHit.user_id).subquery()
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
    visitors = int(
        (
            await session.execute(select(func.count()).select_from(hit_users))
        ).scalar()
        or 0
    )

    contests = []
    if contest_id is None:
        rows = list((await session.execute(select(Contest).order_by(Contest.id.desc()).limit(20))).scalars().all())
        for item in rows:
            one = await contest_stats(session, period, item.id)
            one["id"] = item.id
            one["code"] = item.code
            one["status"] = item.status
            contests.append(one)

    code = ""
    status = ""
    if contest_id:
        row = await session.get(Contest, contest_id)
        if row:
            code = row.code
            status = row.status

    return {
        "period": period,
        "contest_id": contest_id,
        "code": code,
        "status": status,
        "hits": hits,
        "new_users": new_users,
        "old_users": old_users,
        "participants": participants,
        "subs_total": subs_total,
        "by_service": by_service,
        "clicked": clicked,
        "not_clicked": max(0, visitors - clicked),
        "clicks_n": clicks_n,
        "visitors": visitors,
        "contests": contests,
    }


def format_contest_stats(data: dict) -> str:
    labels = {"day": "день", "week": "неделя", "month": "месяц"}
    period = labels.get(data.get("period") or "day", data.get("period") or "")
    if data.get("contest_id"):
        title = f"📊 <b>Стата конкурса</b> <code>{data.get('code') or data['contest_id']}</code> · {period}"
    else:
        title = f"📊 <b>Стата конкурсов</b> · {period}"
    lines = [
        title,
        "",
        f"👥 <b>Участников</b> — <b>{data['participants']}</b>",
        f"👣 <b>Переходов по ссылке</b> — <b>{data['hits']}</b>",
        f"🆕 <b>Новых в боте</b> — <b>{data['new_users']}</b>",
        f"♻ <b>Старых</b> — <b>{data['old_users']}</b>",
        f"✅ <b>Подписок на каналы сервисов</b> — <b>{data['subs_total']}</b>",
        "",
        "<b>Клики после конкурса</b>",
        f"⚡ <b>Начали кликать</b> — <b>{data.get('clicked', 0)}</b> из <b>{data.get('visitors', 0)}</b>",
        f"😴 <b>Не кликали</b> — <b>{data.get('not_clicked', 0)}</b>",
        f"👆 <b>Кликов с конкурса</b> — <b>{data.get('clicks_n', 0)}</b>",
        "",
        "<b>ПДП по сервисам</b>",
    ]
    by_service: dict = data.get("by_service") or {}
    if not by_service:
        lines.append("пока нет")
    else:
        for key in ("subgram", "tgrass", "botohub"):
            if key in by_service:
                lines.append(f"• {SERVICE_TITLES.get(key, key)} — <b>{by_service[key]}</b>")
        for key, value in by_service.items():
            if key in SERVICE_TITLES:
                continue
            lines.append(f"• {key or 'другое'} — <b>{value}</b>")
    extra = [c for c in (data.get("contests") or []) if c.get("participants") or c.get("hits") or c.get("subs_total")]
    if extra:
        lines.append("")
        lines.append("<b>По конкурсам</b>")
        for item in extra[:12]:
            lines.append(
                f"<code>{item.get('code')}</code> · уч. {item['participants']} · "
                f"новые {item['new_users']} · кликали {item.get('clicked', 0)} · "
                f"кликов {item.get('clicks_n', 0)}"
            )
    return "\n".join(lines)
