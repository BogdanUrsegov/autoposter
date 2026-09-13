from __future__ import annotations

import json
import secrets
from datetime import timedelta
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import desc, select

from bot.keyboards import (
    admin_root_kb,
    contest_end_type_kb,
    contest_own_pick_kb,
    contest_sponsors_kb,
    dump_inline_markup,
    load_inline_markup,
)
from bot.loader import bot
from bot.services.content import is_forwarded_message, iter_copy_sources, message_copy_source, send_content
from bot.services.emoji import (
    button_icon_and_text,
    dumps_map,
    extract_first_custom_id,
    extract_from_message,
    get_map,
    loads_map,
    set_map,
    used_emojis,
)
from bot.states import AdminSG
from bot import texts as t
from config import settings
from database import SessionLocal
from database.crud import (
    count_referrals,
    get_setting,
    get_settings_map,
    get_user_by_tg,
    ref_income,
    referrals_earned_total,
    set_setting,
    stats_bundle,
)
from database.models import Ad, Broadcast, Campaign, ChannelGreeting, ChannelNamer, Contest, GreetingPost, Offer, SavedChannel, SavedResource, Show, SpamJob, User, Withdrawal

router = Router()


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return bool(user and settings.is_admin(user.id))


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

SKIP = {
    t.BTN_CLICKER,
    t.BTN_CABINET,
    t.BTN_REF,
    t.BTN_WITHDRAW,
    t.BTN_CLICK,
    t.BTN_BACK,
    t.BTN_REFRESH,
    t.BTN_SHARE,
    t.BTN_ADMIN,
}

SETTING_KEYS = [
    ("click_reward", "Награда за клик"),
    ("click_cooldown", "Кулдаун, сек"),
    ("click_daily_limit", "Лимит кликов (0=нет)"),
    ("task_every_n", "Задание каждые N кликов"),
    ("withdraw_min", "Минимум вывода"),
    ("withdraw_friends", "Друзей для вывода"),
    ("withdraw_check_stars", "Инвойс проверки, ⭐"),
    ("ref_bonus", "Бонус за друга"),
    ("ref_threshold", "Порог друга"),
    ("ref_percent", "% с друга"),
    ("welcome_text", "Текст меню"),
]


def back_kb(*rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    extra = list(rows)
    extra.append([InlineKeyboardButton(text="⬅️ Админка", callback_data="adm:root")])
    return InlineKeyboardMarkup(inline_keyboard=extra)


async def open_admin(target: Message | CallbackQuery) -> None:
    text = (
        "🛠 <b>Админка</b>\n\n"
        "Разделы кнопками ниже. Команда: /admin\n"
        f"Веб: <code>http://127.0.0.1:{settings.web_port}</code>"
    )
    kb = admin_root_kb()
    if isinstance(target, CallbackQuery) and target.message:
        try:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await target.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await target.answer()
        return
    if isinstance(target, Message):
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("admin"))
@router.message(F.text == t.BTN_ADMIN)
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await open_admin(message)


