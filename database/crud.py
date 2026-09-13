from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    Ad,
    Campaign,
    CampaignHit,
    Click,
    Offer,
    OfferCompletion,
    OfferView,
    Payment,
    ReferralEarning,
    Setting,
    Show,
    ShowSend,
    User,
    Withdrawal,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today_str() -> str:
    return utcnow().date().isoformat()


async def get_setting(session: AsyncSession, key: str, default: str = "") -> str:
    row = await session.get(Setting, key)
    return row.value if row else default


async def get_settings_map(session: AsyncSession) -> dict[str, str]:
    rows = (await session.execute(select(Setting))).scalars().all()
    return {row.key: row.value for row in rows}


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))


async def upsert_user(
    session: AsyncSession,
    tg_id: int,
    username: str | None,
    first_name: str,
    last_name: str | None,
    is_premium: bool,
    language_code: str | None,
) -> tuple[User, bool]:
    user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
    is_new = user is None
    if user is None:
        user = User(
            tg_id=tg_id,
            username=username,
            first_name=first_name or "",
            last_name=last_name,
            is_premium=bool(is_premium),
            language_code=language_code,
            hour_remind_sent=False,
        )
        session.add(user)
        await session.flush()
    else:
        user.username = username
        user.first_name = first_name or user.first_name
        user.last_name = last_name
        user.is_premium = bool(is_premium)
        user.language_code = language_code
        user.last_active_at = utcnow()
    return user, is_new


async def get_user_by_tg(session: AsyncSession, tg_id: int) -> User | None:
    return (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()


async def count_referrals(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(User).where(User.referrer_id == user_id)
            )
        ).scalar()
        or 0
    )


async def referrals_earned_total(session: AsyncSession, user_id: int) -> Decimal:
    value = (
        await session.execute(
            select(func.coalesce(func.sum(User.total_earned), 0)).where(User.referrer_id == user_id)
        )
    ).scalar()
    return Decimal(value or 0)


async def ref_income(session: AsyncSession, user_id: int) -> Decimal:
    value = (
        await session.execute(
            select(func.coalesce(func.sum(ReferralEarning.amount), 0)).where(
                ReferralEarning.referrer_id == user_id
            )
        )
    ).scalar()
    return Decimal(value or 0)


async def active_ads(session: AsyncSession) -> list[Ad]:
    return list(
        (
            await session.execute(
                select(Ad).where(Ad.is_active.is_(True)).order_by(Ad.sort_order, Ad.id)
            )
        ).scalars().all()
    )


async def active_offers(session: AsyncSession, kind: str | None = None) -> list[Offer]:
    stmt: Select[tuple[Offer]] = select(Offer).where(Offer.is_active.is_(True))
    if kind:
        stmt = stmt.where(Offer.kind == kind)
    stmt = stmt.order_by(Offer.sort_order, Offer.id)
    return list((await session.execute(stmt)).scalars().all())


async def completed_offer_ids(session: AsyncSession, user_id: int) -> set[int]:
    rows = (
        await session.execute(
            select(OfferCompletion.offer_id).where(OfferCompletion.user_id == user_id)
        )
    ).scalars().all()
    return set(rows)


async def active_shows(session: AsyncSession) -> list[Show]:
    return list(
        (
            await session.execute(
                select(Show).where(Show.is_active.is_(True)).order_by(Show.delay_seconds, Show.sort_order, Show.id)
            )
        ).scalars().all()
    )


async def subscribe_channels(session: AsyncSession) -> list[Offer]:
    return list(
        (
            await session.execute(
                select(Offer).where(
                    Offer.is_active.is_(True),
                    Offer.check_type.in_(("subscribe", "bot")),
                    Offer.chat_id != "",
                )
            )
        ).scalars().all()
    )


def period_start(period: str) -> datetime | None:
    msk = timezone(timedelta(hours=3))
    now = datetime.now(msk)
    today = datetime(now.year, now.month, now.day, tzinfo=msk)
    if period == "day":
        return today.astimezone(timezone.utc)
    if period == "week":
        return (today - timedelta(days=now.weekday())).astimezone(timezone.utc)
    return None


