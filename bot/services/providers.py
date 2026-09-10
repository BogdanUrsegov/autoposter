from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

import httpx

from config import settings
from bot import texts as t
from database.models import User

logger = logging.getLogger(__name__)


@dataclass
class SponsorTask:
    service: str
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)
    payload: dict = field(default_factory=dict)


def _key(cfg: dict[str, str], name: str) -> str:
    db_key = (cfg.get(f"{name}_key") or "").strip()
    if db_key:
        return db_key
    return (getattr(settings, f"{name}_key", "") or "").strip()


def sponsor_label(url: str, raw: str = "") -> str:
    blob = f"{raw} {url}".lower()
    if "запуст" in blob or "start=" in (url or "").lower() or "?start" in (url or "").lower():
        return t.TASK_BTN_START
    return t.TASK_BTN_SUB


def take_one(task: SponsorTask | None) -> SponsorTask | None:
    if not task or not task.buttons:
        return None
    label, url = task.buttons[0]
    pretty = sponsor_label(url, label)
    task.buttons = [(pretty, url)]
    extra = dict(task.payload or {})
    extra["url"] = url
    extra["label"] = pretty
    task.payload = extra
    return task


def payload_url(user: User) -> str:
    try:
        data = json.loads(user.pending_service_payload or "{}")
    except json.JSONDecodeError:
        return ""
    return str(data.get("url") or "")


async def _try_member(user: User, url: str) -> bool:
    from bot.loader import bot
    from bot.services.tasks import chat_from_url, chat_target

    chat = chat_from_url(url)
    if not chat:
        return False
    try:
        member = await bot.get_chat_member(chat_target(chat), user.tg_id)
        return member.status in ("member", "administrator", "creator", "restricted")
    except Exception:
        return False


def _on(cfg: dict[str, str], name: str) -> bool:
    return (cfg.get(f"{name}_enabled") or "1") == "1"


async def _post(url: str, headers: dict, payload: dict, verify: bool = True) -> dict | None:
    timeout = httpx.Timeout(3.5, connect=2.5)
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=verify, proxy=settings.proxy_url) as client:
            resp = await client.post(url, headers=headers, json=payload)
            try:
                return resp.json()
            except Exception:
                logger.warning("%s non-json %s %s", url, resp.status_code, resp.text[:200])
                return None
    except httpx.HTTPError as err:
        logger.warning("request failed %s: %s", url, err.__class__.__name__)
        return None
    except Exception:
        logger.exception("request failed %s", url)
        return None


async def fetch_subgram(user: User, cfg: dict[str, str]) -> SponsorTask | None:
    key = _key(cfg, "subgram")
    if not key:
        return None
    data = await _post(
        "https://api.subgram.org/get-sponsors",
        {"Auth": key, "Content-Type": "application/json"},
        {
            "user_id": user.tg_id,
            "chat_id": user.tg_id,
            "action": "task",
            "get_links": 1,
            "is_premium": int(bool(user.is_premium)),
            "first_name": user.first_name or "",
            "username": (user.username or ""),
            "language_code": user.language_code or "ru",
        },
    )
    if not data or data.get("status") != "warning":
        return None
    pending = set(data.get("links") or [])
    buttons: list[tuple[str, str]] = []
    for sp in (data.get("additional") or {}).get("sponsors") or []:
        link = sp.get("link") or ""
        if not link:
            continue
        if pending and link not in pending:
            continue
        if not pending and not (sp.get("available_now") and sp.get("status") == "unsubscribed"):
            continue
        buttons.append((sp.get("button_text") or sp.get("resource_name") or "Подписаться", link))
    if not buttons:
        for link in pending:
            buttons.append(("Подписаться", link))
    if not buttons:
        return None
    return SponsorTask(
        service="subgram",
        text=t.TASK_PROMPT,
        buttons=buttons,
        payload={"status": "warning"},
    )


async def check_subgram(user: User, cfg: dict[str, str]) -> bool:
    key = _key(cfg, "subgram")
    if not key:
        return True
    data = await _post(
        "https://api.subgram.org/get-sponsors",
        {"Auth": key, "Content-Type": "application/json"},
        {
            "user_id": user.tg_id,
            "chat_id": user.tg_id,
            "action": "task",
            "get_links": 1,
            "is_premium": int(bool(user.is_premium)),
            "username": user.username or "",
            "language_code": user.language_code or "ru",
        },
    )
    if not data:
        return False
    url = payload_url(user)
    pending = list(data.get("links") or [])
    if data.get("status") != "warning":
        return True
    if url and url not in pending:
        return True
    return await _try_member(user, url)


async def fetch_tgrass(user: User, cfg: dict[str, str]) -> SponsorTask | None:
    key = _key(cfg, "tgrass")
    if not key:
        return None
    data = await _post(
        "https://tgrass.space/offers",
        {"Auth": key, "Content-Type": "application/json", "accept": "application/json"},
        {
            "tg_user_id": user.tg_id,
            "tg_login": user.username,
            "lang": user.language_code or "ru",
            "is_premium": bool(user.is_premium),
        },
        verify=False,
    )
    if not data or data.get("status") != "not_ok":
        return None
    buttons = []
    for offer in data.get("offers") or []:
        if offer.get("subscribed"):
            continue
        link = offer.get("link")
        if not link:
            continue
        label = "Подписаться" if offer.get("type") == "channel" else "Перейти"
        buttons.append((offer.get("name") or label, link))
    if not buttons:
        return None
    return SponsorTask(
        service="tgrass",
        text=t.TASK_PROMPT,
        buttons=buttons,
        payload={},
    )


