from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from bot import texts as t
from bot.keyboards import contest_op_kb, contest_status_kb
from bot.services.contests import check_contest_op, join_own_contest, send_contest_card
from bot.services.emoji import apply_text

router = Router()


@router.callback_query(F.data.startswith("contest:check:"))
async def contest_check(callback: CallbackQuery) -> None:
    if not callback.from_user:
        return
    try:
        contest_id = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    kind, payload = await check_contest_op(callback.from_user.id, contest_id)
    if kind == "gone":
        await callback.answer(t.CONTEST_GONE, show_alert=True)
        return
    if kind == "ended":
        await callback.answer(t.CONTEST_ENDED, show_alert=True)
        contest = payload
        if callback.message and contest:
            await send_contest_card(callback.message.chat.id, contest, extra_text=t.CONTEST_ENDED)
        return
    if kind == "left":
        contest, buttons = payload
        await callback.answer(t.CONTEST_NOT_DONE, show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=contest_op_kb(contest.id, buttons))
            except TelegramBadRequest:
                await send_contest_card(
                    callback.message.chat.id,
                    contest,
                    reply_markup=contest_op_kb(contest.id, buttons),
                    extra_text=t.CONTEST_OP_LEFT,
                )
        return
    contest, count = payload
    text = t.CONTEST_ALREADY.format(count=count) if kind == "already" else t.CONTEST_JOINED.format(count=count)
    await callback.answer("Готово! Ты в конкурсе 🎉" if kind != "already" else "Ты уже участвуешь")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            apply_text(text),
            parse_mode="HTML",
            reply_markup=contest_status_kb(),
        )


@router.callback_query(F.data.startswith("contest:join:"))
async def contest_join(callback: CallbackQuery) -> None:
    if not callback.from_user:
        return
    try:
        contest_id = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    kind, payload = await join_own_contest(callback.from_user.id, contest_id)
    if kind == "gone":
        await callback.answer(t.CONTEST_GONE, show_alert=True)
        return
    if kind == "ended":
        await callback.answer(t.CONTEST_ENDED, show_alert=True)
        return
    contest, count = payload
    text = t.CONTEST_ALREADY.format(count=count) if kind == "already" else t.CONTEST_JOINED.format(count=count)
    await callback.answer("Готово! Ты в конкурсе 🎉" if kind != "already" else "Ты уже участвуешь")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            apply_text(text),
            parse_mode="HTML",
            reply_markup=contest_status_kb(),
        )
