from __future__ import annotations

import asyncio
import logging

from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, ErrorEvent

from bot.handlers.admin import router as admin_router
from bot.handlers.channel import router as channel_router
from bot.handlers.clicker import router as clicker_router
from bot.handlers.combo_admin import router as combo_admin_router
from bot.handlers.contest import router as contest_router
from bot.handlers.dev_panel import router as dev_panel_router
from bot.handlers.greet import router as greet_router
from bot.handlers.menu import router as menu_router
from bot.handlers.start import router as start_router
from bot.handlers.userbot_admin import router as userbot_admin_router
from bot.handlers.withdraw import router as withdraw_router
from bot.loader import bot, dp
from bot.middlewares.user import UserMiddleware
from bot.services.emoji import loads_map, set_map
from bot.services.webapp_menu import setup_admin_webapp_button
from config import settings
from database import SessionLocal, init_db
from database.crud import get_setting, set_setting

logger = logging.getLogger(__name__)


def setup_dispatcher() -> None:
    dp.update.outer_middleware(UserMiddleware())
    dp.include_router(channel_router)
    dp.include_router(start_router)
    dp.include_router(greet_router)
    dp.include_router(admin_router)
    dp.include_router(combo_admin_router)
    dp.include_router(userbot_admin_router)
    dp.include_router(dev_panel_router)
    dp.include_router(contest_router)
    dp.include_router(menu_router)
    dp.include_router(clicker_router)
    dp.include_router(withdraw_router)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> None:
        logger.exception("Update failed: %s", event.exception)


async def on_startup() -> None:
    await init_db()
    # Юзер-боты — отдельной задачей, чтобы Telethon никогда не валил основной бот
    async def _boot_userbots() -> None:
        try:
            from bot.services.userbot import userbot_startup

            await userbot_startup()
        except Exception:
            logger.exception("userbot boot failed (ignored)")

    asyncio.create_task(_boot_userbots(), name="userbot")

    if settings.bot_token and ":" in settings.bot_token:
        try:
            me = await bot.get_me()
            async with SessionLocal() as session:
                await set_setting(session, "bot_username", me.username or "")
                raw = await get_setting(session, "premium_emojis", "{}")
                set_map(loads_map(raw))
                await session.commit()
            await bot.set_my_commands(
                [BotCommand(command="start", description="Открыть меню ✨")],
                scope=BotCommandScopeDefault(),
            )
            admin_cmds = [
                BotCommand(command="start", description="Открыть меню ✨"),
                BotCommand(command="admin", description="Админка"),
            ]
            for admin_id in settings.admin_id_list:
                try:
                    await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=admin_id))
                except Exception:
                    logger.exception("admin commands for %s", admin_id)
            await setup_admin_webapp_button(bot)
            from bot.services.combo import combo_loop
            from bot.services.contests import contest_watcher
            from bot.services.namer import namer_loop
            from bot.services.reminders import reminders_loop, resume_hour_reminds
            from bot.services.spammer import spam_loop

            asyncio.create_task(reminders_loop(), name="reminders")
            asyncio.create_task(contest_watcher(), name="contests")
            asyncio.create_task(spam_loop(), name="spammer")
            asyncio.create_task(combo_loop(), name="combo")
            asyncio.create_task(namer_loop(), name="namer")
            await resume_hour_reminds()
            logger.info("Bot @%s started", me.username)
        except Exception:
            logger.exception("Could not reach Telegram API")


async def run_bot() -> None:
    setup_dispatcher()
    await on_startup()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(
        bot,
        handle_signals=False,
        allowed_updates=list(
            dict.fromkeys(
                [*dp.resolve_used_update_types(), "channel_post", "message", "callback_query", "chat_join_request"]
            )
        ),
    )