@router.callback_query(F.data == "go:admin")
async def go_admin(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await open_admin(callback)


@router.callback_query(F.data == "adm:root")
async def adm_root(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await open_admin(callback)


@router.callback_query(F.data.startswith("adm:stats:"))
async def adm_stats(callback: CallbackQuery) -> None:
    period = callback.data.split(":")[-1]
    async with SessionLocal() as session:
        s = await stats_bundle(session, period)
    label = {"day": "день", "week": "неделя", "all": "всё время"}[period]
    text = (
        f"📊 <b>Статистика · {label}</b>\n\n"
        f"Люди: <b>{s['total_users']}</b> · новые {s['new_users']}\n"
        f"Активные: <b>{s['active_users']}</b>\n"
        f"Премиум: <b>{s['premium']}</b> ({s['premium_percent']}%)\n"
        f"Клики: <b>{s['clicks']}</b> · начислено {s['stars_accrued']:.2f} ⭐\n"
        f"Выведено: <b>{s['stars_withdrawn']:.0f}</b> ⭐ ({s['withdraw_count']} заявок)\n"
        f"В обработке: {s['pending_withdrawals']}\n"
        f"Входящие ⭐: <b>{s['stars_income']}</b>\n"
        f"Рефералы: {s['referrals']} · задания {s['tasks_done']}\n"
        f"Показы: {s.get('shows_sent', 0)}\n"
        f"Конверсия в кликер: {s['conversion_click']}%\n"
        f"Конверсия в вывод: {s['conversion_withdraw']}%"
    )
    kb = back_kb(
        [
            InlineKeyboardButton(text="День", callback_data="adm:stats:day"),
            InlineKeyboardButton(text="Неделя", callback_data="adm:stats:week"),
            InlineKeyboardButton(text="Всё", callback_data="adm:stats:all"),
        ]
    )
    if callback.message:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "adm:settings")
async def adm_settings(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        cfg = await get_settings_map(session)
    rows = []
    for key, title in SETTING_KEYS:
        rows.append(
            [InlineKeyboardButton(text=f"{title}: {cfg.get(key, '')}", callback_data=f"adm:set:{key}")]
        )
    if callback.message:
        await callback.message.edit_text(
            "⚙️ Нажми параметр, чтобы изменить.",
            reply_markup=back_kb(*rows),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:set:"))
async def adm_set(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 2)[-1]
    await state.set_state(AdminSG.set_value)
    await state.update_data(set_key=key)
    await callback.answer()
    if callback.message:
        await callback.message.answer(f"Отправь новое значение для <code>{key}</code>", parse_mode="HTML")


@router.message(AdminSG.set_value, ~F.text.in_(SKIP))
async def adm_set_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    key = data.get("set_key")
    await state.clear()
    if not key or not message.text:
        return
    async with SessionLocal() as session:
        await set_setting(session, key, message.text.strip())
        await session.commit()
    await message.answer("Сохранено.", reply_markup=admin_root_kb())


@router.callback_query(F.data == "adm:svc")
async def adm_svc(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        cfg = await get_settings_map(session)
    rows = []
    for name, title in (("subgram", "SubGram"), ("tgrass", "Tgrass"), ("botohub", "BotoHub")):
        on = cfg.get(f"{name}_enabled", "1") == "1"
        rows.append(
            [InlineKeyboardButton(text=f"{title}: {'вкл' if on else 'выкл'}", callback_data=f"adm:svc:tog:{name}")]
        )
    if callback.message:
        await callback.message.edit_text(
            "🔌 Сервисы ОП. Если спонсоров нет — на кликере крутятся показы по кругу, пока спонсоры снова не появятся.",
            reply_markup=back_kb(*rows),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:svc:tog:"))
async def adm_svc_tog(callback: CallbackQuery) -> None:
    name = callback.data.split(":")[-1]
    async with SessionLocal() as session:
        cfg = await get_settings_map(session)
        cur = cfg.get(f"{name}_enabled", "1")
        await set_setting(session, f"{name}_enabled", "0" if cur == "1" else "1")
        await session.commit()
    await adm_svc(callback)


async def _load_emoji_map() -> dict[str, str]:
    async with SessionLocal() as session:
        mapping = loads_map(await get_setting(session, "premium_emojis", "{}"))
    set_map(mapping)
    return mapping


def _emoji_list_kb(mapping: dict[str, str]) -> tuple[str, InlineKeyboardMarkup]:
    catalog = used_emojis()
    done = sum(1 for e in catalog if e in mapping)
    text = (
        "🎨 <b>Премиум-эмодзи</b>\n\n"
        "Нажми обычный эмодзи — потом пришли премиум, на который его заменить.\n\n"
        f"Заменено: <b>{done}/{len(catalog)}</b>\n"
        "✓ на кнопке = уже стоит премиум"
    )
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, emo in enumerate(catalog):
        mark = "✓" if emo in mapping else ""
        row.append(InlineKeyboardButton(text=f"{emo}{mark}", callback_data=f"adm:em:s:{i}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🗑 Сбросить все", callback_data="adm:emoji:reset")])
    return text, back_kb(*rows)


@router.callback_query(F.data == "adm:emoji")
async def adm_emoji(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    mapping = await _load_emoji_map()
    text, kb = _emoji_list_kb(mapping)
    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:em:s:"))
async def adm_emoji_pick(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        idx = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    catalog = used_emojis()
    if idx < 0 or idx >= len(catalog):
        await callback.answer("Нет такого")
        return
    emo = catalog[idx]
    await state.set_state(AdminSG.wait_emojis)
    await state.update_data(emoji_target=emo, emoji_idx=idx)
    mapped = get_map().get(emo)
    extra = "\nСейчас уже стоит премиум — пришли новый, чтобы поменять." if mapped else ""
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"Пришли премиум-эмодзи для замены {emo}{extra}\n\n"
            "Или отправь «-» чтобы убрать замену.",
            reply_markup=back_kb(
                [InlineKeyboardButton(text="🗑 Убрать замену", callback_data=f"adm:em:x:{idx}")],
                [InlineKeyboardButton(text="⬅️ К списку", callback_data="adm:emoji")],
            ),
        )


@router.callback_query(F.data.startswith("adm:em:x:"))
async def adm_emoji_clear_one(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        idx = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    catalog = used_emojis()
    if idx < 0 or idx >= len(catalog):
        await callback.answer()
        return
    emo = catalog[idx]
    async with SessionLocal() as session:
        current = loads_map(await get_setting(session, "premium_emojis", "{}"))
        current.pop(emo, None)
        await set_setting(session, "premium_emojis", dumps_map(current))
        await session.commit()
    set_map(current)
    await state.clear()
    await callback.answer("Убрал")
    await adm_emoji(callback, state)


@router.callback_query(F.data == "adm:emoji:reset")
async def adm_emoji_reset(callback: CallbackQuery, state: FSMContext) -> None:
    async with SessionLocal() as session:
        await set_setting(session, "premium_emojis", "{}")
        await session.commit()
    set_map({})
    await state.clear()
    await callback.answer("Сброшено")
    await adm_emoji(callback, state)


@router.message(AdminSG.wait_emojis)
async def adm_wait_emojis(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    target = data.get("emoji_target")
    if not target:
        await state.clear()
        await message.answer("Сначала выбери эмодзи в списке.")
        return
    raw = (message.text or "").strip()
    if raw in ("-", "нет", "убрать"):
        async with SessionLocal() as session:
            current = loads_map(await get_setting(session, "premium_emojis", "{}"))
            current.pop(target, None)
            await set_setting(session, "premium_emojis", dumps_map(current))
            await session.commit()
        set_map(current)
        await state.clear()
        text, kb = _emoji_list_kb(current)
        await message.answer(f"Убрал замену для {target}")
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return
    eid = extract_first_custom_id(message)
    if not eid:
        await message.answer("Это не премиум-эмодзи. Пришли премиум, или «-» чтобы отменить замену.")
        return
    async with SessionLocal() as session:
        current = loads_map(await get_setting(session, "premium_emojis", "{}"))
        current[target] = eid
        await set_setting(session, "premium_emojis", dumps_map(current))
        await session.commit()
    set_map(current)
    await state.clear()
    text, kb = _emoji_list_kb(current)
    await message.answer(f"{target} заменён на премиум. Выбери следующий:")
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


def _list_kb(prefix: str, items: list, title_fn) -> InlineKeyboardMarkup:
    rows = []
    for item in items[:15]:
        mark = "●" if item.is_active else "○"
        rows.append(
            [
                InlineKeyboardButton(text=f"{mark} {title_fn(item)}", callback_data=f"{prefix}:tog:{item.id}"),
                InlineKeyboardButton(text="🗑", callback_data=f"{prefix}:del:{item.id}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data=f"{prefix}:add")])
    rows.append([InlineKeyboardButton(text="⬅️ Админка", callback_data="adm:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "adm:ads")
async def adm_ads(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        items = (await session.execute(select(Ad).order_by(Ad.id.desc()))).scalars().all()
    if callback.message:
        await callback.message.edit_text(
            "📢 Реклама после /start. Добавить — пришли пост (текст/фото/форвард).",
            reply_markup=_list_kb("adm:ad", items, lambda a: a.title or f"#{a.id}"),
        )
    await callback.answer()


@router.callback_query(F.data == "adm:shows")
async def adm_shows(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        items = (await session.execute(select(Show).order_by(Show.delay_seconds, Show.id))).scalars().all()
    if callback.message:
        await callback.message.edit_text(
            "👁 Показы — пост через N секунд после старта. Можно несколько.",
            reply_markup=_list_kb(
                "adm:show",
                items,
                lambda s: f"{s.delay_seconds}с · {s.title or s.id}",
            ),
        )
    await callback.answer()


@router.callback_query(F.data == "adm:offers")
async def adm_offers(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        items = (await session.execute(select(Offer).order_by(Offer.kind, Offer.id))).scalars().all()
    if callback.message:
        await callback.message.edit_text(
            "📋 Задания кликера. Подписка обязательна, иначе тапать дальше нельзя. Показы — отдельно, без проверки.",
            reply_markup=_list_kb("adm:off", items, lambda o: f"{o.kind} · {o.title or o.id}"),
        )
    await callback.answer()


@router.callback_query(F.data == "adm:ad:add")
async def adm_ad_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSG.wait_post)
    await state.update_data(post_kind="ad")
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🛠 <b>Конструктор · шаг 1/3</b>\n\n"
            "Пришли <b>текст</b> поста. Можно с фото или видео.",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "adm:show:add")
async def adm_show_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSG.wait_post)
    await state.update_data(post_kind="show")
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🛠 <b>Конструктор показа · шаг 1</b>\n\n"
            "Пришли <b>текст</b> поста. Можно с фото или видео.",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "adm:off:add")
async def adm_off_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSG.wait_post)
    await state.update_data(post_kind="offer")
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🛠 <b>Конструктор задания · шаг 1</b>\n\n"
            "Пришли <b>текст</b> поста. Можно с фото или видео.",
            parse_mode="HTML",
        )


def _post_snapshot(message: Message) -> dict:
    media_type = "none"
    file_id = ""
    text = message.html_text or message.caption or message.text or ""
    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
        text = message.html_text or message.caption or ""
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
        text = message.html_text or message.caption or ""
    elif message.animation:
        media_type = "animation"
        file_id = message.animation.file_id
        text = message.html_text or message.caption or ""
    extra_rows = dump_inline_markup(message)
    first = extra_rows[0][0] if extra_rows and extra_rows[0] else {}
    copy_chat_id, copy_message_id = message_copy_source(message)
    return {
        "text": text,
        "parse_mode": "HTML",
        "media_type": media_type,
        "media_file_id": file_id,
        "copy_chat_id": copy_chat_id,
        "copy_message_id": copy_message_id,
        "button_text": first.get("text") or "",
        "button_url": first.get("url") or "",
        "button_type": "url" if first.get("url") else "none",
        "extra_buttons": json.dumps(extra_rows, ensure_ascii=False),
        "title": (message.text or message.caption or "Пост")[:80],
    }


def _normalize_btn_url(url: str) -> str:
    raw = (url or "").strip().strip("<>")
    if not raw:
        return ""
    if raw.startswith("@"):
        return f"https://t.me/{raw[1:]}"
    if raw.startswith("t.me/"):
        return "https://" + raw
    if raw.startswith(("http://", "https://")):
        return raw
    return ""


def _parse_constructor_buttons(message: Message) -> list[dict]:
    raw = message.text or message.caption or ""
    custom = extract_from_message(message)
    buttons: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        label = ""
        url = ""
        if "|" in line:
            left, right = line.rsplit("|", 1)
            label, url = left.strip(), right.strip()
        elif " - " in line:
            left, right = line.rsplit(" - ", 1)
            label, url = left.strip(), right.strip()
        else:
            parts = line.rsplit(None, 1)
            if len(parts) == 2 and (parts[1].startswith("http") or parts[1].startswith("t.me/") or parts[1].startswith("@")):
                label, url = parts[0], parts[1]
        url = _normalize_btn_url(url)
        if not label or not url:
            continue
        item: dict = {"text": label, "url": url}
        icon, rest = button_icon_and_text(label)
        if icon:
            item["icon_custom_emoji_id"] = icon
            item["text"] = rest or label
        else:
            for uni, eid in sorted(custom.items(), key=lambda kv: len(kv[0]), reverse=True):
                if label == uni or label.startswith(uni):
                    item["icon_custom_emoji_id"] = eid
                    item["text"] = label[len(uni) :].lstrip() or label
                    break
        buttons.append(item)
    return buttons


def _buttons_to_rows(buttons: list[dict]) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for btn in buttons:
        if rows and len(rows[-1]) == 1:
            rows[-1].append(btn)
        else:
            rows.append([btn])
    return rows


def _ctor_snap(data: dict) -> dict:
    buttons = data.get("ctor_buttons") or []
    extra = json.dumps(_buttons_to_rows(buttons), ensure_ascii=False)
    first = buttons[0] if buttons else {}
    title = data.get("title") or "Пост"
    return {
        "text": data.get("text") or "",
        "parse_mode": "HTML",
        "media_type": data.get("media_type") or "none",
        "media_file_id": data.get("media_file_id") or "",
        "button_text": first.get("text") or "",
        "button_url": first.get("url") or "",
        "button_type": "url" if first.get("url") else "none",
        "extra_buttons": extra,
        "title": title[:80],
        "copy_chat_id": None,
        "copy_message_id": None,
    }


def _color_pick_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔴 Красная", callback_data="adm:bc:danger", style="danger"),
                InlineKeyboardButton(text="🔵 Синяя", callback_data="adm:bc:primary", style="primary"),
                InlineKeyboardButton(text="🟢 Зелёная", callback_data="adm:bc:success", style="success"),
            ]
        ]
    )


async def _ask_button_color(target: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    buttons = data.get("ctor_buttons") or []
    idx = int(data.get("ctor_color_i") or 0)
    if idx >= len(buttons):
        await _finish_constructor(target, state)
        return
    await state.set_state(AdminSG.wait_btn_color)
    btn = buttons[idx]
    text = (
        f"🎨 <b>Шаг 3/3 · цвет кнопки {idx + 1}/{len(buttons)}</b>\n\n"
        f"<b>{btn.get('text') or 'кнопка'}</b>"
    )
    kb = _color_pick_kb()
    if isinstance(target, CallbackQuery) and target.message:
        try:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await target.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await target.answer()
        return
    if isinstance(target, Message):
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


async def _finish_constructor(target: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    kind = data.get("post_kind")
    snap = _ctor_snap(data)
    chat_id = target.message.chat.id if isinstance(target, CallbackQuery) and target.message else target.chat.id  # type: ignore[union-attr]
    markup = load_inline_markup(snap.get("extra_buttons") or "[]")
    await send_content(
        bot,
        chat_id,
        snap.get("text") or "",
        parse_mode="HTML",
        media_type=snap.get("media_type") or "none",
        media_file_id=snap.get("media_file_id") or "",
        reply_markup=markup,
        replace_markup=bool(markup),
    )
    if kind == "ad":
        async with SessionLocal() as session:
            session.add(Ad(**{k: snap[k] for k in snap if k in Ad.__table__.c}))
            await session.commit()
        await state.clear()
        text = "Реклама сохранена. Выше — как увидит человек."
        if isinstance(target, CallbackQuery) and target.message:
            await target.message.answer(text, reply_markup=admin_root_kb())
            await target.answer()
        elif isinstance(target, Message):
            await target.answer(text, reply_markup=admin_root_kb())
        return
    if kind == "show":
        await state.set_state(AdminSG.wait_delay)
        await state.update_data(show_snap=snap)
        ask = "Через сколько секунд после /start отправить? Напиши число, например 10."
        if isinstance(target, CallbackQuery) and target.message:
            await target.message.answer(ask)
            await target.answer()
        elif isinstance(target, Message):
            await target.answer(ask)
        return
    if kind == "offer":
        await state.set_state(AdminSG.wait_offer_chat)
        await state.update_data(offer_snap=snap)
        ask = (
            "Теперь пришли @канал или @бота для проверки подписки.\n"
            "Бот должен быть админом канала, иначе проверка не сработает."
        )
        if isinstance(target, CallbackQuery) and target.message:
            await target.message.answer(ask)
            await target.answer()
        elif isinstance(target, Message):
            await target.answer(ask)
        return
    await state.clear()


@router.message(AdminSG.wait_post, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_wait_post(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    kind = data.get("post_kind")
    if kind not in ("ad", "show", "offer"):
        snap = _post_snapshot(message)
        await state.clear()
        await message.answer("Не понял, что сохранять.", reply_markup=admin_root_kb())
        return
    media_type = "none"
    file_id = ""
    text = message.html_text or message.caption or message.text or ""
    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
        text = message.html_text or message.caption or ""
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
        text = message.html_text or message.caption or ""
    elif message.animation:
        media_type = "animation"
        file_id = message.animation.file_id
        text = message.html_text or message.caption or ""
    if not (text or "").strip() and media_type == "none":
        await message.answer("Нужен текст или фото. Пришли ещё раз.")
        return
    title = (message.text or message.caption or "Пост")[:80]
    await state.update_data(
        text=text,
        media_type=media_type,
        media_file_id=file_id,
        title=title,
        ctor_buttons=[],
        ctor_color_i=0,
    )
    await state.set_state(AdminSG.wait_buttons)
    await message.answer(
        "🔘 <b>Шаг 2/3 · кнопки</b>\n\n"
        "По одной на строку:\n"
        "<code>эмодзи Текст | ссылка</code>\n\n"
        "Пример:\n"
        "<code>🧸 Забрать | https://t.me/gift1</code>\n"
        "<code>💞 Забрать | https://t.me/gift2</code>\n"
        "<code>🌵 Продать ( 199 ⭐ ) | https://t.me/sell</code>\n\n"
        "Можно премиум-эмодзи. Без кнопок — отправь «-».\n"
        "Две кнопки подряд встанут в один ряд, третья — снизу на всю ширину.",
        parse_mode="HTML",
    )


@router.message(AdminSG.wait_buttons, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_wait_buttons(message: Message, state: FSMContext) -> None:
    raw = (message.text or message.caption or "").strip()
    if raw in ("-", "нет", "0", "без кнопок"):
        await state.update_data(ctor_buttons=[], ctor_color_i=0)
        await _finish_constructor(message, state)
        return
    buttons = _parse_constructor_buttons(message)
    if not buttons:
        await message.answer(
            "Не разобрал кнопки. Строка: <code>Текст | https://t.me/...</code>",
            parse_mode="HTML",
        )
        return
    await state.update_data(ctor_buttons=buttons, ctor_color_i=0)
    await _ask_button_color(message, state)


@router.callback_query(AdminSG.wait_btn_color, F.data.startswith("adm:bc:"))
async def adm_btn_color(callback: CallbackQuery, state: FSMContext) -> None:
    style = callback.data.split(":")[-1]
    if style not in ("danger", "primary", "success"):
        await callback.answer()
        return
    data = await state.get_data()
    buttons = list(data.get("ctor_buttons") or [])
    idx = int(data.get("ctor_color_i") or 0)
    if 0 <= idx < len(buttons):
        buttons[idx]["style"] = style
    await state.update_data(ctor_buttons=buttons, ctor_color_i=idx + 1)
    await _ask_button_color(callback, state)


@router.message(AdminSG.wait_delay, F.text, ~F.text.in_(SKIP))
async def adm_wait_delay(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужно число секунд, например 15.")
        return
    data = await state.get_data()
    snap = data.get("show_snap") or {}
    async with SessionLocal() as session:
        show = Show(
            title=snap.get("title") or "Показ",
            text=snap.get("text") or "",
            parse_mode="HTML",
            media_type=snap.get("media_type") or "none",
            media_file_id=snap.get("media_file_id") or "",
            button_type=snap.get("button_type") or "none",
            button_text=snap.get("button_text") or "",
            button_url=snap.get("button_url") or "",
            extra_buttons=snap.get("extra_buttons") or "[]",
            copy_chat_id=snap.get("copy_chat_id"),
            copy_message_id=snap.get("copy_message_id"),
            delay_seconds=int(raw),
            is_active=True,
        )
        session.add(show)
        await session.commit()
    await state.clear()
    await message.answer(f"Показ сохранён, задержка {raw} сек.", reply_markup=admin_root_kb())


@router.message(AdminSG.wait_offer_chat, F.text, ~F.text.in_(SKIP))
async def adm_wait_offer_chat(message: Message, state: FSMContext) -> None:
    chat = (message.text or "").strip()
    if chat in ("-", "нет", "привет"):
        await message.answer("Нужен @канал или @бот для проверки. Показы без проверки — в разделе «Показы».")
        return
    data = await state.get_data()
    snap = data.get("offer_snap") or {}

    url = snap.get("button_url") or ""
    if not url:
        raw = chat.lstrip("@")
        if not raw.lstrip("-").isdigit():
            url = f"https://t.me/{raw}"
    if not snap.get("button_text"):
        snap["button_text"] = t.TASK_BTN_SUB
    async with SessionLocal() as session:
        offer = Offer(
            kind="my_op",
            title=snap.get("title") or "Задание",
            text=snap.get("text") or "",
            parse_mode="HTML",
            media_type=snap.get("media_type") or "none",
            media_file_id=snap.get("media_file_id") or "",
            button_text=snap.get("button_text") or t.TASK_BTN_SUB,
            button_url=url,
            check_type="subscribe",
            chat_id=chat,
            extra_buttons=snap.get("extra_buttons") or "[]",
            copy_chat_id=snap.get("copy_chat_id"),
            copy_message_id=snap.get("copy_message_id"),
            is_active=True,
        )
        session.add(offer)
        await session.commit()
    await state.clear()
    await message.answer("Задание сохранено. Проверка подписки включена.", reply_markup=admin_root_kb())


async def _tog_del(model, item_id: int, action: str) -> None:
    async with SessionLocal() as session:
        row = await session.get(model, item_id)
        if not row:
            return
        if action == "del":
            await session.delete(row)
        else:
            row.is_active = not row.is_active
        await session.commit()


@router.callback_query(F.data.regexp(r"^adm:(ad|show|off):(tog|del):\d+$"))
async def adm_tog_del(callback: CallbackQuery) -> None:
    _, kind, action, sid = callback.data.split(":")
    model = {"ad": Ad, "show": Show, "off": Offer}[kind]
    await _tog_del(model, int(sid), action)
    if kind == "ad":
        await adm_ads(callback)
    elif kind == "show":
        await adm_shows(callback)
    else:
        await adm_offers(callback)


@router.callback_query(F.data == "adm:camps")
async def adm_camps(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        from database.crud import campaign_stats, get_setting

        rows = (await session.execute(select(Campaign).order_by(desc(Campaign.id)))).scalars().all()
        username = await get_setting(session, "bot_username", "")
        lines = ["🔗 <b>Реф-ссылки</b>\n"]
        for camp in rows[:10]:
            st = await campaign_stats(session, camp)
            link = f"https://t.me/{username}?start=c_{camp.code}" if username else f"?start=c_{camp.code}"
            lines.append(
                f"<b>{camp.name}</b> <code>{camp.code}</code>\n"
                f"{link}\n"
                f"переходы {st['transitions']} · уники {st['uniques']} ({st['conv_unique']}%) · "
                f"прем {st['premiums']} ({st['conv_premium']}%) · CPU {st['cpu']:.2f}\n"
            )
    kb = back_kb([[InlineKeyboardButton(text="➕ Создать", callback_data="adm:camp:add")]])
    if callback.message:
        await callback.message.edit_text("\n".join(lines) or "Пусто", parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "adm:camp:add")
async def adm_camp_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSG.wait_campaign)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Формат: <code>код | название | цена</code>\nПример: <code>yt1 | YouTube | 1500</code>", parse_mode="HTML")


@router.message(AdminSG.wait_campaign, F.text, ~F.text.in_(SKIP))
async def adm_camp_save(message: Message, state: FSMContext) -> None:
    parts = [p.strip() for p in (message.text or "").split("|")]
    code = secrets.token_hex(3) if not parts else parts[0]
    name = parts[1] if len(parts) > 1 else code
    price = parts[2] if len(parts) > 2 else "0"
    async with SessionLocal() as session:
        session.add(Campaign(code=code, name=name, price=price or 0))
        await session.commit()
    await state.clear()
    await message.answer(f"Ссылка: c_{code}", reply_markup=admin_root_kb())


@router.callback_query(F.data == "adm:br")
async def adm_br(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSG.wait_broadcast)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Пришли пост для рассылки. Он уйдёт всем пользователям.")


@router.message(AdminSG.wait_broadcast, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_br_send(message: Message, state: FSMContext) -> None:
    await state.clear()
    snap = _post_snapshot(message)
    async with SessionLocal() as session:
        users = (await session.execute(select(User.tg_id).where(User.blocked.is_(False)))).scalars().all()
        item = Broadcast(text=snap.get("text") or "", status="running", total=len(users))
        session.add(item)
        await session.commit()
    sent = failed = 0
    extra_rows = []
    try:
        extra_rows = json.loads(snap.get("extra_buttons") or "[]")
    except json.JSONDecodeError:
        extra_rows = []
    markup = load_inline_markup(extra_rows)
    for tg_id in users:
        try:
            await send_content(
                bot,
                tg_id,
                snap.get("text") or "",
                parse_mode="HTML",
                media_type=snap.get("media_type") or "none",
                media_file_id=snap.get("media_file_id") or "",
                reply_markup=markup,
                copy_chat_id=snap.get("copy_chat_id"),
                copy_message_id=snap.get("copy_message_id"),
                replace_markup=False,
            )
            sent += 1
        except Exception:
            failed += 1
    await message.answer(f"Рассылка: {sent} ок, {failed} ошибок.", reply_markup=admin_root_kb())


async def _user_card(session, user: User) -> tuple[str, InlineKeyboardMarkup]:
    refs = await count_referrals(session, user.id)
    income = await ref_income(session, user.id)
    earned_refs = await referrals_earned_total(session, user.id)
    status = "🚫 бан" if user.blocked else "✅ активен"
    uname = f"@{user.username}" if user.username else "—"
    text = (
        f"👤 <b>Юзер {user.tg_id}</b>\n\n"
        f"Имя: <b>{user.first_name or '—'}</b> · {uname}\n"
        f"Статус: <b>{status}</b>\n"
        f"Premium: <b>{'да' if user.is_premium else 'нет'}</b>\n\n"
        "<blockquote>"
        f"💰 <b>Баланс</b> — <b>{Decimal(user.balance):.2f}</b> ⭐\n"
        f"🎁 <b>Собрал</b> — <b>{Decimal(user.total_earned):.2f}</b> ⭐\n"
        f"📦 <b>Вывел</b> — <b>{Decimal(user.total_withdrawn):.2f}</b> ⭐\n"
        f"👆 <b>Тапов</b> — <b>{user.clicks_total}</b>\n"
        f"👥 <b>Друзей</b> — <b>{refs}</b>\n"
        f"💎 <b>С друзей</b> — <b>{income:.2f}</b> ⭐\n"
        f"🌟 <b>Они собрали</b> — <b>{earned_refs:.2f}</b> ⭐"
        "</blockquote>"
    )
    ban_label = "✅ Разбан" if user.blocked else "🚫 Бан"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=ban_label, callback_data=f"adm:u:ban:{user.tg_id}"),
            ],
            [
                InlineKeyboardButton(text="➕ Выдать ⭐", callback_data=f"adm:u:plus:{user.tg_id}"),
                InlineKeyboardButton(text="➖ Забрать ⭐", callback_data=f"adm:u:minus:{user.tg_id}"),
            ],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"adm:u:show:{user.tg_id}")],
            [InlineKeyboardButton(text="⬅️ Админка", callback_data="adm:root")],
        ]
    )
    return text, kb


async def _send_user_card(target: Message | CallbackQuery, user: User) -> None:
    async with SessionLocal() as session:
        fresh = await session.get(User, user.id) or await get_user_by_tg(session, user.tg_id)
        if not fresh:
            return
        text, kb = await _user_card(session, fresh)
    if isinstance(target, CallbackQuery) and target.message:
        try:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await target.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await target.answer()
        return
    if isinstance(target, Message):
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "adm:users")
async def adm_users(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSG.wait_user)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Пришли Telegram ID или @username человека.")


@router.message(AdminSG.wait_user, F.text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_user_find(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().lstrip("@")
    async with SessionLocal() as session:
        user = None
        if raw.isdigit():
            user = await get_user_by_tg(session, int(raw))
        elif raw:
            user = (
                await session.execute(select(User).where(User.username.ilike(raw)).limit(1))
            ).scalar_one_or_none()
    if not user:
        await message.answer("Не нашёл. Пришли другой ID или @username.")
        return
    await state.clear()
    await _send_user_card(message, user)


@router.callback_query(F.data.startswith("adm:u:show:"))
async def adm_user_show(callback: CallbackQuery) -> None:
    tg_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, tg_id)
    if not user:
        await callback.answer("Нет такого", show_alert=True)
        return
    await _send_user_card(callback, user)


@router.callback_query(F.data.startswith("adm:u:ban:"))
async def adm_user_ban(callback: CallbackQuery) -> None:
    tg_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, tg_id)
        if not user:
            await callback.answer("Нет такого", show_alert=True)
            return
        user.blocked = not user.blocked
        await session.commit()
        blocked = user.blocked
        uid = user.id
    await callback.answer("Забанен" if blocked else "Разбанен")
    async with SessionLocal() as session:
        user = await session.get(User, uid)
        if user:
            await _send_user_card(callback, user)


@router.callback_query(F.data.startswith("adm:u:plus:"))
@router.callback_query(F.data.startswith("adm:u:minus:"))
async def adm_user_stars_ask(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    action = parts[2]
    tg_id = int(parts[3])
    await state.set_state(AdminSG.wait_user_stars)
    await state.update_data(star_tg=tg_id, star_sign=1 if action == "plus" else -1)
    await callback.answer()
    if callback.message:
        word = "начислить" if action == "plus" else "списать"
        await callback.message.answer(f"Сколько ⭐ {word}? Напиши число, например 10")


@router.message(AdminSG.wait_user_stars, F.text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_user_stars_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    raw = (message.text or "").strip().replace(",", ".")
    try:
        amount = Decimal(raw)
    except Exception:
        await message.answer("Нужно число, например 10 или 2.5")
        return
    if amount <= 0:
        await message.answer("Число должно быть больше 0.")
        return
    tg_id = int(data.get("star_tg") or 0)
    sign = int(data.get("star_sign") or 1)
    delta = amount if sign > 0 else -amount
    await state.clear()
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, tg_id)
        if not user:
            await message.answer("Юзер пропал.")
            return
        new_bal = Decimal(user.balance) + delta
        if new_bal < 0:
            new_bal = Decimal("0")
        user.balance = new_bal
        if delta > 0:
            user.total_earned = Decimal(user.total_earned) + delta
        await session.commit()
        uid = user.id
    try:
        if delta > 0:
            await bot.send_message(tg_id, f"✨ Тебе начислили <b>{amount:.2f}</b> ⭐", parse_mode="HTML")
        else:
            await bot.send_message(tg_id, f"Админ списал <b>{amount:.2f}</b> ⭐", parse_mode="HTML")
    except Exception:
        pass
    await message.answer(f"Готово. Новый баланс: {new_bal:.2f} ⭐")
    async with SessionLocal() as session:
        user = await session.get(User, uid)
        if user:
            await _send_user_card(message, user)


@router.callback_query(F.data == "adm:wd")
async def adm_wd(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Withdrawal).where(Withdrawal.status == "pending_review").order_by(desc(Withdrawal.id)).limit(10)
            )
        ).scalars().all()
        lines = ["💸 <b>Заявки на вывод</b>\n"]
        kb_rows = []
        for w in rows:
            user = await session.get(User, w.user_id)
            who = f"{user.tg_id}" if user else "?"
            lines.append(f"#{w.id} · {w.amount} ⭐ · {who}")
            kb_rows.append(
                [
                    InlineKeyboardButton(text=f"✅ {w.id}", callback_data=f"adm:wd:ok:{w.id}"),
                    InlineKeyboardButton(text=f"❌ {w.id}", callback_data=f"adm:wd:no:{w.id}"),
                ]
            )
    if callback.message:
        await callback.message.edit_text(
            "\n".join(lines) if len(lines) > 1 else "Пусто",
            parse_mode="HTML",
            reply_markup=back_kb(*kb_rows),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:wd:ok:"))
async def adm_wd_ok(callback: CallbackQuery) -> None:
    wid = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        wd = await session.get(Withdrawal, wid)
        if wd:
            from database.crud import utcnow

            wd.status = "approved"
            wd.processed_at = utcnow()
            user = await session.get(User, wd.user_id)
            await session.commit()
            if user:
                try:
                    await bot.send_message(
                        user.tg_id,
                        t.WD_APPROVED.format(amount=f"{float(wd.amount):.0f}"),
                    )
                except Exception:
                    pass
    await callback.answer("Ок")
    await adm_wd(callback)


@router.callback_query(F.data.startswith("adm:wd:no:"))
async def adm_wd_no(callback: CallbackQuery) -> None:
    wid = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        wd = await session.get(Withdrawal, wid)
        if wd:
            from decimal import Decimal

            from database.crud import utcnow

            user = await session.get(User, wd.user_id)
            if user and wd.status == "pending_review":
                user.balance = Decimal(user.balance) + Decimal(wd.amount)
                user.total_withdrawn = max(Decimal("0"), Decimal(user.total_withdrawn) - Decimal(wd.amount))
            wd.status = "rejected"
            wd.processed_at = utcnow()
            await session.commit()
            if user:
                try:
                    await bot.send_message(
                        user.tg_id,
                        t.WD_REJECTED.format(comment="Попробуй ещё раз чуть позже."),
                    )
                except Exception:
                    pass
    await callback.answer("Отклонено")
    await adm_wd(callback)


def _contest_end_label(item: Contest) -> str:
    if item.end_type == "none":
        return "♾ без итогов"
    if item.end_type == "time":
        return f"⏰ {item.end_value}с"
    return f"👥 {item.end_value}"


async def _active_resources() -> list[dict]:
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(SavedResource).where(SavedResource.is_active.is_(True)).order_by(SavedResource.id)
                )
            ).scalars().all()
        )
    return [{"title": r.title, "url": r.url} for r in rows if r.url]


async def _upsert_saved_channel(info) -> SavedChannel:
    chat_id = str(info.id)
    title = (getattr(info, "title", None) or getattr(info, "username", None) or chat_id)[:128]
    username = f"@{info.username}" if getattr(info, "username", None) else ""
    async with SessionLocal() as session:
        row = (
            await session.execute(select(SavedChannel).where(SavedChannel.chat_id == chat_id))
        ).scalar_one_or_none()
        if row:
            row.title = title
            row.username = username
        else:
            row = SavedChannel(title=title, chat_id=chat_id, username=username)
            session.add(row)
        from bot.services.greetings import get_or_create_greeting

        await get_or_create_greeting(session, chat_id)
        await session.commit()
        await session.refresh(row)
        return row


async def _resolve_channel(raw: str) -> SavedChannel | None:
    from bot.services.contests import channel_target

    chat = channel_target(raw)
    if chat is None:
        return None
    try:
        info = await bot.get_chat(chat)
    except Exception:
        return None
    return await _upsert_saved_channel(info)


async def _channel_pick_kb(prefix: str, back: str) -> InlineKeyboardMarkup:
    async with SessionLocal() as session:
        rows = list((await session.execute(select(SavedChannel).order_by(SavedChannel.id))).scalars().all())
    kb: list[list[InlineKeyboardButton]] = []
    for ch in rows:
        label = (ch.username or ch.title or ch.chat_id)[:32]
        kb.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}:{ch.id}")])
    kb.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data=f"{prefix}:new")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def _contest_nav(state: FSMContext) -> str:
    data = await state.get_data()
    cid = data.get("combo_id")
    try:
        return f"adm:cmb:o:{int(cid)}" if cid else "adm:contest"
    except (TypeError, ValueError):
        return "adm:contest"


async def _after_contest_channel(target: Message, state: FSMContext, channel_id: str) -> None:
    await state.update_data(contest_channel=channel_id)
    await state.set_state(None)
    back = await _contest_nav(state)
    await target.answer(t.ADM_CONTEST_ASK_END, parse_mode="HTML", reply_markup=contest_end_type_kb(back))


async def _save_contest(
    message: Message,
    state: FSMContext,
    *,
    end_type: str,
    end_value: int = 0,
    ends_at=None,
    winners_n: int = 0,
) -> None:
    from bot.services.contests import bot_username_cached, contest_link, post_contest_to_channel
    from database.crud import utcnow

    data = await state.get_data()
    text = data.get("contest_text") or ""
    photo = data.get("contest_photo") or ""
    button = data.get("contest_button") or "🎁 Участвовать"
    channel_id = data.get("contest_channel") or ""
    kind = data.get("contest_kind") or "service"
    if kind not in ("own", "service", "none"):
        kind = "service"
    sponsors = int(data.get("contest_sponsors") or 0)
    own = data.get("contest_own") or "[]"
    if kind != "service":
        sponsors = 0
    if kind != "own":
        own = "[]"
    if end_type == "none":
        winners_n = 0
    else:
        winners_n = max(1, int(winners_n or data.get("contest_winners") or 1))
    if not text or not channel_id:
        if data.get("combo_id") and text:
            channel_id = data.get("combo_channel") or channel_id
        if not text or not channel_id:
            await state.clear()
            await message.answer("Что-то потерялось. Создай конкурс заново из админки.")
            return
    combo_id = data.get("combo_id")
    if combo_id:
        from bot.handlers.combo_admin import _combo_view
        from bot.services.combo import save_contest_template

        combo, _post = await save_contest_template(
            int(combo_id),
            text=text,
            photo=photo,
            button=button,
            kind=kind,
            sponsors=sponsors,
            own=own if isinstance(own, str) else json.dumps(own, ensure_ascii=False),
            end_type=end_type,
            end_value=end_value,
            winners_n=winners_n,
        )
        await state.clear()
        if combo:
            await message.answer(
                "🏆 Конкурс запомнил. Выложу после байтов до конкурса, в середине связки.",
                parse_mode="HTML",
            )
            view = await _combo_view(combo.id)
            if view:
                body, kb = view
                await _send_html(message, body, kb)
            return
        await message.answer("Не смог сохранить конкурс в связку.")
        return
    code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10]
    await state.clear()
    async with SessionLocal() as session:
        contest = Contest(
            code=code,
            text=text,
            parse_mode="HTML",
            media_type="photo" if photo else "none",
            media_file_id=photo,
            button_text=button,
            sponsors_count=sponsors,
            channel_id=channel_id,
            end_type=end_type,
            end_value=end_value,
            winners_count=winners_n,
            sponsor_kind=kind,
            own_sponsors=own if isinstance(own, str) else json.dumps(own, ensure_ascii=False),
            status="active",
            started_at=utcnow(),
            ends_at=ends_at,
        )
        session.add(contest)
        await session.commit()
        await session.refresh(contest)
        contest_id = contest.id
    username = await bot_username_cached()
    link = contest_link(username, code)
    msg_id = await post_contest_to_channel(contest, username)
    if msg_id:
        async with SessionLocal() as session:
            row = await session.get(Contest, contest_id)
            if row:
                row.channel_message_id = msg_id
                await session.commit()
        post_line = t.ADM_CONTEST_POST_OK
    else:
        post_line = t.ADM_CONTEST_POST_FAIL
    await message.answer(
        t.ADM_CONTEST_SAVED.format(link=link, post_line=post_line),
        parse_mode="HTML",
        reply_markup=admin_root_kb(),
    )


async def _contest_list_text() -> tuple[str, InlineKeyboardMarkup]:
    from bot.services.contests import bot_username_cached, contest_link, participant_count

    username = await bot_username_cached()
    async with SessionLocal() as session:
        rows = list((await session.execute(select(Contest).order_by(desc(Contest.id)).limit(12))).scalars().all())
        lines = [
            "🏆 <b>Конкурсы</b>\n",
            "📢 вернуть пост в канал · 🔁 такой же конкурс заново",
        ]
        kb_rows: list[list[InlineKeyboardButton]] = []
        if not rows:
            lines.append("Пока пусто. Создай конкурс — бот сам запостит в канал и выдаст спец-ссылку.")
        for item in rows:
            count = await participant_count(session, item.id)
            mark = "🟢" if item.status == "active" else "🏁"
            if (item.source or "") == "auto":
                mark = "🎯 " + mark
            link = contest_link(username, item.code)
            winner = "" if item.end_type == "none" else f" · 👑 {item.winners_count}"
            lines.append(
                f"{mark} <b>{item.code}</b> · {_contest_end_label(item)} · {count} чел.{winner}\n"
                f"<code>{link}</code>"
            )
            row = [InlineKeyboardButton(text=f"🔗 {item.code}", copy_text=CopyTextButton(text=link))]
            row.append(InlineKeyboardButton(text="📊", callback_data=f"adm:cts:day:{item.id}"))
            row.append(InlineKeyboardButton(text="📢", callback_data=f"adm:ct:rest:{item.id}"))
            row.append(InlineKeyboardButton(text="🔁", callback_data=f"adm:ct:copy:{item.id}"))
            if item.status == "active":
                row.append(InlineKeyboardButton(text="🏁", callback_data=f"adm:ct:fin:{item.id}"))
            row.append(InlineKeyboardButton(text="🗑", callback_data=f"adm:ct:del:{item.id}"))
            kb_rows.append(row)
    kb_rows.append([InlineKeyboardButton(text="📊 Стата", callback_data="adm:cts:day")])
    kb_rows.append(
        [
            InlineKeyboardButton(text="📌 Каналы", callback_data="adm:ch"),
            InlineKeyboardButton(text="🧩 Ресурсы", callback_data="adm:res"),
        ]
    )
    kb_rows.append([InlineKeyboardButton(text="➕ Создать конкурс", callback_data="adm:contest:add")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Админка", callback_data="adm:root")])
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data == "adm:contest")
async def adm_contest(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _contest_list_text()
    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "adm:contest:add")
async def adm_contest_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSG.wait_contest_text)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_CONTEST_ASK_TEXT, parse_mode="HTML")