async def check_tgrass(user: User, cfg: dict[str, str]) -> bool:
    key = _key(cfg, "tgrass")
    if not key:
        return True
    data = await _post(
        "https://tgrass.space/offers",
        {"Auth": key, "Content-Type": "application/json", "accept": "application/json"},
        {
            "tg_user_id": user.tg_id,
            "tg_login": user.username,
            "lang": user.language_code or "ru",
            "is_premium": bool(user.is_premium),
        },
        verify=False,
    )
    if not data:
        return False
    url = payload_url(user)
    if data.get("status") in ("ok", "no_offers"):
        return True
    left = [
        offer.get("link")
        for offer in (data.get("offers") or [])
        if offer.get("link") and not offer.get("subscribed")
    ]
    if url and url not in left:
        return True
    return await _try_member(user, url)


async def fetch_botohub(user: User, cfg: dict[str, str]) -> SponsorTask | None:
    key = _key(cfg, "botohub")
    if not key:
        return None
    data = await _post(
        "https://botohub.me/get-tasks",
        {"Auth": key, "Content-Type": "application/json"},
        {"chat_id": user.tg_id, "is_task": True},
    )
    if not data or data.get("skip") or data.get("completed"):
        return None
    tasks = [u for u in (data.get("tasks") or []) if u]
    if not tasks:
        return None
    buttons = [("Подписаться", url) for url in tasks]
    return SponsorTask(
        service="botohub",
        text=t.TASK_PROMPT,
        buttons=buttons,
        payload={},
    )


async def check_botohub(user: User, cfg: dict[str, str]) -> bool:
    key = _key(cfg, "botohub")
    if not key:
        return True
    data = await _post(
        "https://botohub.me/get-tasks",
        {"Auth": key, "Content-Type": "application/json"},
        {"chat_id": user.tg_id, "is_task": True},
    )
    if not data:
        return False
    if data.get("skip") or data.get("completed") or data.get("prev_success"):
        return True
    url = payload_url(user)
    left = [u for u in (data.get("tasks") or []) if u]
    if url and url not in left:
        return True
    if not left:
        return True
    return await _try_member(user, url)


FETCHERS = {
    "subgram": fetch_subgram,
    "tgrass": fetch_tgrass,
    "botohub": fetch_botohub,
}
CHECKERS = {
    "subgram": check_subgram,
    "tgrass": check_tgrass,
    "botohub": check_botohub,
}
SERVICE_ORDER = ("subgram", "tgrass", "botohub")


async def next_sponsor(user: User, cfg: dict[str, str]) -> SponsorTask | None:
    names = [name for name in SERVICE_ORDER if _on(cfg, name)]
    if not names:
        return None

    async def _one(name: str) -> tuple[str, SponsorTask | None]:
        try:
            return name, take_one(await FETCHERS[name](user, cfg))
        except Exception:
            logger.warning("fetch %s failed", name, exc_info=True)
            return name, None

    results = await asyncio.gather(*[_one(name) for name in names])
    found = {name: task for name, task in results}
    for name in names:
        if found.get(name):
            return found[name]
    return None


async def verify_sponsor(name: str, user: User, cfg: dict[str, str]) -> bool:
    fn = CHECKERS.get(name)
    if not fn:
        return True
    try:
        return await fn(user, cfg)
    except Exception:
        logger.exception("check %s failed", name)
        return False


async def collect_sponsors(user: User, cfg: dict[str, str], n: int) -> list[tuple[str, str, str]]:
    need = max(0, int(n or 0))
    if need <= 0:
        return []
    names = [name for name in SERVICE_ORDER if _on(cfg, name)]
    if not names:
        return []

    async def _one(name: str) -> tuple[str, SponsorTask | None, bool]:
        try:
            return name, await FETCHERS[name](user, cfg), True
        except Exception:
            logger.warning("fetch %s failed", name, exc_info=True)
            return name, None, False

    results = await asyncio.gather(*[_one(name) for name in names])
    buttons: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for name, task, _ok in results:
        if not task:
            continue
        for label, url in task.buttons or []:
            if not url or url in seen:
                continue
            seen.add(url)
            buttons.append((sponsor_label(url, label), url, name))
            if len(buttons) >= need:
                return buttons
    return buttons


async def leftover_urls(user: User, cfg: dict[str, str], urls: list[str]) -> list[str]:
    wanted = [u for u in urls if u]
    if not wanted:
        return []
    names = [name for name in SERVICE_ORDER if _on(cfg, name)]
    pending: set[str] = set()
    any_ok = False
    if names:
        async def _one(name: str) -> tuple[list[str], bool]:
            try:
                task = await FETCHERS[name](user, cfg)
                links = [u for _, u in (task.buttons if task else []) if u]
                return links, True
            except Exception:
                logger.warning("fetch %s failed", name, exc_info=True)
                return [], False

        results = await asyncio.gather(*[_one(name) for name in names])
        for links, ok in results:
            if ok:
                any_ok = True
                pending.update(links)

    left: list[str] = []
    for url in wanted:
        if await _try_member(user, url):
            continue
        if any_ok and url not in pending:
            continue
        if not any_ok and names:
            left.append(url)
            continue
        left.append(url)
    return left
