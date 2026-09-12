from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from bot.services.emoji import apply_text, dumps_map, extract_from_message, loads_map, set_map
from bot.services.userbot import (
    HELLO_DEFAULT,
    begin_login,
    create_account,
    delete_account,
    is_online,
    list_accounts,
    set_active,
    status_label,
    submit_code,
    submit_password,
    update_texts,
)
from bot.services.userbot_format import dumps_entities, entities_from_aiogram
from bot.services.userbot_stats import PERIOD_LABELS, PERIODS, collect_stats, format_stats_text, save_stats_chart
from bot.states import AdminSG
from bot.utils.envfile import (
    api_configured,
    api_status_text,
    apply_api_credentials,
    apply_userbot_timing,
    userbot_global_settings,
)
from config import settings
from database import SessionLocal
from database.crud import get_setting, set_setting
from database.models import UserBotAccount, UserBotLead

router = Router(name="userbot_admin")


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return bool(user and settings.is_admin(user.id))


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

SKIP = {"/admin", "/start"}


async def _merge_premium(message: Message) -> None:
    found = extract_from_message(message)
    if not found:
        return
    async with SessionLocal() as session:
        current = loads_map(await get_setting(session, "premium_emojis", "{}"))
        current.update(found)
        await set_setting(session, "premium_emojis", dumps_map(current))
        await session.commit()
        set_map(current)


async def _ub_list_view() -> tuple[str, InlineKeyboardMarkup]:
    rows = await list_accounts()
    g = userbot_global_settings()
    async with SessionLocal() as session:
        lead_counts = dict(
            (
                await session.execute(
                    select(UserBotLead.account_id, func.count()).group_by(UserBotLead.account_id)
                )
            ).all()
        )
    lines = [
        "🤖 <b>Юзер-бот</b>\n",
        "ЛС → привет → ответ/1ч → подарок → дожим 30м / 6ч / 24ч.\n",
        f"API: {api_status_text()}\n",
        f"⏱ SLA ответа: <b>{g['max_reply_sec']}</b> сек · пауза: <b>{g['min_send_gap']}</b> сек\n",
    ]
    if not api_configured():
        lines.append(
            "⚠️ Сначала задай <b>API_ID</b> и <b>API_HASH</b> "
            "(кнопка ниже, взять на my.telegram.org).\n"
        )
    if not rows:
        lines.append("Пока нет аккаунтов.")
    kb: list[list[InlineKeyboardButton]] = []
    for row in rows:
        uname = f"@{row.username}" if row.username else row.phone
        mark = "🟢" if is_online(row.id) else "⚪"
        n = int(lead_counts.get(row.id) or 0)
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {status_label(row)} · {uname} · {n}",
                    callback_data=f"adm:ub:v:{row.id}",
                )
            ]
        )
    kb.append(
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="adm:ub:stats"),
            InlineKeyboardButton(text="⚙️ Глобальные", callback_data="adm:ub:globals"),
        ]
    )
    kb.append([InlineKeyboardButton(text="🔐 API ID / HASH", callback_data="adm:ub:api")])
    if api_configured():
        kb.append([InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="adm:ub:add")])
    kb.append([InlineKeyboardButton(text="⬅️ Админка", callback_data="adm:root")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


async def _ub_card(account_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    async with SessionLocal() as session:
        row = await session.get(UserBotAccount, account_id)
        if not row:
            return None
        leads = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(UserBotLead)
                    .where(UserBotLead.account_id == account_id)
                )
            ).scalar()
            or 0
        )
        gifted = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(UserBotLead)
                    .where(
                        UserBotLead.account_id == account_id,
                        UserBotLead.stage.in_(("gifted", "done")),
                    )
                )
            ).scalar()
            or 0
        )
        def _short(raw: str, n: int = 120) -> str:
            t = (raw or "").strip()
            if len(t) > n:
                t = t[:n] + "…"
            return t or "<i>не задан</i>"

        gift_preview = _short(row.gift_text or "", 180)
        hello = apply_text(row.hello_text or HELLO_DEFAULT)
        online = "🟢 онлайн" if is_online(row.id) else f"⚪ {escape(status_label(row))}"
        uname = escape(row.username or "—")
        text = (
            f"🤖 <b>Юзер-бот #{row.id}</b>\n\n"
            f"Тел: <code>{escape(row.phone)}</code>\n"
            f"TG: <code>{row.tg_id or '—'}</code> @{uname}\n"
            f"Статус: {online}\n"
            f"Диалогов: <b>{leads}</b> · подарков: <b>{gifted}</b>\n"
            f"Лимит приветов/час: <b>{row.max_greets_hour or 1000}</b>\n"
            f"Привет: {hello}\n\n"
            f"<b>Подарок:</b>\n{gift_preview}\n\n"
            f"<b>Дожим 30м:</b> {_short(row.nudge_30_text)}\n"
            f"<b>Дожим 6ч:</b> {_short(row.nudge_6h_text)}\n"
            f"<b>Дожим 24ч:</b> {_short(row.nudge_24h_text)}\n"
        )
        if row.last_error:
            text += f"\n⚠️ <code>{escape(row.last_error[:200])}</code>\n"
        run_btn = "⏹ Стоп" if is_online(row.id) else "▶️ Старт"
        kb = [
            [
                InlineKeyboardButton(text=run_btn, callback_data=f"adm:ub:run:{row.id}"),
                InlineKeyboardButton(text="🔑 Войти (код)", callback_data=f"adm:ub:code:{row.id}"),
            ],
            [
                InlineKeyboardButton(text="✏️ Привет", callback_data=f"adm:ub:hello:{row.id}"),
                InlineKeyboardButton(text="🎁 Подарок", callback_data=f"adm:ub:gift:{row.id}"),
            ],
            [
                InlineKeyboardButton(text="⏰ 30м", callback_data=f"adm:ub:n30:{row.id}"),
                InlineKeyboardButton(text="⏰ 6ч", callback_data=f"adm:ub:n6:{row.id}"),
                InlineKeyboardButton(text="⏰ 24ч", callback_data=f"adm:ub:n24:{row.id}"),
            ],
            [
                InlineKeyboardButton(text="🔢 Лимит/час", callback_data=f"adm:ub:limit:{row.id}"),
                InlineKeyboardButton(text="📊 Стата", callback_data=f"adm:ub:stats:{row.id}"),
            ],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm:ub:del:{row.id}")],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="adm:ub")],
        ]
        return text, InlineKeyboardMarkup(inline_keyboard=kb)


