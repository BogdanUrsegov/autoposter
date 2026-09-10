from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message


def _push_copy_source(out: list[tuple], chat_id, message_id, username: str = "") -> None:
    if not message_id:
        return
    mid = int(message_id)
    uname = (username or "").lstrip("@")
    if uname:
        item = (f"@{uname}", mid)
        if item not in out:
            out.append(item)
    if chat_id is not None:
        item = (int(chat_id), mid)
        if item not in out:
            out.append(item)


def iter_copy_sources(message: Message) -> list[tuple]:
    out: list[tuple] = []
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        chat = getattr(origin, "chat", None)
        _push_copy_source(
            out,
            getattr(chat, "id", None),
            getattr(origin, "message_id", None),
            getattr(chat, "username", None) or "",
        )
    src = getattr(message, "forward_from_chat", None)
    _push_copy_source(
        out,
        getattr(src, "id", None) if src is not None else None,
        getattr(message, "forward_from_message_id", None),
        getattr(src, "username", None) if src is not None else "",
    )
    try:
        raw = message.model_dump(mode="python", exclude_none=True)
    except Exception:
        raw = {}
    fo = raw.get("forward_origin") or {}
    if isinstance(fo, dict):
        chat = fo.get("chat") or {}
        if isinstance(chat, dict):
            _push_copy_source(out, chat.get("id"), fo.get("message_id"), chat.get("username") or "")
    src_raw = raw.get("forward_from_chat") or {}
    if isinstance(src_raw, dict):
        _push_copy_source(out, src_raw.get("id"), raw.get("forward_from_message_id"), src_raw.get("username") or "")
    return out


def message_copy_source(message: Message) -> tuple[int, int]:
    for chat_id, mid in iter_copy_sources(message):
        if isinstance(chat_id, int):
            return chat_id, mid
    return int(message.chat.id), int(message.message_id)


def is_forwarded_message(message: Message) -> bool:
    if getattr(message, "forward_origin", None) is not None:
        return True
    if getattr(message, "forward_from_chat", None) is not None:
        return True
    if getattr(message, "forward_from", None) is not None:
        return True
    if getattr(message, "forward_date", None) is not None:
        return True
    if iter_copy_sources(message):
        return True
    return False

from bot.services.emoji import apply_text
from config import BASE_DIR


def parse_mode_or_none(value: str | None) -> str | None:
    if not value or value.upper() in ("NONE", "PLAIN", ""):
        return None
    return value


def _markup_without_icons(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    if not markup or not markup.inline_keyboard:
        return markup
    rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        items: list[InlineKeyboardButton] = []
        for btn in row:
            data = btn.model_dump(exclude_none=True)
            data.pop("icon_custom_emoji_id", None)
            items.append(InlineKeyboardButton(**data))
        if items:
            rows.append(items)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_content(
    bot: Bot,
    chat_id: int | str,
    text: str,
    parse_mode: str | None = "HTML",
    media_type: str = "none",
    media_file_id: str = "",
    media_path: str = "",
    reply_markup: InlineKeyboardMarkup | None = None,
    reply_to: Message | None = None,
    copy_chat_id: int | None = None,
    copy_message_id: int | None = None,
    replace_markup: bool = False,
) -> Message | None:
    kwargs: dict[str, Any] = {"chat_id": chat_id}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    if reply_to is not None:
        kwargs["reply_to_message_id"] = reply_to.message_id
    mode = parse_mode_or_none(parse_mode)
    if mode:
        kwargs["parse_mode"] = mode
        text = apply_text(text)

    if copy_chat_id and copy_message_id:
        try:
            copy_kw: dict[str, Any] = {
                "chat_id": chat_id,
                "from_chat_id": copy_chat_id,
                "message_id": copy_message_id,
            }
            if replace_markup:
                copy_kw["reply_markup"] = reply_markup
            return await bot.copy_message(**copy_kw)
        except TelegramBadRequest:
            pass

    media = None
    if media_file_id:
        media = media_file_id
    elif media_path:
        path = BASE_DIR / media_path if not media_path.startswith(":") else None
        if path and path.exists():
            media = FSInputFile(path)

    async def _send(payload: dict[str, Any]) -> Message:
        if media and media_type == "photo":
            return await bot.send_photo(photo=media, caption=text or None, **payload)
        if media and media_type == "video":
            return await bot.send_video(video=media, caption=text or None, **payload)
        if media and media_type == "animation":
            return await bot.send_animation(animation=media, caption=text or None, **payload)
        return await bot.send_message(text=text or "‎", **payload)

    try:
        return await _send(kwargs)
    except TelegramBadRequest:
        stripped = dict(kwargs)
        if reply_markup:
            stripped["reply_markup"] = _markup_without_icons(reply_markup)
            try:
                return await _send(stripped)
            except TelegramBadRequest:
                pass
        stripped.pop("parse_mode", None)
        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=text or "‎",
                reply_markup=stripped.get("reply_markup"),
            )
        except TelegramBadRequest:
            return None
