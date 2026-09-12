from __future__ import annotations

import re
from datetime import timezone
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import desc, select

from bot.handlers.admin import (
    SKIP,
    IsAdmin,
    _apply_spam_buttons,
    _buttons_to_rows,
    _capture_post_buttons,
    _edit_html,
    _parse_constructor_buttons,
    _post_snapshot,
    _send_html,
)
from bot.keyboards import dump_inline_markup
from bot import texts as t
from bot.states import AdminSG
from bot.services.combo import (
    contest_ready,
    fmt_secs,
    list_baits,
    parse_combo_duration,
    save_bait_post,
    schedule_now,
    set_enabled,
)
from database import SessionLocal
from database.crud import utcnow
from database.models import AutoCombo, AutoComboPost, SavedChannel

router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

_SET_FIELDS = {
    "ss": ("spam_seconds", "Спам после связки"),
    "si": ("post_interval_seconds", "КД между байтами"),
    "sg": ("after_contest_seconds", "Пауза после конкурса"),
    "sl": ("bait_lifetime_seconds", "Пауза после последнего байта"),
}


def _strip_html(text: str, limit: int = 40) -> str:
    raw = re.sub(r"<[^>]+>", " ", text or "")
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:limit] if raw else ""


def _left(due) -> int:
    if due is None:
        return 0
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return max(0, int((due - utcnow()).total_seconds()))


def _phase_line(combo: AutoCombo) -> str:
    phase = combo.phase or "idle"
    left = _left(combo.next_at)
    if not combo.is_enabled and phase == "idle":
        return "выключен"
    if phase == "idle":
        if combo.next_at:
            return f"обычный спам ещё {fmt_secs(left)}"
        return "ждёт старт"
    if phase == "pre":
        return f"байты до конкурса · следующий через {fmt_secs(left)}"
    if phase == "contest":
        return "выкладываю конкурс"
    if phase == "gap":
        return f"конкурс в канале · байты через {fmt_secs(left)}"
    if phase == "after":
        return f"байты после конкурса · следующий через {fmt_secs(left)}"
    if phase == "tail":
        return f"удаление постов через {fmt_secs(left)}"
    return phase


def _end_label_combo(combo: AutoCombo) -> str:
    kind = combo.contest_end_type or "participants"
    if kind == "none":
        return "без итогов"
    if kind == "time":
        return f"по времени {fmt_secs(combo.contest_end_value)}"
    return f"по участникам {int(combo.contest_end_value or 0)}"


def _bait_title(post: AutoComboPost) -> str:
    title = _strip_html(post.title or post.text or "байт", 36) or "байт"
    return f"🎣 {title}"


async def _channel_label(channel_id: str) -> str:
    async with SessionLocal() as session:
        row = (
            await session.execute(select(SavedChannel).where(SavedChannel.chat_id == str(channel_id)))
        ).scalar_one_or_none()
    if row:
        return row.username or row.title or channel_id
    return channel_id


