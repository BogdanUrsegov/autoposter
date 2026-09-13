from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, select
from starlette.middleware.sessions import SessionMiddleware

from bot.loader import bot
from bot.services.content import send_content
from bot.services.webapp import admin_from_init_data
from config import BASE_DIR, UPLOADS_DIR, settings
from database import SessionLocal, init_db
from database.crud import campaign_stats, get_settings_map, set_setting, stats_bundle
from database.models import Ad, Broadcast, Campaign, Offer, Show, User, UserBotAccount, UserBotLead, Withdrawal

logger = logging.getLogger(__name__)

app = FastAPI(title="Clickbot Admin")
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, max_age=60 * 60 * 24 * 7)

TEMPLATES = Path(__file__).resolve().parent / "templates"
STATIC = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")


def _init_data(request: Request) -> str:
    header = request.headers.get("X-Telegram-Init-Data") or ""
    if header:
        return header
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("tma "):
        return auth[4:].strip()
    return ""


def is_admin_request(request: Request) -> bool:
    if request.session.get("admin"):
        return True
    return admin_from_init_data(_init_data(request)) is not None


def require_admin(request: Request) -> None:
    if not is_admin_request(request):
        raise HTTPException(status_code=401, detail="auth")


def dec(value: Any) -> float:
    if value is None:
        return 0.0
    return float(Decimal(value))


def user_dict(u: User) -> dict:
    return {
        "id": u.id,
        "tg_id": u.tg_id,
        "username": u.username,
        "first_name": u.first_name,
        "is_premium": u.is_premium,
        "balance": dec(u.balance),
        "total_earned": dec(u.total_earned),
        "total_withdrawn": dec(u.total_withdrawn),
        "clicks_total": u.clicks_total,
        "blocked": u.blocked,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "referrer_id": u.referrer_id,
        "campaign_id": u.campaign_id,
        "start_count": u.start_count,
    }


def ad_dict(a: Ad) -> dict:
    extra = []
    try:
        extra = json.loads(a.extra_buttons or "[]")
    except json.JSONDecodeError:
        extra = []
    return {
        "id": a.id,
        "title": a.title,
        "text": a.text,
        "parse_mode": a.parse_mode,
        "media_type": a.media_type,
        "media_file_id": a.media_file_id,
        "media_path": a.media_path,
        "button_type": a.button_type,
        "button_text": a.button_text,
        "button_url": a.button_url,
        "button_color": a.button_color,
        "extra_buttons": extra,
        "is_active": a.is_active,
        "sort_order": a.sort_order,
    }


def offer_dict(o: Offer) -> dict:
    return {
        "id": o.id,
        "kind": o.kind,
        "title": o.title,
        "text": o.text,
        "parse_mode": o.parse_mode,
        "media_type": o.media_type,
        "media_file_id": o.media_file_id,
        "media_path": o.media_path,
        "button_text": o.button_text,
        "button_url": o.button_url,
        "check_type": o.check_type,
        "chat_id": o.chat_id,
        "service_name": o.service_name,
        "is_active": o.is_active,
        "sort_order": o.sort_order,
    }


def show_dict(s: Show) -> dict:
    extra = []
    try:
        extra = json.loads(s.extra_buttons or "[]")
    except json.JSONDecodeError:
        extra = []
    return {
        "id": s.id,
        "title": s.title,
        "text": s.text,
        "parse_mode": s.parse_mode,
        "media_type": s.media_type,
        "media_file_id": s.media_file_id,
        "media_path": s.media_path,
        "button_type": s.button_type,
        "button_text": s.button_text,
        "button_url": s.button_url,
        "button_color": s.button_color,
        "extra_buttons": extra,
        "delay_seconds": s.delay_seconds,
        "is_active": s.is_active,
        "sort_order": s.sort_order,
    }


@app.on_event("startup")
async def _startup() -> None:
    await init_db()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    password = str(body.get("password") or "")
    if not settings.admin_password or password != settings.admin_password:
        raise HTTPException(status_code=403, detail="Неверный пароль")
    request.session["admin"] = True
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request):
    webapp_user = admin_from_init_data(_init_data(request))
    admin = bool(request.session.get("admin") or webapp_user)
    return {"admin": admin, "webapp": bool(_init_data(request))}


