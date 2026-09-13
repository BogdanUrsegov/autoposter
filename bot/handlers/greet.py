from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import ChatJoinRequest, Message

from bot.services.greetings import deliver_pending_post, handle_join_request
from config import settings
from database import SessionLocal
from database.crud import get_user_by_tg

router = Router()


class HasPendingGreet(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if not message.from_user or settings.is_admin(message.from_user.id):
            return False
        async with SessionLocal() as session:
            user = await get_user_by_tg(session, message.from_user.id)
            return bool(user and user.pending_greeting_id)


@router.chat_join_request()
async def on_channel_join_request(event: ChatJoinRequest) -> None:
    user = event.from_user
    if not user or settings.is_admin(user.id):
        return
    async with SessionLocal() as session:
        row = await get_user_by_tg(session, user.id)
        is_new = bool(row and row.start_count == 0)
    await handle_join_request(user.id, str(event.chat.id), is_new=is_new)


@router.message(HasPendingGreet(), F.chat.type == ChatType.PRIVATE, ~CommandStart())
async def on_greet_reply(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        return
    await deliver_pending_post(message.from_user.id)
