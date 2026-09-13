from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession

from config import settings

_session = None
if settings.proxy_url:
    _session = AiohttpSession(proxy=settings.proxy_url)

bot = Bot(
    token=settings.bot_token or "0:init",
    default=DefaultBotProperties(parse_mode=None),
    session=_session,
)
dp = Dispatcher()