@app.post("/api/webapp-auth")
async def webapp_auth(request: Request):
    body = await request.json()
    init_data = str(body.get("init_data") or _init_data(request))
    user = admin_from_init_data(init_data)
    if not user:
        parsed = None
        from bot.services.webapp import parse_webapp_user

        parsed = parse_webapp_user(init_data)
        if parsed:
            raise HTTPException(status_code=403, detail="Нет доступа")
        raise HTTPException(status_code=401, detail="auth")
    request.session["admin"] = True
    request.session["tg_id"] = int(user["id"])
    return {"ok": True, "admin": True, "tg_id": int(user["id"])}


@app.get("/api/stats")
async def stats(request: Request, period: str = "day", _: None = Depends(require_admin)):
    if period not in ("day", "week", "all"):
        period = "day"
    async with SessionLocal() as session:
        data = await stats_bundle(session, period)
        cfg = await get_settings_map(session)
    rate = float(cfg.get("star_fiat_rate") or 0)
    data["income_fiat"] = round(data["stars_income"] * rate, 2)
    data["withdraw_fiat"] = round(data["stars_withdrawn"] * rate, 2)
    data["fiat_currency"] = cfg.get("fiat_currency") or "RUB"
    return data


@app.get("/api/settings")
async def get_settings(request: Request, _: None = Depends(require_admin)):
    async with SessionLocal() as session:
        return await get_settings_map(session)


@app.put("/api/settings")
async def put_settings(request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    async with SessionLocal() as session:
        for key, value in body.items():
            await set_setting(session, str(key), str(value))
        await session.commit()
        return await get_settings_map(session)


@app.get("/api/ads")
async def list_ads(request: Request, _: None = Depends(require_admin)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Ad).order_by(Ad.sort_order, Ad.id))).scalars().all()
        return [ad_dict(a) for a in rows]


@app.post("/api/ads")
async def create_ad(request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    async with SessionLocal() as session:
        ad = Ad(
            title=body.get("title") or "Реклама",
            text=body.get("text") or "",
            parse_mode=body.get("parse_mode") or "HTML",
            media_type=body.get("media_type") or "none",
            media_file_id=body.get("media_file_id") or "",
            media_path=body.get("media_path") or "",
            button_type=body.get("button_type") or "none",
            button_text=body.get("button_text") or "",
            button_url=body.get("button_url") or "",
            button_color=body.get("button_color") or "#2AABEE",
            extra_buttons=json.dumps(body.get("extra_buttons") or []),
            is_active=bool(body.get("is_active", True)),
            sort_order=int(body.get("sort_order") or 0),
        )
        session.add(ad)
        await session.commit()
        await session.refresh(ad)
        return ad_dict(ad)


@app.put("/api/ads/{ad_id}")
async def update_ad(ad_id: int, request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    async with SessionLocal() as session:
        ad = await session.get(Ad, ad_id)
        if not ad:
            raise HTTPException(404)
        for field in (
            "title",
            "text",
            "parse_mode",
            "media_type",
            "media_file_id",
            "media_path",
            "button_type",
            "button_text",
            "button_url",
            "button_color",
            "is_active",
            "sort_order",
        ):
            if field in body:
                setattr(ad, field, body[field])
        if "extra_buttons" in body:
            ad.extra_buttons = json.dumps(body["extra_buttons"] or [])
        await session.commit()
        return ad_dict(ad)


@app.delete("/api/ads/{ad_id}")
async def delete_ad(ad_id: int, request: Request, _: None = Depends(require_admin)):
    async with SessionLocal() as session:
        ad = await session.get(Ad, ad_id)
        if ad:
            await session.delete(ad)
            await session.commit()
    return {"ok": True}


@app.get("/api/offers")
async def list_offers(request: Request, _: None = Depends(require_admin)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Offer).order_by(Offer.kind, Offer.sort_order))).scalars().all()
        return [offer_dict(o) for o in rows]


