from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select

from bot.loader import bot
from bot.services.contests import channel_target
from database import SessionLocal
from database.crud import utcnow
from database.models import ChannelNamer, SavedChannel

logger = logging.getLogger(__name__)

_namer_loop_started = False
_TITLE_MAX = 255


def parse_names(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    if "\n" in text:
        parts = text.splitlines()
    elif "|" in text:
        parts = text.split("|")
    else:
        parts = [text]
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = " ".join(part.strip().split())
        if not name:
            continue
        name = name[:_TITLE_MAX]
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def dump_names(names: list[str]) -> str:
    return json.dumps(names, ensure_ascii=False)


def load_names(raw: str | None) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(item).strip()[:_TITLE_MAX] for item in data if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return parse_names(text)


async def get_or_create_namer(session, channel_id: str) -> ChannelNamer:
    row = (
        await session.execute(select(ChannelNamer).where(ChannelNamer.channel_id == str(channel_id)))
    ).scalar_one_or_none()
    if row:
        return row
    row = ChannelNamer(channel_id=str(channel_id), is_active=False)
    session.add(row)
    await session.flush()
    return row


async def current_title(channel_id: str) -> str:
    chat = channel_target(channel_id)
    if chat is None:
        return ""
    try:
        info = await bot.get_chat(chat)
    except Exception:
        logger.warning("namer get_chat failed %s", channel_id)
        return ""
    return (getattr(info, "title", None) or "")[:_TITLE_MAX]


async def set_title(channel_id: str, title: str) -> str | None:
    chat = channel_target(channel_id)
    name = (title or "").strip()[:_TITLE_MAX]
    if chat is None or not name:
        return "empty"
    try:
        await bot.set_chat_title(chat, name)
        return None
    except TelegramRetryAfter as exc:
        logger.warning("namer flood %s: %s", channel_id, exc)
        return "flood"
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning("namer title failed %s: %s", channel_id, exc)
        return str(exc)
    except Exception:
        logger.exception("namer title failed %s", channel_id)
        return "error"


async def start_namer(channel_id: str) -> str:
    async with SessionLocal() as session:
        namer = await get_or_create_namer(session, channel_id)
        names = load_names(namer.names)
        if not names:
            await session.commit()
            return "no_names"
        title = await current_title(channel_id)
        namer.original_title = title or namer.original_title
        namer.is_active = True
        namer.name_index = 0
        namer.next_at = utcnow()
        await session.commit()
    return "ok"


async def stop_namer(channel_id: str) -> str:
    original = ""
    async with SessionLocal() as session:
        namer = await get_or_create_namer(session, channel_id)
        namer.is_active = False
        namer.next_at = None
        original = namer.original_title or ""
        await session.commit()
    if original:
        err = await set_title(channel_id, original)
        if err:
            return err
    return "ok"


async def _tick_namer(namer_id: int) -> None:
    async with SessionLocal() as session:
        namer = await session.get(ChannelNamer, namer_id)
        if not namer or not namer.is_active:
            return
        names = load_names(namer.names)
        if not names:
            namer.is_active = False
            namer.next_at = None
            await session.commit()
            return
        now = utcnow()
        due = namer.next_at
        if due is not None and due.tzinfo is None:
            from datetime import timezone

            due = due.replace(tzinfo=timezone.utc)
        if due is not None and due > now:
            return
        idx = int(namer.name_index or 0) % len(names)
        title = names[idx]
        interval = max(5, int(namer.interval_seconds or 10))
        channel_id = namer.channel_id
        namer.name_index = (idx + 1) % len(names)
        namer.next_at = now + timedelta(seconds=interval)
        await session.commit()
    err = await set_title(channel_id, title)
    if err == "flood":
        async with SessionLocal() as session:
            namer = await session.get(ChannelNamer, namer_id)
            if namer:
                namer.next_at = utcnow() + timedelta(seconds=max(interval, 30))
                await session.commit()


async def namer_loop() -> None:
    global _namer_loop_started
    if _namer_loop_started:
        logger.warning("namer_loop already running")
        return
    _namer_loop_started = True
    await asyncio.sleep(2)
    try:
        while True:
            try:
                async with SessionLocal() as session:
                    ids = list(
                        (
                            await session.execute(
                                select(ChannelNamer.id).where(ChannelNamer.is_active.is_(True))
                            )
                        ).scalars().all()
                    )
                for namer_id in ids:
                    try:
                        await _tick_namer(namer_id)
                    except Exception:
                        logger.exception("namer tick %s", namer_id)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("namer loop")
            await asyncio.sleep(1)
    finally:
        _namer_loop_started = False


def format_namer(namer: ChannelNamer, channel: SavedChannel | None) -> str:
    names = load_names(namer.names)
    label = ""
    if channel:
        label = channel.username or channel.title or channel.chat_id
    mark = "🟢 вкл" if namer.is_active else "⚪️ выкл"
    preview = "\n".join(f"• {name}" for name in names[:12]) or "пока пусто"
    extra = f"\n… ещё {len(names) - 12}" if len(names) > 12 else ""
    original = namer.original_title or "—"
    return (
        f"🏷 <b>Имена канала</b>\n\n"
        f"Канал: <b>{label or namer.channel_id}</b>\n"
        f"Статус: {mark}\n"
        f"Интервал: <b>{namer.interval_seconds}</b> сек\n"
        f"Прежнее имя: <b>{original}</b>\n"
        f"Имен: <b>{len(names)}</b>\n\n"
        f"{preview}{extra}\n\n"
        "Старт — крутит названия по кругу. Стоп — возвращает прежнее."
    )
