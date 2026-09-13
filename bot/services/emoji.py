from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache

from aiogram.types import Message, MessageEntity

from config import BASE_DIR

_map: dict[str, str] = {}

_SKIP_EMOJI = {"✓", "●", "○", "•"}

_SCAN_FILES = (
    BASE_DIR / "bot" / "texts.py",
    BASE_DIR / "bot" / "keyboards.py",
    BASE_DIR / "bot" / "handlers" / "admin.py",
    BASE_DIR / "bot" / "handlers" / "clicker.py",
    BASE_DIR / "bot" / "handlers" / "contest.py",
    BASE_DIR / "bot" / "handlers" / "withdraw.py",
    BASE_DIR / "bot" / "handlers" / "menu.py",
    BASE_DIR / "bot" / "services" / "reminders.py",
    BASE_DIR / "bot" / "services" / "contests.py",
)


def set_map(data: dict[str, str] | None) -> None:
    global _map
    _map = {str(k): str(v) for k, v in (data or {}).items() if k and v}


def get_map() -> dict[str, str]:
    return dict(_map)


def loads_map(raw: str | None) -> dict[str, str]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v}


def dumps_map(data: dict[str, str]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _utf16_slice(text: str, offset: int, length: int) -> str:
    encoded = text.encode("utf-16-le")
    start = offset * 2
    end = (offset + length) * 2
    return encoded[start:end].decode("utf-16-le")


def extract_from_message(message: Message) -> dict[str, str]:
    text = message.text or message.caption or ""
    entities: list[MessageEntity] = list(message.entities or message.caption_entities or [])
    found: dict[str, str] = {}
    for ent in entities:
        if ent.type != "custom_emoji" or not ent.custom_emoji_id:
            continue
        try:
            uni = _utf16_slice(text, ent.offset, ent.length)
        except Exception:
            continue
        if uni:
            found[uni] = ent.custom_emoji_id
    return found


def extract_first_custom_id(message: Message) -> str | None:
    entities = list(message.entities or message.caption_entities or [])
    for ent in entities:
        if ent.type == "custom_emoji" and ent.custom_emoji_id:
            return ent.custom_emoji_id
    return None


def _is_emoji_base(ch: str) -> bool:
    if not ch:
        return False
    o = ord(ch)
    if unicodedata.category(ch) == "So":
        return True
    return (
        0x1F000 <= o <= 0x1FAFF
        or 0x2600 <= o <= 0x27BF
        or 0x2B00 <= o <= 0x2BFF
        or 0x2300 <= o <= 0x23FF
        or 0x2190 <= o <= 0x21FF
        or 0x25A0 <= o <= 0x25FF
    )


def extract_emojis(text: str) -> list[str]:
    found: list[str] = []
    chars = list(text)
    i = 0
    n = len(chars)
    while i < n:
        if not _is_emoji_base(chars[i]):
            i += 1
            continue
        j = i + 1
        while j < n:
            o = ord(chars[j])
            if o in (0xFE0F, 0x20E3, 0x200D):
                j += 1
                continue
            if _is_emoji_base(chars[j]) and j > i and ord(chars[j - 1]) == 0x200D:
                j += 1
                continue
            break
        seq = "".join(chars[i:j])
        if seq not in found:
            found.append(seq)
        i = j
    return found


@lru_cache(maxsize=1)
def used_emojis() -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for path in _SCAN_FILES:
        if not path.exists():
            continue
        for emo in extract_emojis(path.read_text(encoding="utf-8")):
            if emo in seen or emo in _SKIP_EMOJI:
                continue
            seen.add(emo)
            ordered.append(emo)
    return tuple(ordered)


def apply_text(text: str | None) -> str:
    raw = text or ""
    if not raw or not _map:
        return raw
    protected: list[str] = []

    def stash(match: re.Match) -> str:
        protected.append(match.group(0))
        return f"\x00P{len(protected) - 1}\x00"

    raw = re.sub(r"<tg-emoji[^>]*>.*?</tg-emoji>", stash, raw, flags=re.DOTALL)
    items = sorted(_map.items(), key=lambda kv: len(kv[0]), reverse=True)
    holders: list[tuple[str, str]] = []
    for uni, eid in items:
        token = f"\x00E{len(holders)}\x00"
        if uni in raw:
            raw = raw.replace(uni, token)
            holders.append((token, f'<tg-emoji emoji-id="{eid}">{uni}</tg-emoji>'))
    for token, wrapped in holders:
        raw = raw.replace(token, wrapped)
    for i, orig in enumerate(protected):
        raw = raw.replace(f"\x00P{i}\x00", orig)
    return raw


_LEADING = re.compile(r"^(\S+)\s*(.*)$", re.DOTALL)


def button_icon_and_text(text: str) -> tuple[str | None, str]:
    raw = text or ""
    if not raw or not _map:
        return None, raw
    items = sorted(_map.keys(), key=len, reverse=True)
    for uni in items:
        if raw == uni or raw.startswith(uni):
            rest = raw[len(uni) :].lstrip()
            return _map[uni], rest or uni
    match = _LEADING.match(raw)
    if match and match.group(1) in _map:
        icon = _map[match.group(1)]
        rest = match.group(2).strip()
        return icon, rest or raw
    return None, raw