@app.post("/api/offers")
async def create_offer(request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    async with SessionLocal() as session:
        offer = Offer(
            kind=body.get("kind") or "my_op",
            title=body.get("title") or "",
            text=body.get("text") or "",
            parse_mode=body.get("parse_mode") or "HTML",
            media_type=body.get("media_type") or "none",
            media_file_id=body.get("media_file_id") or "",
            media_path=body.get("media_path") or "",
            button_text=body.get("button_text") or "",
            button_url=body.get("button_url") or "",
            check_type=body.get("check_type") or "subscribe",
            chat_id=str(body.get("chat_id") or ""),
            service_name=body.get("service_name") or "",
            is_active=bool(body.get("is_active", True)),
            sort_order=int(body.get("sort_order") or 0),
        )
        session.add(offer)
        await session.commit()
        await session.refresh(offer)
        return offer_dict(offer)


@app.put("/api/offers/{offer_id}")
async def update_offer(offer_id: int, request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    async with SessionLocal() as session:
        offer = await session.get(Offer, offer_id)
        if not offer:
            raise HTTPException(404)
        for field in (
            "kind",
            "title",
            "text",
            "parse_mode",
            "media_type",
            "media_file_id",
            "media_path",
            "button_text",
            "button_url",
            "check_type",
            "chat_id",
            "service_name",
            "is_active",
            "sort_order",
        ):
            if field in body:
                setattr(offer, field, body[field])
        await session.commit()
        return offer_dict(offer)


@app.delete("/api/offers/{offer_id}")
async def delete_offer(offer_id: int, request: Request, _: None = Depends(require_admin)):
    async with SessionLocal() as session:
        offer = await session.get(Offer, offer_id)
        if offer:
            await session.delete(offer)
            await session.commit()
    return {"ok": True}


@app.get("/api/shows")
async def list_shows(request: Request, _: None = Depends(require_admin)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Show).order_by(Show.delay_seconds, Show.id))).scalars().all()
        return [show_dict(s) for s in rows]


