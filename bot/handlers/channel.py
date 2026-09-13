from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from bot.services.spammer import on_channel_post

router = Router()


@router.channel_post()
async def channel_post(message: Message) -> None:
    await on_channel_post(message)


@router.message(F.new_chat_title | F.new_chat_photo | F.delete_chat_photo)
async def chat_service(message: Message) -> None:
    await on_channel_post(message)
