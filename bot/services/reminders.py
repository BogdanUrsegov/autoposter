from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from sqlalchemy import select

from bot import texts as t
from bot.keyboards import remind_kb
from bot.loader import bot
from bot.services.emoji import apply_text
from database import SessionLocal
from database.crud import get_setting, set_setting, today_str, utcnow
from database.models import User

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))

_hour_jobs: dict[int, asyncio.Task] = {}


def cancel_hour_remind(tg_id: int) -> None:
    task = _hour_jobs.pop(tg_id, None)
    if task and not task.done():
        task.cancel()


def schedule_hour_remind(tg_id: int, delay: float | None = None) -> None:
    cancel_hour_remind(tg_id)
    wait = 3600.0 if delay is None else max(0.0, delay)
    _hour_jobs[tg_id] = asyncio.create_task(_send_hour_later(tg_id, wait))


async def _mark_blocked(tg_id: int) -> None:
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
        if user:
            user.blocked = True
            await session.commit()


async def _send_hour_later(tg_id: int, delay: float) -> None:
    try:
        if delay:
            await asyncio.sleep(delay)
        async with SessionLocal() as session:
            user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
            if not user or user.blocked or user.hour_remind_sent:
                return
            if user.clicks_total > 0:
                user.hour_remind_sent = True
                await session.commit()
                return
            user.hour_remind_sent = True
            await session.commit()
        try:
            await bot.send_message(
                tg_id,
                apply_text(t.REMIND_HOUR),
                parse_mode="HTML",
                reply_markup=remind_kb(),
            )
        except TelegramForbiddenError:
            await _mark_blocked(tg_id)
        except TelegramBadRequest:
            logger.warning("hour remind bad request %s", tg_id)
        except Exception:
            logger.exception("hour remind failed %s", tg_id)
    except asyncio.CancelledError:
        return
    finally:
        _hour_jobs.pop(tg_id, None)


async def resume_hour_reminds() -> None:
    now = utcnow()
    cutoff = now - timedelta(hours=6)
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(User).where(
                    User.hour_remind_sent.is_(False),
                    User.blocked.is_(False),
                    User.created_at >= cutoff,
                )
            )
        ).scalars().all()
        packed = []
        for user in rows:
            created = user.created_at
            if created and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            left = 3600.0 - (now - created).total_seconds() if created else 0.0
            packed.append((user.tg_id, left))
    for tg_id, left in packed:
        schedule_hour_remind(tg_id, left)


def _seconds_until_13() -> float:
    now = datetime.now(MSK)
    target = now.replace(hour=13, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


async def send_daily_reminders() -> None:
    day = datetime.now(MSK).date().isoformat()
    async with SessionLocal() as session:
        last = await get_setting(session, "last_daily_remind_date", "")
        if last == day:
            return
        await set_setting(session, "last_daily_remind_date", day)
        await session.commit()
        users = (
            await session.execute(
                select(User.tg_id, User.clicks_today, User.clicks_today_date).where(User.blocked.is_(False))
            )
        ).all()
    today = today_str()
    sent = failed = 0
    for tg_id, clicks_today, clicks_date in users:
        if clicks_date == today and int(clicks_today or 0) > 0:
            continue
        try:
            await bot.send_message(
                tg_id,
                apply_text(t.REMIND_DAILY),
                parse_mode="HTML",
                reply_markup=remind_kb(),
            )
            sent += 1
        except TelegramForbiddenError:
            failed += 1
            await _mark_blocked(tg_id)
        except Exception:
            failed += 1
        if (sent + failed) % 20 == 0:
            await asyncio.sleep(1)
        else:
            await asyncio.sleep(0.05)
    logger.info("daily remind sent=%s failed=%s", sent, failed)


async def reminders_loop() -> None:
    await asyncio.sleep(3)
    while True:
        try:
            await asyncio.sleep(_seconds_until_13())
            await send_daily_reminders()
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("daily remind loop")
            await asyncio.sleep(30)