@app.post("/api/shows")
async def create_show(request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    async with SessionLocal() as session:
        item = Show(
            title=body.get("title") or "Показ",
            text=body.get("text") or "",
            parse_mode=body.get("parse_mode") or "HTML",
            media_type=body.get("media_type") or "none",
            media_file_id=body.get("media_file_id") or "",
            media_path=body.get("media_path") or "",
            button_type=body.get("button_type") or "none",
            button_text=body.get("button_text") or "",
            button_url=body.get("button_url") or "",
            button_color=body.get("button_color") or "#2AABEE",
            extra_buttons=json.dumps(body.get("extra_buttons") or []),
            delay_seconds=int(body.get("delay_seconds") or 10),
            is_active=bool(body.get("is_active", True)),
            sort_order=int(body.get("sort_order") or 0),
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return show_dict(item)


@app.put("/api/shows/{show_id}")
async def update_show(show_id: int, request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    async with SessionLocal() as session:
        item = await session.get(Show, show_id)
        if not item:
            raise HTTPException(404)
        for field in (
            "title",
            "text",
            "parse_mode",
            "media_type",
            "media_file_id",
            "media_path",
            "button_type",
            "button_text",
            "button_url",
            "button_color",
            "is_active",
            "sort_order",
            "delay_seconds",
        ):
            if field in body:
                setattr(item, field, body[field])
        if "extra_buttons" in body:
            item.extra_buttons = json.dumps(body["extra_buttons"] or [])
        await session.commit()
        return show_dict(item)


@app.delete("/api/shows/{show_id}")
async def delete_show(show_id: int, request: Request, _: None = Depends(require_admin)):
    async with SessionLocal() as session:
        item = await session.get(Show, show_id)
        if item:
            await session.delete(item)
            await session.commit()
    return {"ok": True}


@app.get("/api/campaigns")
async def list_campaigns(request: Request, _: None = Depends(require_admin)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Campaign).order_by(desc(Campaign.id)))).scalars().all()
        cfg = await get_settings_map(session)
        username = cfg.get("bot_username") or ""
        out = []
        for camp in rows:
            item = await campaign_stats(session, camp)
            item["link"] = f"https://t.me/{username}?start=c_{camp.code}" if username else f"?start=c_{camp.code}"
            out.append(item)
        return out


@app.post("/api/campaigns")
async def create_campaign(request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    code = re.sub(r"[^a-zA-Z0-9_]", "", str(body.get("code") or secrets.token_hex(4)))
    if not code:
        code = secrets.token_hex(4)
    async with SessionLocal() as session:
        exists = (
            await session.execute(select(Campaign).where(Campaign.code == code))
        ).scalar_one_or_none()
        if exists:
            raise HTTPException(400, "Код уже занят")
        camp = Campaign(
            code=code,
            name=body.get("name") or code,
            price=Decimal(str(body.get("price") or 0)),
            comment=body.get("comment") or "",
            is_active=True,
        )
        session.add(camp)
        await session.commit()
        await session.refresh(camp)
        item = await campaign_stats(session, camp)
        cfg = await get_settings_map(session)
        username = cfg.get("bot_username") or ""
        item["link"] = f"https://t.me/{username}?start=c_{camp.code}" if username else f"?start=c_{camp.code}"
        return item


@app.put("/api/campaigns/{camp_id}")
async def update_campaign(camp_id: int, request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    async with SessionLocal() as session:
        camp = await session.get(Campaign, camp_id)
        if not camp:
            raise HTTPException(404)
        if "name" in body:
            camp.name = body["name"]
        if "price" in body:
            camp.price = Decimal(str(body["price"] or 0))
        if "comment" in body:
            camp.comment = body["comment"]
        if "is_active" in body:
            camp.is_active = bool(body["is_active"])
        await session.commit()
        return await campaign_stats(session, camp)


@app.delete("/api/campaigns/{camp_id}")
async def delete_campaign(camp_id: int, request: Request, _: None = Depends(require_admin)):
    async with SessionLocal() as session:
        camp = await session.get(Campaign, camp_id)
        if camp:
            await session.delete(camp)
            await session.commit()
    return {"ok": True}


@app.get("/api/users")
async def list_users(request: Request, q: str = "", _: None = Depends(require_admin)):
    async with SessionLocal() as session:
        stmt = select(User).order_by(desc(User.id)).limit(100)
        if q:
            if q.isdigit():
                stmt = select(User).where(User.tg_id == int(q)).limit(100)
            else:
                stmt = (
                    select(User)
                    .where(User.username.ilike(f"%{q.lstrip('@')}%"))
                    .order_by(desc(User.id))
                    .limit(100)
                )
        rows = (await session.execute(stmt)).scalars().all()
        return [user_dict(u) for u in rows]


@app.post("/api/users/{user_id}/block")
async def block_user(user_id: int, request: Request, _: None = Depends(require_admin)):
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404)
        user.blocked = not user.blocked
        await session.commit()
        return user_dict(user)


@app.post("/api/users/{user_id}/stars")
async def change_user_stars(user_id: int, request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    try:
        delta = Decimal(str(body.get("amount") or "0"))
    except Exception:
        raise HTTPException(400, "bad amount")
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404)
        new_bal = Decimal(user.balance) + delta
        if new_bal < 0:
            new_bal = Decimal("0")
        user.balance = new_bal
        if delta > 0:
            user.total_earned = Decimal(user.total_earned) + delta
        await session.commit()
        out = user_dict(user)
    try:
        if delta > 0:
            await bot.send_message(out["tg_id"], f"✨ Тебе начислили {abs(float(delta)):.2f} ⭐")
        elif delta < 0:
            await bot.send_message(out["tg_id"], f"Админ списал {abs(float(delta)):.2f} ⭐")
    except Exception:
        logger.exception("notify stars")
    return out


@app.get("/api/withdrawals")
async def list_withdrawals(request: Request, _: None = Depends(require_admin)):
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(Withdrawal).order_by(desc(Withdrawal.id)).limit(200))
        ).scalars().all()
        out = []
        for w in rows:
            user = await session.get(User, w.user_id)
            out.append(
                {
                    "id": w.id,
                    "amount": dec(w.amount),
                    "status": w.status,
                    "created_at": w.created_at.isoformat() if w.created_at else None,
                    "admin_comment": w.admin_comment,
                    "user": user_dict(user) if user else None,
                }
            )
        return out


@app.post("/api/withdrawals/{wid}/approve")
async def approve_wd(wid: int, request: Request, _: None = Depends(require_admin)):
    async with SessionLocal() as session:
        wd = await session.get(Withdrawal, wid)
        if not wd:
            raise HTTPException(404)
        wd.status = "approved"
        from database.crud import utcnow

        wd.processed_at = utcnow()
        user = await session.get(User, wd.user_id)
        await session.commit()
    if user:
        try:
            await bot.send_message(
                user.tg_id,
                f"Заявка #{wd.id} на {Decimal(wd.amount):.0f} ⭐ одобрена. Звёзды будут отправлены.",
            )
        except Exception:
            logger.exception("notify approve")
    return {"ok": True}


