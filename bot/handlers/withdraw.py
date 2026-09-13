from __future__ import annotations

from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from bot import texts as t
from bot.keyboards import pay_check_kb, user_menu, withdraw_channels_kb, withdraw_check_kb
from bot.services.menu import send_main_menu
from bot.states import WithdrawSG
from database import SessionLocal
from bot.services.tasks import chat_target
from database.crud import count_referrals, get_settings_map, get_user_by_tg, subscribe_channels
from database.models import Payment, Withdrawal

router = Router()


def _int(value: str, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _dec(value: str, default: str) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return Decimal(default)


async def _open_wd(user_id: int, amount: Decimal | None = None) -> Withdrawal | None:
    from sqlalchemy import select

    async with SessionLocal() as session:
        stmt = select(Withdrawal).where(
            Withdrawal.user_id == user_id,
            Withdrawal.status.in_(("wait_friends", "wait_subscribe", "wait_invoice", "pending_review")),
        )
        return (await session.execute(stmt)).scalars().first()


def _reply_target(event: Message | CallbackQuery) -> Message | None:
    if isinstance(event, CallbackQuery):
        return event.message
    return event


@router.message(F.text == t.BTN_WITHDRAW)
@router.callback_query(F.data == "go:withdraw")
async def withdraw_start(event: Message | CallbackQuery, state: FSMContext) -> None:
    user_tg = event.from_user
    if not user_tg:
        return
    target = _reply_target(event)
    if not target:
        return
    if isinstance(event, CallbackQuery):
        await event.answer()
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, user_tg.id)
        if not user or user.blocked:
            return
        cfg = await get_settings_map(session)
        existing = await _open_wd(user.id)
        kb = user_menu(user_tg.id)
        if existing and existing.status == "pending_review":
            await target.answer(t.WD_ALREADY, reply_markup=kb)
            return
        if existing:
            await _continue_withdraw(target, existing, user, cfg)
            return
        minimum = _dec(cfg.get("withdraw_min", "100"), "100")
        await state.set_state(WithdrawSG.amount)
        await target.answer(
            t.WD_INTRO.format(balance=f"{Decimal(user.balance):.2f}", minimum=f"{minimum:.0f}"),
            parse_mode="HTML",
            reply_markup=kb,
        )


@router.message(WithdrawSG.amount, F.text == t.BTN_BACK)
async def withdraw_cancel_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_main_menu(message)


@router.message(WithdrawSG.amount)
async def withdraw_amount(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.text:
        return
    raw = message.text.replace(",", ".").strip()
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, message.from_user.id)
        cfg = await get_settings_map(session)
        minimum = _dec(cfg.get("withdraw_min", "100"), "100")
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            await message.answer(t.WD_BAD_AMOUNT)
            return
        if amount != amount.to_integral_value() or amount < minimum:
            await message.answer(t.WD_TOO_SMALL.format(minimum=f"{minimum:.0f}"))
            return
        if amount > Decimal(user.balance):
            await message.answer(t.WD_TOO_BIG.format(balance=f"{Decimal(user.balance):.2f}"))
            return
        wd = Withdrawal(user_id=user.id, amount=amount, status="wait_friends")
        session.add(wd)
        await session.commit()
        await session.refresh(wd)
        await state.clear()
        await _continue_withdraw(message, wd, user, cfg)


async def _continue_withdraw(target: Message, wd: Withdrawal, user, cfg: dict) -> None:
    from bot.handlers.menu import bot_username, ref_link

    need = _int(cfg.get("withdraw_friends", "3"), 3)
    async with SessionLocal() as session:
        have = await count_referrals(session, user.id)
        stored = await get_setting_safe(session)
        channels = await subscribe_channels(session)
    username = stored or await bot_username()
    link = ref_link(username, user.tg_id)

    if wd.status == "wait_friends" and have < need:
        await target.answer(
            t.WD_FRIENDS.format(amount=f"{Decimal(wd.amount):.0f}", need=need, have=have, link=link),
            parse_mode="HTML",
            reply_markup=withdraw_check_kb("friends"),
        )
        return
    if wd.status == "wait_friends":
        async with SessionLocal() as session:
            row = await session.get(Withdrawal, wd.id)
            if row:
                row.status = "wait_subscribe"
                await session.commit()
                wd.status = "wait_subscribe"

    if wd.status == "wait_subscribe":
        if not channels:
            await target.answer(t.WD_NO_CHANNELS, reply_markup=user_menu(user.tg_id))
            return
        await target.answer(
            t.WD_SUBSCRIBE,
            reply_markup=withdraw_channels_kb(channels, "subscribe"),
        )
        return

    stars = _int(cfg.get("withdraw_check_stars", "3"), 3)
    await target.answer(
        t.WD_INVOICE.format(stars=stars),
        parse_mode="HTML",
        reply_markup=pay_check_kb(),
    )


async def get_setting_safe(session) -> str:
    from database.crud import get_setting

    return await get_setting(session, "bot_username", "")