async def stats_bundle(session: AsyncSession, period: str) -> dict:
    start = period_start(period)

    def since(column):
        if start is None:
            return True
        return column >= start

    users_q = select(func.count()).select_from(User)
    new_users_q = users_q
    if start:
        new_users_q = users_q.where(User.created_at >= start)

    premium_q = select(func.count()).select_from(User).where(User.is_premium.is_(True))
    if start:
        premium_q = premium_q.where(User.created_at >= start)

    clicks_q = select(func.count()).select_from(Click)
    if start:
        clicks_q = clicks_q.where(Click.created_at >= start)

    click_sum_q = select(func.coalesce(func.sum(Click.reward), 0))
    if start:
        click_sum_q = click_sum_q.where(Click.created_at >= start)

    withdrawn_q = select(func.coalesce(func.sum(Withdrawal.amount), 0)).where(
        Withdrawal.status == "approved"
    )
    if start:
        withdrawn_q = withdrawn_q.where(Withdrawal.processed_at >= start)

    withdraw_count_q = select(func.count()).select_from(Withdrawal).where(
        Withdrawal.status == "approved"
    )
    pending_wd_q = select(func.count()).select_from(Withdrawal).where(
        Withdrawal.status == "pending_review"
    )
    payments_q = select(func.coalesce(func.sum(Payment.amount), 0))
    payments_count_q = select(func.count()).select_from(Payment)
    if start:
        payments_q = payments_q.where(Payment.created_at >= start)
        payments_count_q = payments_count_q.where(Payment.created_at >= start)

    active_q = select(func.count()).select_from(User)
    if start:
        active_q = active_q.where(User.last_active_at >= start)

    tasks_q = select(func.count()).select_from(OfferCompletion)
    views_q = select(func.count()).select_from(OfferView)
    if start:
        tasks_q = tasks_q.where(OfferCompletion.created_at >= start)
        views_q = views_q.where(OfferView.created_at >= start)

    ref_q = select(func.count()).select_from(User).where(User.referrer_id.is_not(None))
    if start:
        ref_q = ref_q.where(User.created_at >= start)

    total_users = int((await session.execute(users_q)).scalar() or 0)
    new_users = int((await session.execute(new_users_q)).scalar() or 0)
    premium = int((await session.execute(premium_q)).scalar() or 0)
    clicks = int((await session.execute(clicks_q)).scalar() or 0)
    stars_accrued = Decimal((await session.execute(click_sum_q)).scalar() or 0)
    withdrawn = Decimal((await session.execute(withdrawn_q)).scalar() or 0)
    withdraw_count = int((await session.execute(withdraw_count_q)).scalar() or 0)
    pending_wd = int((await session.execute(pending_wd_q)).scalar() or 0)
    stars_in = int((await session.execute(payments_q)).scalar() or 0)
    pay_count = int((await session.execute(payments_count_q)).scalar() or 0)
    active = int((await session.execute(active_q)).scalar() or 0)
    tasks = int((await session.execute(tasks_q)).scalar() or 0)
    views = int((await session.execute(views_q)).scalar() or 0)
    refs = int((await session.execute(ref_q)).scalar() or 0)
    shows_q = select(func.count()).select_from(ShowSend)
    if start:
        shows_q = shows_q.where(ShowSend.created_at >= start)
    shows_sent = int((await session.execute(shows_q)).scalar() or 0)

    users_clicked_q = select(func.count(func.distinct(Click.user_id)))
    if start:
        users_clicked_q = users_clicked_q.where(Click.created_at >= start)
    users_clicked = int((await session.execute(users_clicked_q)).scalar() or 0)

    users_withdraw_q = select(func.count(func.distinct(Withdrawal.user_id))).where(
        Withdrawal.status.in_(("pending_review", "approved"))
    )
    if start:
        users_withdraw_q = users_withdraw_q.where(Withdrawal.created_at >= start)
    users_withdraw = int((await session.execute(users_withdraw_q)).scalar() or 0)

    denom = new_users if start else total_users
    denom = denom or 1

    avg_clicks = round(clicks / denom, 2) if denom else 0
    avg_balance = (
        await session.execute(select(func.coalesce(func.avg(User.balance), 0)))
    ).scalar() or 0

    funnel_start = new_users if start else total_users
    return {
        "total_users": total_users,
        "new_users": new_users,
        "premium": premium,
        "premium_percent": pct(premium, funnel_start),
        "clicks": clicks,
        "stars_accrued": float(stars_accrued),
        "stars_withdrawn": float(withdrawn),
        "withdraw_count": withdraw_count,
        "pending_withdrawals": pending_wd,
        "stars_income": stars_in,
        "payments_count": pay_count,
        "active_users": active,
        "tasks_done": tasks,
        "offer_views": views,
        "shows_sent": shows_sent,
        "referrals": refs,
        "users_clicked": users_clicked,
        "users_withdraw": users_withdraw,
        "avg_clicks_per_user": avg_clicks,
        "avg_balance": float(Decimal(avg_balance)),
        "conversion_click": pct(users_clicked, funnel_start),
        "conversion_task": pct(tasks, clicks if clicks else 1),
        "conversion_withdraw": pct(users_withdraw, funnel_start),
        "conversion_pay": pct(pay_count, funnel_start),
        "funnel": {
            "starts": funnel_start,
            "clicked": users_clicked,
            "tasks": tasks,
            "withdraw_started": users_withdraw,
            "paid_check": pay_count,
            "withdraw_done": withdraw_count,
        },
    }