@app.post("/api/withdrawals/{wid}/reject")
async def reject_wd(wid: int, request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    comment = str(body.get("comment") or "")
    async with SessionLocal() as session:
        wd = await session.get(Withdrawal, wid)
        if not wd:
            raise HTTPException(404)
        user = await session.get(User, wd.user_id)
        if user and wd.status == "pending_review":
            user.balance = Decimal(user.balance) + Decimal(wd.amount)
            user.total_withdrawn = max(Decimal("0"), Decimal(user.total_withdrawn) - Decimal(wd.amount))
        wd.status = "rejected"
        wd.admin_comment = comment
        from database.crud import utcnow

        wd.processed_at = utcnow()
        await session.commit()
    if user:
        try:
            extra = f"\n{comment}" if comment else ""
            await bot.send_message(user.tg_id, f"Заявка #{wd.id} отклонена.{extra}")
        except Exception:
            logger.exception("notify reject")
    return {"ok": True}


@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...), _: None = Depends(require_admin)):
    suffix = Path(file.filename or "file.bin").suffix[:8]
    name = secrets.token_hex(8) + suffix
    dest = UPLOADS_DIR / name
    data = await file.read()
    dest.write_bytes(data)
    return {"path": f"data/uploads/{name}", "url": f"/uploads/{name}"}


