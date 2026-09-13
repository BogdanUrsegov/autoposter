from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import texts as t
from bot.keyboards import clicker_kb
from bot.loader import bot
from bot.services.emoji import apply_text
from bot.services.menu import send_main_menu
from database import SessionLocal
from database.crud import (
    count_referrals,
    get_setting,
    get_user_by_tg,
    ref_income,
    referrals_earned_total,
)

router = Router()


async def bot_username() -> str:
    me = await bot.get_me()
    return me.username or "bot"


def ref_link(username: str, tg_id: int) -> str:
    return f"https://t.me/{username}?start=r{tg_id}"


async def show_cabinet(event: Message | CallbackQuery) -> None:
    user_tg = event.from_user
    if not user_tg:
        return
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, user_tg.id)
        if not user or user.blocked:
            return
        refs = await count_referrals(session, user.id)
        income = await ref_income(session, user.id)
        refs_earned = await referrals_earned_total(session, user.id)
        stored = await get_setting(session, "bot_username", "")
    username = stored or await bot_username()
    text = t.CABINET.format(
        balance=f"{Decimal(user.balance):.4f}",
        clicks=user.clicks_total,
        earned=f"{Decimal(user.total_earned):.4f}",
        withdrawn=f"{Decimal(user.total_withdrawn):.4f}",
        refs=refs,
        ref_income=f"{Decimal(income):.4f}",
        refs_earned=f"{Decimal(refs_earned):.4f}",
    )
    if isinstance(event, CallbackQuery) and event.message:
        await event.message.answer(apply_text(text), parse_mode="HTML", reply_markup=clicker_kb())
        await event.answer()
    elif isinstance(event, Message):
        await event.answer(apply_text(text), parse_mode="HTML", reply_markup=clicker_kb())


async def show_referral(event: Message | CallbackQuery) -> None:
    user_tg = event.from_user
    if not user_tg:
        return
    async with SessionLocal() as session:
        user = await get_user_by_tg(session, user_tg.id)
        if not user or user.blocked:
            return
        refs = await count_referrals(session, user.id)
        income = await ref_income(session, user.id)
        refs_earned = await referrals_earned_total(session, user.id)
        bonus = await get_setting(session, "ref_bonus", "5")
        threshold = await get_setting(session, "ref_threshold", "10")
        percent = await get_setting(session, "ref_percent", "5")
        stored = await get_setting(session, "bot_username", "")
    username = stored or await bot_username()
    text = t.REF_TEXT.format(
        link=ref_link(username, user.tg_id),
        bonus=bonus,
        threshold=threshold,
        percent=percent,
        refs=refs,
        ref_income=f"{Decimal(income):.4f}",
        refs_earned=f"{Decimal(refs_earned):.4f}",
    )
    if isinstance(event, CallbackQuery) and event.message:
        await event.message.answer(apply_text(text), parse_mode="HTML", reply_markup=clicker_kb())
        await event.answer()
    elif isinstance(event, Message):
        await event.answer(apply_text(text), parse_mode="HTML", reply_markup=clicker_kb())


@router.message(F.text == t.BTN_BACK)
@router.callback_query(F.data == "go:menu")
async def back_menu(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await send_main_menu(event)


@router.callback_query(F.data == "go:refresh")
async def refresh_menu(event: CallbackQuery) -> None:
    await send_main_menu(event, edit=True)
    try:
        await event.answer(t.MENU_REFRESHED)
    except Exception:
        pass


@router.message(F.text == t.BTN_CABINET)
@router.callback_query(F.data == "go:cabinet")
async def cabinet(event: Message | CallbackQuery) -> None:
    await show_cabinet(event)


@router.message(F.text == t.BTN_REF)
@router.callback_query(F.data == "go:ref")
async def referral(event: Message | CallbackQuery) -> None:
    await show_referral(event)
