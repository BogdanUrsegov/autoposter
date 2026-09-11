"""Telethon userbot service: login, private-chat funnel, anti-ban pacing.

CRITICAL: never crash the main bot — userbot_startup must not raise.
Telethon is lazy-imported inside functions only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from bot.services.userbot_format import prepare_send_payload
from config import SESSIONS_DIR, settings
from database import SessionLocal
from database.crud import utcnow
from database.models import UserBotAccount, UserBotLead

logger = logging.getLogger(__name__)

SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

HELLO_DEFAULT = "привеееет, ты хочешь подарочек?"
NUDGE_30_TEXT = "ну что там???"
NUDGE_6H_TEXT = "эййййй, скоро кончатся призы!!!"
NUDGE_24H_TEXT = "ну что там???"

GIFT_WAIT_SECONDS = 3600
NUDGE_30_SECONDS = 30 * 60
NUDGE_6H = 6 * 3600
NUDGE_24H = 24 * 3600

MIN_SEND_GAP = 4.5
REPLY_DELAY = (2.8, 7.5)
TYPING_SECONDS = (1.2, 2.8)

WATCHER_TICK = 20
DEFAULT_MAX_GREETS_HOUR = 1000
CONNECT_TIMEOUT = 25

_PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")

_clients: dict[int, Any] = {}
_locks: dict[int, asyncio.Lock] = {}
_last_send: dict[int, float] = {}
_pending_auth: dict[int, dict[str, Any]] = {}
_watcher_task: asyncio.Task | None = None
_started = False

def normalize_phone(phone: str) -> str:
    raw = (phone or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not raw:
        return ""
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("8"):
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return "+" + digits if digits else ""

def phone_ok(phone: str) -> bool:
    return bool(_PHONE_RE.match(normalize_phone(phone)))

def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def _hour_key(now: datetime | None = None) -> str:
    now = now or utcnow()
    return now.astimezone(timezone.utc).strftime("%Y%m%d%H")

def _require_api() -> tuple[int, str]:
    api_id = int(settings.api_id or 0)
    api_hash = (settings.api_hash or "").strip()
    if api_id <= 0 or not api_hash or api_hash.startswith("your_"):
        raise RuntimeError(
            "API_ID / API_HASH не заданы. Укажи их кнопкой «API» в админке бота "
            "(или в .env: API_ID, API_HASH с https://my.telegram.org)."
        )
    return api_id, api_hash

def _api_ok() -> bool:
    try:
        _require_api()
        return True
    except RuntimeError:
        return False

def _proxy_tuple() -> tuple | None:
    raw = settings.proxy_url
    if not raw:
        return None
    u = urlparse(raw)
    host, port = u.hostname, u.port
    if not host or not port:
        return None
    scheme = (u.scheme or "socks5").lower()
    try:
        import socks  # PySocks
    except ImportError:
        socks = None  # type: ignore
    if socks is not None:
        mapping = {
            "socks5": socks.SOCKS5,
            "socks5h": socks.SOCKS5,
            "socks4": socks.SOCKS4,
            "http": socks.HTTP,
            "https": socks.HTTP,
        }
        ptype = mapping.get(scheme, socks.SOCKS5)
        if u.username:
            return (ptype, host, port, True, u.username, u.password or "")
        return (ptype, host, port)
    # fallback numeric types for Telethon without PySocks
    ptype = {"socks5": 2, "socks5h": 2, "socks4": 3, "http": 1, "https": 1}.get(scheme, 2)
    if u.username:
        return (ptype, host, port, True, u.username, u.password or "")
    return (ptype, host, port)

def _make_client(session_str: str = ""):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id, api_hash = _require_api()
    return TelegramClient(
        StringSession(session_str or ""),
        api_id,
        api_hash,
        proxy=_proxy_tuple(),
        device_model="Desktop",
        system_version="Windows 10",
        app_version="4.16.8",
        lang_code="ru",
        system_lang_code="ru",
        flood_sleep_threshold=24,
        connection_retries=3,
        retry_delay=2,
        auto_reconnect=True,
        sequential_updates=True,
    )

def _lock(account_id: int) -> asyncio.Lock:
    if account_id not in _locks:
        _locks[account_id] = asyncio.Lock()
    return _locks[account_id]

async def _close_client(client: Any) -> None:
    if not client:
        return
    try:
        await client.disconnect()
    except Exception:
        pass

async def _set_fields(account_id: int, **fields: Any) -> None:
    async with SessionLocal() as session:
        row = await session.get(UserBotAccount, account_id)
        if not row:
            return
        for k, v in fields.items():
            if hasattr(row, k):
                setattr(row, k, v)
        row.updated_at = utcnow()
        await session.commit()

async def get_account(account_id: int) -> UserBotAccount | None:
    async with SessionLocal() as session:
        return await session.get(UserBotAccount, account_id)

async def list_accounts() -> list[UserBotAccount]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(UserBotAccount).order_by(UserBotAccount.id))).scalars().all()
        return list(rows)

async def create_account(phone: str) -> UserBotAccount:
    phone = normalize_phone(phone)
    if not phone_ok(phone):
        raise ValueError("Некорректный номер телефона")
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(UserBotAccount).where(UserBotAccount.phone == phone))
        ).scalar_one_or_none()
        if existing:
            raise ValueError("Аккаунт с таким номером уже есть")
        row = UserBotAccount(
            phone=phone,
            status="new",
            hello_text=HELLO_DEFAULT,
            max_greets_hour=DEFAULT_MAX_GREETS_HOUR,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

async def delete_account(account_id: int) -> None:
    await stop_account(account_id, set_inactive=False)
    pending = _pending_auth.pop(account_id, None)
    if pending:
        await _close_client(pending.get("client"))
    async with SessionLocal() as session:
        row = await session.get(UserBotAccount, account_id)
        if row:
            await session.delete(row)
            await session.commit()

async def update_texts(
    account_id: int,
    *,
    hello_text: str | None = None,
    gift_text: str | None = None,
    gift_parse_mode: str | None = None,
    gift_entities: str | list | None = None,
) -> None:
    import json

    fields: dict[str, Any] = {}
    if hello_text is not None:
        fields["hello_text"] = hello_text
    if gift_text is not None:
        fields["gift_text"] = gift_text
    if gift_parse_mode is not None:
        fields["gift_parse_mode"] = gift_parse_mode
    if gift_entities is not None:
        if isinstance(gift_entities, str):
            fields["gift_entities"] = gift_entities
        else:
            fields["gift_entities"] = json.dumps(gift_entities, ensure_ascii=False)
    if fields:
        await _set_fields(account_id, **fields)

async def set_active(account_id: int, active: bool) -> None:
    if active:
        await _set_fields(account_id, is_active=True)
        await start_account(account_id)
    else:
        await stop_account(account_id, set_inactive=True)

def is_online(account_id: int) -> bool:
    client = _clients.get(account_id)
    if not client:
        return False
    try:
        return bool(client.is_connected())
    except Exception:
        return False

def status_label(account: UserBotAccount | None, *, online: bool | None = None) -> str:
    if account is None:
        return "нет"
    on = is_online(account.id) if online is None else online
    st = (account.status or "").strip()
    if on:
        return "онлайн"
    labels = {
        "new": "новый",
        "wait_code": "ждёт код",
        "wait_2fa": "ждёт 2FA",
        "ready": "готов",
        "stopped": "остановлен",
        "error": "ошибка",
    }
    base = labels.get(st, st or "—")
    err = (account.last_error or "").strip()
    if st == "error" and err:
        return f"ошибка: {err[:80]}"
    return base

async def begin_login(account_id: int) -> dict:
    _require_api()
    async with SessionLocal() as session:
        row = await session.get(UserBotAccount, account_id)
        if not row:
            raise RuntimeError("Аккаунт не найден")
        phone = row.phone
    old = _pending_auth.pop(account_id, None)
    if old:
        await _close_client(old.get("client"))
    client = _make_client()
    try:
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
        result = await client.send_code_request(phone)
        _pending_auth[account_id] = {
            "phone": phone,
            "phone_code_hash": result.phone_code_hash,
            "client": client,
        }
        await _set_fields(account_id, status="wait_code", last_error="")
        return {"ok": True, "status": "wait_code"}
    except Exception as e:
        await _close_client(client)
        await _set_fields(account_id, status="error", last_error=str(e)[:2000])
        raise

async def submit_code(account_id: int, code: str) -> dict:
    from telethon.errors import SessionPasswordNeededError

    pending = _pending_auth.get(account_id)
    if not pending or not pending.get("client"):
        raise RuntimeError("Сначала запроси код")
    code = (code or "").strip().replace(" ", "")
    try:
        await pending["client"].sign_in(
            phone=pending["phone"],
            code=code,
            phone_code_hash=pending["phone_code_hash"],
        )
    except SessionPasswordNeededError:
        await _set_fields(account_id, status="wait_2fa", last_error="")
        return {"ok": True, "status": "wait_2fa"}
    except Exception as e:
        await _set_fields(account_id, status="wait_code", last_error=str(e)[:2000])
        raise
    return await _finalize_login(account_id)

async def submit_password(account_id: int, password: str) -> dict:
    pending = _pending_auth.get(account_id)
    if not pending or not pending.get("client"):
        raise RuntimeError("Сначала запроси код и введи его")
    try:
        await pending["client"].sign_in(password=password)
    except Exception as e:
        await _set_fields(account_id, status="wait_2fa", last_error=str(e)[:2000])
        raise
    return await _finalize_login(account_id)

async def _finalize_login(account_id: int) -> dict:
    pending = _pending_auth.pop(account_id, None)
    if not pending or not pending.get("client"):
        raise RuntimeError("Нет активной авторизации")
    client = pending["client"]
    me = await client.get_me()
    session_str = client.session.save()
    await _set_fields(
        account_id,
        status="ready",
        last_error="",
        session_string=session_str,
        tg_id=int(me.id) if me else None,
        username=(me.username or "") if me else "",
        first_name=(me.first_name or "") if me else "",
        is_active=True,
    )
    await _close_client(client)
    await start_account(account_id)
    return {
        "ok": True,
        "status": "ready",
        "tg_id": int(me.id) if me else None,
        "username": (me.username or "") if me else "",
    }

def _ensure_watcher() -> None:
    global _watcher_task
    if _watcher_task and not _watcher_task.done():
        return
    _watcher_task = asyncio.create_task(_watcher_loop(), name="userbot-watcher")

async def start_account(account_id: int) -> None:
    if account_id in _clients:
        return
    _require_api()
    async with SessionLocal() as session:
        row = await session.get(UserBotAccount, account_id)
        if not row or not (row.session_string or "").strip():
            raise RuntimeError("Нет сессии — пройди вход по коду")
        session_str = row.session_string

    client = _make_client(session_str)
    try:
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
        if not await client.is_user_authorized():
            await _close_client(client)
            await _set_fields(
                account_id,
                status="error",
                last_error="Сессия недействительна",
                is_active=False,
            )
            raise RuntimeError("Сессия недействительна — войди заново")
    except Exception as e:
        await _close_client(client)
        await _set_fields(
            account_id,
            status="error",
            last_error=str(e)[:2000],
            is_active=False,
        )
        raise

    from telethon import events

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def _on_message(event):  # noqa: ANN001
        try:
            await _handle_incoming(account_id, event)
        except Exception:
            logger.exception("userbot msg #%s", account_id)

    _clients[account_id] = client
    await _set_fields(account_id, status="ready", last_error="", is_active=True)
    try:
        me = await client.get_me()
        if me:
            await _set_fields(
                account_id,
                tg_id=int(me.id),
                username=me.username or "",
                first_name=me.first_name or "",
            )
    except Exception:
        logger.exception("userbot get_me #%s", account_id)
    _ensure_watcher()
    logger.info("Userbot #%s online", account_id)

async def stop_account(account_id: int, set_inactive: bool = True) -> None:
    client = _clients.pop(account_id, None)
    await _close_client(client)
    if set_inactive:
        await _set_fields(account_id, status="stopped", is_active=False)

async def stop_all() -> None:
    for aid in list(_clients.keys()):
        await stop_account(aid, set_inactive=False)

async def start_all_active() -> None:
    if not _api_ok():
        logger.warning("Userbot: API не задан — пропускаю start_all_active")
        return
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(UserBotAccount).where(
                    UserBotAccount.is_active.is_(True),
                    UserBotAccount.session_string != "",
                )
            )
        ).scalars().all()
        ids = [r.id for r in rows]
    for aid in ids:
        try:
            await start_account(aid)
        except Exception:
            logger.exception("userbot start_all #%s", aid)

async def _can_greet(account_id: int) -> bool:
    key = _hour_key()
    async with SessionLocal() as session:
        row = await session.get(UserBotAccount, account_id)
        if not row:
            return False
        limit = int(row.max_greets_hour or DEFAULT_MAX_GREETS_HOUR)
        if row.greets_hour_key != key:
            row.greets_hour_key = key
            row.greets_hour = 0
            await session.commit()
            return True
        return int(row.greets_hour or 0) < limit

async def _bump_greet(account_id: int) -> None:
    key = _hour_key()
    async with SessionLocal() as session:
        row = await session.get(UserBotAccount, account_id)
        if not row:
            return
        if row.greets_hour_key != key:
            row.greets_hour_key = key
            row.greets_hour = 0
        row.greets_hour = int(row.greets_hour or 0) + 1
        await session.commit()

async def _send_payload(
    account_id: int,
    peer_id: int,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    entities_json: str | None = None,
    humanize: bool = True,
) -> bool:
    client = _clients.get(account_id)
    if not client or not (text or "").strip():
        return False
    plain, entities = prepare_send_payload(text, parse_mode, entities_json)
    if not (plain or "").strip() and not entities:
        return False

    async with _lock(account_id):
        now = time.monotonic()
        gap = now - _last_send.get(account_id, 0.0)
        if gap < MIN_SEND_GAP:
            await asyncio.sleep(MIN_SEND_GAP - gap + random.uniform(0.15, 0.8))
        if humanize:
            try:
                async with client.action(peer_id, "typing"):
                    await asyncio.sleep(random.uniform(*TYPING_SECONDS))
            except Exception:
                await asyncio.sleep(random.uniform(0.8, 1.8))
        kwargs: dict[str, Any] = {}
        if entities:
            kwargs["formatting_entities"] = entities
        try:
            await client.send_message(peer_id, plain, **kwargs)
            _last_send[account_id] = time.monotonic()
            return True
        except Exception as e:
            from telethon.errors import FloodWaitError, PeerFloodError, UserIsBlockedError

            if isinstance(e, FloodWaitError):
                wait = int(getattr(e, "seconds", 5)) + random.randint(2, 8)
                logger.warning("userbot #%s FloodWait %ss", account_id, wait)
                await asyncio.sleep(min(wait, 180))
                try:
                    await client.send_message(peer_id, plain, **kwargs)
                    _last_send[account_id] = time.monotonic()
                    return True
                except Exception:
                    logger.exception("userbot retry send #%s", account_id)
                    return False
            if isinstance(e, (UserIsBlockedError, PeerFloodError)):
                logger.warning("userbot #%s blocked/flood peer %s", account_id, peer_id)
                async with SessionLocal() as session:
                    lead = (
                        await session.execute(
                            select(UserBotLead).where(
                                UserBotLead.account_id == account_id,
                                UserBotLead.peer_tg_id == peer_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if lead:
                        lead.blocked = True
                        lead.stage = "done"
                        await session.commit()
                return False
            logger.exception("userbot send #%s → %s", account_id, peer_id)
            return False

async def _handle_incoming(account_id: int, event) -> None:  # noqa: ANN001
    if not event.is_private:
        return
    sender = await event.get_sender()
    if not sender or getattr(sender, "bot", False) or getattr(sender, "is_self", False):
        return
    peer_id = int(sender.id)
    username = (getattr(sender, "username", None) or "")[:64]
    first_name = (getattr(sender, "first_name", None) or "")[:128]
    now = utcnow()

    async with SessionLocal() as session:
        acc = await session.get(UserBotAccount, account_id)
        if not acc or not acc.is_active:
            return
        hello = (acc.hello_text or HELLO_DEFAULT).strip() or HELLO_DEFAULT
        gift = (acc.gift_text or "").strip()
        gift_mode = acc.gift_parse_mode or "HTML"
        gift_ents = acc.gift_entities or "[]"
        lead = (
            await session.execute(
                select(UserBotLead).where(
                    UserBotLead.account_id == account_id,
                    UserBotLead.peer_tg_id == peer_id,
                )
            )
        ).scalar_one_or_none()
        if lead and lead.blocked:
            return

        if lead is None:
            if not await _can_greet(account_id):
                logger.info("userbot #%s greet limit, skip %s", account_id, peer_id)
                return
            lead = UserBotLead(
                account_id=account_id,
                peer_tg_id=peer_id,
                username=username,
                first_name=first_name,
                stage="greeted",
                greeted_at=now,
                gift_due_at=now + timedelta(seconds=GIFT_WAIT_SECONDS),
                last_user_at=now,
                last_bot_at=now,
            )
            session.add(lead)
            await session.commit()
            lead_id = lead.id
            await _bump_greet(account_id)
            await asyncio.sleep(random.uniform(*REPLY_DELAY))
            ok = await _send_payload(account_id, peer_id, hello, parse_mode="HTML")
            if not ok:
                async with SessionLocal() as s2:
                    row = await s2.get(UserBotLead, lead_id)
                    if row:
                        await s2.delete(row)
                        await s2.commit()
            return

        lead.last_user_at = now
        lead.username = username or lead.username
        lead.first_name = first_name or lead.first_name
        stage = lead.stage or "greeted"
        lead_id = lead.id
        await session.commit()

    if stage == "greeted":
        if gift:
            await asyncio.sleep(random.uniform(*REPLY_DELAY))
            ok = await _send_payload(
                account_id,
                peer_id,
                gift,
                parse_mode=gift_mode,
                entities_json=gift_ents,
            )
            if ok:
                async with SessionLocal() as session:
                    lead = await session.get(UserBotLead, lead_id)
                    if lead and lead.stage == "greeted":
                        lead.stage = "gifted"
                        lead.gift_sent_at = utcnow()
                        lead.last_bot_at = utcnow()
                        await session.commit()
        else:
            async with SessionLocal() as session:
                lead = await session.get(UserBotLead, lead_id)
                if lead:
                    lead.stage = "done"
                    await session.commit()
        return

    if stage == "gifted":
        async with SessionLocal() as session:
            lead = await session.get(UserBotLead, lead_id)
            if lead and lead.stage == "gifted":
                lead.stage = "done"
                await session.commit()

async def _watcher_loop() -> None:
    while _started:
        try:
            await _process_due()
        except Exception:
            logger.exception("userbot watcher")
        await asyncio.sleep(WATCHER_TICK)

async def _process_due() -> None:
    now = utcnow()
    async with SessionLocal() as session:
        due_gifts = (
            await session.execute(
                select(UserBotLead).where(
                    UserBotLead.stage == "greeted",
                    UserBotLead.blocked.is_(False),
                    UserBotLead.gift_due_at.is_not(None),
                    UserBotLead.gift_due_at <= now,
                )
            )
        ).scalars().all()
        gift_jobs = [(l.id, l.account_id, l.peer_tg_id) for l in due_gifts]

        gifted = (
            await session.execute(
                select(UserBotLead).where(
                    UserBotLead.stage == "gifted",
                    UserBotLead.blocked.is_(False),
                    UserBotLead.gift_sent_at.is_not(None),
                )
            )
        ).scalars().all()
        nudge_jobs: list[tuple[int, int, int, str, str]] = []
        for lead in gifted:
            sent_at = _aware(lead.gift_sent_at)
            if not sent_at:
                continue
            last_user = _aware(lead.last_user_at)
            if last_user and last_user > sent_at:
                lead.stage = "done"
                continue
            if not lead.nudge_30_sent and now >= sent_at + timedelta(seconds=NUDGE_30_SECONDS):
                nudge_jobs.append((lead.id, lead.account_id, lead.peer_tg_id, "nudge_30_sent", NUDGE_30_TEXT))
            elif not lead.nudge_6h_sent and now >= sent_at + timedelta(seconds=NUDGE_6H):
                nudge_jobs.append((lead.id, lead.account_id, lead.peer_tg_id, "nudge_6h_sent", NUDGE_6H_TEXT))
            elif not lead.nudge_24h_sent and now >= sent_at + timedelta(seconds=NUDGE_24H):
                nudge_jobs.append((lead.id, lead.account_id, lead.peer_tg_id, "nudge_24h_sent", NUDGE_24H_TEXT))
        await session.commit()

    for lead_id, account_id, peer_id in gift_jobs:
        if account_id not in _clients:
            continue
        async with SessionLocal() as session:
            acc = await session.get(UserBotAccount, account_id)
            lead = await session.get(UserBotLead, lead_id)
            if not acc or not lead or lead.stage != "greeted":
                continue
            gift = (acc.gift_text or "").strip()
            mode = acc.gift_parse_mode or "HTML"
            ents = acc.gift_entities or "[]"
        if not gift:
            async with SessionLocal() as session:
                lead = await session.get(UserBotLead, lead_id)
                if lead and lead.stage == "greeted":
                    lead.stage = "done"
                    await session.commit()
            continue
        await asyncio.sleep(random.uniform(0.4, 1.6))
        ok = await _send_payload(account_id, peer_id, gift, parse_mode=mode, entities_json=ents)
        if ok:
            async with SessionLocal() as session:
                lead = await session.get(UserBotLead, lead_id)
                if lead and lead.stage == "greeted":
                    lead.stage = "gifted"
                    lead.gift_sent_at = utcnow()
                    lead.last_bot_at = utcnow()
                    await session.commit()

    for lead_id, account_id, peer_id, flag, text in nudge_jobs:
        if account_id not in _clients:
            continue
        await asyncio.sleep(random.uniform(0.3, 1.2))
        ok = await _send_payload(account_id, peer_id, text, parse_mode=None, humanize=True)
        if ok:
            async with SessionLocal() as session:
                lead = await session.get(UserBotLead, lead_id)
                if lead and lead.stage == "gifted":
                    setattr(lead, flag, True)
                    lead.last_bot_at = utcnow()
                    if flag == "nudge_24h_sent":
                        lead.stage = "done"
                    await session.commit()

async def userbot_startup() -> None:
    """Never raise — main bot must keep running even if Telethon fails."""
    global _started, _watcher_task
    try:
        if _started:
            return
        _started = True
        await asyncio.sleep(1.5)
        if not _api_ok():
            logger.warning(
                "Userbot: API_ID/API_HASH не заданы — юзер-боты не запускаются "
                "(кнопка «API» в админке бота)."
            )
            return
        await start_all_active()
        _ensure_watcher()
        logger.info("Userbot service started (%s online)", len(_clients))
    except Exception:
        logger.exception("userbot_startup failed (ignored)")

async def userbot_shutdown() -> None:
    global _started, _watcher_task
    _started = False
    task = _watcher_task
    _watcher_task = None
    if task:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await stop_all()
    for pending in list(_pending_auth.values()):
        await _close_client(pending.get("client"))
    _pending_auth.clear()