@app.get("/api/broadcasts")
async def list_broadcasts(request: Request, _: None = Depends(require_admin)):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Broadcast).order_by(desc(Broadcast.id)).limit(50))).scalars().all()
        return [
            {
                "id": b.id,
                "text": b.text,
                "status": b.status,
                "total": b.total,
                "sent": b.sent,
                "failed": b.failed,
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
            for b in rows
        ]


@app.post("/api/broadcast")
async def create_broadcast(request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    async with SessionLocal() as session:
        from sqlalchemy import func

        total = int(
            (await session.execute(select(func.count()).select_from(User).where(User.blocked.is_(False)))).scalar()
            or 0
        )
        item = Broadcast(
            text=body.get("text") or "",
            parse_mode=body.get("parse_mode") or "HTML",
            media_type=body.get("media_type") or "none",
            media_file_id=body.get("media_file_id") or "",
            media_path=body.get("media_path") or "",
            button_text=body.get("button_text") or "",
            button_url=body.get("button_url") or "",
            status="running",
            total=total,
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        bid = item.id
    asyncio.create_task(_run_broadcast(bid))
    return {"id": bid, "status": "running", "total": total}


async def _run_broadcast(bid: int) -> None:
    from bot.keyboards import url_button

    async with SessionLocal() as session:
        item = await session.get(Broadcast, bid)
        if not item:
            return
        users = (
            await session.execute(select(User.tg_id).where(User.blocked.is_(False)))
        ).scalars().all()
        text = item.text
        parse_mode = item.parse_mode
        media_type = item.media_type
        media_file_id = item.media_file_id
        media_path = item.media_path
        markup = None
        if item.button_text and item.button_url:
            markup = url_button(item.button_text, item.button_url)

    sent = failed = 0
    for tg_id in users:
        try:
            await send_content(
                bot,
                tg_id,
                text,
                parse_mode=parse_mode,
                media_type=media_type,
                media_file_id=media_file_id,
                media_path=media_path,
                reply_markup=markup,
            )
            sent += 1
        except Exception:
            failed += 1
        if (sent + failed) % 20 == 0:
            async with SessionLocal() as session:
                row = await session.get(Broadcast, bid)
                if row:
                    row.sent = sent
                    row.failed = failed
                    await session.commit()
            await asyncio.sleep(1)
        else:
            await asyncio.sleep(0.05)

    async with SessionLocal() as session:
        row = await session.get(Broadcast, bid)
        if row:
            row.sent = sent
            row.failed = failed
            row.status = "done"
            await session.commit()


def _userbot_dict(a: UserBotAccount, leads: int = 0) -> dict:
    from bot.services.userbot import is_online

    try:
        gift_media = json.loads(getattr(a, "gift_media", None) or "[]")
        if not isinstance(gift_media, list):
            gift_media = []
    except Exception:
        gift_media = []

    return {
        "id": a.id,
        "phone": a.phone,
        "tg_id": a.tg_id,
        "username": a.username,
        "first_name": a.first_name,
        "status": a.status,
        "is_active": a.is_active,
        "hello_text": a.hello_text,
        "gift_text": a.gift_text,
        "gift_parse_mode": a.gift_parse_mode or "HTML",
        "gift_entities": a.gift_entities or "[]",
        "gift_media": gift_media,
        "followup2_text": getattr(a, "followup2_text", None) or "",
        "followup2_parse_mode": getattr(a, "followup2_parse_mode", None) or "HTML",
        "followup3_text": getattr(a, "followup3_text", None) or "",
        "followup3_parse_mode": getattr(a, "followup3_parse_mode", None) or "HTML",
        "nudge_30_text": a.nudge_30_text or "",
        "nudge_6h_text": a.nudge_6h_text or "",
        "nudge_24h_text": a.nudge_24h_text or "",
        "max_greets_hour": a.max_greets_hour or 1000,
        "last_error": a.last_error,
        "greets_hour": a.greets_hour,
        "leads": leads,
        "has_session": bool(a.session_string),
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
        "online": is_online(a.id),
    }


@app.get("/api/userbots")
async def list_userbots(request: Request, _: None = Depends(require_admin)):
    from sqlalchemy import func

    from bot.utils.envfile import api_configured, userbot_global_settings

    async with SessionLocal() as session:
        rows = (await session.execute(select(UserBotAccount).order_by(UserBotAccount.id))).scalars().all()
        counts = dict(
            (
                await session.execute(
                    select(UserBotLead.account_id, func.count()).group_by(UserBotLead.account_id)
                )
            ).all()
        )
        return {
            "items": [_userbot_dict(a, int(counts.get(a.id, 0))) for a in rows],
            "api_configured": api_configured(),
            "globals": userbot_global_settings(),
        }


@app.get("/api/userbots/globals")
async def get_userbot_globals(request: Request, _: None = Depends(require_admin)):
    from bot.utils.envfile import userbot_global_settings

    return userbot_global_settings()


@app.put("/api/userbots/globals")
async def put_userbot_globals(request: Request, _: None = Depends(require_admin)):
    from bot.utils.envfile import apply_api_credentials, apply_userbot_timing, userbot_global_settings

    body = await request.json()
    if "api_id" in body or "api_hash" in body:
        try:
            apply_api_credentials(int(body.get("api_id") or 0), str(body.get("api_hash") or ""))
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e
    try:
        apply_userbot_timing(
            max_reply_sec=int(body["max_reply_sec"]) if "max_reply_sec" in body else None,
            min_send_gap=float(body["min_send_gap"]) if "min_send_gap" in body else None,
        )
    except (TypeError, ValueError) as e:
        raise HTTPException(400, detail=str(e)) from e
    return userbot_global_settings()


@app.get("/api/userbots/stats")
async def userbot_stats(request: Request, _: None = Depends(require_admin)):
    from bot.services.userbot_stats import PERIODS, collect_all_periods, collect_stats

    period = str(request.query_params.get("period") or "day")
    aid_raw = request.query_params.get("account_id")
    account_id = int(aid_raw) if aid_raw and str(aid_raw).isdigit() else None
    if request.query_params.get("all_periods") in ("1", "true", "yes"):
        return await collect_all_periods(account_id)
    if period not in PERIODS:
        period = "day"
    return await collect_stats(period, account_id)


@app.get("/api/userbots/stats/chart")
async def userbot_stats_chart(request: Request, _: None = Depends(require_admin)):
    from fastapi.responses import FileResponse

    from bot.services.userbot_stats import PERIODS, save_stats_chart

    period = str(request.query_params.get("period") or "day")
    if period not in PERIODS:
        period = "day"
    aid_raw = request.query_params.get("account_id")
    account_id = int(aid_raw) if aid_raw and str(aid_raw).isdigit() else None
    try:
        _data, path = await save_stats_chart(period, account_id)
    except Exception as e:
        raise HTTPException(500, detail=f"chart error: {e}") from e
    return FileResponse(path, media_type="image/png", filename=path.name)


@app.post("/api/userbots")
async def create_userbot(request: Request, _: None = Depends(require_admin)):
    from bot.services.userbot import create_account, get_account, update_texts

    body = await request.json()
    try:
        row = await create_account(str(body.get("phone") or ""))
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    await update_texts(
        row.id,
        hello_text=str(body.get("hello_text") or "") or None,
        gift_text=str(body.get("gift_text") or ""),
        gift_parse_mode=str(body.get("gift_parse_mode") or "HTML"),
    )
    fresh = await get_account(row.id)
    return _userbot_dict(fresh or row)


@app.put("/api/userbots/{aid}")
async def update_userbot(aid: int, request: Request, _: None = Depends(require_admin)):
    from bot.services.userbot import get_account, update_texts

    body = await request.json()
    row = await get_account(aid)
    if not row:
        raise HTTPException(404)
    await update_texts(
        aid,
        hello_text=body.get("hello_text") if "hello_text" in body else None,
        gift_text=body.get("gift_text") if "gift_text" in body else None,
        gift_parse_mode=body.get("gift_parse_mode") if "gift_parse_mode" in body else None,
        gift_media=body.get("gift_media") if "gift_media" in body else None,
        followup2_text=body.get("followup2_text") if "followup2_text" in body else None,
        followup2_parse_mode=body.get("followup2_parse_mode") if "followup2_parse_mode" in body else None,
        followup3_text=body.get("followup3_text") if "followup3_text" in body else None,
        followup3_parse_mode=body.get("followup3_parse_mode") if "followup3_parse_mode" in body else None,
        nudge_30_text=body.get("nudge_30_text") if "nudge_30_text" in body else None,
        nudge_6h_text=body.get("nudge_6h_text") if "nudge_6h_text" in body else None,
        nudge_24h_text=body.get("nudge_24h_text") if "nudge_24h_text" in body else None,
        max_greets_hour=body.get("max_greets_hour") if "max_greets_hour" in body else None,
    )
    row = await get_account(aid)
    return _userbot_dict(row)


@app.post("/api/userbots/{aid}/send-code")
async def userbot_send_code(aid: int, request: Request, _: None = Depends(require_admin)):
    from bot.services.userbot import begin_login

    try:
        return await begin_login(aid)
    except Exception as e:
        raise HTTPException(400, detail=str(e)) from e


@app.post("/api/userbots/{aid}/confirm-code")
async def userbot_confirm_code(aid: int, request: Request, _: None = Depends(require_admin)):
    from bot.services.userbot import submit_code

    body = await request.json()
    code = str(body.get("code") or "")
    if not code.strip():
        raise HTTPException(400, detail="Введи код из Telegram")
    try:
        return await submit_code(aid, code)
    except Exception as e:
        raise HTTPException(400, detail=str(e)) from e


@app.post("/api/userbots/{aid}/confirm-2fa")
async def userbot_confirm_2fa(aid: int, request: Request, _: None = Depends(require_admin)):
    from bot.services.userbot import submit_password

    body = await request.json()
    password = str(body.get("password") or "")
    if not password:
        raise HTTPException(400, detail="Введи облачный пароль Telegram")
    try:
        return await submit_password(aid, password)
    except Exception as e:
        raise HTTPException(400, detail=str(e)) from e


@app.post("/api/userbots/{aid}/start")
async def userbot_start(aid: int, request: Request, _: None = Depends(require_admin)):
    from bot.services.userbot import set_active

    try:
        await set_active(aid, True)
        return {"ok": True, "status": "ready"}
    except Exception as e:
        raise HTTPException(400, detail=str(e)) from e


@app.post("/api/userbots/{aid}/stop")
async def userbot_stop(aid: int, request: Request, _: None = Depends(require_admin)):
    from bot.services.userbot import set_active

    await set_active(aid, False)
    return {"ok": True, "status": "stopped"}


@app.delete("/api/userbots/{aid}")
async def delete_userbot(aid: int, request: Request, _: None = Depends(require_admin)):
    from bot.services.userbot import delete_account, get_account

    if not await get_account(aid):
        raise HTTPException(404)
    await delete_account(aid)
    return {"ok": True}