@router.message(AdminSG.wait_contest_text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_contest_text(message: Message, state: FSMContext) -> None:
    text = message.html_text or message.caption or message.text or ""
    if not text.strip():
        await message.answer("Нужен текст поста. Пришли ещё раз.")
        return
    await state.update_data(contest_text=text)
    await state.set_state(AdminSG.wait_contest_photo)
    await message.answer(t.ADM_CONTEST_ASK_PHOTO, parse_mode="HTML")


@router.message(AdminSG.wait_contest_photo, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_contest_photo(message: Message, state: FSMContext) -> None:
    if not message.photo:
        await message.answer("Нужно фото. Пришли картинку для поста.")
        return
    await state.update_data(contest_photo=message.photo[-1].file_id)
    await state.set_state(None)
    back = await _contest_nav(state)
    await message.answer(t.ADM_CONTEST_ASK_KIND, parse_mode="HTML", reply_markup=contest_sponsors_kb(back))


@router.callback_query(F.data == "adm:ct:kind:none")
async def adm_contest_kind_none(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(contest_kind="none", contest_sponsors=0, contest_own="[]")
    await state.set_state(AdminSG.wait_contest_button)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_CONTEST_ASK_BUTTON, parse_mode="HTML")


@router.callback_query(F.data == "adm:ct:kind:service")
async def adm_contest_kind_service(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(contest_kind="service", contest_own="[]")
    await state.set_state(AdminSG.wait_contest_sponsors)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_CONTEST_ASK_SPONSORS, parse_mode="HTML")


@router.callback_query(F.data == "adm:ct:kind:own")
async def adm_contest_kind_own(callback: CallbackQuery, state: FSMContext) -> None:
    saved = await _active_resources()
    await state.update_data(contest_kind="own", contest_sponsors=0, contest_own=json.dumps(saved, ensure_ascii=False))
    await callback.answer()
    if saved:
        if callback.message:
            await callback.message.answer(
                f"Есть <b>{len(saved)}</b> своих ресурсов по умолчанию (боты без проверки).",
                parse_mode="HTML",
                reply_markup=contest_own_pick_kb(len(saved), await _contest_nav(state)),
            )
        return
    await state.set_state(AdminSG.wait_contest_own)
    if callback.message:
        await callback.message.answer(t.ADM_CONTEST_ASK_OWN, parse_mode="HTML")


@router.callback_query(F.data == "adm:ct:own:use")
async def adm_contest_own_use(callback: CallbackQuery, state: FSMContext) -> None:
    saved = await _active_resources()
    await state.update_data(contest_kind="own", contest_sponsors=0, contest_own=json.dumps(saved, ensure_ascii=False))
    await state.set_state(AdminSG.wait_contest_button)
    await callback.answer(f"Свои: {len(saved)}")
    if callback.message:
        await callback.message.answer(t.ADM_CONTEST_ASK_BUTTON, parse_mode="HTML")


@router.callback_query(F.data == "adm:ct:own:custom")
async def adm_contest_own_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(contest_kind="own", contest_sponsors=0)
    await state.set_state(AdminSG.wait_contest_own)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_CONTEST_ASK_OWN, parse_mode="HTML")


@router.message(AdminSG.wait_contest_own, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_contest_own(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw in ("-", "нет", "все"):
        async with SessionLocal() as session:
            rows = list(
                (await session.execute(select(SavedResource).where(SavedResource.is_active.is_(True)))).scalars().all()
            )
        items = [{"title": r.title, "url": r.url} for r in rows if r.url]
    else:
        buttons = _parse_constructor_buttons(message)
        items = [{"title": b.get("text") or "Ресурс", "url": b.get("url")} for b in buttons if b.get("url")]
        if not items:
            await message.answer("Не разобрал. Строка: <code>Название | https://t.me/...</code>", parse_mode="HTML")
            return
    await state.update_data(contest_own=json.dumps(items, ensure_ascii=False), contest_sponsors=0)
    await state.set_state(AdminSG.wait_contest_button)
    await message.answer(t.ADM_CONTEST_ASK_BUTTON, parse_mode="HTML")


@router.callback_query(F.data == "adm:ct:sponsors:0")
async def adm_contest_sponsors_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await adm_contest_kind_none(callback, state)


@router.message(AdminSG.wait_contest_sponsors, F.text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_contest_sponsors(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().lower()
    if raw in ("-", "нет", "без", "без спонсоров", "0"):
        n = 0
    elif raw.isdigit():
        n = int(raw)
    else:
        await message.answer("Напиши число, например 3. Или 0 / «нет» — без спонсоров.")
        return
    if n == 0:
        await state.update_data(contest_kind="none", contest_sponsors=0)
    else:
        await state.update_data(contest_sponsors=n)
    await state.set_state(AdminSG.wait_contest_button)
    await message.answer(t.ADM_CONTEST_ASK_BUTTON, parse_mode="HTML")


@router.message(AdminSG.wait_contest_button, F.text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_contest_button(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Напиши название кнопки.")
        return
    await state.update_data(contest_button=name[:64])
    data = await state.get_data()
    if data.get("combo_id"):
        channel_id = data.get("combo_channel") or data.get("contest_channel") or ""
        if channel_id:
            await _after_contest_channel(message, state, channel_id)
            return
    await state.set_state(AdminSG.wait_contest_channel)
    await message.answer(
        t.ADM_CONTEST_ASK_CHANNEL,
        parse_mode="HTML",
        reply_markup=await _channel_pick_kb("adm:ct:ch", "adm:contest"),
    )


@router.callback_query(F.data == "adm:ct:ch:new")
async def adm_contest_channel_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSG.wait_contest_channel)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_CHANNEL_ASK, parse_mode="HTML")


@router.callback_query(F.data.startswith("adm:ct:ch:"))
async def adm_contest_channel_pick(callback: CallbackQuery, state: FSMContext) -> None:
    raw = callback.data.split(":")[-1]
    if raw == "new":
        return
    try:
        cid = int(raw)
    except ValueError:
        await callback.answer()
        return
    async with SessionLocal() as session:
        row = await session.get(SavedChannel, cid)
    if not row:
        await callback.answer("Канал не найден", show_alert=True)
        return
    await callback.answer(row.username or row.title)
    if callback.message:
        await _after_contest_channel(callback.message, state, row.chat_id)


@router.message(AdminSG.wait_contest_channel, F.text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_contest_channel(message: Message, state: FSMContext) -> None:
    row = await _resolve_channel(message.text or "")
    if not row:
        await message.answer("Не смог открыть канал. Бот должен быть админом и видеть этот чат.")
        return
    await _after_contest_channel(message, state, row.chat_id)


@router.message(AdminSG.wait_contest_winners, F.text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_contest_winners(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Нужно число победителей больше 0, например 1 или 3.")
        return
    await state.update_data(contest_winners=int(raw))
    data = await state.get_data()
    end_type = data.get("contest_end_type") or "participants"
    await state.set_state(AdminSG.wait_contest_end_value)
    prompt = t.ADM_CONTEST_ASK_END_TIME if end_type == "time" else t.ADM_CONTEST_ASK_END_PEOPLE
    await message.answer(prompt, parse_mode="HTML")


@router.callback_query(F.data == "adm:ct:end:people")
async def adm_contest_end_people(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(contest_end_type="participants")
    await state.set_state(AdminSG.wait_contest_winners)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_CONTEST_ASK_WINNERS, parse_mode="HTML")


@router.callback_query(F.data == "adm:ct:end:time")
async def adm_contest_end_time(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(contest_end_type="time")
    await state.set_state(AdminSG.wait_contest_winners)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_CONTEST_ASK_WINNERS, parse_mode="HTML")


@router.callback_query(F.data == "adm:ct:end:none")
async def adm_contest_end_none(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Без итогов")
    if callback.message:
        await _save_contest(callback.message, state, end_type="none", end_value=0, winners_n=0)


@router.message(AdminSG.wait_contest_end_value, F.text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_contest_end_value(message: Message, state: FSMContext) -> None:
    from bot.services.contests import parse_duration
    from database.crud import utcnow

    data = await state.get_data()
    end_type = data.get("contest_end_type") or "participants"
    raw = (message.text or "").strip()
    ends_at = None
    if end_type == "time":
        seconds = parse_duration(raw)
        if not seconds or seconds <= 0:
            await message.answer("Не понял время. Примеры: 30м, 2ч, 1д")
            return
        end_value = seconds
        ends_at = utcnow() + timedelta(seconds=seconds)
    else:
        if not raw.isdigit() or int(raw) <= 0:
            await message.answer("Нужно число участников больше 0.")
            return
        end_value = int(raw)
    await _save_contest(
        message,
        state,
        end_type=end_type,
        end_value=end_value,
        ends_at=ends_at,
        winners_n=int(data.get("contest_winners") or 1),
    )


@router.callback_query(F.data.startswith("adm:ct:rest:"))
async def adm_contest_restore(callback: CallbackQuery, state: FSMContext) -> None:
    from bot.services.contests import restore_contest_post

    cid = int(callback.data.split(":")[-1])
    result = await restore_contest_post(cid)
    notes = {
        "ok": "Пост снова в канале ✅",
        "alive": "Пост на месте, кнопку обновил",
        "fail": "Не смог запостить. Бот должен быть админом канала.",
        "missing": "Конкурс не найден",
    }
    await callback.answer(notes.get(result, result), show_alert=True)
    await adm_contest(callback, state)


@router.callback_query(F.data.startswith("adm:ct:copy:"))
async def adm_contest_copy(callback: CallbackQuery, state: FSMContext) -> None:
    from bot.services.contests import clone_contest, contest_link

    cid = int(callback.data.split(":")[-1])
    contest, status = await clone_contest(cid)
    if not contest:
        await callback.answer("Не смог скопировать", show_alert=True)
        return
    username = ""
    from bot.services.contests import bot_username_cached

    username = await bot_username_cached()
    link = contest_link(username, contest.code)
    post_line = t.ADM_CONTEST_POST_OK if status == "posted" else t.ADM_CONTEST_POST_FAIL
    await callback.answer("Запустил такой же конкурс")
    if callback.message:
        await callback.message.answer(
            t.ADM_CONTEST_SAVED.format(link=link, post_line=post_line),
            parse_mode="HTML",
        )
    await adm_contest(callback, state)


@router.callback_query(F.data.startswith("adm:ct:fin:"))
async def adm_contest_finish(callback: CallbackQuery, state: FSMContext) -> None:
    from bot.services.contests import finish_contest

    cid = int(callback.data.split(":")[-1])
    ok = await finish_contest(cid)
    await callback.answer("Завершён" if ok else "Уже завершён")
    await adm_contest(callback, state)


@router.callback_query(F.data.startswith("adm:ct:del:"))
async def adm_contest_del(callback: CallbackQuery, state: FSMContext) -> None:
    cid = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        row = await session.get(Contest, cid)
        if row:
            await session.delete(row)
            await session.commit()
    await callback.answer("Удалён")
    await adm_contest(callback, state)


def _cts_kb(period: str, data: dict) -> InlineKeyboardMarkup:
    cid = data.get("contest_id")
    rows = [
        [
            InlineKeyboardButton(text=("• День" if period == "day" else "День"), callback_data=f"adm:cts:day{':' + str(cid) if cid else ''}"),
            InlineKeyboardButton(text=("• Неделя" if period == "week" else "Неделя"), callback_data=f"adm:cts:week{':' + str(cid) if cid else ''}"),
            InlineKeyboardButton(text=("• Месяц" if period == "month" else "Месяц"), callback_data=f"adm:cts:month{':' + str(cid) if cid else ''}"),
        ]
    ]
    if not cid:
        for item in (data.get("contests") or [])[:10]:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🏆 {item.get('code')} · {item.get('participants') or 0}",
                        callback_data=f"adm:cts:{period}:{item['id']}",
                    )
                ]
            )
    else:
        rows.append([InlineKeyboardButton(text="⬅️ Все конкурсы", callback_data=f"adm:cts:{period}")])
    rows.append([InlineKeyboardButton(text="⬅️ Конкурсы", callback_data="adm:contest")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.regexp(r"^adm:cts:(day|week|month)(?::\d+)?$"))
async def adm_contest_stats(callback: CallbackQuery) -> None:
    from bot.services.contests import contest_stats, format_contest_stats

    parts = callback.data.split(":")
    period = parts[2]
    contest_id = int(parts[3]) if len(parts) > 3 else None
    async with SessionLocal() as session:
        data = await contest_stats(session, period, contest_id)
    text = format_contest_stats(data)
    kb = _cts_kb(period, data)
    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _send_html(message: Message, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return
    except Exception:
        pass
    try:
        await message.answer(text, reply_markup=kb)
    except Exception:
        pass


async def _edit_html(target: CallbackQuery | Message, text: str, kb: InlineKeyboardMarkup) -> None:
    if isinstance(target, CallbackQuery) and target.message:
        try:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await _send_html(target.message, text, kb)
        try:
            await target.answer()
        except Exception:
            pass
        return
    if isinstance(target, Message):
        await _send_html(target, text, kb)


async def _channels_view() -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        rows = list((await session.execute(select(SavedChannel).order_by(SavedChannel.id))).scalars().all())
    lines = ["📌 <b>Каналы</b>\n", "Сохраняются навсегда. При посте просто выбираешь."]
    kb: list[list[InlineKeyboardButton]] = []
    if not rows:
        lines.append("Пока пусто.")
    for ch in rows:
        label = ch.username or ch.title or ch.chat_id
        lines.append(f"• {label} <code>{ch.chat_id}</code>")
        kb.append(
            [
                InlineKeyboardButton(text=label[:28], callback_data=f"adm:ch:info:{ch.id}"),
                InlineKeyboardButton(text="🗑", callback_data=f"adm:ch:del:{ch.id}"),
            ]
        )
    kb.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm:ch:add")])
    kb.append([InlineKeyboardButton(text="🏷 Имена канала", callback_data="adm:namer")])
    kb.append([InlineKeyboardButton(text="👋 Приветка", callback_data="adm:greet")])
    kb.append([InlineKeyboardButton(text="⬅️ Конкурсы", callback_data="adm:contest")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(F.data == "adm:ch")
async def adm_channels(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _channels_view()
    await _edit_html(callback, text, kb)


@router.callback_query(F.data == "adm:ch:add")
async def adm_channel_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(channel_next="list")
    await state.set_state(AdminSG.wait_channel_add)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_CHANNEL_ASK, parse_mode="HTML")


@router.callback_query(F.data.startswith("adm:ch:info:"))
async def adm_channel_info(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("adm:ch:del:"))
async def adm_channel_del(callback: CallbackQuery, state: FSMContext) -> None:
    cid = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        row = await session.get(SavedChannel, cid)
        if row:
            await session.delete(row)
            await session.commit()
    await callback.answer("Удалён")
    text, kb = await _channels_view()
    await _edit_html(callback, text, kb)


@router.message(AdminSG.wait_channel_add, F.text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_channel_add_msg(message: Message, state: FSMContext) -> None:
    row = await _resolve_channel(message.text or "")
    if not row:
        await message.answer("Не смог открыть канал. Бот должен быть админом и видеть этот чат.")
        return
    data = await state.get_data()
    nxt = data.get("channel_next") or "list"
    if nxt == "spam":
        await _after_spam_channel(message, state, row.chat_id)
        return
    if nxt == "combo":
        from bot.handlers.combo_admin import after_combo_channel

        await after_combo_channel(message, state, row.chat_id)
        return
    await state.clear()
    await message.answer(f"Сохранил {row.username or row.title}.")
    text, kb = await _channels_view()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _resources_view() -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        rows = list((await session.execute(select(SavedResource).order_by(SavedResource.id))).scalars().all())
    lines = [
        "🧩 <b>Свои ресурсы</b>\n",
        "Боты и ссылки без проверки. По умолчанию подставляются в конкурс, если выбрал «свои спонсоры».",
    ]
    kb: list[list[InlineKeyboardButton]] = []
    if not rows:
        lines.append("Пока пусто.")
    for item in rows:
        mark = "🟢" if item.is_active else "⚪️"
        lines.append(f"{mark} {item.title} — {item.url}")
        kb.append(
            [
                InlineKeyboardButton(text=f"{mark} {item.title[:22]}", callback_data=f"adm:res:tog:{item.id}"),
                InlineKeyboardButton(text="🗑", callback_data=f"adm:res:del:{item.id}"),
            ]
        )
    kb.append([InlineKeyboardButton(text="➕ Добавить", callback_data="adm:res:add")])
    kb.append([InlineKeyboardButton(text="⬅️ Конкурсы", callback_data="adm:contest")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(F.data == "adm:res")
async def adm_resources(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _resources_view()
    await _edit_html(callback, text, kb)


@router.callback_query(F.data == "adm:res:add")
async def adm_resource_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSG.wait_resource_add)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_RESOURCE_ASK, parse_mode="HTML")


@router.message(AdminSG.wait_resource_add, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_resource_add_msg(message: Message, state: FSMContext) -> None:
    buttons = _parse_constructor_buttons(message)
    if not buttons:
        await message.answer("Не разобрал. Строка: <code>Название | https://t.me/...</code>", parse_mode="HTML")
        return
    async with SessionLocal() as session:
        for btn in buttons:
            session.add(SavedResource(title=(btn.get("text") or "Ресурс")[:128], url=btn.get("url") or "", is_active=True))
        await session.commit()
    await state.clear()
    text, kb = await _resources_view()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("adm:res:tog:"))
async def adm_resource_tog(callback: CallbackQuery) -> None:
    rid = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        row = await session.get(SavedResource, rid)
        if row:
            row.is_active = not row.is_active
            await session.commit()
    text, kb = await _resources_view()
    await _edit_html(callback, text, kb)


@router.callback_query(F.data.startswith("adm:res:del:"))
async def adm_resource_del(callback: CallbackQuery) -> None:
    rid = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        row = await session.get(SavedResource, rid)
        if row:
            await session.delete(row)
            await session.commit()
    await callback.answer("Удалён")
    text, kb = await _resources_view()
    await _edit_html(callback, text, kb)


def _spam_label(job: SpamJob, contest_code: str = "") -> str:
    mark = "🟢" if job.is_active else "⚪️"
    if job.kind == "contest":
        what = f"🏆 {contest_code or job.contest_id}"
    else:
        what = f"📣 {(job.title or 'Пост')[:24]}"
    return f"{mark} {what} · {job.lifetime_seconds}/{job.pause_seconds}с"


async def _spam_view() -> tuple[str, InlineKeyboardMarkup]:
    from bot.services.spammer import spam_is_running

    async with SessionLocal() as session:
        running = await spam_is_running(session)
        jobs = list((await session.execute(select(SpamJob).order_by(desc(SpamJob.id)).limit(20))).scalars().all())
        codes: dict[int, str] = {}
        for job in jobs:
            if job.contest_id:
                ct = await session.get(Contest, job.contest_id)
                if ct:
                    codes[job.id] = ct.code
    lines = [
        "📣 <b>Спамер</b>\n",
        f"Общий: {'🟢 запущен' if running else '⏹ стоп'}",
        "Посты в одном канале идут <b>по кругу</b>: один висит → удаляется → пауза → следующий.",
        "Тайминг: <code>[жизнь] [пауза]</code> — например <code>20 5</code>.",
    ]
    kb: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="⏹ Стоп все", callback_data="adm:spam:stopall"),
            InlineKeyboardButton(text="▶️ Запуск", callback_data="adm:spam:startall"),
        ]
    ]
    if not jobs:
        lines.append("\nПока пусто.")
    for job in jobs:
        lines.append(_spam_label(job, codes.get(job.id, "")))
        toggle = "⏸ Стоп" if job.is_active else "▶️ Старт"
        kb.append(
            [
                InlineKeyboardButton(text="👁", callback_data=f"adm:spam:prev:{job.id}"),
                InlineKeyboardButton(text=toggle, callback_data=f"adm:spam:tog:{job.id}"),
                InlineKeyboardButton(text="🗑", callback_data=f"adm:spam:del:{job.id}"),
            ]
        )
    kb.append(
        [
            InlineKeyboardButton(text="➕ Пост", callback_data="adm:spam:add:post"),
            InlineKeyboardButton(text="🏆 Конкурс", callback_data="adm:spam:add:ct"),
        ]
    )
    kb.append([InlineKeyboardButton(text="🎯 Авто-конкурс и байт", callback_data="adm:cmb")])
    kb.append([InlineKeyboardButton(text="📌 Каналы", callback_data="adm:ch")])
    kb.append([InlineKeyboardButton(text="⬅️ Админка", callback_data="adm:root")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(F.data == "adm:spam")
async def adm_spam(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _spam_view()
    await _edit_html(callback, text, kb)


@router.callback_query(F.data == "adm:spam:stopall")
async def adm_spam_stop_all(callback: CallbackQuery) -> None:
    from bot.services.spammer import stop_all_spam

    n = await stop_all_spam()
    await callback.answer(f"Стоп. Слотов: {n}")
    text, kb = await _spam_view()
    await _edit_html(callback, text, kb)


@router.callback_query(F.data == "adm:spam:startall")
async def adm_spam_start_all(callback: CallbackQuery) -> None:
    from bot.services.spammer import start_all_spam

    n = await start_all_spam()
    await callback.answer(f"Запуск. Слотов: {n}")
    text, kb = await _spam_view()
    await _edit_html(callback, text, kb)


@router.callback_query(F.data.startswith("adm:spam:prev:"))
async def adm_spam_preview(callback: CallbackQuery) -> None:
    from bot.services.spammer import preview_spam_job

    jid = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        job = await session.get(SpamJob, jid)
    if not job:
        await callback.answer("Нет такого слота", show_alert=True)
        return
    await callback.answer("Предпросмотр")
    if not callback.from_user:
        return
    await bot.send_message(callback.from_user.id, "👁 <b>Предпросмотр поста</b>", parse_mode="HTML")
    ok = await preview_spam_job(job, callback.from_user.id)
    if not ok:
        await bot.send_message(callback.from_user.id, "Не смог показать этот пост.")


@router.callback_query(F.data == "adm:spam:add:post")
async def adm_spam_add_post(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(spam_kind="post")
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            t.ADM_SPAM_ASK_CHANNEL,
            parse_mode="HTML",
            reply_markup=await _channel_pick_kb("adm:spam:ch", "adm:spam"),
        )


async def _spam_contest_kb() -> InlineKeyboardMarkup:
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(Contest).where(Contest.status == "active").order_by(desc(Contest.id)).limit(20)
                )
            ).scalars().all()
        )
    kb: list[list[InlineKeyboardButton]] = []
    for item in rows:
        kb.append([InlineKeyboardButton(text=f"🏆 {item.code}", callback_data=f"adm:spam:ct:{item.id}")])
    if not rows:
        kb.append([InlineKeyboardButton(text="Нет активных конкурсов", callback_data="adm:spam")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:spam")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(F.data == "adm:spam:add:ct")
async def adm_spam_add_ct(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(spam_kind="contest")
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Какой конкурс крутить в спамере?",
            reply_markup=await _spam_contest_kb(),
        )


@router.callback_query(F.data.startswith("adm:spam:ct:"))
async def adm_spam_pick_ct(callback: CallbackQuery, state: FSMContext) -> None:
    cid = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        row = await session.get(Contest, cid)
    if not row:
        await callback.answer("Нет такого", show_alert=True)
        return
    await state.update_data(spam_kind="contest", spam_contest_id=cid, spam_channel=row.channel_id or "")
    await callback.answer(row.code)
    if callback.message:
        await callback.message.answer(
            t.ADM_SPAM_ASK_CHANNEL + "\nМожно взять тот же канал, что у конкурса, или другой.",
            parse_mode="HTML",
            reply_markup=await _channel_pick_kb("adm:spam:ch", "adm:spam"),
        )


@router.callback_query(F.data == "adm:spam:ch:new")
async def adm_spam_channel_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(channel_next="spam")
    await state.set_state(AdminSG.wait_channel_add)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_CHANNEL_ASK, parse_mode="HTML")


async def _after_spam_channel(target: Message, state: FSMContext, channel_id: str) -> None:
    await state.update_data(spam_channel=channel_id)
    data = await state.get_data()
    if data.get("spam_kind") == "contest":
        await state.set_state(AdminSG.wait_spam_timing)
        await _send_spam_preview(
            target.chat.id,
            _job_like(kind="contest", contest_id=data.get("spam_contest_id"), channel_id=channel_id),
        )
        await target.answer(t.ADM_SPAM_ASK_TIMING, parse_mode="HTML")
        return
    await state.set_state(AdminSG.wait_spam_post)
    await target.answer(t.ADM_SPAM_ASK_POST, parse_mode="HTML")


@router.callback_query(F.data.startswith("adm:spam:ch:"))
async def adm_spam_channel_pick(callback: CallbackQuery, state: FSMContext) -> None:
    raw = callback.data.split(":")[-1]
    if raw == "new":
        return
    try:
        cid = int(raw)
    except ValueError:
        await callback.answer()
        return
    async with SessionLocal() as session:
        row = await session.get(SavedChannel, cid)
    if not row:
        await callback.answer("Канал не найден", show_alert=True)
        return
    await callback.answer(row.username or row.title)
    if callback.message:
        await _after_spam_channel(callback.message, state, row.chat_id)


def _apply_spam_buttons(snap: dict, extra_rows: list) -> dict:
    first = extra_rows[0][0] if extra_rows and extra_rows[0] else {}
    snap["extra_buttons"] = json.dumps(extra_rows, ensure_ascii=False)
    snap["button_text"] = first.get("text") or ""
    snap["button_url"] = first.get("url") or ""
    snap["button_type"] = "url" if first.get("url") else "none"
    return snap


async def _capture_post_buttons(message: Message) -> list:
    rows = dump_inline_markup(message)
    if rows:
        return rows
    for from_chat, mid in iter_copy_sources(message):
        try:
            probe = await bot.copy_message(
                chat_id=message.chat.id,
                from_chat_id=from_chat,
                message_id=mid,
            )
        except Exception:
            continue
        rows = dump_inline_markup(probe)
        try:
            await bot.delete_message(message.chat.id, probe.message_id)
        except Exception:
            pass
        if rows:
            return rows
    return []


def _job_like(*, kind: str = "post", contest_id=None, channel_id: str = "", snap: dict | None = None):
    class _Job:
        pass

    snap = snap or {}
    job = _Job()
    job.kind = kind
    job.contest_id = contest_id
    job.channel_id = channel_id
    job.copy_chat_id = snap.get("copy_chat_id")
    job.copy_message_id = snap.get("copy_message_id")
    job.extra_buttons = snap.get("extra_buttons") or "[]"
    job.text = snap.get("text") or ""
    job.parse_mode = snap.get("parse_mode") or "HTML"
    job.media_type = snap.get("media_type") or "none"
    job.media_file_id = snap.get("media_file_id") or ""
    return job


async def _send_spam_preview(chat_id: int, job) -> None:
    from bot.services.spammer import preview_spam_job

    await bot.send_message(chat_id, "👁 <b>Предпросмотр поста</b>", parse_mode="HTML")
    ok = await preview_spam_job(job, chat_id)
    if not ok:
        await bot.send_message(chat_id, "Не смог показать предпросмотр. Проверь пост и кнопки.")


async def _ask_spam_timing(message: Message, state: FSMContext, snap: dict, extra_rows: list) -> None:
    if extra_rows:
        _apply_spam_buttons(snap, extra_rows)
    else:
        snap.setdefault("extra_buttons", "[]")
    await state.update_data(spam_snap=snap)
    await state.set_state(AdminSG.wait_spam_timing)
    data = await state.get_data()
    await _send_spam_preview(
        message.chat.id,
        _job_like(
            kind=data.get("spam_kind") or "post",
            contest_id=data.get("spam_contest_id"),
            channel_id=data.get("spam_channel") or "",
            snap=snap,
        ),
    )
    note = "Кнопки на месте ✅\n\n" if extra_rows else ""
    await message.answer(note + t.ADM_SPAM_ASK_TIMING, parse_mode="HTML")


@router.message(AdminSG.wait_spam_post, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_spam_post(message: Message, state: FSMContext) -> None:
    snap = _post_snapshot(message)
    snap["copy_chat_id"] = message.chat.id
    snap["copy_message_id"] = message.message_id
    extra_rows = await _capture_post_buttons(message)
    if extra_rows:
        await _ask_spam_timing(message, state, snap, extra_rows)
        return
    await state.update_data(spam_snap=snap)
    await state.set_state(AdminSG.wait_spam_buttons)
    hint = t.ADM_SPAM_ASK_BUTTONS
    if not is_forwarded_message(message):
        hint = "Кнопок на посте нет. " + hint
    await message.answer(hint, parse_mode="HTML")


@router.message(AdminSG.wait_spam_buttons, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_spam_buttons(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    snap = data.get("spam_snap") or {}
    raw = (message.text or "").strip().lower()
    extra_rows: list = dump_inline_markup(message)
    if not extra_rows and raw not in ("-", "нет", "без", "0"):
        extra_rows = _buttons_to_rows(_parse_constructor_buttons(message))
        if not extra_rows:
            await message.answer(
                "Не разобрал. Строка: <code>Забрать | https://t.me/...</code>\nИли «-» без кнопок.",
                parse_mode="HTML",
            )
            return
        for row in extra_rows:
            for btn in row:
                btn.setdefault("style", "danger")
    elif raw in ("-", "нет", "без", "0"):
        extra_rows = []
    await _ask_spam_timing(message, state, snap, extra_rows)


@router.message(AdminSG.wait_spam_timing, F.text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_spam_timing(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await message.answer("Нужны два числа через пробел. Пример: <code>3600 300</code>", parse_mode="HTML")
        return
    life = max(1, int(parts[0]))
    pause = max(0, int(parts[1]))
    data = await state.get_data()
    channel_id = data.get("spam_channel") or ""
    kind = data.get("spam_kind") or "post"
    if not channel_id:
        await state.clear()
        await message.answer("Канал потерялся. Открой спамер заново.")
        return
    snap = data.get("spam_snap") or {}
    contest_id = data.get("spam_contest_id")
    title = "Конкурс"
    if kind == "contest" and contest_id:
        async with SessionLocal() as session:
            ct = await session.get(Contest, int(contest_id))
            title = f"Конкурс {ct.code}" if ct else "Конкурс"
    elif snap:
        title = str(snap.get("title") or "Пост")[:80]
    await state.clear()
    from bot.services.spammer import join_rotation

    async with SessionLocal() as session:
        start_at = await join_rotation(session, channel_id)
        job = SpamJob(
            kind="contest" if kind == "contest" else "post",
            title=title,
            channel_id=channel_id,
            lifetime_seconds=life,
            pause_seconds=pause,
            is_active=True,
            copy_chat_id=snap.get("copy_chat_id"),
            copy_message_id=snap.get("copy_message_id"),
            extra_buttons=snap.get("extra_buttons") or "[]",
            text=snap.get("text") or "",
            parse_mode=snap.get("parse_mode") or "HTML",
            media_type=snap.get("media_type") or "none",
            media_file_id=snap.get("media_file_id") or "",
            contest_id=int(contest_id) if kind == "contest" and contest_id else None,
            phase="idle",
            next_at=start_at,
        )
        session.add(job)
        await session.commit()
    await message.answer(
        t.ADM_SPAM_SAVED.format(life=life, pause=pause),
        parse_mode="HTML",
        reply_markup=admin_root_kb(),
    )


@router.callback_query(F.data.startswith("adm:spam:tog:"))
async def adm_spam_tog(callback: CallbackQuery) -> None:
    from bot.services.spammer import join_rotation, release_rotation

    jid = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        job = await session.get(SpamJob, jid)
        if not job:
            await callback.answer("Нет")
            return
        if job.is_active:
            job.is_active = False
            await release_rotation(session, job, after_pause=job.phase == "live")
        else:
            job.is_active = True
            job.phase = "idle"
            job.last_message_id = None
            job.next_at = await join_rotation(session, job.channel_id, exclude_id=job.id)
        await session.commit()
    text, kb = await _spam_view()
    await _edit_html(callback, text, kb)


@router.callback_query(F.data.startswith("adm:spam:del:"))
async def adm_spam_del(callback: CallbackQuery) -> None:
    from bot.services.spammer import release_rotation

    jid = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        job = await session.get(SpamJob, jid)
        if job:
            job.is_active = False
            await release_rotation(session, job, after_pause=job.phase == "live")
            await session.delete(job)
            await session.commit()
    await callback.answer("Удалён")
    text, kb = await _spam_view()
    await _edit_html(callback, text, kb)


async def _greet_view(channel_pk: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    from bot.services.greetings import get_or_create_greeting, list_greeting_posts, packed_greeting_posts

    async with SessionLocal() as session:
        channels = list((await session.execute(select(SavedChannel).order_by(desc(SavedChannel.id)))).scalars().all())
        current = None
        if channel_pk:
            current = await session.get(SavedChannel, channel_pk)
        if current is None and channels:
            current = channels[0]
        greet = await get_or_create_greeting(session, current.chat_id) if current else None
        posts = await list_greeting_posts(session, greet.id) if greet else []
        packed = await packed_greeting_posts(session, greet) if greet else []
        await session.commit()
        if greet:
            await session.refresh(greet)
    if not current:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📌 Добавить канал", callback_data="adm:ch:add")],
                [InlineKeyboardButton(text="⬅️ Админка", callback_data="adm:root")],
            ]
        )
        return (
            "👋 <b>Приветка</b>\n\nСначала добавь канал. Заявка в этот канал — бот пишет в личку.",
            kb,
        )
    mark = "🟢" if greet and greet.is_active else "⚪️"
    n_posts = len(packed)
    post_ok = f"{n_posts} шт. ✅" if n_posts else "нет"
    label = current.username or current.title or current.chat_id
    chain = []
    for i, post in enumerate(posts, 1):
        wait = max(0, int(post.delay_after_seconds or 0))
        if i < len(posts):
            chain.append(f"{i} → {wait}с")
        else:
            chain.append(str(i))
    chain_line = " → ".join(chain) if chain else "пусто"
    text = (
        f"👋 <b>Приветка</b>\n\n"
        f"Канал: <b>{label}</b>\n"
        f"Статус: {mark} {'вкл' if greet and greet.is_active else 'выкл'}\n"
        f"Текст: {greet.greet_text if greet else t.GREET_DEFAULT}\n"
        f"Посты после ответа: <b>{post_ok}</b>\n"
        f"Цепочка: <b>{chain_line}</b>\n\n"
        "Юзер кидает заявку → бот пишет текст → любой ответ → посты по очереди.\n"
        "КД ставится <b>после каждого поста</b>: сколько ждать до следующего."
    )
    kb_rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="⏸ Выкл" if greet and greet.is_active else "▶️ Вкл",
                callback_data=f"adm:greet:tog:{current.id}",
            ),
            InlineKeyboardButton(text="✏️ Текст", callback_data=f"adm:greet:txt:{current.id}"),
        ],
        [InlineKeyboardButton(text="➕ Пост", callback_data=f"adm:greet:post:{current.id}")],
    ]
    for i, post in enumerate(posts[:10], 1):
        wait = max(0, int(post.delay_after_seconds or 0))
        kb_rows.append(
            [
                InlineKeyboardButton(text=f"👁 {i}", callback_data=f"adm:greet:pv:{current.id}:{post.id}"),
                InlineKeyboardButton(text=f"⏱ {wait}с", callback_data=f"adm:greet:kd:{current.id}:{post.id}"),
                InlineKeyboardButton(text="🗑", callback_data=f"adm:greet:rm:{current.id}:{post.id}"),
            ]
        )
    kb_rows.append([InlineKeyboardButton(text="📊 Стата", callback_data=f"adm:greet:st:day:{current.id}")])
    if len(channels) > 1:
        for ch in channels[:8]:
            name = ch.username or ch.title or ch.chat_id
            prefix = "• " if ch.id == current.id else ""
            kb_rows.append([InlineKeyboardButton(text=f"{prefix}{name[:28]}", callback_data=f"adm:greet:{ch.id}")])
    kb_rows.append([InlineKeyboardButton(text="📌 Каналы", callback_data="adm:ch")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Админка", callback_data="adm:root")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data == "adm:greet")
async def adm_greet(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _greet_view()
    await _edit_html(callback, text, kb)


@router.callback_query(F.data.regexp(r"^adm:greet:\d+$"))
async def adm_greet_ch(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _greet_view(int(callback.data.split(":")[-1]))
    await _edit_html(callback, text, kb)


@router.callback_query(F.data.startswith("adm:greet:tog:"))
async def adm_greet_tog(callback: CallbackQuery) -> None:
    from bot.services.greetings import get_or_create_greeting, packed_greeting_posts

    pk = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        ch = await session.get(SavedChannel, pk)
        if not ch:
            await callback.answer("Нет канала", show_alert=True)
            return
        greet = await get_or_create_greeting(session, ch.chat_id)
        posts = await packed_greeting_posts(session, greet)
        if not greet.is_active and not posts:
            await session.commit()
            await callback.answer("Сначала пришли пост", show_alert=True)
            return
        greet.is_active = not greet.is_active
        await session.commit()
    text, kb = await _greet_view(pk)
    await _edit_html(callback, text, kb)


@router.callback_query(F.data.startswith("adm:greet:txt:"))
async def adm_greet_txt(callback: CallbackQuery, state: FSMContext) -> None:
    pk = int(callback.data.split(":")[-1])
    await state.update_data(greet_channel_pk=pk)
    await state.set_state(AdminSG.wait_greet_text)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_GREET_ASK_TEXT, parse_mode="HTML")


@router.message(AdminSG.wait_greet_text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_greet_text_msg(message: Message, state: FSMContext) -> None:
    from bot.services.greetings import get_or_create_greeting

    raw = (message.html_text or message.text or "").strip()
    if not raw:
        await message.answer("Нужен текст.")
        return
    data = await state.get_data()
    pk = int(data.get("greet_channel_pk") or 0)
    await state.clear()
    async with SessionLocal() as session:
        ch = await session.get(SavedChannel, pk)
        if ch:
            greet = await get_or_create_greeting(session, ch.chat_id)
            greet.greet_text = raw
            await session.commit()
    text, kb = await _greet_view(pk)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.regexp(r"^adm:greet:kd:\d+:\d+$"))
async def adm_greet_kd_ask(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    pk = int(parts[3])
    pid = int(parts[4])
    await state.update_data(greet_channel_pk=pk, greet_post_id=pid)
    await state.set_state(AdminSG.wait_greet_kd)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_GREET_ASK_KD, parse_mode="HTML")


def _parse_greet_delay(raw: str) -> int | None:
    text = (raw or "").strip().lower()
    if text in ("0", "сразу", "-", "нет"):
        return 0
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    return max(0, min(int(digits), 86400))


@router.message(AdminSG.wait_greet_kd, F.text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_greet_kd_msg(message: Message, state: FSMContext) -> None:
    delay = _parse_greet_delay(message.text or "")
    if delay is None:
        await message.answer("Нужно число в секундах. Пример: <code>15</code>", parse_mode="HTML")
        return
    data = await state.get_data()
    pk = int(data.get("greet_channel_pk") or 0)
    pid = int(data.get("greet_post_id") or 0)
    await state.clear()
    async with SessionLocal() as session:
        post = await session.get(GreetingPost, pid) if pid else None
        if post:
            post.delay_after_seconds = delay
            await session.commit()
    text, kb = await _greet_view(pk)
    await message.answer(f"После этого поста жду <b>{delay}</b> сек ✅", parse_mode="HTML")
    await _send_html(message, text, kb)


@router.callback_query(F.data.regexp(r"^adm:greet:pv:\d+:\d+$"))
async def adm_greet_preview(callback: CallbackQuery) -> None:
    from bot.services.greetings import send_greeting_payload

    parts = callback.data.split(":")
    pk = int(parts[3])
    pid = int(parts[4])
    async with SessionLocal() as session:
        post = await session.get(GreetingPost, pid)
        if not post:
            await callback.answer("Пост не найден", show_alert=True)
            return
        payload = {
            "copy_chat_id": post.copy_chat_id,
            "copy_message_id": post.copy_message_id,
            "extra_buttons": post.extra_buttons or "[]",
            "text": post.text or "",
            "parse_mode": post.parse_mode or "HTML",
            "media_type": post.media_type or "none",
            "media_file_id": post.media_file_id or "",
        }
    await callback.answer()
    ok = await send_greeting_payload(callback.from_user.id, payload)
    if not ok:
        await bot.send_message(callback.from_user.id, "Не смог показать предпросмотр.")


@router.callback_query(F.data.regexp(r"^adm:greet:rm:\d+:\d+$"))
async def adm_greet_rm(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    pk = int(parts[3])
    pid = int(parts[4])
    async with SessionLocal() as session:
        post = await session.get(GreetingPost, pid)
        if post:
            await session.delete(post)
            await session.commit()
    await callback.answer("Удалён")
    text, kb = await _greet_view(pk)
    await _edit_html(callback, text, kb)


@router.callback_query(F.data.startswith("adm:greet:post:"))
async def adm_greet_post_ask(callback: CallbackQuery, state: FSMContext) -> None:
    pk = int(callback.data.split(":")[-1])
    await state.update_data(greet_channel_pk=pk)
    await state.set_state(AdminSG.wait_greet_post)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_GREET_ASK_POST, parse_mode="HTML")


async def _save_greet_snap(pk: int, snap: dict) -> int | None:
    from bot.services.greetings import get_or_create_greeting, list_greeting_posts
    from database.crud import utcnow

    async with SessionLocal() as session:
        ch = await session.get(SavedChannel, pk)
        if not ch:
            return None
        greet = await get_or_create_greeting(session, ch.chat_id)
        posts = await list_greeting_posts(session, greet.id)
        same = None
        for row in posts:
            if snap.get("copy_chat_id") and row.copy_chat_id == snap.get("copy_chat_id") and row.copy_message_id == snap.get("copy_message_id"):
                same = row
                break
        delay = snap.get("delay_after_seconds")
        default_delay = max(0, int(greet.post_delay_seconds or 5))
        if same is None:
            order = (posts[-1].sort_order + 1) if posts else 0
            post = GreetingPost(
                greeting_id=greet.id,
                sort_order=order,
                copy_chat_id=snap.get("copy_chat_id"),
                copy_message_id=snap.get("copy_message_id"),
                extra_buttons=snap.get("extra_buttons") or "[]",
                text=snap.get("text") or "",
                parse_mode=snap.get("parse_mode") or "HTML",
                media_type=snap.get("media_type") or "none",
                media_file_id=snap.get("media_file_id") or "",
                delay_after_seconds=int(delay) if delay is not None else default_delay,
            )
            session.add(post)
            await session.flush()
            pid = post.id
        else:
            same.extra_buttons = snap.get("extra_buttons") or same.extra_buttons or "[]"
            if delay is not None:
                same.delay_after_seconds = int(delay)
            pid = same.id
        greet.copy_chat_id = snap.get("copy_chat_id")
        greet.copy_message_id = snap.get("copy_message_id")
        greet.extra_buttons = snap.get("extra_buttons") or "[]"
        greet.text = snap.get("text") or ""
        greet.parse_mode = snap.get("parse_mode") or "HTML"
        greet.media_type = snap.get("media_type") or "none"
        greet.media_file_id = snap.get("media_file_id") or ""
        greet.updated_at = utcnow()
        await session.commit()
        return pid


async def _ask_greet_post_kd(message: Message, state: FSMContext, pk: int, post_id: int) -> None:
    await state.set_state(AdminSG.wait_greet_kd)
    await state.update_data(greet_channel_pk=pk, greet_post_id=post_id)
    await message.answer("Пост добавлен ✅")
    await message.answer(t.ADM_GREET_ASK_KD, parse_mode="HTML")


@router.message(AdminSG.wait_greet_post, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_greet_post_msg(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    pk = int(data.get("greet_channel_pk") or 0)
    snap = _post_snapshot(message)
    snap["copy_chat_id"] = message.chat.id
    snap["copy_message_id"] = message.message_id
    extra_rows = await _capture_post_buttons(message)
    if extra_rows:
        _apply_spam_buttons(snap, extra_rows)
        pid = await _save_greet_snap(pk, snap)
        if pid:
            await _ask_greet_post_kd(message, state, pk, pid)
        else:
            await state.clear()
            await message.answer("Не смог сохранить пост.")
        return
    await state.update_data(greet_snap=snap, greet_channel_pk=pk)
    await state.set_state(AdminSG.wait_greet_buttons)
    await message.answer(t.ADM_SPAM_ASK_BUTTONS, parse_mode="HTML")


@router.message(AdminSG.wait_greet_buttons, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_greet_buttons_msg(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    snap = data.get("greet_snap") or {}
    pk = int(data.get("greet_channel_pk") or 0)
    raw = (message.text or "").strip().lower()
    extra_rows = dump_inline_markup(message)
    if not extra_rows and raw not in ("-", "нет", "без", "0"):
        extra_rows = _buttons_to_rows(_parse_constructor_buttons(message))
        if not extra_rows:
            await message.answer(
                "Не разобрал. <code>Забрать | https://t.me/...</code> или «-».",
                parse_mode="HTML",
            )
            return
        for row in extra_rows:
            for btn in row:
                btn.setdefault("style", "danger")
    if extra_rows:
        _apply_spam_buttons(snap, extra_rows)
    pid = await _save_greet_snap(pk, snap)
    if pid:
        await _ask_greet_post_kd(message, state, pk, pid)
        return
    await state.clear()
    await message.answer("Не смог сохранить пост.")


def _greet_stats_kb(period: str, pk: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=("• День" if period == "day" else "День"), callback_data=f"adm:greet:st:day:{pk}"),
                InlineKeyboardButton(text=("• Неделя" if period == "week" else "Неделя"), callback_data=f"adm:greet:st:week:{pk}"),
                InlineKeyboardButton(text=("• Месяц" if period == "month" else "Месяц"), callback_data=f"adm:greet:st:month:{pk}"),
            ],
            [InlineKeyboardButton(text="⬅️ Приветка", callback_data=f"adm:greet:{pk}")],
        ]
    )


@router.callback_query(F.data.regexp(r"^adm:greet:st:(day|week|month):\d+$"))
async def adm_greet_stats(callback: CallbackQuery) -> None:
    from bot.services.greetings import format_greeting_stats, greeting_stats

    parts = callback.data.split(":")
    period = parts[3]
    pk = int(parts[4])
    async with SessionLocal() as session:
        ch = await session.get(SavedChannel, pk)
        data = await greeting_stats(session, period, ch.chat_id if ch else None)
    await _edit_html(callback, format_greeting_stats(data), _greet_stats_kb(period, pk))


async def _namer_view(channel_pk: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    from bot.services.namer import format_namer, get_or_create_namer

    async with SessionLocal() as session:
        channels = list((await session.execute(select(SavedChannel).order_by(desc(SavedChannel.id)))).scalars().all())
        current = None
        if channel_pk:
            current = await session.get(SavedChannel, channel_pk)
        if current is None and channels:
            current = channels[0]
        namer = await get_or_create_namer(session, current.chat_id) if current else None
        await session.commit()
        if namer:
            await session.refresh(namer)
    if not current:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📌 Добавить канал", callback_data="adm:ch:add")],
                [InlineKeyboardButton(text="⬅️ Админка", callback_data="adm:root")],
            ]
        )
        return "🏷 <b>Имена канала</b>\n\nСначала добавь канал.", kb
    text = format_namer(namer, current)
    kb_rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="⏹ Стоп" if namer and namer.is_active else "▶️ Старт",
                callback_data=f"adm:namer:run:{current.id}",
            ),
        ],
        [
            InlineKeyboardButton(text="📋 Имена", callback_data=f"adm:namer:names:{current.id}"),
            InlineKeyboardButton(
                text=f"⏱ {namer.interval_seconds if namer else 10}с",
                callback_data=f"adm:namer:int:{current.id}",
            ),
        ],
    ]
    if len(channels) > 1:
        for ch in channels[:8]:
            name = ch.username or ch.title or ch.chat_id
            prefix = "• " if ch.id == current.id else ""
            kb_rows.append([InlineKeyboardButton(text=f"{prefix}{name[:28]}", callback_data=f"adm:namer:{ch.id}")])
    kb_rows.append([InlineKeyboardButton(text="📌 Каналы", callback_data="adm:ch")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Админка", callback_data="adm:root")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data == "adm:namer")
async def adm_namer(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _namer_view()
    await _edit_html(callback, text, kb)


@router.callback_query(F.data.regexp(r"^adm:namer:\d+$"))
async def adm_namer_ch(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _namer_view(int(callback.data.split(":")[-1]))
    await _edit_html(callback, text, kb)


@router.callback_query(F.data.startswith("adm:namer:run:"))
async def adm_namer_run(callback: CallbackQuery) -> None:
    from bot.services.namer import load_names, start_namer, stop_namer

    pk = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        ch = await session.get(SavedChannel, pk)
        if not ch:
            await callback.answer("Нет канала", show_alert=True)
            return
        namer = (
            await session.execute(select(ChannelNamer).where(ChannelNamer.channel_id == ch.chat_id))
        ).scalar_one_or_none()
        active = bool(namer and namer.is_active)
        names = load_names(namer.names) if namer else []
        chat_id = ch.chat_id
    if active:
        err = await stop_namer(chat_id)
        if err not in (None, "ok"):
            await callback.answer(f"Стоп, но имя не вернул: {err}"[:180], show_alert=True)
        else:
            await callback.answer("Стоп. Вернул прежнее имя")
    else:
        if not names:
            await callback.answer("Сначала загрузи имена", show_alert=True)
            return
        err = await start_namer(chat_id)
        if err == "no_names":
            await callback.answer("Сначала загрузи имена", show_alert=True)
            return
        await callback.answer("Кручу названия")
    text, kb = await _namer_view(pk)
    await _edit_html(callback, text, kb)


@router.callback_query(F.data.startswith("adm:namer:names:"))
async def adm_namer_names_ask(callback: CallbackQuery, state: FSMContext) -> None:
    pk = int(callback.data.split(":")[-1])
    await state.update_data(namer_channel_pk=pk)
    await state.set_state(AdminSG.wait_namer_names)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_NAMER_ASK_NAMES, parse_mode="HTML")


@router.message(AdminSG.wait_namer_names, F.text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_namer_names_msg(message: Message, state: FSMContext) -> None:
    from bot.services.namer import dump_names, get_or_create_namer, parse_names

    names = parse_names(message.text or "")
    if not names:
        await message.answer("Не вижу названий. Каждое с новой строки.")
        return
    data = await state.get_data()
    pk = int(data.get("namer_channel_pk") or 0)
    await state.clear()
    async with SessionLocal() as session:
        ch = await session.get(SavedChannel, pk)
        if ch:
            namer = await get_or_create_namer(session, ch.chat_id)
            namer.names = dump_names(names)
            namer.name_index = 0
            await session.commit()
    text, kb = await _namer_view(pk)
    await message.answer(f"Загрузил {len(names)} имён ✅")
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("adm:namer:int:"))
async def adm_namer_int_ask(callback: CallbackQuery, state: FSMContext) -> None:
    pk = int(callback.data.split(":")[-1])
    await state.update_data(namer_channel_pk=pk)
    await state.set_state(AdminSG.wait_namer_interval)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_NAMER_ASK_INTERVAL, parse_mode="HTML")


@router.message(AdminSG.wait_namer_interval, F.text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_namer_int_msg(message: Message, state: FSMContext) -> None:
    from bot.services.namer import get_or_create_namer

    raw = (message.text or "").strip().split()[0] if (message.text or "").strip() else ""
    if not raw.isdigit():
        await message.answer("Нужно число в секундах. Пример: <code>10</code>", parse_mode="HTML")
        return
    delay = max(5, min(int(raw), 86400))
    data = await state.get_data()
    pk = int(data.get("namer_channel_pk") or 0)
    await state.clear()
    async with SessionLocal() as session:
        ch = await session.get(SavedChannel, pk)
        if ch:
            namer = await get_or_create_namer(session, ch.chat_id)
            namer.interval_seconds = delay
            await session.commit()
    text, kb = await _namer_view(pk)
    await message.answer(f"Интервал {delay} сек ✅")
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
