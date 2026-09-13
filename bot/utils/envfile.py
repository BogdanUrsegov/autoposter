"""Чтение/запись ключей в .env и применение в runtime settings."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from config import BASE_DIR, settings

logger = logging.getLogger(__name__)

ENV_PATH = BASE_DIR / ".env"
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def upsert_env(updates: dict[str, str], path: Path | None = None) -> Path:
    target = path or ENV_PATH
    clean: dict[str, str] = {}
    for key, value in updates.items():
        key = (key or "").strip()
        if not _KEY_RE.match(key):
            raise ValueError(f"Некорректный ключ .env: {key}")
        clean[key] = str(value).strip()

    lines: list[str] = []
    if target.exists():
        lines = target.read_text(encoding="utf-8").splitlines()

    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        left, _, _right = line.partition("=")
        key = left.strip()
        if key in clean:
            prefix = left[: len(left) - len(left.lstrip())] if left.startswith((" ", "\t")) else ""
            out.append(f"{prefix}{key}={clean[key]}")
            seen.add(key)
        else:
            out.append(line)

    for key, value in clean.items():
        if key not in seen:
            if out and out[-1].strip():
                out.append("")
            out.append(f"{key}={value}")

    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    logger.info("Updated .env keys: %s", ", ".join(sorted(clean)))
    return target


def apply_api_credentials(api_id: int, api_hash: str) -> None:
    api_hash = (api_hash or "").strip()
    if api_id <= 0:
        raise ValueError("API_ID должен быть положительным числом")
    if len(api_hash) < 16:
        raise ValueError("API_HASH слишком короткий")
    upsert_env({"API_ID": str(api_id), "API_HASH": api_hash})
    settings.api_id = int(api_id)
    settings.api_hash = api_hash


def api_configured() -> bool:
    try:
        return bool(int(settings.api_id or 0) > 0 and (settings.api_hash or "").strip())
    except Exception:
        return False


def api_status_text() -> str:
    if not api_configured():
        return "не заданы"
    hid = str(settings.api_id)
    hsh = settings.api_hash or ""
    masked = (hsh[:4] + "…" + hsh[-4:]) if len(hsh) > 10 else "***"
    return f"ID <code>{hid}</code> · hash <code>{masked}</code>"


def apply_userbot_timing(*, max_reply_sec: int | None = None, min_send_gap: float | None = None) -> None:
    updates: dict[str, str] = {}
    if max_reply_sec is not None:
        sec = int(max_reply_sec)
        updates["USERBOT_MAX_REPLY_SEC"] = str(sec)
        settings.userbot_max_reply_sec = sec
    if min_send_gap is not None:
        gap = float(min_send_gap)
        updates["USERBOT_MIN_SEND_GAP"] = str(gap)
        settings.userbot_min_send_gap = gap
    if updates:
        upsert_env(updates)


def userbot_global_settings() -> dict:
    return {
        "api_configured": api_configured(),
        "api_id": int(settings.api_id or 0) if api_configured() else 0,
        "api_hash_masked": (
            ((settings.api_hash or "")[:4] + "…" + (settings.api_hash or "")[-4:])
            if api_configured() and len(settings.api_hash or "") > 10
            else ("***" if api_configured() else "")
        ),
        "max_reply_sec": int(getattr(settings, "userbot_max_reply_sec", 60) or 60),
        "min_send_gap": float(getattr(settings, "userbot_min_send_gap", 2.0) or 2.0),
    }
