from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from bot.loader import bot
from bot.services.contests import (
    bot_username_cached,
    contest_stats,
    finish_contest,
    parse_duration,
    post_contest_to_channel,
)
from bot.services.emoji import apply_text
from config import settings
from database import SessionLocal
from database.crud import get_setting, set_setting, utcnow
from database.models import (
    AutoCombo,
    AutoComboHit,
    AutoComboPost,
    AutoComboRun,
    Click,
    Contest,
)

logger = logging.getLogger(__name__)

_loop_started = False
_combo_locks: dict[int, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()

ACTIVE_PHASES = ("pre", "contest", "gap", "after", "tail")


def fmt_secs(value: int | None) -> str:
    n = max(0, int(value or 0))
    if n >= 3600 and n % 3600 == 0:
        return f"{n // 3600}ч"
    if n >= 60 and n % 60 == 0:
        return f"{n // 60}м"
    if n >= 3600:
        h, rem = divmod(n, 3600)
        m = rem // 60
        return f"{h}ч {m}м" if m else f"{h}ч"
    if n >= 60:
        m, s = divmod(n, 60)
        return f"{m}м {s}с" if s else f"{m}м"
    return f"{n}с"


def fmt_clock(seconds: int | None) -> str:
    n = max(0, int(seconds or 0))
    h, rem = divmod(n, 3600)
    m = rem // 60
    return f"{h}:{m:02d}"


def parse_combo_duration(raw: str) -> int | None:
    text = (raw or "").strip().lower()
    if not text:
        return None
    chunks = re.findall(r"(\d+)\s*([a-zа-я]*)", text)
    if not chunks:
        return None
    total = 0
    for value, unit in chunks:
        part = parse_duration(value + unit)
        if part is None:
            return None
        total += part
    return total if total > 0 else None


def parse_combo_offset(raw: str) -> int | None:
    text = (raw or "").strip().lower()
    if text in ("0", "сразу", "старт", "-"):
        return 0
    seconds = parse_duration(raw)
    if seconds is None or seconds < 0:
        return None
    return seconds


def playlist_ready(combo: AutoCombo, posts: list[AutoComboPost] | None = None) -> bool:
    if posts is not None:
        return bool(posts)
    return bool(combo.channel_id)


def contest_ready(combo: AutoCombo) -> bool:
    return bool((combo.contest_text or "").strip() and combo.channel_id)


def bait_ready(combo: AutoCombo) -> bool:
    if combo.bait_copy_chat_id and combo.bait_copy_message_id:
        return True
    if (combo.bait_text or "").strip():
        return True
    return bool(combo.bait_media_file_id)


def _ids(raw: str | None) -> list[int]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    out: list[int] = []
    for item in data if isinstance(data, list) else []:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value and value not in out:
            out.append(value)
    return out


def _dump_ids(values: list[int]) -> str:
    out: list[int] = []
    for item in values:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value and value not in out:
            out.append(value)
    return json.dumps(out)


def _mids(raw: str | None) -> list[int]:
    return _ids(raw)


def _dump_mids(values: list[int]) -> str:
    return _dump_ids(values)


async def _lock_for(combo_id: int) -> asyncio.Lock:
    async with _locks_guard:
        lock = _combo_locks.get(combo_id)
        if lock is None:
            lock = asyncio.Lock()
            _combo_locks[combo_id] = lock
        return lock


def _job_from_snap(channel_id: str, snap: dict[str, Any]):
    class _Job:
        pass

    job = _Job()
    job.kind = "post"
    job.contest_id = None
    job.channel_id = channel_id
    job.copy_chat_id = snap.get("copy_chat_id")
    job.copy_message_id = snap.get("copy_message_id")
    job.extra_buttons = snap.get("extra_buttons") or "[]"
    job.text = snap.get("text") or ""
    job.parse_mode = snap.get("parse_mode") or "HTML"
    job.media_type = snap.get("media_type") or "none"
    job.media_file_id = snap.get("media_file_id") or ""
    return job


def _post_snap(post: AutoComboPost) -> dict[str, Any]:
    return {
        "copy_chat_id": post.copy_chat_id,
        "copy_message_id": post.copy_message_id,
        "extra_buttons": post.extra_buttons or "[]",
        "text": post.text or "",
        "parse_mode": post.parse_mode or "HTML",
        "media_type": post.media_type or "none",
        "media_file_id": post.media_file_id or "",
    }


async def is_channel_held(session, channel_id: str) -> bool:
    from bot.services.spammer import _keys_for, _lock_name

    for key in _keys_for(channel_id):
        flag = await get_setting(session, f"spam_hold:{_lock_name(key)}", "0")
        if flag == "1":
            return True
    return False


async def _set_hold_flags(session, channel_id: str, on: bool) -> None:
    from bot.services.spammer import _keys_for, _lock_name

    value = "1" if on else "0"
    for key in _keys_for(channel_id):
        await set_setting(session, f"spam_hold:{_lock_name(key)}", value)


def _same_channel(left: str, right: str) -> bool:
    from bot.services.spammer import _keys_for

    return bool(_keys_for(left) & _keys_for(right))


async def hold_channel_spam(channel_id: str) -> None:
    from bot.services.spammer import purge_channel_posts
    from database.models import SpamJob

    async with SessionLocal() as session:
        await _set_hold_flags(session, channel_id, True)
        jobs = list((await session.execute(select(SpamJob))).scalars().all())
        extras: dict[str, list[int]] = {}
        matched: list[str] = []
        for job in jobs:
            if not job.channel_id or not _same_channel(job.channel_id, channel_id):
                continue
            if job.last_message_id:
                extras.setdefault(job.channel_id, []).append(int(job.last_message_id))
            job.phase = "idle"
            job.last_message_id = None
            job.next_at = None
            if job.channel_id not in matched:
                matched.append(job.channel_id)
        if channel_id not in matched:
            matched.append(channel_id)
        for cid in matched:
            await purge_channel_posts(session, cid, extras.get(cid) or [])
        await session.commit()


async def release_channel_spam(channel_id: str) -> None:
    from bot.services.spammer import spam_is_running
    from database.models import SpamJob

    async with SessionLocal() as session:
        await _set_hold_flags(session, channel_id, False)
        if not await spam_is_running(session):
            await session.commit()
            return
        jobs = list(
            (
                await session.execute(
                    select(SpamJob).where(SpamJob.is_active.is_(True)).order_by(SpamJob.id)
                )
            ).scalars().all()
        )
        now = utcnow()
        first = True
        for job in jobs:
            if not job.channel_id or not _same_channel(job.channel_id, channel_id):
                continue
            job.phase = "idle"
            job.last_message_id = None
            if first:
                job.next_at = now
                first = False
            else:
                job.next_at = None
        await session.commit()


async def _notify_admins(text: str) -> None:
    body = apply_text(text)
    for admin_id in settings.admin_id_list:
        try:
            await bot.send_message(admin_id, body, parse_mode="HTML")
        except Exception:
            logger.warning("combo admin notify failed %s", admin_id)


async def _append_run_mid(run_id: int, mid: int | None) -> None:
    if not mid:
        return
    async with SessionLocal() as session:
        run = await session.get(AutoComboRun, run_id)
        if not run:
            return
        mids = _mids(run.posted_mids)
        if int(mid) not in mids:
            mids.append(int(mid))
            run.posted_mids = _dump_mids(mids)
            await session.commit()


async def _mark_posted(run_id: int, post_id: int, contest_id: int | None = None) -> None:
    async with SessionLocal() as session:
        run = await session.get(AutoComboRun, run_id)
        if not run:
            return
        posted = _ids(run.posted_ids)
        if post_id and post_id not in posted:
            posted.append(post_id)
            run.posted_ids = _dump_ids(posted)
        if contest_id:
            contests = _ids(run.contest_ids)
            if contest_id not in contests:
                contests.append(contest_id)
                run.contest_ids = _dump_ids(contests)
            run.contest_id = contest_id
        await session.commit()


async def _post_payload(channel_id: str, snap: dict[str, Any]) -> int | None:
    from bot.services.spammer import (
        _resolve_targets,
        begin_expect,
        send_spam_payload,
        wait_observed,
    )

    job = _job_from_snap(channel_id, snap)
    targets = await _resolve_targets(channel_id, None)
    chat = targets[0] if targets else None
    if chat is None:
        return None
    begin_expect(channel_id)
    mid = await send_spam_payload(job, chat)
    mids = await wait_observed(channel_id, mid, timeout=2.5 if not mid else 0.6)
    return mids[-1] if mids else mid


async def list_playlist(session, combo_id: int) -> list[AutoComboPost]:
    return list(
        (
            await session.execute(
                select(AutoComboPost)
                .where(AutoComboPost.combo_id == combo_id)
                .order_by(AutoComboPost.sort_order, AutoComboPost.id)
            )
        ).scalars().all()
    )


def post_slot(post: AutoComboPost) -> str:
    kind = (post.kind or "bait").strip() or "bait"
    if kind in ("after", "post"):
        return "after"
    if kind == "contest":
        return "contest"
    return "pre"


async def list_baits(session, combo_id: int, slot: str) -> list[AutoComboPost]:
    return [post for post in await list_playlist(session, combo_id) if post_slot(post) == slot]


def spawn_contest(combo: AutoCombo) -> Contest:
    now = utcnow()
    ends_at = None
    end_type = combo.contest_end_type or "participants"
    end_value = int(combo.contest_end_value or 0)
    if end_type == "time" and end_value > 0:
        ends_at = now + timedelta(seconds=end_value)
    winners = 0 if end_type == "none" else max(1, int(combo.contest_winners_count or 1))
    return Contest(
        code=secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10],
        text=combo.contest_text or "",
        parse_mode=combo.contest_parse_mode or "HTML",
        media_type=combo.contest_media_type or "none",
        media_file_id=combo.contest_media_file_id or "",
        button_text=combo.contest_button_text or "Участвовать",
        sponsors_count=int(combo.contest_sponsors_count or 0),
        channel_id=combo.channel_id or "",
        end_type=end_type,
        end_value=end_value,
        winners_count=winners,
        sponsor_kind=combo.contest_sponsor_kind or "service",
        own_sponsors=combo.contest_own_sponsors or "[]",
        status="active",
        source="auto",
        started_at=now,
        ends_at=ends_at,
    )


