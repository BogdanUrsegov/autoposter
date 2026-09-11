from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from config import settings


def parse_webapp_user(init_data: str, max_age: int = 86400) -> dict | None:
    if not init_data or not settings.bot_token:
        return None
    parsed = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    given_hash = parsed.pop("hash", "")
    if not given_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, given_hash):
        return None
    try:
        auth_date = int(parsed.get("auth_date") or 0)
    except ValueError:
        return None
    if auth_date and abs(time.time() - auth_date) > max_age:
        return None
    try:
        user = json.loads(parsed.get("user") or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict) or not user.get("id"):
        return None
    return user


def admin_from_init_data(init_data: str) -> dict | None:
    user = parse_webapp_user(init_data)
    if not user:
        return None
    if not settings.is_admin(int(user["id"])):
        return None
    return user
