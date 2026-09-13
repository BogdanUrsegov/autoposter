"""Статистика юзер-ботов + одна картинка с 3 графиками (bar / pie / line)."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, select

from config import DATA_DIR, settings
from database import SessionLocal
from database.crud import utcnow
from database.models import UserBotAccount, UserBotLead
from bot.services.userbot import is_online

PERIODS = ("hour", "day", "week", "month", "all")
PERIOD_LABELS = {
    "hour": "час",
    "day": "сутки",
    "week": "неделя",
    "month": "месяц",
    "all": "всё время",
}


def _period_start(period: str, now: datetime | None = None) -> datetime | None:
    now = now or utcnow()
    if period == "hour":
        return now - timedelta(hours=1)
    if period == "day":
        return now - timedelta(days=1)
    if period == "week":
        return now - timedelta(days=7)
    if period == "month":
        return now - timedelta(days=30)
    return None


def _pct(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(100.0 * part / total, 1)


def _lead_filter(account_id: int | None, since: datetime | None):
    clauses = []
    if account_id is not None:
        clauses.append(UserBotLead.account_id == account_id)
    if since is not None:
        clauses.append(UserBotLead.created_at >= since)
    return and_(*clauses) if clauses else True


async def collect_stats(period: str = "day", account_id: int | None = None) -> dict[str, Any]:
    if period not in PERIODS:
        period = "day"
    since = _period_start(period)
    filt = _lead_filter(account_id, since)

    async with SessionLocal() as session:
        total = int(
            (await session.execute(select(func.count()).select_from(UserBotLead).where(filt))).scalar() or 0
        )
        blocked = int(
            (
                await session.execute(
                    select(func.count()).select_from(UserBotLead).where(filt, UserBotLead.blocked.is_(True))
                )
            ).scalar()
            or 0
        )
        hello_replied = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(UserBotLead)
                    .where(filt, UserBotLead.hello_replied.is_(True))
                )
            ).scalar()
            or 0
        )
        gift_replied = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(UserBotLead)
                    .where(filt, UserBotLead.gift_replied.is_(True))
                )
            ).scalar()
            or 0
        )
        gift_sent = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(UserBotLead)
                    .where(filt, UserBotLead.gift_sent_at.is_not(None))
                )
            ).scalar()
            or 0
        )
        nudge_30 = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(UserBotLead)
                    .where(filt, UserBotLead.nudge_30_sent.is_(True))
                )
            ).scalar()
            or 0
        )
        nudge_6h = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(UserBotLead)
                    .where(filt, UserBotLead.nudge_6h_sent.is_(True))
                )
            ).scalar()
            or 0
        )
        nudge_24h = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(UserBotLead)
                    .where(filt, UserBotLead.nudge_24h_sent.is_(True))
                )
            ).scalar()
            or 0
        )
        done = int(
            (
                await session.execute(
                    select(func.count()).select_from(UserBotLead).where(filt, UserBotLead.stage == "done")
                )
            ).scalar()
            or 0
        )
        gifted_stage = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(UserBotLead)
                    .where(filt, UserBotLead.stage.in_(("gifted", "done")))
                )
            ).scalar()
            or 0
        )

        # динамика по дням для линейного графика (до 14 точек)
        buckets: list[tuple[str, int]] = []
        if period == "hour":
            # 12 слотов по 5 минут
            for i in range(12):
                end = utcnow() - timedelta(minutes=5 * (11 - i))
                start = end - timedelta(minutes=5)
                c = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(UserBotLead)
                            .where(
                                _lead_filter(account_id, None),
                                UserBotLead.created_at >= start,
                                UserBotLead.created_at < end,
                            )
                        )
                    ).scalar()
                    or 0
                )
                buckets.append((end.strftime("%H:%M"), c))
        else:
            days = {"day": 24, "week": 7, "month": 30, "all": 14}.get(period, 7)
            if period == "day":
                for i in range(12):
                    end = utcnow() - timedelta(hours=2 * (11 - i))
                    start = end - timedelta(hours=2)
                    c = int(
                        (
                            await session.execute(
                                select(func.count())
                                .select_from(UserBotLead)
                                .where(
                                    _lead_filter(account_id, None),
                                    UserBotLead.created_at >= start,
                                    UserBotLead.created_at < end,
                                )
                            )
                        ).scalar()
                        or 0
                    )
                    buckets.append((end.strftime("%H:%M"), c))
            else:
                n = min(days, 14) if period != "month" else 15
                step = 1 if period != "month" else 2
                for i in range(n):
                    end = utcnow().replace(hour=23, minute=59, second=59, microsecond=0) - timedelta(
                        days=step * (n - 1 - i)
                    )
                    start = end.replace(hour=0, minute=0, second=0) - timedelta(days=step - 1)
                    c = int(
                        (
                            await session.execute(
                                select(func.count())
                                .select_from(UserBotLead)
                                .where(
                                    _lead_filter(account_id, None),
                                    UserBotLead.created_at >= start,
                                    UserBotLead.created_at <= end,
                                )
                            )
                        ).scalar()
                        or 0
                    )
                    buckets.append((end.strftime("%d.%m"), c))

        accounts = list((await session.execute(select(UserBotAccount))).scalars().all())
        online = sum(1 for a in accounts if is_online(a.id))
        if account_id is not None:
            accounts = [a for a in accounts if a.id == account_id]
            online = sum(1 for a in accounts if is_online(a.id))

    silent = max(0, total - hello_replied)
    gift_no_reply = max(0, gift_sent - gift_replied)

    return {
        "period": period,
        "period_label": PERIOD_LABELS.get(period, period),
        "account_id": account_id,
        "total": total,
        "blocked": blocked,
        "hello_replied": hello_replied,
        "gift_replied": gift_replied,
        "gift_sent": gift_sent,
        "nudge_30": nudge_30,
        "nudge_6h": nudge_6h,
        "nudge_24h": nudge_24h,
        "done": done,
        "gifted_or_done": gifted_stage,
        "silent_after_hello": silent,
        "gift_no_reply": gift_no_reply,
        "accounts": len(accounts),
        "online": online,
        "pct": {
            "blocked": _pct(blocked, total),
            "hello_replied": _pct(hello_replied, total),
            "gift_replied": _pct(gift_replied, total),
            "gift_sent": _pct(gift_sent, total),
            "nudge_30": _pct(nudge_30, total),
            "nudge_6h": _pct(nudge_6h, total),
            "nudge_24h": _pct(nudge_24h, total),
            "done": _pct(done, total),
            "silent": _pct(silent, total),
            "gift_reply_of_gifted": _pct(gift_replied, gift_sent),
            "hello_of_total": _pct(hello_replied, total),
        },
        "timeline": [{"label": a, "value": b} for a, b in buckets],
        "max_reply_sec": int(getattr(settings, "userbot_max_reply_sec", 60) or 60),
        "min_send_gap": float(getattr(settings, "userbot_min_send_gap", 2.0) or 2.0),
    }


def format_stats_text(data: dict[str, Any]) -> str:
    p = data["pct"]
    label = data["period_label"]
    return (
        f"📊 <b>Юзер-бот · {label}</b>\n\n"
        f"👥 Написали: <b>{data['total']}</b>\n"
        f"🚫 Заблокировали: <b>{data['blocked']}</b> ({p['blocked']}%)\n"
        f"1️⃣ Ответ на привет: <b>{data['hello_replied']}</b> ({p['hello_replied']}%)\n"
        f"2️⃣ Ответ на подарок: <b>{data['gift_replied']}</b> ({p['gift_replied']}%)\n"
        f"🎁 Подарок ушёл: <b>{data['gift_sent']}</b> ({p['gift_sent']}%)\n"
        f"⏰ Напоминание 30м: <b>{data['nudge_30']}</b> ({p['nudge_30']}%)\n"
        f"⏰ Напоминание 6ч: <b>{data['nudge_6h']}</b> ({p['nudge_6h']}%)\n"
        f"⏰ Напоминание 24ч: <b>{data['nudge_24h']}</b> ({p['nudge_24h']}%)\n"
        f"✅ Завершили: <b>{data['done']}</b> ({p['done']}%)\n"
        f"😶 Молчат после привета: <b>{data['silent_after_hello']}</b> ({p['silent']}%)\n"
        f"📈 Ответ на подарок / кому ушёл: <b>{p['gift_reply_of_gifted']}%</b>\n"
        f"🟢 Онлайн аккаунтов: <b>{data['online']}</b> / {data['accounts']}\n"
    )


def render_stats_chart(data: dict[str, Any]) -> bytes:
    """Одна картинка: столбчатая + круговая + линейная."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(14, 4.6), dpi=120)
    fig.patch.set_facecolor("#0f1720")
    for ax in axes:
        ax.set_facecolor("#15202b")
        ax.tick_params(colors="#c9d4e0")
        for spine in ax.spines.values():
            spine.set_color("#2a3a4d")
        ax.title.set_color("#e8eef6")

    # --- bar: ключевые метрики ---
    ax = axes[0]
    labels = ["Написали", "Блок", "Отв.1", "Отв.2", "30м", "6ч", "24ч"]
    values = [
        data["total"],
        data["blocked"],
        data["hello_replied"],
        data["gift_replied"],
        data["nudge_30"],
        data["nudge_6h"],
        data["nudge_24h"],
    ]
    colors = ["#2aabee", "#ff6b6b", "#3dd68c", "#f5c542", "#a78bfa", "#60a5fa", "#f472b6"]
    ax.bar(labels, values, color=colors)
    ax.set_title(f"Столбцы · {data['period_label']}")
    ax.tick_params(axis="x", rotation=25, labelsize=8)

    # --- pie: воронка % ---
    ax = axes[1]
    pie_labels = ["Отв. привет", "Молчат", "Блок", "Прочее"]
    silent = max(0, int(data.get("silent_after_hello") or 0) - int(data.get("blocked") or 0))
    other = max(
        0,
        int(data.get("total") or 0)
        - int(data.get("hello_replied") or 0)
        - silent
        - int(data.get("blocked") or 0),
    )
    pie_vals = [int(data.get("hello_replied") or 0), silent, int(data.get("blocked") or 0), other]
    if sum(pie_vals) <= 0:
        pie_vals = [1]
        pie_labels = ["нет данных"]
    wedges, texts, autotexts = ax.pie(
        pie_vals,
        labels=pie_labels,
        autopct="%1.0f%%",
        colors=["#3dd68c", "#64748b", "#ff6b6b", "#334155"],
        textprops={"color": "#e8eef6", "fontsize": 8},
    )
    for t in autotexts:
        t.set_color("#0b1118")
        t.set_fontsize(8)
    ax.set_title("Круговая · доли")

    # --- line: динамика ---
    ax = axes[2]
    tl = data.get("timeline") or []
    xs = [t["label"] for t in tl]
    ys = [t["value"] for t in tl]
    if not xs:
        xs, ys = ["—"], [0]
    ax.plot(xs, ys, color="#2aabee", linewidth=2, marker="o", markersize=4)
    ax.fill_between(range(len(ys)), ys, color="#2aabee", alpha=0.2)
    ax.set_xticks(range(len(xs)))
    ax.set_xticklabels(xs, rotation=35, ha="right", fontsize=7)
    ax.set_title("Линия · динамика входов")
    ax.set_ylim(bottom=0)

    fig.suptitle("Юзер-бот · статистика", color="#f5c542", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


async def save_stats_chart(period: str = "day", account_id: int | None = None) -> tuple[dict, Path]:
    data = await collect_stats(period, account_id)
    png = render_stats_chart(data)
    out_dir = DATA_DIR / "uploads"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"ub_stats_{period}_{account_id or 'all'}.png"
    path = out_dir / name
    path.write_bytes(png)
    return data, path


async def collect_all_periods(account_id: int | None = None) -> dict[str, Any]:
    periods = {}
    for p in PERIODS:
        periods[p] = await collect_stats(p, account_id)
    return {
        "periods": periods,
        "labels": PERIOD_LABELS,
        "globals": {
            "max_reply_sec": int(getattr(settings, "userbot_max_reply_sec", 60) or 60),
            "min_send_gap": float(getattr(settings, "userbot_min_send_gap", 2.0) or 2.0),
        },
    }
