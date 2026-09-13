from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import MenuButtonCommands, MenuButtonWebApp, WebAppInfo

from config import settings

logger = logging.getLogger(__name__)


def webapp_info() -> WebAppInfo | None:
    url = (settings.webapp_url or "").strip()
    if not url.startswith("https://"):
        return None
    return WebAppInfo(url=url)


async def setup_admin_webapp_button(bot: Bot, chat_id: int | None = None) -> None:
    info = webapp_info()
    if chat_id is None:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception:
            logger.exception("reset default menu button")
        if not info:
            return
        for admin_id in settings.admin_id_list:
            try:
                await bot.set_chat_menu_button(
                    chat_id=admin_id,
                    menu_button=MenuButtonWebApp(text="Админка", web_app=info),
                )
            except Exception:
                logger.exception("webapp menu for %s", admin_id)
        return
    if not settings.is_admin(chat_id) or not info:
        return
    try:
        await bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonWebApp(text="Админка", web_app=info),
        )
    except Exception:
        logger.exception("webapp menu for %s", chat_id)