async def _show(target: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    if isinstance(target, CallbackQuery) and target.message:
        try:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await target.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await target.answer()
        return
    if isinstance(target, Message):
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "adm:ub")
async def ub_root(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _ub_list_view()
    await _show(callback, text, kb)


@router.callback_query(F.data == "adm:ub:api")
async def ub_api_ask(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSG.wait_ub_api_id)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🔐 <b>API для юзер-бота</b>\n\n"
            "1. Открой <a href=\"https://my.telegram.org\">my.telegram.org</a> → "
            "<b>API development tools</b>\n"
            "2. Пришли сюда <b>api_id</b> (число)\n\n"
            f"Сейчас: {api_status_text()}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


@router.message(AdminSG.wait_ub_api_id, F.text)
async def ub_api_id_msg(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() in SKIP:
        return
    raw = (message.text or "").strip().replace(" ", "")
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(
            "Нужен числовой <b>api_id</b>, например <code>12345678</code>",
            parse_mode="HTML",
        )
        return
    await state.update_data(ub_api_id=int(raw))
    await state.set_state(AdminSG.wait_ub_api_hash)
    await message.answer(
        "Ок. Теперь пришли <b>api_hash</b> (строка с my.telegram.org).\n"
        "После сохранения сообщение с hash лучше удали.",
        parse_mode="HTML",
    )


@router.message(AdminSG.wait_ub_api_hash, F.text)
async def ub_api_hash_msg(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() in SKIP:
        return
    data = await state.get_data()
    api_id = int(data.get("ub_api_id") or 0)
    api_hash = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    try:
        apply_api_credentials(api_id, api_hash)
    except ValueError as exc:
        await message.answer(f"Не принято: <code>{escape(str(exc))}</code>", parse_mode="HTML")
        return
    await state.clear()
    await message.answer(
        f"✅ Сохранено в <code>.env</code> и применено сразу.\nAPI: {api_status_text()}",
        parse_mode="HTML",
    )
    text, kb = await _ub_list_view()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.regexp(r"^adm:ub:v:\d+$"))
async def ub_view(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    aid = int(callback.data.split(":")[-1])
    card = await _ub_card(aid)
    if not card:
        await callback.answer("Не найден", show_alert=True)
        return
    await _show(callback, *card)


@router.callback_query(F.data == "adm:ub:add")
async def ub_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not api_configured():
        await callback.answer("Сначала задай API ID / HASH", show_alert=True)
        return
    await state.set_state(AdminSG.wait_ub_phone)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Отправь номер в международном формате.\nПример: <code>+79001234567</code>",
            parse_mode="HTML",
        )


@router.message(AdminSG.wait_ub_phone, F.text)
async def ub_phone_msg(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() in SKIP:
        return
    try:
        row = await create_account(message.text or "")
    except ValueError as exc:
        await message.answer(f"<code>{escape(str(exc))}</code>", parse_mode="HTML")
        return
    try:
        await begin_login(row.id)
    except Exception as exc:
        await message.answer(
            f"Не удалось отправить код: <code>{escape(str(exc))}</code>",
            parse_mode="HTML",
        )
        await state.clear()
        text, kb = await _ub_list_view()
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return
    await state.update_data(ub_id=row.id)
    await state.set_state(AdminSG.wait_ub_code)
    await message.answer(
        f"Код отправлен на <code>{escape(row.phone)}</code>.\n"
        "Пришли код из Telegram / SMS.",
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^adm:ub:code:\d+$"))
async def ub_resend_code(callback: CallbackQuery, state: FSMContext) -> None:
    if not api_configured():
        await callback.answer("Сначала задай API ID / HASH", show_alert=True)
        return
    aid = int(callback.data.split(":")[-1])
    try:
        await begin_login(aid)
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    await state.update_data(ub_id=aid)
    await state.set_state(AdminSG.wait_ub_code)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Код отправлен. Пришли цифры из Telegram / SMS.")


@router.message(AdminSG.wait_ub_code, F.text)
async def ub_code_msg(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() in SKIP:
        return
    data = await state.get_data()
    aid = int(data.get("ub_id") or 0)
    if not aid:
        await state.clear()
        await message.answer("Сессия сброшена. Открой юзер-бота заново.")
        return
    try:
        result = await submit_code(aid, message.text or "")
    except Exception as exc:
        await message.answer(f"Ошибка кода: <code>{escape(str(exc))}</code>", parse_mode="HTML")
        return
    if (result or {}).get("status") == "wait_2fa":
        await state.set_state(AdminSG.wait_ub_2fa)
        await message.answer("Нужен облачный пароль 2FA. Пришли пароль (он не сохраняется).")
        return
    await state.clear()
    await message.answer("✅ Аккаунт вошёл и запущен.")
    card = await _ub_card(aid)
    if card:
        await message.answer(card[0], parse_mode="HTML", reply_markup=card[1])


@router.message(AdminSG.wait_ub_2fa, F.text)
async def ub_2fa_msg(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() in SKIP:
        return
    data = await state.get_data()
    aid = int(data.get("ub_id") or 0)
    if not aid:
        await state.clear()
        return
    password = message.text or ""
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await submit_password(aid, password)
    except Exception as exc:
        await message.answer(f"Ошибка 2FA: <code>{escape(str(exc))}</code>", parse_mode="HTML")
        return
    await state.clear()
    await message.answer("✅ 2FA ок, аккаунт онлайн.")
    card = await _ub_card(aid)
    if card:
        await message.answer(card[0], parse_mode="HTML", reply_markup=card[1])


@router.callback_query(F.data.regexp(r"^adm:ub:run:\d+$"))
async def ub_run(callback: CallbackQuery) -> None:
    aid = int(callback.data.split(":")[-1])
    try:
        if is_online(aid):
            await set_active(aid, False)
            await callback.answer("Остановлен")
        else:
            await set_active(aid, True)
            await callback.answer("Запущен")
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    card = await _ub_card(aid)
    if card:
        await _show(callback, *card)


@router.callback_query(F.data.regexp(r"^adm:ub:hello:\d+$"))
async def ub_hello_ask(callback: CallbackQuery, state: FSMContext) -> None:
    aid = int(callback.data.split(":")[-1])
    await state.update_data(ub_id=aid)
    await state.set_state(AdminSG.wait_ub_hello)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Пришли новый текст приветствия (можно HTML / прем-эмодзи).")


@router.message(AdminSG.wait_ub_hello)
async def ub_hello_msg(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() in SKIP:
        return
    data = await state.get_data()
    aid = int(data.get("ub_id") or 0)
    if not aid:
        await state.clear()
        return
    await _merge_premium(message)
    text = (message.html_text or message.text or message.caption or "").strip() or HELLO_DEFAULT
    await update_texts(aid, hello_text=text)
    await state.clear()
    await message.answer("Привет сохранён.")
    card = await _ub_card(aid)
    if card:
        await message.answer(card[0], parse_mode="HTML", reply_markup=card[1])


@router.callback_query(F.data.regexp(r"^adm:ub:gift:\d+$"))
async def ub_gift_ask(callback: CallbackQuery, state: FSMContext) -> None:
    aid = int(callback.data.split(":")[-1])
    await state.update_data(ub_id=aid)
    await state.set_state(AdminSG.wait_ub_gift)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Пришли текст подарка с <b>жирным</b> и прем-эмодзи — сохраню как есть.\n"
            "Или HTML: <code>&lt;b&gt;…&lt;/b&gt;</code>, "
            "<code>&lt;tg-emoji emoji-id=\"ID\"&gt;🎁&lt;/tg-emoji&gt;</code>",
            parse_mode="HTML",
        )


@router.message(AdminSG.wait_ub_gift)
async def ub_gift_msg(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() in SKIP:
        return
    data = await state.get_data()
    aid = int(data.get("ub_id") or 0)
    if not aid:
        await state.clear()
        return
    await _merge_premium(message)
    plain, ents = entities_from_aiogram(message)
    if ents:
        await update_texts(
            aid,
            gift_text=plain,
            gift_parse_mode="HTML",
            gift_entities=dumps_entities(ents),
        )
    else:
        html = message.html_text or plain
        await update_texts(
            aid,
            gift_text=html,
            gift_parse_mode="HTML",
            gift_entities="[]",
        )
    await state.clear()
    await message.answer("🎁 Текст подарка сохранён.")
    card = await _ub_card(aid)
    if card:
        await message.answer(card[0], parse_mode="HTML", reply_markup=card[1])


@router.callback_query(F.data.regexp(r"^adm:ub:del:\d+$"))
async def ub_del(callback: CallbackQuery) -> None:
    aid = int(callback.data.split(":")[-1])
    await delete_account(aid)
    await callback.answer("Удалён")
    text, kb = await _ub_list_view()
    await _show(callback, text, kb)


def _stats_kb(period: str, account_id: int | None = None) -> InlineKeyboardMarkup:
    prefix = f"adm:ub:st:{account_id}:" if account_id else "adm:ub:st:0:"
    rows = [
        [
            InlineKeyboardButton(
                text=("• " if period == p else "") + PERIOD_LABELS[p],
                callback_data=f"{prefix}{p}",
            )
            for p in PERIODS[:3]
        ],
        [
            InlineKeyboardButton(
                text=("• " if period == p else "") + PERIOD_LABELS[p],
                callback_data=f"{prefix}{p}",
            )
            for p in PERIODS[3:]
        ],
        [
            InlineKeyboardButton(
                text="🖼 Графики",
                callback_data=f"adm:ub:chart:{account_id or 0}:{period}",
            )
        ],
    ]
    if account_id:
        rows.append([InlineKeyboardButton(text="⬅️ К аккаунту", callback_data=f"adm:ub:v:{account_id}")])
    else:
        rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data="adm:ub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "adm:ub:stats")
@router.callback_query(F.data.regexp(r"^adm:ub:stats:\d+$"))
async def ub_stats_open(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    account_id = int(parts[-1]) if len(parts) >= 4 and parts[-1].isdigit() else None
    if account_id == 0:
        account_id = None
    data = await collect_stats("day", account_id)
    text = format_stats_text(data)
    if account_id:
        text = f"Аккаунт #{account_id}\n" + text
    await _show(callback, text, _stats_kb("day", account_id))


@router.callback_query(F.data.regexp(r"^adm:ub:st:\d+:(hour|day|week|month|all)$"))
async def ub_stats_period(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    account_id = int(parts[3]) or None
    period = parts[4]
    data = await collect_stats(period, account_id)
    text = format_stats_text(data)
    if account_id:
        text = f"Аккаунт #{account_id}\n" + text
    await _show(callback, text, _stats_kb(period, account_id))


@router.callback_query(F.data.regexp(r"^adm:ub:chart:\d+:(hour|day|week|month|all)$"))
async def ub_stats_chart(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    account_id = int(parts[3]) or None
    period = parts[4]
    await callback.answer("Рисую…")
    try:
        data, path = await save_stats_chart(period, account_id)
        caption = format_stats_text(data)
        if account_id:
            caption = f"Аккаунт #{account_id}\n" + caption
        if callback.message:
            await callback.message.answer_photo(
                BufferedInputFile(path.read_bytes(), filename=path.name),
                caption=caption[:1024],
                parse_mode="HTML",
                reply_markup=_stats_kb(period, account_id),
            )
    except Exception as exc:
        if callback.message:
            await callback.message.answer(f"Не удалось построить график: <code>{escape(str(exc))}</code>", parse_mode="HTML")


@router.callback_query(F.data == "adm:ub:globals")
async def ub_globals(callback: CallbackQuery) -> None:
    g = userbot_global_settings()
    text = (
        "⚙️ <b>Глобальные настройки юзер-бота</b>\n\n"
        f"API: {api_status_text()}\n"
        f"Макс. ответ (SLA): <b>{g['max_reply_sec']}</b> сек (до 60)\n"
        f"Мин. пауза между отправками: <b>{g['min_send_gap']}</b> сек\n"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏱ SLA сек", callback_data="adm:ub:set:sla"),
                InlineKeyboardButton(text="⏸ Пауза сек", callback_data="adm:ub:set:gap"),
            ],
            [InlineKeyboardButton(text="🔐 API", callback_data="adm:ub:api")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:ub")],
        ]
    )
    await _show(callback, text, kb)


@router.callback_query(F.data == "adm:ub:set:sla")
async def ub_set_sla_ask(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSG.wait_ub_sla)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"Пришли максимум секунд на ответ (15–60).\nСейчас: <b>{settings.userbot_max_reply_sec}</b>",
            parse_mode="HTML",
        )


@router.message(AdminSG.wait_ub_sla, F.text)
async def ub_set_sla_msg(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() in SKIP:
        return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        val = int(float(raw))
    except ValueError:
        await message.answer("Нужно число, например <code>60</code>", parse_mode="HTML")
        return
    apply_userbot_timing(max_reply_sec=val)
    await state.clear()
    await message.answer(f"✅ SLA = <b>{settings.userbot_max_reply_sec}</b> сек", parse_mode="HTML")
    text, kb = await _ub_list_view()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "adm:ub:set:gap")
async def ub_set_gap_ask(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSG.wait_ub_gap)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"Пришли мин. паузу между отправками в секундах (0.5–30).\nСейчас: <b>{settings.userbot_min_send_gap}</b>",
            parse_mode="HTML",
        )


@router.message(AdminSG.wait_ub_gap, F.text)
async def ub_set_gap_msg(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() in SKIP:
        return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        val = float(raw)
    except ValueError:
        await message.answer("Нужно число, например <code>2</code> или <code>1.5</code>", parse_mode="HTML")
        return
    apply_userbot_timing(min_send_gap=val)
    await state.clear()
    await message.answer(f"✅ Пауза = <b>{settings.userbot_min_send_gap}</b> сек", parse_mode="HTML")
    text, kb = await _ub_list_view()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.regexp(r"^adm:ub:n30:\d+$"))
async def ub_n30_ask(callback: CallbackQuery, state: FSMContext) -> None:
    aid = int(callback.data.split(":")[-1])
    await state.update_data(ub_id=aid)
    await state.set_state(AdminSG.wait_ub_nudge30)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Пришли текст напоминания через 30 минут.")


@router.callback_query(F.data.regexp(r"^adm:ub:n6:\d+$"))
async def ub_n6_ask(callback: CallbackQuery, state: FSMContext) -> None:
    aid = int(callback.data.split(":")[-1])
    await state.update_data(ub_id=aid)
    await state.set_state(AdminSG.wait_ub_nudge6h)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Пришли текст напоминания через 6 часов.")


@router.callback_query(F.data.regexp(r"^adm:ub:n24:\d+$"))
async def ub_n24_ask(callback: CallbackQuery, state: FSMContext) -> None:
    aid = int(callback.data.split(":")[-1])
    await state.update_data(ub_id=aid)
    await state.set_state(AdminSG.wait_ub_nudge24h)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Пришли текст напоминания через 24 часа.")


async def _save_nudge(message: Message, state: FSMContext, field: str) -> None:
    if (message.text or "").strip() in SKIP:
        return
    data = await state.get_data()
    aid = int(data.get("ub_id") or 0)
    if not aid:
        await state.clear()
        return
    await _merge_premium(message)
    text = (message.html_text or message.text or message.caption or "").strip()
    await update_texts(aid, **{field: text})
    await state.clear()
    await message.answer("Сохранено.")
    card = await _ub_card(aid)
    if card:
        await message.answer(card[0], parse_mode="HTML", reply_markup=card[1])


@router.message(AdminSG.wait_ub_nudge30)
async def ub_n30_msg(message: Message, state: FSMContext) -> None:
    await _save_nudge(message, state, "nudge_30_text")


@router.message(AdminSG.wait_ub_nudge6h)
async def ub_n6_msg(message: Message, state: FSMContext) -> None:
    await _save_nudge(message, state, "nudge_6h_text")


@router.message(AdminSG.wait_ub_nudge24h)
async def ub_n24_msg(message: Message, state: FSMContext) -> None:
    await _save_nudge(message, state, "nudge_24h_text")


@router.callback_query(F.data.regexp(r"^adm:ub:limit:\d+$"))
async def ub_limit_ask(callback: CallbackQuery, state: FSMContext) -> None:
    aid = int(callback.data.split(":")[-1])
    await state.update_data(ub_id=aid)
    await state.set_state(AdminSG.wait_ub_limit)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Пришли лимит приветов в час (1–5000).")


@router.message(AdminSG.wait_ub_limit, F.text)
async def ub_limit_msg(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() in SKIP:
        return
    data = await state.get_data()
    aid = int(data.get("ub_id") or 0)
    if not aid:
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужно целое число.")
        return
    await update_texts(aid, max_greets_hour=int(raw))
    await state.clear()
    await message.answer("Лимит сохранён.")
    card = await _ub_card(aid)
    if card:
        await message.answer(card[0], parse_mode="HTML", reply_markup=card[1])
