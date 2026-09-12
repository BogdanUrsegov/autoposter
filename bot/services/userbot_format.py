"""HTML / aiogram entities → Telethon MessageEntity (в т.ч. прем-эмодзи)."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

_OPEN_RE = re.compile(
    r"<(b|strong|i|em|u|s|strike|del|code|pre|a|tg-emoji)(\s+[^>]*)?>",
    re.IGNORECASE,
)
_ATTR_RE = re.compile(r'([a-zA-Z_:-]+)\s*=\s*"([^"]*)"')


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def entities_from_aiogram(message) -> tuple[str, list[dict]]:
    text = message.text or message.caption or ""
    raw_ents = list(message.entities or message.caption_entities or [])
    out: list[dict] = []
    for ent in raw_ents:
        item: dict[str, Any] = {
            "type": ent.type if isinstance(ent.type, str) else getattr(ent.type, "value", str(ent.type)),
            "offset": int(ent.offset),
            "length": int(ent.length),
        }
        if item["type"] == "text_link" and ent.url:
            item["url"] = ent.url
        if item["type"] == "custom_emoji" and ent.custom_emoji_id:
            item["custom_emoji_id"] = str(ent.custom_emoji_id)
        if item["type"] == "pre" and getattr(ent, "language", None):
            item["language"] = ent.language
        out.append(item)
    return text, out


def dumps_entities(entities: list[dict] | None) -> str:
    return json.dumps(entities or [], ensure_ascii=False)


def loads_entities(raw: str | None) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def html_to_text_entities(html: str) -> tuple[str, list[dict]]:
    raw = html or ""
    if not raw.strip():
        return "", []
    if "<" not in raw:
        return unescape(raw), []

    text_parts: list[str] = []
    entities: list[dict] = []
    stack: list[dict] = []
    pos = 0
    utf16 = 0

    while pos < len(raw):
        if raw[pos] == "<":
            close = raw.find(">", pos)
            if close < 0:
                text_parts.append(raw[pos])
                utf16 += _utf16_len(raw[pos])
                pos += 1
                continue
            tag = raw[pos : close + 1]
            pos = close + 1
            low = tag.lower()
            if low.startswith("</"):
                name = low[2:-1].strip().split()[0] if low[2:-1].strip() else ""
                aliases = {"strong": "b", "em": "i", "strike": "s", "del": "s"}
                name = aliases.get(name, name)
                for i in range(len(stack) - 1, -1, -1):
                    if stack[i]["name"] == name:
                        opened = stack.pop(i)
                        length = utf16 - opened["offset"]
                        if length > 0:
                            ent = {
                                "type": opened["type"],
                                "offset": opened["offset"],
                                "length": length,
                            }
                            if opened.get("url"):
                                ent["url"] = opened["url"]
                            if opened.get("custom_emoji_id"):
                                ent["custom_emoji_id"] = opened["custom_emoji_id"]
                            entities.append(ent)
                        break
                continue

            m = _OPEN_RE.match(tag)
            if not m:
                continue
            name = m.group(1).lower()
            attrs = dict(_ATTR_RE.findall(m.group(2) or ""))
            aliases = {"strong": "b", "em": "i", "strike": "s", "del": "s"}
            name = aliases.get(name, name)
            type_map = {
                "b": "bold",
                "i": "italic",
                "u": "underline",
                "s": "strikethrough",
                "code": "code",
                "pre": "pre",
                "a": "text_link",
                "tg-emoji": "custom_emoji",
            }
            etype = type_map.get(name)
            if not etype:
                continue
            item = {"name": name, "type": etype, "offset": utf16}
            if etype == "text_link":
                item["url"] = attrs.get("href") or ""
            if etype == "custom_emoji":
                item["custom_emoji_id"] = attrs.get("emoji-id") or attrs.get("emoji_id") or ""
            stack.append(item)
            continue

        next_lt = raw.find("<", pos)
        chunk = raw[pos:] if next_lt < 0 else raw[pos:next_lt]
        pos = len(raw) if next_lt < 0 else next_lt
        chunk = unescape(chunk)
        text_parts.append(chunk)
        utf16 += _utf16_len(chunk)

    return "".join(text_parts), entities


def to_telethon_entities(entities: list[dict] | None):
    from telethon.tl.types import (
        MessageEntityBold,
        MessageEntityCode,
        MessageEntityCustomEmoji,
        MessageEntityItalic,
        MessageEntityPre,
        MessageEntityStrike,
        MessageEntityTextUrl,
        MessageEntityUnderline,
    )

    out = []
    for ent in entities or []:
        if not isinstance(ent, dict):
            continue
        etype = str(ent.get("type") or "")
        offset = int(ent.get("offset") or 0)
        length = int(ent.get("length") or 0)
        if length <= 0:
            continue
        if etype == "bold":
            out.append(MessageEntityBold(offset, length))
        elif etype == "italic":
            out.append(MessageEntityItalic(offset, length))
        elif etype == "underline":
            out.append(MessageEntityUnderline(offset, length))
        elif etype in ("strikethrough", "strike"):
            out.append(MessageEntityStrike(offset, length))
        elif etype == "code":
            out.append(MessageEntityCode(offset, length))
        elif etype == "pre":
            out.append(MessageEntityPre(offset, length, ent.get("language") or ""))
        elif etype == "text_link" and ent.get("url"):
            out.append(MessageEntityTextUrl(offset, length, ent["url"]))
        elif etype == "custom_emoji" and ent.get("custom_emoji_id"):
            try:
                eid = int(ent["custom_emoji_id"])
            except (TypeError, ValueError):
                continue
            out.append(MessageEntityCustomEmoji(offset, length, eid))
    return out


def prepare_send_payload(text: str, parse_mode: str | None, entities_json: str | None) -> tuple[str, list]:
    from bot.services.emoji import apply_text

    ents = loads_entities(entities_json)
    if ents:
        return text or "", to_telethon_entities(ents)
    raw = apply_text(text or "")
    mode = (parse_mode or "HTML").upper()
    if mode == "HTML" and raw and "<" in raw:
        plain, parsed = html_to_text_entities(raw)
        return plain, to_telethon_entities(parsed)
    return raw, []