@router.callback_query(F.data == "wd:cancel")
async def wd_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.from_user:
        async with SessionLocal() as session:
            user = await get_user_by_tg(session, callback.from_user.id)
            if user:
                from sqlalchemy import select

                rows = (
                    await session.execute(
                        select(Withdrawal).where(
                            Withdrawal.user_id == user.id,
                            Withdrawal.status.in_(("wait_friends", "wait_subscribe", "wait_invoice")),
                        )
                    )
                ).scalars().all()
                for row in rows:
                    await session.delete(row)
                await session.commit()
    await callback.answer("🤍 Ок")
    if callback.message:
        await send_main_menu(callback)


@router.callback_query(F.data.startswith("wd:check:"))
async def wd_check(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    kind = callback.data.split(":")[-1]
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, callback.from_user.id)
        if not user:
            return
        cfg = await get_settings_map(session)
        from sqlalchemy import select

        wd = (
            await session.execute(
                select(Withdrawal).where(
                    Withdrawal.user_id == user.id,
                    Withdrawal.status.in_(("wait_friends", "wait_subscribe", "wait_invoice")),
                )
            )
        ).scalars().first()
        if not wd:
            await callback.answer("👀 Запрос не найден", show_alert=True)
            return
        if kind == "friends":
            have = await count_referrals(session, user.id)
            need = _int(cfg.get("withdraw_friends", "3"), 3)
            if have < need:
                await callback.answer(f"Ещё не хватает: {need - have}", show_alert=True)
                return
            wd.status = "wait_subscribe"
            await session.commit()
            channels = await subscribe_channels(session)
            await callback.answer("💚 Друзья на месте")
            if not channels:
                await callback.message.answer(t.WD_NO_CHANNELS, reply_markup=user_menu(user.tg_id))
                return
            await callback.message.answer(
                t.WD_SUBSCRIBE,
                reply_markup=withdraw_channels_kb(channels, "subscribe"),
            )
            return
        if kind == "subscribe":
            channels = await subscribe_channels(session)
            ok = False
            for offer in channels:
                try:
                    member = await callback.bot.get_chat_member(chat_target(offer.chat_id), user.tg_id)
                    if member.status in ("member", "administrator", "creator", "restricted"):
                        ok = True
                        break
                except TelegramBadRequest:
                    continue
                except Exception:
                    continue
            if not ok:
                await callback.answer("👀 Ещё не заглянул. Открой любой канал и жми «Готово».", show_alert=True)
                return
            wd.status = "wait_invoice"
            await session.commit()
            stars = _int(cfg.get("withdraw_check_stars", "3"), 3)
            await callback.answer("💚 Есть!")
            await callback.message.answer(
                t.WD_INVOICE.format(stars=stars),
                parse_mode="HTML",
                reply_markup=pay_check_kb(),
            )


@router.callback_query(F.data == "wd:invoice")
async def wd_invoice(callback: CallbackQuery) -> None:
    if not callback.from_user:
        return
    async with SessionLocal() as session:
        cfg = await get_settings_map(session)
        user = await get_user_by_tg(session, callback.from_user.id)
        from sqlalchemy import select

        wd = (
            await session.execute(
                select(Withdrawal).where(
                    Withdrawal.user_id == user.id,
                    Withdrawal.status.in_(("wait_invoice", "wait_subscribe", "wait_friends")),
                )
            )
        ).scalars().first()
        if not wd:
            await callback.answer("👀 Запрос не найден", show_alert=True)
            return
        wd.status = "wait_invoice"
        await session.commit()
        stars = _int(cfg.get("withdraw_check_stars", "3"), 3)
        wid = wd.id
    await callback.answer()
    await callback.bot.send_invoice(
        chat_id=callback.from_user.id,
        title="✨ Три звёздочки",
        description="Маленькая проверка, что ты настоящий",
        payload=f"wdcheck:{wid}",
        currency="XTR",
        prices=[LabeledPrice(label="Звёздочки", amount=stars)],
        provider_token="",
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    if not message.from_user or not message.successful_payment:
        return
    pay = message.successful_payment
    payload = pay.invoice_payload or ""
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, message.from_user.id)
        if not user:
            return
        session.add(
            Payment(
                user_id=user.id,
                amount=pay.total_amount,
                payload=payload,
                telegram_charge_id=pay.telegram_payment_charge_id,
            )
        )
        if payload.startswith("wdcheck:"):
            wid = int(payload.split(":")[1])
            wd = await session.get(Withdrawal, wid)
            if wd and wd.user_id == user.id:
                amount = Decimal(wd.amount)
                if Decimal(user.balance) >= amount:
                    user.balance = Decimal(user.balance) - amount
                    user.total_withdrawn = Decimal(user.total_withdrawn) + amount
                    wd.status = "pending_review"
                    wd.invoice_charge_id = pay.telegram_payment_charge_id
                    await session.commit()
                    await message.answer(t.WD_DONE.format(amount=f"{amount:.0f}"), parse_mode="HTML")
                    from config import settings

                    for admin_id in settings.admin_id_list:
                        try:
                            await message.bot.send_message(
                                admin_id,
                                f"Новая заявка на вывод #{wd.id}\n"
                                f"Пользователь: {user.tg_id} @{user.username or '—'}\n"
                                f"Сумма: {amount:.0f} ⭐",
                            )
                        except Exception:
                            pass
                    return
        await session.commit()
        await message.answer("✨ Получили, спасибо!")