def pct(part: int | float, whole: int | float) -> float:
    if not whole:
        return 0.0
    return round(float(part) * 100.0 / float(whole), 2)


async def campaign_stats(session: AsyncSession, campaign: Campaign) -> dict:
    hits = list(
        (
            await session.execute(
                select(CampaignHit).where(CampaignHit.campaign_id == campaign.id)
            )
        ).scalars().all()
    )
    transitions = len(hits)
    uniques = sum(1 for h in hits if h.is_unique)
    premiums = sum(1 for h in hits if h.is_unique and h.is_premium)
    unique_ids = {h.user_id for h in hits if h.is_unique}
    paid = 0
    paid_stars = 0
    earned = Decimal("0")
    if unique_ids:
        paid = int(
            (
                await session.execute(
                    select(func.count(func.distinct(Payment.user_id))).where(
                        Payment.user_id.in_(unique_ids)
                    )
                )
            ).scalar()
            or 0
        )
        paid_stars = int(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Payment.amount), 0)).where(
                        Payment.user_id.in_(unique_ids)
                    )
                )
            ).scalar()
            or 0
        )
        earned = Decimal(
            (
                await session.execute(
                    select(func.coalesce(func.sum(User.total_earned), 0)).where(User.id.in_(unique_ids))
                )
            ).scalar()
            or 0
        )
    price = Decimal(campaign.price or 0)
    return {
        "id": campaign.id,
        "code": campaign.code,
        "name": campaign.name,
        "price": float(price),
        "comment": campaign.comment,
        "is_active": campaign.is_active,
        "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
        "transitions": transitions,
        "uniques": uniques,
        "premiums": premiums,
        "payers": paid,
        "paid_stars": paid_stars,
        "earned_stars": float(earned),
        "conv_unique": pct(uniques, transitions),
        "conv_premium": pct(premiums, uniques),
        "conv_pay": pct(paid, uniques),
        "cpt": float(price / transitions) if transitions else 0,
        "cpu": float(price / uniques) if uniques else 0,
        "cpp": float(price / premiums) if premiums else 0,
        "cp_payer": float(price / paid) if paid else 0,
    }


async def apply_referral_percent(
    session: AsyncSession, user: User, earned: Decimal, percent: Decimal
) -> None:
    if not user.referrer_id or percent <= 0 or earned <= 0:
        return
    bonus = (earned * percent / Decimal("100")).quantize(Decimal("0.01"))
    if bonus <= 0:
        return
    referrer = await session.get(User, user.referrer_id)
    if not referrer:
        return
    referrer.balance = Decimal(referrer.balance) + bonus
    referrer.total_earned = Decimal(referrer.total_earned) + bonus
    session.add(
        ReferralEarning(
            referrer_id=referrer.id,
            referral_id=user.id,
            amount=bonus,
            kind="percent",
        )
    )


async def apply_referral_milestone(
    session: AsyncSession, user: User, threshold: Decimal, bonus: Decimal
) -> None:
    if user.ref_milestone_paid or not user.referrer_id:
        return
    if Decimal(user.total_earned) < threshold:
        return
    referrer = await session.get(User, user.referrer_id)
    if not referrer:
        return
    user.ref_milestone_paid = True
    referrer.balance = Decimal(referrer.balance) + bonus
    referrer.total_earned = Decimal(referrer.total_earned) + bonus
    session.add(
        ReferralEarning(
            referrer_id=referrer.id,
            referral_id=user.id,
            amount=bonus,
            kind="milestone",
        )
    )