def _copy_contest_from_post(combo: AutoCombo, post: AutoComboPost) -> None:
    combo.contest_text = post.contest_text or ""
    combo.contest_parse_mode = post.contest_parse_mode or "HTML"
    combo.contest_media_type = post.contest_media_type or "none"
    combo.contest_media_file_id = post.contest_media_file_id or ""
    combo.contest_button_text = post.contest_button_text or "Участвовать"
    combo.contest_sponsors_count = int(post.contest_sponsors_count or 0)
    combo.contest_sponsor_kind = post.contest_sponsor_kind or "service"
    combo.contest_own_sponsors = post.contest_own_sponsors or "[]"
    combo.contest_end_type = post.contest_end_type or "participants"
    combo.contest_end_value = int(post.contest_end_value or 0)
    combo.contest_winners_count = int(post.contest_winners_count or 1)


async def ensure_contest_template(session, combo: AutoCombo) -> None:
    if contest_ready(combo):
        return
    for post in await list_playlist(session, combo.id):
        if post_slot(post) == "contest" and (post.contest_text or "").strip():
            _copy_contest_from_post(combo, post)
            combo.updated_at = utcnow()
            return


async def combo_can_run(session, combo: AutoCombo) -> bool:
    await ensure_contest_template(session, combo)
    if contest_ready(combo):
        return True
    pres = await list_baits(session, combo.id, "pre")
    afters = await list_baits(session, combo.id, "after")
    return bool(pres or afters)


