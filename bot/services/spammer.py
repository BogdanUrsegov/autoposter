from __future__ import annotations

import asyncio
import json
import logging
import time
import zlib
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from sqlalchemy import select, text, update

from bot.keyboards import load_inline_markup
from bot.loader import bot
from bot.services.content import send_content
from bot.services.contests import (
    bot_username_cached,
    channel_target,
    contest_join_markup,
    participant_count,
)
from database import SessionLocal
from database.crud import get_setting, set_setting, utcnow
from database.models import Contest, SavedChannel, SpamJob

SPAM_FLAG = "spam_running"

_COPY_GONE = (
    "message to copy not found",
    "message can't be copied",
    "message_id_invalid",
    "chat not found",
    "chat_id is empty",
)

logger = logging.getLogger(__name__)
_spam_loop_started = False

_channel_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()
_expect: dict[str, dict] = {}
_chat_targets: dict[str, tuple[float, list]] = {}

_GONE = (
    "message to delete not found",
    "message identifier is invalid",
    "message_id_invalid",
    "message not found",
    "message can't be deleted",
    "message_delete_forbidden",
)

# A mid that keeps failing to delete (no rights, older than 48h, …) must not pin the
# rotation forever — after this many attempts we stop retrying it.
_MAX_DELETE_ATTEMPTS = 5


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _lock_name(channel_id: str) -> str:
    raw = str(channel_id or "").strip()
    if raw.lstrip("-").isdigit():
        return str(int(raw))
    return raw


async def _lock_for(channel_id: str) -> asyncio.Lock:
    name = _lock_name(channel_id)
    async with _locks_guard:
        lock = _channel_locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            _channel_locks[name] = lock
        return lock


def _snapshot(job: SpamJob):
    class _Snap:
        pass

    snap = _Snap()
    for key in (
        "kind",
        "channel_id",
        "contest_id",
        "copy_chat_id",
        "copy_message_id",
        "extra_buttons",
        "text",
        "parse_mode",
        "media_type",
        "media_file_id",
    ):
        setattr(snap, key, getattr(job, key))
    return snap


def _result_mid(sent) -> int | None:
    mid = getattr(sent, "message_id", None) if sent is not None else None
    try:
        return int(mid) if mid else None
    except (TypeError, ValueError):
        return None


def _uniq_mids(values) -> list[int]:
    out: list[int] = []
    for item in values or []:
        try:
            mid = int(item)
        except (TypeError, ValueError):
            continue
        if mid and mid not in out:
            out.append(mid)
    return out


def _keys_for(channel_id: str) -> set[str]:
    keys = {str(channel_id or "").strip()}
    raw = str(channel_id or "").strip()
    if raw.lstrip("-").isdigit():
        keys.add(str(int(raw)))
    tgt = channel_target(raw)
    if tgt is not None:
        keys.add(str(tgt))
    return {key for key in keys if key}


def _live_key(channel_id: str) -> str:
    return f"spam_live:{channel_id}"


def begin_expect(channel_id: str) -> None:
    bucket = {"until": time.monotonic() + 6, "mids": [], "chat": None}
    for key in _keys_for(channel_id):
        _expect[key] = bucket


def _expect_bucket(channel_id: str) -> dict | None:
    for key in _keys_for(channel_id):
        bucket = _expect.get(key)
        if bucket:
            return bucket
    return None


async def wait_observed(channel_id: str, known: int | None = None, timeout: float = 0.8) -> list[int]:
    if known:
        timeout = min(timeout, 0.8)
    deadline = time.monotonic() + timeout
    found: list[int] = []
    while time.monotonic() < deadline:
        bucket = _expect_bucket(channel_id)
        found = list(bucket["mids"]) if bucket else []
        if known or found:
            await asyncio.sleep(0.25)
            bucket = _expect_bucket(channel_id)
            found = list(bucket["mids"]) if bucket else []
            break
        await asyncio.sleep(0.1)
    return _uniq_mids(([known] if known else []) + found)


def is_channel_service(message) -> bool:
    if getattr(message, "new_chat_title", None):
        return True
    if getattr(message, "new_chat_photo", None):
        return True
    if getattr(message, "delete_chat_photo", None):
        return True
    ctype = str(getattr(message, "content_type", "") or "")
    return ctype in {"new_chat_title", "new_chat_photo", "delete_chat_photo"}


