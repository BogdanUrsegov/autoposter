from __future__ import annotations

import asyncio
import logging

from bot.keyboards import post_markup
from bot.loader import bot
from bot.services.content import send_content
from database import SessionLocal
from database.models import Show, ShowSend

logger = logging.getLogger(__name__)
_jobs: dict[int, list[asyncio.Task]] = {}


def cancel_shows(tg_id: int) -> None:
    for task in _jobs.pop(tg_id, []):
        if not task.done():
            task.cancel()


async def schedule_shows(tg_id: int) -> None:
    cancel_shows(tg_id)
    async with SessionLocal() as session:
        from sqlalchemy import select

        shows = list(
            (
                await session.execute(
                    select(Show).where(Show.is_active.is_(True)).order_by(Show.delay_seconds, Show.sort_order, Show.id)
                )
            ).scalars().all()
        )
        packed = [
            {
                "id": s.id,
                "delay": max(0, int(s.delay_seconds or 0)),
            }
            for s in shows
        ]
    tasks = []
    for item in packed:
        tasks.append(asyncio.create_task(_send_later(tg_id, item["id"], item["delay"])))
    if tasks:
        _jobs[tg_id] = tasks


async def _send_later(tg_id: int, show_id: int, delay: int) -> None:
    try:
        if delay:
            await asyncio.sleep(delay)
        async with SessionLocal() as session:
            show = await session.get(Show, show_id)
            if not show or not show.is_active:
                return
            from database.crud import get_user_by_tg

            user = await get_user_by_tg(session, tg_id)
            markup = post_markup(
                show.extra_buttons, show.button_type, show.button_text, show.button_url
            )
            await send_content(
                bot,
                tg_id,
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
            if user:
                session.add(ShowSend(user_id=user.id, show_id=show.id))
                await session.commit()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("show %s failed for %s", show_id, tg_id)