async def save_contest_template(
    combo_id: int,
    *,
    text: str,
    photo: str,
    button: str,
    kind: str,
    sponsors: int,
    own: str,
    end_type: str,
    end_value: int,
    winners_n: int,
) -> tuple[AutoCombo | None, AutoComboPost | None]:
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if not combo:
            return None, None
        if kind not in ("own", "service", "none"):
            kind = "service"
        own_raw = own if kind == "own" else "[]"
        combo.contest_text = text
        combo.contest_parse_mode = "HTML"
        combo.contest_media_type = "photo" if photo else "none"
        combo.contest_media_file_id = photo or ""
        combo.contest_button_text = (button or "Участвовать")[:64]
        combo.contest_sponsor_kind = kind
        combo.contest_sponsors_count = int(sponsors or 0) if kind == "service" else 0
        combo.contest_own_sponsors = own_raw
        combo.contest_end_type = end_type
        combo.contest_end_value = int(end_value or 0)
        combo.contest_winners_count = 0 if end_type == "none" else max(1, int(winners_n or 1))
        combo.updated_at = utcnow()
        await session.commit()
        await session.refresh(combo)
        return combo, None


async def save_bait_post(combo_id: int, snap: dict, slot: str = "pre") -> AutoComboPost | None:
    kind = "after" if str(slot or "pre") == "after" else "pre"
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if not combo:
            return None
        combo.updated_at = utcnow()
        posts = await list_baits(session, combo.id, kind)
        order = (posts[-1].sort_order + 1) if posts else 0
        post = AutoComboPost(
            combo_id=combo.id,
            kind=kind,
            offset_seconds=0,
            sort_order=order,
            title=str(snap.get("title") or "Байт")[:128],
            copy_chat_id=snap.get("copy_chat_id"),
            copy_message_id=snap.get("copy_message_id"),
            extra_buttons=snap.get("extra_buttons") or "[]",
            text=snap.get("text") or "",
            parse_mode=snap.get("parse_mode") or "HTML",
            media_type=snap.get("media_type") or "none",
            media_file_id=snap.get("media_file_id") or "",
        )
        session.add(post)
        await session.commit()
        await session.refresh(post)
        return post