async def on_channel_post(message) -> None:
    chat = getattr(message, "chat", None)
    mid = getattr(message, "message_id", None)
    if chat is None or not mid:
        return
    if is_channel_service(message):
        try:
            await bot.delete_message(chat_id=chat.id, message_id=int(mid))
        except Exception:
            try:
                await message.delete()
            except Exception:
                logger.warning("service msg delete failed %s/%s", chat.id, mid)
        return
    keys = {str(chat.id)}
    username = getattr(chat, "username", None)
    if username:
        keys.add(f"@{username}")
        keys.add(str(username))
    now = time.monotonic()
    for key, bucket in list(_expect.items()):
        if now > float(bucket.get("until") or 0):
            continue
        same = key in keys
        if not same and str(key).lstrip("-").isdigit():
            try:
                same = int(key) == int(chat.id)
            except (TypeError, ValueError):
                same = False
        if not same:
            continue
        if int(mid) not in bucket["mids"]:
            bucket["mids"].append(int(mid))
        bucket["chat"] = int(chat.id)


async def _load_live_full(session, channel_id: str) -> tuple[int | None, list[int], dict[str, int]]:
    raw = await get_setting(session, _live_key(channel_id), "")
    if not raw:
        return None, [], {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, [], {}
    if isinstance(data, list):
        return None, _uniq_mids(data), {}
    if not isinstance(data, dict):
        return None, [], {}
    chat = data.get("chat")
    try:
        chat_id = int(chat) if chat else None
    except (TypeError, ValueError):
        chat_id = None
    fails = data.get("fails")
    if not isinstance(fails, dict):
        fails = {}
    clean: dict[str, int] = {}
    for key, value in fails.items():
        try:
            clean[str(int(key))] = int(value)
        except (TypeError, ValueError):
            continue
    return chat_id, _uniq_mids(data.get("mids") or []), clean


async def _load_live(session, channel_id: str) -> tuple[int | None, list[int]]:
    chat, mids, _ = await _load_live_full(session, channel_id)
    return chat, mids


async def _save_live(
    session,
    channel_id: str,
    chat: int | None,
    mids: list[int],
    fails: dict[str, int] | None = None,
) -> None:
    kept = _uniq_mids(mids)
    payload: dict = {"chat": chat, "mids": kept}
    if fails:
        payload["fails"] = {key: val for key, val in fails.items() if int(key) in kept}
    await set_setting(
        session,
        _live_key(channel_id),
        json.dumps(payload, ensure_ascii=False),
    )


async def append_live_mid(channel_id: str, chat: int | None, mid: int | None) -> None:
    """Persist a freshly posted mid right away, so a crash between posting and the
    bookkeeping commit cannot leave an undeletable orphan in the channel."""
    if not mid:
        return
    async with SessionLocal() as session:
        known_chat, mids, fails = await _load_live_full(session, channel_id)
        if int(mid) in mids:
            return
        await _save_live(session, channel_id, chat or known_chat, mids + [int(mid)], fails)
        await session.commit()


async def pop_live_mids(session, channel_id: str) -> tuple[int | None, list[int]]:
    return await _load_live(session, channel_id)


async def _resolve_targets(channel_id: str, extra_chat: int | None = None) -> list:
    now = time.monotonic()
    cached = _chat_targets.get(str(channel_id))
    if cached and cached[0] > now:
        targets = list(cached[1])
    else:
        targets = []
        tgt = channel_target(channel_id)
        if tgt is not None:
            targets.append(tgt)
        try:
            info = await bot.get_chat(tgt if tgt is not None else extra_chat)
            if getattr(info, "id", None):
                targets.insert(0, int(info.id))
            if getattr(info, "username", None):
                targets.append(f"@{info.username}")
        except Exception:
            pass
        async with SessionLocal() as session:
            raw = str(channel_id or "").strip()
            row = (
                await session.execute(select(SavedChannel).where(SavedChannel.chat_id == raw))
            ).scalar_one_or_none()
            if row is None and raw.lstrip("-").isdigit():
                row = (
                    await session.execute(select(SavedChannel).where(SavedChannel.chat_id == str(int(raw))))
                ).scalar_one_or_none()
            if row is None and raw:
                uname = raw if raw.startswith("@") else f"@{raw.lstrip('@')}"
                row = (
                    await session.execute(select(SavedChannel).where(SavedChannel.username == uname))
                ).scalar_one_or_none()
            if row:
                if str(row.chat_id).lstrip("-").isdigit():
                    targets.insert(0, int(row.chat_id))
                if row.username:
                    targets.append(row.username if str(row.username).startswith("@") else f"@{row.username}")
        uniq: list = []
        for item in targets:
            if item not in uniq:
                uniq.append(item)
        targets = uniq
        _chat_targets[str(channel_id)] = (now + 60, list(targets))
    if extra_chat is not None and extra_chat not in targets:
        targets.insert(0, extra_chat)
    return targets


async def _delete_one(target, mid: int) -> str:
    try:
        await bot.delete_message(chat_id=target, message_id=int(mid))
        return "ok"
    except TelegramRetryAfter as exc:
        wait = min(float(getattr(exc, "retry_after", 1) or 1) + 0.2, 8)
        await asyncio.sleep(wait)
        try:
            await bot.delete_message(chat_id=target, message_id=int(mid))
            return "ok"
        except TelegramBadRequest as inner:
            err = str(inner).lower()
            return "gone" if any(token in err for token in _GONE) else "fail"
        except Exception:
            return "fail"
    except TelegramBadRequest as exc:
        err = str(exc).lower()
        if any(token in err for token in _GONE):
            return "gone"
        logger.warning("spam delete %s/%s: %s", target, mid, exc)
        return "fail"
    except TelegramForbiddenError as exc:
        logger.warning("spam delete forbidden %s/%s: %s", target, mid, exc)
        return "fail"
    except Exception:
        logger.warning("spam delete failed %s/%s", target, mid)
        return "fail"


async def _delete_mids(channel_id: str, mids: list[int], chat: int | None = None) -> list[int]:
    # Only the ids we actually posted. The old code also tried mid+1 and mid+2 to guess
    # album parts, but every spam post is a single message (copy_message / send_content
    # never build a media group), so that only deleted other people's posts and filled
    # the log with "message to delete not found".
    ids = _uniq_mids(mids)
    if not ids:
        return []
    targets = await _resolve_targets(channel_id, chat)
    if not targets:
        return _uniq_mids(mids)
    failed: list[int] = []
    for mid in ids:
        status = "fail"
        for target in targets:
            status = await _delete_one(target, mid)
            if status in ("ok", "gone"):
                break
        if status == "fail":
            failed.append(mid)
    return failed


async def purge_channel_posts(session, channel_id: str, extra: list[int] | None = None) -> list[int]:
    chat, mids, fails = await _load_live_full(session, channel_id)
    all_mids = _uniq_mids(list(mids) + list(extra or []))
    failed = await _delete_mids(channel_id, all_mids, chat)

    retry: list[int] = []
    next_fails: dict[str, int] = {}
    for mid in failed:
        attempts = fails.get(str(mid), 0) + 1
        if attempts >= _MAX_DELETE_ATTEMPTS:
            logger.warning("spam giving up on %s/%s after %s attempts", channel_id, mid, attempts)
            continue
        next_fails[str(mid)] = attempts
        retry.append(mid)

    await _save_live(session, channel_id, chat, retry, next_fails)
    return retry


async def send_spam_payload(job: SpamJob, chat) -> int | None:
    if chat is None:
        return None
    if job.kind == "contest" and job.contest_id:
        username = await bot_username_cached()
        async with SessionLocal() as session:
            contest = await session.get(Contest, job.contest_id)
            if not contest:
                return None
            count = await participant_count(session, contest.id)
            markup = contest_join_markup(contest, username, count)
            text = contest.text
            parse_mode = contest.parse_mode
            media_type = contest.media_type
            media_file_id = contest.media_file_id
        try:
            sent = await send_content(
                bot,
                chat,
                text or "",
                parse_mode=parse_mode or "HTML",
                media_type=media_type or "none",
                media_file_id=media_file_id or "",
                reply_markup=markup,
                replace_markup=True,
            )
            return _result_mid(sent)
        except Exception:
            logger.exception("spam contest post failed")
            return None

    markup = load_inline_markup(job.extra_buttons or "[]")
    copied = False
    if job.copy_chat_id and job.copy_message_id:
        try:
            copy_kw: dict = {
                "chat_id": chat,
                "from_chat_id": job.copy_chat_id,
                "message_id": job.copy_message_id,
            }
            if markup:
                copy_kw["reply_markup"] = markup
            sent = await bot.copy_message(**copy_kw)
            copied = True
            mid = _result_mid(sent)
            if mid:
                # copy_message already carries the markup (copy_kw sets it whenever
                # markup is truthy), and it returns a MessageId — which has no
                # reply_markup attribute, so the old check always fired a pointless
                # re-apply that Telegram answered with "message is not modified".
                # Keyword args only here: aiogram 3.7+ made business_connection_id the
                # first positional parameter, so positional (chat, mid) raised a
                # pydantic ValidationError that threw away an already-posted mid.
                if markup and "reply_markup" not in copy_kw:
                    try:
                        await bot.edit_message_reply_markup(
                            chat_id=chat,
                            message_id=mid,
                            reply_markup=markup,
                        )
                    except Exception as exc:
                        logger.warning("spam markup edit failed %s/%s: %s", chat, mid, exc)
                return mid
            return None
        except (TelegramNetworkError, TelegramRetryAfter, TimeoutError, asyncio.TimeoutError):
            logger.warning("spam copy network error, not retrying send")
            return None
        except TelegramBadRequest as exc:
            err = str(exc).lower()
            if copied or not any(token in err for token in _COPY_GONE):
                logger.warning("spam copy failed, skip fallback: %s", exc)
                return None
            logger.warning("spam copy source gone, fallback send")
        except Exception:
            logger.exception("spam copy failed")
            return None
    try:
        sent = await send_content(
            bot,
            chat,
            job.text or "",
            parse_mode=job.parse_mode or "HTML",
            media_type=job.media_type or "none",
            media_file_id=job.media_file_id or "",
            reply_markup=markup,
            replace_markup=bool(markup),
        )
        return _result_mid(sent)
    except Exception:
        logger.exception("spam post failed")
        return None


async def post_spam_job(job: SpamJob) -> int | None:
    targets = await _resolve_targets(job.channel_id, None)
    chat = targets[0] if targets else channel_target(job.channel_id)
    return await send_spam_payload(job, chat)


async def preview_spam_job(job: SpamJob, admin_chat_id: int) -> bool:
    return bool(await send_spam_payload(job, admin_chat_id))


async def delete_spam_message(job: SpamJob) -> None:
    await _delete_mids(job.channel_id, [job.last_message_id] if job.last_message_id else [])


async def _active_jobs(session, channel_id: str) -> list[SpamJob]:
    rows = list(
        (
            await session.execute(
                select(SpamJob)
                .where(SpamJob.is_active.is_(True), SpamJob.channel_id == channel_id)
                .order_by(SpamJob.id)
            )
        ).scalars().all()
    )
    for job in rows:
        if job.kind == "contest":
            contest = await session.get(Contest, job.contest_id) if job.contest_id else None
            if not contest or contest.status != "active":
                await delete_spam_message(job)
                job.is_active = False
                job.last_message_id = None
                job.phase = "idle"
                job.next_at = None
    await session.flush()
    return [job for job in rows if job.is_active]


def _next_job(jobs: list[SpamJob], after_id: int) -> SpamJob | None:
    if not jobs:
        return None
    ids = [job.id for job in jobs]
    if after_id in ids:
        return jobs[(ids.index(after_id) + 1) % len(ids)]
    return jobs[0]


async def spam_is_running(session) -> bool:
    return (await get_setting(session, SPAM_FLAG, "1")) != "0"


async def join_rotation(session, channel_id: str, exclude_id: int | None = None) -> datetime | None:
    if not await spam_is_running(session):
        return None
    jobs = [job for job in await _active_jobs(session, channel_id) if job.id != exclude_id]
    return None if jobs else utcnow()


async def stop_all_spam() -> int:
    async with SessionLocal() as session:
        await set_setting(session, SPAM_FLAG, "0")
        jobs = list((await session.execute(select(SpamJob))).scalars().all())
        extras: dict[str, list[int]] = {}
        for job in jobs:
            if job.channel_id and job.last_message_id:
                extras.setdefault(job.channel_id, []).append(int(job.last_message_id))
        for channel_id in {job.channel_id for job in jobs if job.channel_id}:
            await purge_channel_posts(session, channel_id, extras.get(channel_id) or [])
        for job in jobs:
            job.last_message_id = None
            job.phase = "idle"
            job.next_at = None
        await session.commit()
        return len(jobs)


async def start_all_spam() -> int:
    async with SessionLocal() as session:
        await set_setting(session, SPAM_FLAG, "1")
        jobs = list(
            (await session.execute(select(SpamJob).where(SpamJob.is_active.is_(True)).order_by(SpamJob.id))).scalars().all()
        )
        extras: dict[str, list[int]] = {}
        for job in jobs:
            if job.channel_id and job.last_message_id:
                extras.setdefault(job.channel_id, []).append(int(job.last_message_id))
        for channel_id in {job.channel_id for job in jobs if job.channel_id}:
            await purge_channel_posts(session, channel_id, extras.get(channel_id) or [])
        seen: set[str] = set()
        now = utcnow()
        for job in jobs:
            job.phase = "idle"
            job.last_message_id = None
            key = _lock_name(job.channel_id)
            if key in seen:
                job.next_at = None
            else:
                job.next_at = now
                seen.add(key)
        await session.commit()
        return len(jobs)


async def release_rotation(session, job: SpamJob, *, after_pause: bool) -> None:
    channel_id = job.channel_id
    pause = max(0, int(job.pause_seconds or 0)) if after_pause else 0
    was_id = job.id
    extra = [int(job.last_message_id)] if job.last_message_id else []
    await purge_channel_posts(session, channel_id, extra)
    job.last_message_id = None
    job.phase = "idle"
    job.next_at = None
    others = [row for row in await _active_jobs(session, channel_id) if row.id != was_id]
    if others and not any(row.phase in ("live", "posting") for row in others):
        nxt = _next_job(others, was_id) or others[0]
        for row in others:
            row.next_at = None
            row.phase = "idle"
        nxt.next_at = utcnow() + timedelta(seconds=pause)


async def tick_channel(channel_id: str) -> None:
    lock = await _lock_for(channel_id)
    async with lock:
        await _tick_channel(channel_id)


def _chan_key(channel_id: str) -> int:
    return zlib.crc32(_lock_name(channel_id).encode("utf-8")) & 0x7FFFFFFF


async def _tick_channel(channel_id: str) -> None:
    packed = None
    job_id = 0
    lifetime = 3600
    leftover_mids: list[int] = []
    leftover_chat: int | None = None
    async with SessionLocal() as session:
        if not await spam_is_running(session):
            return
        from bot.services.combo import is_channel_held

        if await is_channel_held(session, channel_id):
            return
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _chan_key(channel_id)})
        result = await session.execute(
            select(SpamJob)
            .where(SpamJob.is_active.is_(True), SpamJob.channel_id == channel_id)
            .order_by(SpamJob.id)
            .with_for_update(skip_locked=True)
        )
        jobs = list(result.scalars().all())
        leftover_mids = []
        alive = []
        for job in jobs:
            if job.kind == "contest":
                contest = await session.get(Contest, job.contest_id) if job.contest_id else None
                if not contest or contest.status != "active":
                    if job.last_message_id:
                        leftover_mids.append(int(job.last_message_id))
                    job.is_active = False
                    job.last_message_id = None
                    job.phase = "idle"
                    job.next_at = None
                    continue
            alive.append(job)
        jobs = alive
        now = utcnow()
        if not jobs:
            await purge_channel_posts(session, channel_id, leftover_mids)
            await session.commit()
            return
        live = [job for job in jobs if job.phase in ("live", "posting")]
        if len(live) > 1:
            keep = live[0]
            for extra in live[1:]:
                if extra.last_message_id:
                    leftover_mids.append(int(extra.last_message_id))
                extra.phase = "idle"
                extra.last_message_id = None
                extra.next_at = None
            live = [keep]

        if live:
            job = live[0]
            due = _aware(job.next_at)
            if due is None or due > now:
                if due is None:
                    job.next_at = now + timedelta(seconds=max(1, int(job.lifetime_seconds or 3600)))
                await session.commit()
                return
            extra = list(leftover_mids)
            if job.last_message_id:
                extra.append(int(job.last_message_id))
            still = await purge_channel_posts(session, channel_id, extra)
            if still:
                job.last_message_id = still[0]
                job.phase = "live"
                job.next_at = now + timedelta(seconds=2)
                await session.commit()
                return
            pause = max(0, int(job.pause_seconds or 0))
            job.phase = "idle"
            job.last_message_id = None
            job.next_at = None
            nxt = _next_job(jobs, job.id)
            for row in jobs:
                if row.id != job.id:
                    row.phase = "idle"
                    row.next_at = None
            if nxt:
                nxt.next_at = now + timedelta(seconds=pause)
            await session.commit()
            return

        extra = list(leftover_mids)
        for row in jobs:
            if row.last_message_id:
                extra.append(int(row.last_message_id))
        still = await purge_channel_posts(session, channel_id, extra)
        if still:
            await session.commit()
            return
        due_jobs = []
        for job in jobs:
            due = _aware(job.next_at)
            if due is not None and due <= now:
                due_jobs.append(job)
        if not due_jobs:
            await session.commit()
            return
        job = due_jobs[0]
        lifetime = max(1, int(job.lifetime_seconds or 3600))
        claimed = await session.execute(
            update(SpamJob)
            .where(
                SpamJob.id == job.id,
                SpamJob.is_active.is_(True),
                SpamJob.phase == "idle",
            )
            .values(phase="posting", next_at=now + timedelta(seconds=lifetime))
        )
        if not claimed.rowcount:
            await session.commit()
            return
        for row in jobs:
            if row.id != job.id:
                row.next_at = None
                row.phase = "idle"
                row.last_message_id = None
        packed = _snapshot(job)
        job_id = job.id
        await session.commit()

    if packed is None:
        return
    begin_expect(channel_id)
    mid = await post_spam_job(packed)
    # Record it before anything else can fail: from here on the mid is on disk, so even
    # a restart mid-tick leaves the post deletable on the next purge.
    await append_live_mid(channel_id, leftover_chat, mid)
    wait_for = 3.5 if not mid else 0.5
    mids = await wait_observed(channel_id, mid, timeout=wait_for)
    bucket = _expect_bucket(channel_id)
    obs_chat = int(bucket["chat"]) if bucket and bucket.get("chat") else leftover_chat
    targets = await _resolve_targets(channel_id, obs_chat)
    if targets and isinstance(targets[0], int):
        obs_chat = int(targets[0])
    async with SessionLocal() as session:
        job = await session.get(SpamJob, job_id)
        if not job:
            await purge_channel_posts(session, channel_id, mids)
            await session.commit()
            return
        if not await spam_is_running(session) or not job.is_active:
            job.last_message_id = None
            job.phase = "idle"
            job.next_at = None
            await purge_channel_posts(session, channel_id, mids)
            await session.commit()
            return
        others = await _active_jobs(session, channel_id)
        extra = []
        for row in others:
            if row.id != job.id:
                if row.last_message_id:
                    extra.append(int(row.last_message_id))
                row.phase = "idle"
                row.next_at = None
                row.last_message_id = None
        job.last_message_id = mids[-1] if mids else mid
        job.phase = "live"
        job.next_at = utcnow() + timedelta(seconds=lifetime)
        # Merge, never overwrite: append_live_mid may already hold this tick's mid, and
        # anything still pending from an earlier tick must stay queued for deletion.
        _, pending, fails = await _load_live_full(session, channel_id)
        await _save_live(session, channel_id, obs_chat, pending + mids, fails)
        await session.commit()
    if extra:
        await _delete_mids(channel_id, extra, obs_chat)


async def spam_loop() -> None:
    global _spam_loop_started
    if _spam_loop_started:
        logger.warning("spam_loop already running")
        return
    _spam_loop_started = True
    await asyncio.sleep(2)
    try:
        while True:
            try:
                async with SessionLocal() as session:
                    running = await spam_is_running(session)
                    channels = list(
                        (
                            await session.execute(
                                select(SpamJob.channel_id).where(SpamJob.is_active.is_(True)).distinct()
                            )
                        ).scalars().all()
                    )
                if not running:
                    await asyncio.sleep(2)
                    continue
                for channel_id in channels:
                    if not channel_id:
                        continue
                    try:
                        await tick_channel(channel_id)
                    except Exception:
                        logger.exception("spam channel %s", channel_id)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("spam loop")
            await asyncio.sleep(1)
    finally:
        _spam_loop_started = False