async def get_or_create_combo(channel_id: str, title: str = "") -> AutoCombo:
    async with SessionLocal() as session:
        row = (
            await session.execute(select(AutoCombo).where(AutoCombo.channel_id == channel_id))
        ).scalar_one_or_none()
        if row:
            return row
        row = AutoCombo(
            title=(title or "Авто-конкурс")[:128],
            channel_id=channel_id,
            is_enabled=False,
            spam_seconds=10800,
            post_interval_seconds=60,
            after_contest_seconds=180,
            bait_lifetime_seconds=60,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def _combo_list_view() -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        rows = list((await session.execute(select(AutoCombo).order_by(desc(AutoCombo.id)))).scalars().all())
        channels = {ch.chat_id: ch for ch in (await session.execute(select(SavedChannel))).scalars().all()}
    lines = [
        "🎯 <b>Авто-конкурс и байт</b>\n",
        "Связка: байты (КД 1 мин) → конкурс → 3 мин → байты → удалить всё → 3 часа обычного спама → снова связка.",
        "Пока связка идёт, спам этого канала выключен.",
    ]
    kb: list[list[InlineKeyboardButton]] = []
    if not rows:
        lines.append("\nПока нет ни одной связки. Выбери канал.")
    for item in rows:
        ch = channels.get(item.channel_id)
        label = (ch.username or ch.title or item.channel_id) if ch else item.channel_id
        mark = "🟢" if item.is_enabled else "⚪️"
        lines.append(f"{mark} {escape(str(label))} · {escape(_phase_line(item))}")
        kb.append([InlineKeyboardButton(text=f"{mark} {str(label)[:28]}", callback_data=f"adm:cmb:o:{item.id}")])
    kb.append([InlineKeyboardButton(text="➕ На канал", callback_data="adm:cmb:n")])
    kb.append(
        [
            InlineKeyboardButton(text="⬅️ Спамер", callback_data="adm:spam"),
            InlineKeyboardButton(text="⬅️ Админка", callback_data="adm:root"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


async def _combo_view(combo_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if not combo:
            return None
        pres = await list_baits(session, combo.id, "pre")
        afters = await list_baits(session, combo.id, "after")
    ch = await _channel_label(combo.channel_id)
    mark = "🟢 вкл" if combo.is_enabled else "⚪️ выкл"
    contest_line = "не задан"
    if contest_ready(combo):
        preview = escape(_strip_html(combo.contest_text or "", 40))
        contest_line = f"задан · {escape(_end_label_combo(combo))}"
        if preview:
            contest_line += f"\n    <i>{preview}</i>"
    lines = [
        "🎯 <b>Авто-конкурс и байт</b>",
        f"Канал: <b>{escape(str(ch))}</b>",
        f"Статус: {mark}",
        f"Сейчас: {escape(_phase_line(combo))}",
        "",
        f"⏱ КД между байтами: <b>{fmt_secs(combo.post_interval_seconds)}</b>",
        f"⏱ После конкурса: <b>{fmt_secs(combo.after_contest_seconds)}</b>",
        f"⏱ После последнего байта: <b>{fmt_secs(combo.bait_lifetime_seconds)}</b>",
        f"⏱ Обычный спам: <b>{fmt_secs(combo.spam_seconds)}</b>",
        "",
        "<b>1. Байты до конкурса</b>",
    ]
    if not pres:
        lines.append("пусто")
    else:
        for i, post in enumerate(pres, 1):
            lines.append(f"{i}. {escape(_bait_title(post))}")
    lines.append("")
    lines.append(f"<b>2. Конкурс</b> — {contest_line}")
    lines.append("")
    lines.append("<b>3. Байты после конкурса</b>")
    if not afters:
        lines.append("пусто")
    else:
        for i, post in enumerate(afters, 1):
            lines.append(f"{i}. {escape(_bait_title(post))}")
    toggle = "⏸ Выкл" if combo.is_enabled else "▶️ Вкл"
    kb: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text=toggle, callback_data=f"adm:cmb:t:{combo.id}"),
            InlineKeyboardButton(text="🚀 Сейчас", callback_data=f"adm:cmb:r:{combo.id}"),
        ],
        [
            InlineKeyboardButton(text="⏱ КД", callback_data=f"adm:cmb:si:{combo.id}"),
            InlineKeyboardButton(text="⏱ После конкурса", callback_data=f"adm:cmb:sg:{combo.id}"),
        ],
        [
            InlineKeyboardButton(text="⏱ После последнего", callback_data=f"adm:cmb:sl:{combo.id}"),
            InlineKeyboardButton(text="⏱ Спам", callback_data=f"adm:cmb:ss:{combo.id}"),
        ],
        [
            InlineKeyboardButton(text="🎣 До конкурса", callback_data=f"adm:cmb:ab:{combo.id}"),
            InlineKeyboardButton(text="🏆 Конкурс", callback_data=f"adm:cmb:ac:{combo.id}"),
        ],
        [InlineKeyboardButton(text="🎣 После конкурса", callback_data=f"adm:cmb:aa:{combo.id}")],
    ]
    for post in pres:
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 до · {_bait_title(post)}"[:32],
                    callback_data=f"adm:cmb:dp:{combo.id}:{post.id}",
                )
            ]
        )
    for post in afters:
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 после · {_bait_title(post)}"[:32],
                    callback_data=f"adm:cmb:dp:{combo.id}:{post.id}",
                )
            ]
        )
    kb.append([InlineKeyboardButton(text="🗑 Удалить связку", callback_data=f"adm:cmb:x:{combo.id}")])
    kb.append(
        [
            InlineKeyboardButton(text="⬅️ Список", callback_data="adm:cmb"),
            InlineKeyboardButton(text="⬅️ Админка", callback_data="adm:root"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


async def show_combo_list(target: CallbackQuery | Message) -> None:
    text, kb = await _combo_list_view()
    await _edit_html(target, text, kb)


async def show_combo(target: CallbackQuery | Message, combo_id: int) -> None:
    try:
        packed = await _combo_view(combo_id)
    except Exception:
        packed = None
    if not packed:
        if isinstance(target, CallbackQuery):
            try:
                await target.answer("Не смог открыть связку", show_alert=True)
            except Exception:
                pass
        await show_combo_list(target)
        return
    text, kb = packed
    await _edit_html(target, text, kb)


async def after_combo_channel(target: Message, state: FSMContext, channel_id: str) -> None:
    label = await _channel_label(channel_id)
    combo = await get_or_create_combo(channel_id, title=str(label)[:128])
    await state.clear()
    packed = await _combo_view(combo.id)
    if not packed:
        await target.answer("Не смог открыть связку.")
        return
    text, kb = packed
    await _send_html(target, text, kb)


async def _channel_pick_kb() -> InlineKeyboardMarkup:
    async with SessionLocal() as session:
        rows = list((await session.execute(select(SavedChannel).order_by(SavedChannel.id))).scalars().all())
    kb: list[list[InlineKeyboardButton]] = []
    for ch in rows:
        label = (ch.username or ch.title or ch.chat_id)[:32]
        kb.append([InlineKeyboardButton(text=label, callback_data=f"adm:cmb:ch:{ch.id}")])
    kb.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm:cmb:ch:new")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:cmb")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(F.data == "adm:cmb")
async def adm_combo_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_combo_list(callback)


@router.callback_query(F.data == "adm:cmb:n")
async def adm_combo_new(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Куда крутить авто-конкурс и байт?",
            reply_markup=await _channel_pick_kb(),
        )


@router.callback_query(F.data == "adm:cmb:ch:new")
async def adm_combo_channel_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(channel_next="combo")
    await state.set_state(AdminSG.wait_channel_add)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_CHANNEL_ASK, parse_mode="HTML")


@router.callback_query(F.data.startswith("adm:cmb:ch:"))
async def adm_combo_channel_pick(callback: CallbackQuery, state: FSMContext) -> None:
    raw = callback.data.split(":")[-1]
    if raw == "new":
        return
    try:
        pk = int(raw)
    except ValueError:
        await callback.answer()
        return
    async with SessionLocal() as session:
        row = await session.get(SavedChannel, pk)
    if not row:
        await callback.answer("Канал не найден", show_alert=True)
        return
    await callback.answer(row.username or row.title)
    if callback.message:
        await after_combo_channel(callback.message, state, row.chat_id)


@router.callback_query(F.data.startswith("adm:cmb:o:"))
async def adm_combo_open(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    combo_id = int(callback.data.split(":")[-1])
    await show_combo(callback, combo_id)


@router.callback_query(F.data.startswith("adm:cmb:t:"))
async def adm_combo_toggle(callback: CallbackQuery) -> None:
    combo_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        row = await session.get(AutoCombo, combo_id)
        enabled = bool(row.is_enabled) if row else False
    combo = await set_enabled(combo_id, not enabled)
    if not combo:
        await callback.answer("Нет", show_alert=True)
        return
    await callback.answer("Включил" if combo.is_enabled else "Выключил")
    await show_combo(callback, combo_id)


@router.callback_query(F.data.startswith("adm:cmb:r:"))
async def adm_combo_run(callback: CallbackQuery) -> None:
    combo_id = int(callback.data.split(":")[-1])
    result = await schedule_now(combo_id)
    notes = {
        "ok": "Стартую связку",
        "busy": "Уже идёт",
        "empty": "Сначала добавь байты или конкурс",
        "missing": "Не найдена",
    }
    await callback.answer(notes.get(result, result), show_alert=result != "ok")
    await show_combo(callback, combo_id)


@router.callback_query(F.data.startswith("adm:cmb:x:"))
async def adm_combo_delete(callback: CallbackQuery, state: FSMContext) -> None:
    combo_id = int(callback.data.split(":")[-1])
    await set_enabled(combo_id, False)
    async with SessionLocal() as session:
        row = await session.get(AutoCombo, combo_id)
        if row:
            await session.delete(row)
            await session.commit()
    await callback.answer("Удалена")
    await state.clear()
    await show_combo_list(callback)


@router.callback_query(F.data.regexp(r"^adm:cmb:s[sigl]:\d+$"))
async def adm_combo_ask_value(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    key = parts[2]
    combo_id = int(parts[3])
    prompts = {
        "ss": "Сколько крутить обычный спам после связки, потом снова байты+конкурс.\nПример: <code>3ч</code>.",
        "si": "КД между байтами. Посты копятся в канале.\nПример: <code>1м</code>.",
        "sg": "Пауза после конкурса, перед байтами после него.\nПример: <code>3м</code>.",
        "sl": "Сколько висит последний байт, потом удаляю все посты связки.\nПример: <code>1м</code>. <code>0</code> — сразу.",
    }
    await state.set_state(AdminSG.wait_combo_value)
    await state.update_data(combo_id=combo_id, combo_set=key)
    await callback.answer()
    if callback.message:
        await callback.message.answer(prompts[key], parse_mode="HTML")


@router.message(AdminSG.wait_combo_value, F.text, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_combo_set_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    combo_id = int(data.get("combo_id") or 0)
    key = data.get("combo_set")
    raw = (message.text or "").strip().lower()
    field_info = _SET_FIELDS.get(str(key) or "")
    if not field_info:
        await state.clear()
        return
    field, label = field_info
    if raw in ("0", "сразу", "-", "нет"):
        seconds = 0
    else:
        seconds = parse_combo_duration(message.text or "")
        if seconds is None:
            await message.answer("Не понял. Пример: <code>1м</code>, <code>3ч</code>.", parse_mode="HTML")
            return
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
        if combo:
            setattr(combo, field, seconds)
            combo.updated_at = utcnow()
            await session.commit()
    await state.clear()
    packed = await _combo_view(combo_id)
    if packed:
        text, kb = packed
        shown = "сразу" if seconds == 0 else fmt_secs(seconds)
        await message.answer(f"Ок, {label}: {shown}.")
        await _send_html(message, text, kb)


@router.callback_query(F.data.startswith("adm:cmb:ab:"))
@router.callback_query(F.data.startswith("adm:cmb:aa:"))
async def adm_combo_add_bait(callback: CallbackQuery, state: FSMContext) -> None:
    combo_id = int(callback.data.split(":")[-1])
    slot = "after" if ":aa:" in (callback.data or "") else "pre"
    await state.set_state(AdminSG.wait_combo_post)
    await state.update_data(combo_id=combo_id, combo_target=slot)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            t.ADM_COMBO_ASK_BAIT_AFTER if slot == "after" else t.ADM_COMBO_ASK_BAIT,
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("adm:cmb:ac:"))
async def adm_combo_add_contest(callback: CallbackQuery, state: FSMContext) -> None:
    combo_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        combo = await session.get(AutoCombo, combo_id)
    if not combo:
        await callback.answer("Нет", show_alert=True)
        return
    await state.set_state(AdminSG.wait_contest_text)
    await state.update_data(combo_id=combo.id, combo_channel=combo.channel_id)
    await callback.answer()
    if callback.message:
        await callback.message.answer(t.ADM_CONTEST_ASK_TEXT, parse_mode="HTML")


@router.message(AdminSG.wait_combo_post, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_combo_post_msg(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    combo_id = int(data.get("combo_id") or 0)
    slot = data.get("combo_target") or "pre"
    snap = _post_snapshot(message)
    snap["copy_chat_id"] = message.chat.id
    snap["copy_message_id"] = message.message_id
    extra_rows = await _capture_post_buttons(message)
    if extra_rows:
        _apply_spam_buttons(snap, extra_rows)
        post = await save_bait_post(combo_id, snap, slot=slot)
        await state.clear()
        if post:
            await message.answer("Запомнил байт.")
            packed = await _combo_view(combo_id)
            if packed:
                text, kb = packed
                await _send_html(message, text, kb)
        else:
            await message.answer("Не смог сохранить байт.")
        return
    await state.update_data(combo_snap=snap, combo_id=combo_id, combo_target=slot)
    await state.set_state(AdminSG.wait_combo_buttons)
    await message.answer(t.ADM_SPAM_ASK_BUTTONS, parse_mode="HTML")


@router.message(AdminSG.wait_combo_buttons, ~F.text.in_(SKIP | {t.BTN_ADMIN}))
async def adm_combo_buttons_msg(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    snap = data.get("combo_snap") or {}
    combo_id = int(data.get("combo_id") or 0)
    slot = data.get("combo_target") or "pre"
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
    post = await save_bait_post(combo_id, snap, slot=slot)
    await state.clear()
    if not post:
        await message.answer("Не смог сохранить байт.")
        return
    await message.answer("Запомнил байт.")
    packed = await _combo_view(combo_id)
    if packed:
        text, kb = packed
        await _send_html(message, text, kb)


@router.callback_query(F.data.startswith("adm:cmb:dp:"))
async def adm_combo_del_post(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    combo_id = int(parts[3])
    post_id = int(parts[4])
    async with SessionLocal() as session:
        row = await session.get(AutoComboPost, post_id)
        if row and row.combo_id == combo_id:
            await session.delete(row)
            await session.commit()
    await callback.answer("Удалил")
    await show_combo(callback, combo_id)