async def record_bait_hit(session, combo_id: int, user_id: int, is_new: bool) -> AutoComboRun | None:
    combo = await session.get(AutoCombo, combo_id)
    if not combo:
        return None
    run = None
    if combo.current_run_id:
        run = await session.get(AutoComboRun, combo.current_run_id)
    if run is None or run.status != "running":
        run = (
            await session.execute(
                select(AutoComboRun)
                .where(AutoComboRun.combo_id == combo_id)
                .order_by(AutoComboRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if run is None:
        return None
    exists = (
        await session.execute(
            select(AutoComboHit.id).where(
                AutoComboHit.run_id == run.id,
                AutoComboHit.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if exists:
        return run
    session.add(
        AutoComboHit(
            run_id=run.id,
            combo_id=combo_id,
            user_id=user_id,
            is_new=bool(is_new),
        )
    )
    return run


async def bait_stats(session, run_id: int) -> dict[str, int]:
    hits = int(
        (
            await session.execute(
                select(func.count()).select_from(AutoComboHit).where(AutoComboHit.run_id == run_id)
            )
        ).scalar()
        or 0
    )
    new_users = int(
        (
            await session.execute(
                select(func.count())
                .select_from(AutoComboHit)
                .where(AutoComboHit.run_id == run_id, AutoComboHit.is_new.is_(True))
            )
        ).scalar()
        or 0
    )
    hit_users = (
        select(AutoComboHit.user_id, func.min(AutoComboHit.created_at).label("first_at"))
        .where(AutoComboHit.run_id == run_id)
        .group_by(AutoComboHit.user_id)
        .subquery()
    )
    visitors = int((await session.execute(select(func.count()).select_from(hit_users))).scalar() or 0)
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
    return {
        "hits": hits,
        "new_users": new_users,
        "old_users": max(0, hits - new_users),
        "visitors": visitors,
        "clicked": clicked,
        "clicks_n": clicks_n,
        "not_clicked": max(0, visitors - clicked),
    }


def format_combo_stats(contests: list[dict], bait_data: dict, *, channel: str = "") -> str:
    lines = [
        "🎯 <b>Связка завершена</b>",
        f"Канал: <code>{channel}</code>" if channel else "",
        "",
    ]
    if not contests:
        lines.append("🏆 Конкурса в этой связке не было")
    for data in contests:
        lines.extend(
            [
                f"🏆 Конкурс <code>{data.get('code') or '—'}</code>",
                f"👥 Участников: <b>{data.get('participants', 0)}</b>",
                f"👣 Переходов: <b>{data.get('hits', 0)}</b>",
                f"🆕 Новых: <b>{data.get('new_users', 0)}</b> · ♻ старых <b>{data.get('old_users', 0)}</b>",
                f"⚡ Начали кликать: <b>{data.get('clicked', 0)}</b> из <b>{data.get('visitors', 0)}</b>",
                f"👆 Кликов: <b>{data.get('clicks_n', 0)}</b>",
                "",
            ]
        )
    lines.extend(
        [
            "🎣 <b>Байт</b>",
            f"👣 Переходов: <b>{bait_data.get('hits', 0)}</b>",
            f"🆕 Новых: <b>{bait_data.get('new_users', 0)}</b>",
            f"⚡ Начали кликать: <b>{bait_data.get('clicked', 0)}</b> из <b>{bait_data.get('visitors', 0)}</b>",
            f"👆 Кликов с байта: <b>{bait_data.get('clicks_n', 0)}</b>",
        ]
    )
    return "\n".join(line for line in lines if line)


async def _send_run_stats(run_id: int) -> None:
    async with SessionLocal() as session:
        run = await session.get(AutoComboRun, run_id)
        if not run:
            return
        contest_ids = _ids(run.contest_ids)
        if run.contest_id and run.contest_id not in contest_ids:
            contest_ids.append(run.contest_id)
        contests = []
        for cid in contest_ids:
            contests.append(await contest_stats(session, "all", cid))
        bait_data = await bait_stats(session, run.id)
        channel = run.channel_id or ""
    await _notify_admins(format_combo_stats(contests, bait_data, channel=channel))


async def _delete_run_posts(run: AutoComboRun) -> None:
    from bot.services.spammer import _delete_mids

    mids = _mids(run.posted_mids)
    extra: list[int] = []
    contest_ids = _ids(run.contest_ids)
    if run.contest_id and run.contest_id not in contest_ids:
        contest_ids.append(run.contest_id)
    if contest_ids:
        async with SessionLocal() as session:
            for cid in contest_ids:
                contest = await session.get(Contest, cid)
                if contest and contest.channel_message_id:
                    extra.append(int(contest.channel_message_id))
    all_mids = mids + extra
    if not all_mids:
        return
    await _delete_mids(run.channel_id or "", all_mids)


async def _finish_run(combo_id: int, *, abort: bool = False) -> None:
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if not combo:
            return
        run = await session.get(AutoComboRun, combo.current_run_id) if combo.current_run_id else None
        contest_ids = _ids(run.contest_ids) if run else []
        if run and run.contest_id and run.contest_id not in contest_ids:
            contest_ids.append(run.contest_id)
        channel_id = combo.channel_id
        run_id = run.id if run else None
        packed_run = run
        pause = max(0, int(combo.spam_seconds or 0))
        enabled = bool(combo.is_enabled)
        combo.phase = "idle"
        combo.warmup_index = 0
        combo.current_contest_id = None
        combo.current_run_id = None
        combo.updated_at = utcnow()
        if enabled and not abort:
            combo.next_at = utcnow() + timedelta(seconds=pause)
        else:
            combo.next_at = None
        if run:
            run.status = "cancelled" if abort else "done"
            run.finished_at = utcnow()
        await session.commit()
    for contest_id in contest_ids:
        try:
            await finish_contest(contest_id)
        except Exception:
            logger.exception("combo finish contest %s", contest_id)
    if packed_run:
        try:
            await _delete_run_posts(packed_run)
        except Exception:
            logger.exception("combo delete posts %s", run_id)
    loop_now = enabled and not abort and pause == 0
    if channel_id and not loop_now:
        try:
            await release_channel_spam(channel_id)
        except Exception:
            logger.exception("combo release spam %s", channel_id)
    if run_id and not abort:
        try:
            await _send_run_stats(run_id)
        except Exception:
            logger.exception("combo stats %s", run_id)
    if loop_now:
        await _begin_run(combo_id)


async def _begin_run(combo_id: int) -> bool:
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if not combo or not combo.is_enabled:
            return False
        await ensure_contest_template(session, combo)
        ready = await combo_can_run(session, combo)
        channel_id = combo.channel_id
        has_pre = bool(await list_baits(session, combo.id, "pre"))
        if not ready:
            combo.phase = "idle"
            pause = max(0, int(combo.spam_seconds or 0))
            combo.next_at = utcnow() + timedelta(seconds=pause or 3600)
            await session.commit()
            missing = True
        else:
            missing = False
            await session.commit()
    if missing:
        await _notify_admins(
            f"🎯 Связка не стартовала: нет байтов и конкурса.\nКанал <code>{channel_id}</code>"
        )
        return False
    await hold_channel_spam(channel_id)
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if not combo or not combo.is_enabled:
            await release_channel_spam(channel_id)
            return False
        run = AutoComboRun(
            combo_id=combo.id,
            channel_id=combo.channel_id,
            status="running",
            posted_mids="[]",
            posted_ids="[]",
            contest_ids="[]",
            started_at=utcnow(),
        )
        session.add(run)
        await session.flush()
        combo.current_run_id = run.id
        combo.warmup_index = 0
        combo.current_contest_id = None
        combo.phase = "pre" if has_pre else "contest"
        combo.next_at = utcnow()
        combo.updated_at = utcnow()
        await session.commit()
    return True


async def _post_bait(combo_id: int, post: AutoComboPost, run_id: int, channel_id: str) -> None:
    snap = _post_snap(post)
    snap["extra_buttons"] = post.extra_buttons or "[]"
    mid = await _post_payload(channel_id, snap)
    await _append_run_mid(run_id, mid)
    if not mid:
        logger.warning("combo bait post failed %s/%s", combo_id, post.id)
        return
    async with SessionLocal() as session:
        run = await session.get(AutoComboRun, run_id)
        if run:
            run.bait_posted_at = utcnow()
            await session.commit()
    await _mark_posted(run_id, post.id)


async def _post_contest(combo_id: int, run_id: int, channel_id: str) -> None:
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if not combo:
            return
        await ensure_contest_template(session, combo)
        if not contest_ready(combo):
            await session.commit()
            return
        packed = spawn_contest(combo)
        session.add(packed)
        await session.flush()
        contest_id = packed.id
        await session.commit()
    username = await bot_username_cached()
    mid = await post_contest_to_channel(packed, username, 0)
    await _append_run_mid(run_id, mid)
    if not mid:
        logger.warning("combo contest post failed %s", combo_id)
        await _notify_admins(f"🎯 Не смог запостить конкурс связки в <code>{channel_id}</code>.")
        await _mark_posted(run_id, 0, contest_id)
        return
    async with SessionLocal() as session:
        contest = await session.get(Contest, contest_id)
        combo = await session.get(AutoCombo, combo_id)
        run = await session.get(AutoComboRun, run_id)
        if contest:
            contest.channel_message_id = mid
        if run:
            run.contest_posted_at = utcnow()
        if combo:
            combo.current_contest_id = contest_id
        await session.commit()
    await _mark_posted(run_id, 0, contest_id)


async def _tick_baits(combo_id: int, slot: str) -> None:
    now = utcnow()
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        expected = "pre" if slot == "pre" else "after"
        if not combo or combo.phase != expected or not combo.current_run_id:
            return
        baits = await list_baits(session, combo.id, slot)
        idx = int(combo.warmup_index or 0)
        run_id = combo.current_run_id
        channel_id = combo.channel_id
        interval = max(0, int(combo.post_interval_seconds or 0))
        lifetime = max(0, int(combo.bait_lifetime_seconds or 0))
        if not baits or idx >= len(baits):
            combo.warmup_index = 0
            if slot == "pre":
                combo.phase = "contest"
                combo.next_at = now
            else:
                combo.phase = "tail"
                combo.next_at = now + timedelta(seconds=lifetime)
            combo.updated_at = now
            await session.commit()
            return
        post = baits[idx]
    await _post_bait(combo_id, post, run_id, channel_id)
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if not combo or combo.phase != expected:
            return
        combo.warmup_index = idx + 1
        baits = await list_baits(session, combo.id, slot)
        interval = max(0, int(combo.post_interval_seconds or 0))
        lifetime = max(0, int(combo.bait_lifetime_seconds or 0))
        if combo.warmup_index >= len(baits):
            combo.warmup_index = 0
            if slot == "pre":
                combo.phase = "contest"
                combo.next_at = utcnow() + timedelta(seconds=interval)
            else:
                combo.phase = "tail"
                combo.next_at = utcnow() + timedelta(seconds=lifetime)
        else:
            combo.next_at = utcnow() + timedelta(seconds=interval)
        combo.updated_at = utcnow()
        await session.commit()


async def _tick_contest(combo_id: int) -> None:
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if not combo or combo.phase != "contest" or not combo.current_run_id:
            return
        run_id = combo.current_run_id
        channel_id = combo.channel_id
        await ensure_contest_template(session, combo)
        ready = contest_ready(combo)
        gap = max(0, int(combo.after_contest_seconds or 0)) if ready else 0
        await session.commit()
    if ready:
        await _post_contest(combo_id, run_id, channel_id)
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if not combo or combo.phase != "contest":
            return
        combo.phase = "gap"
        combo.next_at = utcnow() + timedelta(seconds=gap)
        combo.updated_at = utcnow()
        await session.commit()


async def _tick_gap(combo_id: int) -> None:
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if not combo or combo.phase != "gap":
            return
        afters = await list_baits(session, combo.id, "after")
        if afters:
            combo.phase = "after"
            combo.warmup_index = 0
            combo.next_at = utcnow()
            combo.updated_at = utcnow()
            await session.commit()
            return
        await session.commit()
    await _finish_run(combo_id, abort=False)


async def tick_combo(combo_id: int) -> None:
    lock = await _lock_for(combo_id)
    async with lock:
        await _tick_combo(combo_id)


def _aware(dt):
    from bot.services.spammer import _aware as spam_aware

    return spam_aware(dt)


async def _tick_combo(combo_id: int) -> None:
    now = utcnow()
    abort = False
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if not combo:
            return
        phase = combo.phase or "idle"
        due = _aware(combo.next_at)
        enabled = bool(combo.is_enabled)
        if phase == "play":
            await session.commit()
            abort = True
        elif phase == "idle":
            if not enabled:
                if due is not None:
                    combo.next_at = None
                    await session.commit()
                return
            if due is not None and due > now:
                return
            await session.commit()
        elif not enabled and phase in ACTIVE_PHASES:
            await session.commit()
            abort = True
        elif due is not None and due > now:
            return
        else:
            await session.commit()

    if abort:
        await _finish_run(combo_id, abort=True)
        return
    if phase == "idle":
        await _begin_run(combo_id)
        return
    if phase == "pre":
        await _tick_baits(combo_id, "pre")
        return
    if phase == "contest":
        await _tick_contest(combo_id)
        return
    if phase == "gap":
        await _tick_gap(combo_id)
        return
    if phase == "after":
        await _tick_baits(combo_id, "after")
        return
    if phase == "tail":
        await _finish_run(combo_id, abort=False)


async def recover_holds() -> None:
    async with SessionLocal() as session:
        combos = list((await session.execute(select(AutoCombo))).scalars().all())
        stale: list[str] = []
        for combo in combos:
            if not combo.channel_id:
                continue
            live = combo.phase in ACTIVE_PHASES
            if live:
                await _set_hold_flags(session, combo.channel_id, True)
            elif await is_channel_held(session, combo.channel_id):
                stale.append(combo.channel_id)
        await session.commit()
    for channel_id in stale:
        await release_channel_spam(channel_id)


async def schedule_now(combo_id: int) -> str:
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if not combo:
            return "missing"
        if combo.phase in ACTIVE_PHASES:
            return "busy"
        if not await combo_can_run(session, combo):
            return "empty"
        combo.is_enabled = True
        combo.phase = "idle"
        combo.next_at = utcnow()
        combo.updated_at = utcnow()
        await session.commit()
        return "ok"


async def set_enabled(combo_id: int, enabled: bool) -> AutoCombo | None:
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if not combo:
            return None
        combo.is_enabled = bool(enabled)
        combo.updated_at = utcnow()
        running = combo.phase in ACTIVE_PHASES
        if enabled and combo.phase == "idle" and combo.next_at is None:
            combo.next_at = utcnow()
        await session.commit()
        packed_id = combo.id
        was_running = running and not enabled
    if was_running:
        await _finish_run(packed_id, abort=True)
        async with SessionLocal() as session:
            return await session.get(AutoCombo, packed_id)
    async with SessionLocal() as session:
        return await session.get(AutoCombo, packed_id)


async def combo_loop() -> None:
    global _loop_started
    if _loop_started:
        logger.warning("combo_loop already running")
        return
    _loop_started = True
    await asyncio.sleep(3)
    try:
        await recover_holds()
    except Exception:
        logger.exception("combo recover")
    try:
        while True:
            try:
                async with SessionLocal() as session:
                    ids = list((await session.execute(select(AutoCombo.id))).scalars().all())
                for combo_id in ids:
                    try:
                        await tick_combo(int(combo_id))
                    except Exception:
                        logger.exception("combo tick %s", combo_id)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("combo loop")
            await asyncio.sleep(1)
    finally:
        _loop_started = False
