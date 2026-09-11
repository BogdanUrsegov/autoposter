from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts as t
from bot.services.providers import SponsorTask, next_sponsor, take_one
from database.crud import active_ads, active_offers, active_shows, completed_offer_ids, get_settings_map
from database.models import Ad, Offer, OfferView, Show, User


@dataclass
class TaskItem:
    offer: Offer | None = None
    sponsor: SponsorTask | None = None
    promo_ad: Ad | None = None
    promo_show: Show | None = None
    view_only: bool = False


def chat_from_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    raw = raw.replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "")
    raw = raw.split("?")[0].split("/")[0].lstrip("@")
    if not raw or raw.startswith("+"):
        return ""
    return raw if raw.lstrip("-").isdigit() else f"@{raw}"


def chat_target(chat_id: str):
    raw = str(chat_id or "").strip()
    if not raw:
        return raw
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw if raw.startswith("@") else f"@{raw}"


def _pick_cycle(items: list, index: int):
    if not items:
        return None
    return items[index % len(items)]


async def _take_offer(session: AsyncSession, user: User, offer: Offer) -> TaskItem:
    session.add(OfferView(user_id=user.id, offer_id=offer.id))
    user.pending_task_id = offer.id
    user.pending_service = ""
    user.pending_service_payload = "{}"
    return TaskItem(offer=offer)


def _clear_pending(user: User) -> None:
    user.pending_task_id = None
    user.pending_service = ""
    user.pending_service_payload = "{}"


async def _next_view_promo(session: AsyncSession, user: User) -> TaskItem | None:
    shows = await active_shows(session)
    if shows:
        user.task_cycle_index = int(user.task_cycle_index or 0) + 1
        _clear_pending(user)
        show = _pick_cycle(shows, user.task_cycle_index)
        return TaskItem(promo_show=show, view_only=True)

    ads = await active_ads(session)
    if ads:
        user.task_cycle_index = int(user.task_cycle_index or 0) + 1
        _clear_pending(user)
        ad = _pick_cycle(ads, user.task_cycle_index)
        return TaskItem(promo_ad=ad, view_only=True)

    greetings = await active_offers(session, "greeting")
    if greetings:
        user.task_cycle_index = int(user.task_cycle_index or 0) + 1
        _clear_pending(user)
        offer = _pick_cycle(greetings, user.task_cycle_index)
        return TaskItem(offer=offer, view_only=True)
    return None


async def next_task(session: AsyncSession, user: User) -> TaskItem | None:
    if user.pending_service:
        payload: dict = {}
        try:
            payload = json.loads(user.pending_service_payload or "{}")
        except json.JSONDecodeError:
            payload = {}
        url = str(payload.get("url") or "")
        if url:
            label = str(payload.get("label") or t.TASK_BTN_SUB)
            return TaskItem(
                sponsor=SponsorTask(
                    service=user.pending_service,
                    text=t.TASK_PROMPT,
                    buttons=[(label, url)],
                    payload=payload,
                )
            )
        cfg = await get_settings_map(session)
        from bot.services.providers import FETCHERS

        fn = FETCHERS.get(user.pending_service)
        if fn:
            sponsor = take_one(await fn(user, cfg))
            if sponsor:
                user.pending_service_payload = json.dumps(sponsor.payload or {}, ensure_ascii=False)
                return TaskItem(sponsor=sponsor)
        user.pending_service = ""
        user.pending_service_payload = "{}"
    if user.pending_task_id:
        offer = await session.get(Offer, user.pending_task_id)
        if offer and offer.is_active:
            return TaskItem(offer=offer)
        user.pending_task_id = None

    done = await completed_offer_ids(session, user.id)
    my_ops = [o for o in await active_offers(session, "my_op") if o.chat_id or o.button_url]
    unused_ops = [o for o in my_ops if o.id not in done]
    if unused_ops:
        return await _take_offer(session, user, unused_ops[0])

    cfg = await get_settings_map(session)
    sponsor = await next_sponsor(user, cfg)
    if sponsor:
        user.pending_task_id = None
        user.pending_service = sponsor.service
        user.pending_service_payload = json.dumps(sponsor.payload or {}, ensure_ascii=False)
        return TaskItem(sponsor=sponsor)

    return await _next_view_promo(session, user)
