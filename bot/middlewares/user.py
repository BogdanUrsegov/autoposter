from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TgUser

from database import SessionLocal
from database.crud import utcnow, upsert_user


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)
        async with SessionLocal() as session:
            user, _ = await upsert_user(
                session,
                tg_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name or "",
                last_name=tg_user.last_name,
                is_premium=bool(tg_user.is_premium),
                language_code=tg_user.language_code,
            )
            user.last_active_at = utcnow()
            await session.commit()
            data["db_user_id"] = user.id
            data["db_blocked"] = user.blocked
        return await handler(event, data)
