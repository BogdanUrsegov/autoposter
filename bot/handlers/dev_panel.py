"""Раздел «Разработчик» — доступен админам из ADMIN_IDS.

Позволяет прямо из Telegram: выгрузить архив с кодом, залить обновление,
доставить зависимости, перезапустить сервис, посмотреть и скачать логи.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.handlers.admin import IsAdmin
from bot.utils.devops import (
    LOG_FILE,
    TMP_DIR,
    apply_update_archive,
    build_project_archive,
    clear_log_file,
    install_requirements,
    latest_backup,
    read_logs,
    restart_service,
    restore_backup,
    service_status,
    systemd_available,
)

logger = logging.getLogger(__name__)

router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

MAX_UPLOAD_MB = 20


class DeployState(StatesGroup):
    wait_archive = State()


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


def _deploy_kb() -> InlineKeyboardMarkup:
    return _kb(
        [
            [("📦 Скачать код проекта", "dev_download")],
            [("⬆️ Залить обновление", "dev_upload")],
            [("📥 Установить зависимости", "dev_pip")],
            [("🔄 Перезапустить бота", "dev_restart_ask")],
            [("📄 Логи", "dev_logs"), ("↩️ Откатить", "dev_rollback_ask")],
            [("ℹ️ Статус", "dev_status")],
            [("◀️ В админ-панель", "adm:root")],
        ]
    )


def _back_kb() -> InlineKeyboardMarkup:
    return _kb([[("◀️ К разработчику", "adm:dev")]])


def _logs_back_kb() -> InlineKeyboardMarkup:
    return _kb([[("🔄 Обновить", "dev_log_tail:50"), ("◀️ К логам", "dev_logs")]])


async def _show_menu(target: Message, edit: bool = False) -> None:
    text = (
        "🛠 <b>Разработчик</b>\n\n"
        "Управление кодом бота прямо с телефона:\n"
        "• 📦 выгрузить текущие исходники\n"
        "• ⬆️ залить zip с обновлением (перезапишет только код)\n"
        "• 🔄 перезапустить сервис\n"
        "• 📄 посмотреть и выгрузить логи\n\n"
        "<i>Секреты (.env), venv, база и логи обновлением не затрагиваются.</i>\n"
        f"Режим: <code>{'systemd' if systemd_available() else 'локальный процесс'}</code>"
    )
    if edit:
        await target.edit_text(text, reply_markup=_deploy_kb(), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=_deploy_kb(), parse_mode="HTML")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _to_thread(func, *args):
    return await asyncio.to_thread(func, *args)


# --------------------------------------------------------------------------- #
# Меню                                                                         #
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "adm:dev")
async def deploy_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.message:
        await _show_menu(callback.message, edit=True)
    await callback.answer()


@router.callback_query(F.data == "dev_status")
async def deploy_status(callback: CallbackQuery):
    if callback.message:
        await callback.message.edit_text(
            f"ℹ️ <b>Статус</b>\n\n<code>{_esc(service_status())}</code>",
            reply_markup=_back_kb(),
            parse_mode="HTML",
        )
    await callback.answer()


# --------------------------------------------------------------------------- #
# Выгрузка кода                                                                #
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "dev_download")
async def deploy_download(callback: CallbackQuery):
    await callback.answer("Собираю архив…")
    archive: Path | None = None
    try:
        archive, count = await _to_thread(build_project_archive)
        size_kb = archive.stat().st_size // 1024
        if callback.message:
            await callback.message.answer_document(
                FSInputFile(archive),
                caption=(
                    f"📦 <b>Исходники проекта</b>\n"
                    f"Файлов: {count} · {size_kb} КБ\n"
                    f"{datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
                    f"<i>Без .env, venv, логов, кэша и бэкапов.</i>"
                ),
                parse_mode="HTML",
            )
    except Exception as e:  # noqa: BLE001
        logger.exception("Не удалось собрать архив")
        if callback.message:
            await callback.message.answer(f"❌ Ошибка сборки архива: <code>{_esc(str(e))}</code>", parse_mode="HTML")
    finally:
        if archive and archive.exists():
            archive.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Загрузка обновления                                                          #
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "dev_upload")
async def deploy_upload_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(DeployState.wait_archive)
    if callback.message:
        await callback.message.edit_text(
            "⬆️ <b>Загрузка обновления</b>\n\n"
            "Пришлите <b>zip-архив</b> с проектом (внутри должна быть папка <code>bot/</code>).\n\n"
            "Что произойдёт:\n"
            "1. Текущий код уйдёт в бэкап (можно откатить)\n"
            "2. Файлы из архива перезапишут код\n"
            "3. <code>.env</code>, venv, логи, база — не тронуты\n"
            "4. Покажу список изменённых файлов и предложу рестарт\n\n"
            f"<i>Лимит Telegram — {MAX_UPLOAD_MB} МБ.</i>",
            reply_markup=_kb([[("✖️ Отмена", "dev_cancel")]]),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "dev_cancel")
async def deploy_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.message:
        await _show_menu(callback.message, edit=True)
    await callback.answer("Отменено")


@router.message(DeployState.wait_archive, F.document)
async def deploy_receive_archive(message: Message, state: FSMContext):
    doc = message.document
    name = (doc.file_name or "update.zip").lower()
    if not name.endswith(".zip"):
        await message.answer("❌ Нужен именно <b>.zip</b>. Пришлите архив ещё раз.", parse_mode="HTML")
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_MB * 1024 * 1024:
        await message.answer(f"❌ Архив больше {MAX_UPLOAD_MB} МБ — Telegram не отдаст его боту.")
        return

    await state.clear()
    status = await message.answer("⏳ Скачиваю архив…")

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    local = TMP_DIR / f"upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    try:
        await message.bot.download(doc, destination=local)
    except Exception as e:  # noqa: BLE001
        logger.exception("Скачивание обновления не удалось")
        await status.edit_text(f"❌ Не удалось скачать файл: <code>{_esc(str(e))}</code>", parse_mode="HTML")
        return

    await status.edit_text("⏳ Применяю обновление…")
    try:
        result = await _to_thread(apply_update_archive, local)
    except Exception as e:  # noqa: BLE001
        logger.exception("Применение обновления упало")
        await status.edit_text(f"❌ Ошибка обновления: <code>{_esc(str(e))}</code>", parse_mode="HTML")
        return
    finally:
        local.unlink(missing_ok=True)

    if not result.ok:
        await status.edit_text(f"❌ Обновление отклонено: {_esc(result.error)}", reply_markup=_back_kb())
        return

    changed = result.updated + result.added
    preview = "\n".join(f"• <code>{p}</code>" for p in changed[:25])
    if len(changed) > 25:
        preview += f"\n• …и ещё {len(changed) - 25}"

    rows: list[list[tuple[str, str]]] = []
    if result.requirements_changed:
        rows.append([("📥 Установить зависимости", "dev_pip")])
    rows.append([("🔄 Перезапустить сейчас", "dev_restart")])
    rows.append([("↩️ Откатить", "dev_rollback_ask")])
    rows.append([("◀️ К разработчику", "adm:dev")])

    text = (
        f"✅ <b>Обновление применено</b>\n\n"
        f"🔁 Изменено: <b>{len(result.updated)}</b>\n"
        f"🆕 Добавлено: <b>{len(result.added)}</b>\n"
        f"⏸ Без изменений: {result.unchanged}\n"
        f"🚫 Пропущено (секреты/мусор): {len(result.skipped)}\n"
    )
    if result.backup:
        text += f"💾 Бэкап: <code>{result.backup.name}</code>\n"
    if result.requirements_changed:
        text += "\n⚠️ <b>requirements.txt изменён</b> — поставьте зависимости до рестарта.\n"
    if preview:
        text += f"\n<b>Файлы:</b>\n{preview}\n"
    if not changed:
        text += "\n<i>Код совпадает с архивом — менять нечего.</i>\n"
    text += "\n<i>Изменения подхватятся после перезапуска.</i>"

    await status.edit_text(text[:4000], reply_markup=_kb(rows), parse_mode="HTML")


@router.message(DeployState.wait_archive)
async def deploy_wrong_input(message: Message):
    await message.answer("📎 Пришлите zip-архив файлом (не текстом и не фото).")


# --------------------------------------------------------------------------- #
# Зависимости                                                                  #
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "dev_pip")
async def deploy_pip(callback: CallbackQuery):
    await callback.answer("Запускаю pip install…")
    msg = await callback.message.answer("⏳ <code>pip install -r requirements.txt</code>…", parse_mode="HTML") if callback.message else None
    ok, output = await _to_thread(install_requirements)
    head = "✅ Зависимости установлены" if ok else "❌ Ошибка установки"
    tail = output[-1200:]
    if msg:
        await msg.edit_text(f"{head}\n\n<pre>{_esc(tail)}</pre>", reply_markup=_back_kb(), parse_mode="HTML")


# --------------------------------------------------------------------------- #
# Рестарт                                                                      #
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "dev_restart_ask")
async def deploy_restart_ask(callback: CallbackQuery):
    if callback.message:
        await callback.message.edit_text(
            "🔄 <b>Перезапуск бота</b>\n\n"
            "Бот отключится на несколько секунд и поднимется с новым кодом.\n"
            "Незавершённые диалоги (FSM) сбросятся.\n\nПродолжить?",
            reply_markup=_kb([[("✅ Да, перезапустить", "dev_restart")], [("✖️ Отмена", "adm:dev")]]),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "dev_restart")
async def deploy_restart(callback: CallbackQuery):
    ok, how = restart_service()
    if callback.message:
        if ok:
            await callback.message.answer(
                f"🔄 Перезапускаюсь…\n<i>{_esc(how)}</i>\n\nНапишите /admin через 10–15 секунд.",
                parse_mode="HTML",
            )
        else:
            await callback.message.answer(f"❌ Не удалось перезапустить: {_esc(how)}")
    await callback.answer("Перезапуск запущен" if ok else "Ошибка", show_alert=not ok)


# --------------------------------------------------------------------------- #
# Откат                                                                        #
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "dev_rollback_ask")
async def deploy_rollback_ask(callback: CallbackQuery):
    backup = latest_backup()
    if not backup:
        await callback.answer("Бэкапов кода пока нет", show_alert=True)
        return
    stamp = datetime.fromtimestamp(backup.stat().st_mtime).strftime("%d.%m.%Y %H:%M")
    if callback.message:
        await callback.message.edit_text(
            f"↩️ <b>Откат к бэкапу</b>\n\n"
            f"Файл: <code>{backup.name}</code>\n"
            f"Создан: {stamp}\n\n"
            f"Код вернётся к состоянию до последнего обновления. Продолжить?",
            reply_markup=_kb(
                [
                    [("✅ Откатить", "dev_rollback")],
                    [("📦 Скачать этот бэкап", "dev_backup_get")],
                    [("✖️ Отмена", "adm:dev")],
                ]
            ),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "dev_backup_get")
async def deploy_backup_get(callback: CallbackQuery):
    backup = latest_backup()
    if not backup:
        await callback.answer("Бэкапов нет", show_alert=True)
        return
    await callback.answer("Отправляю…")
    if callback.message:
        await callback.message.answer_document(FSInputFile(backup), caption="💾 Бэкап кода")


@router.callback_query(F.data == "dev_rollback")
async def deploy_rollback(callback: CallbackQuery):
    backup = latest_backup()
    if not backup:
        await callback.answer("Бэкапов нет", show_alert=True)
        return
    await callback.answer("Откатываю…")
    result = await _to_thread(restore_backup, backup)
    if not result.ok:
        if callback.message:
            await callback.message.answer(f"❌ Откат не удался: {_esc(result.error)}")
        return
    if callback.message:
        await callback.message.answer(
            f"↩️ <b>Откат выполнен</b>\n\n"
            f"Возвращено файлов: {result.total_changed}\n"
            f"Источник: <code>{backup.name}</code>\n\n"
            f"<i>Нужен перезапуск.</i>",
            reply_markup=_kb([[("🔄 Перезапустить", "dev_restart")], [("◀️ К разработчику", "adm:dev")]]),
            parse_mode="HTML",
        )


# --------------------------------------------------------------------------- #
# Логи                                                                         #
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "dev_logs")
async def deploy_logs_menu(callback: CallbackQuery):
    src = "journalctl (systemd)" if systemd_available() else "logs/bot.log"
    if callback.message:
        await callback.message.edit_text(
            f"📄 <b>Логи</b>\n\nИсточник: <code>{src}</code>\n\n"
            "Выгрузка приходит текстовым файлом — удобно листать и искать.",
            reply_markup=_kb(
                [
                    [("👁 Последние 50 строк", "dev_log_tail:50")],
                    [("📥 500", "dev_log_file:500"), ("📥 2000", "dev_log_file:2000"), ("📥 10000", "dev_log_file:10000")],
                    [("⚠️ Только ошибки (300)", "dev_log_err:300")],
                    [("📄 Файл logs/bot.log", "dev_log_raw")],
                    [("🧹 Очистить bot.log", "dev_log_clear")],
                    [("◀️ К разработчику", "adm:dev")],
                ]
            ),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("dev_log_tail:"))
async def deploy_log_tail(callback: CallbackQuery):
    await callback.answer()
    lines = int(callback.data.split(":")[1])
    text, source = await _to_thread(read_logs, lines, False, "auto")
    if not callback.message:
        return
    if not text:
        await callback.message.edit_text(
            f"📄 Логи пусты · <code>{source}</code>", reply_markup=_logs_back_kb(), parse_mode="HTML"
        )
        return
    body = _esc(text)[-3500:]
    await callback.message.edit_text(
        f"📄 <b>Последние {lines} строк</b> · <code>{source}</code>\n\n<pre>{body}</pre>",
        reply_markup=_logs_back_kb(),
        parse_mode="HTML",
    )


async def _send_log_file(callback: CallbackQuery, lines: int, errors_only: bool) -> None:
    text, source = await _to_thread(read_logs, lines, errors_only, "auto")
    if not text:
        await callback.answer(f"Ничего не найдено ({source})", show_alert=True)
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = "errors" if errors_only else "log"
    filename = f"autoposter_{prefix}_{stamp}.txt"
    if callback.message:
        await callback.message.answer_document(
            BufferedInputFile(text.encode("utf-8", errors="replace"), filename=filename),
            caption=(
                f"📄 <b>Логи</b> · <code>{source}</code>\n"
                f"Строк: {len(text.splitlines())}\n"
                f"{datetime.now().strftime('%d.%m.%Y %H:%M')}"
            ),
            parse_mode="HTML",
        )
    await callback.answer("Отправлено")


@router.callback_query(F.data.startswith("dev_log_file:"))
async def deploy_log_file(callback: CallbackQuery):
    await callback.answer("Готовлю файл…")
    await _send_log_file(callback, int(callback.data.split(":")[1]), errors_only=False)


@router.callback_query(F.data.startswith("dev_log_err:"))
async def deploy_log_errors(callback: CallbackQuery):
    await callback.answer("Ищу ошибки…")
    await _send_log_file(callback, int(callback.data.split(":")[1]), errors_only=True)


@router.callback_query(F.data == "dev_log_raw")
async def deploy_log_raw(callback: CallbackQuery):
    if not LOG_FILE.exists() or LOG_FILE.stat().st_size == 0:
        await callback.answer("Файл logs/bot.log пуст или не создан", show_alert=True)
        return
    size_kb = LOG_FILE.stat().st_size // 1024
    if size_kb > 45 * 1024:
        await callback.answer("Файл слишком большой — используйте выгрузку по строкам", show_alert=True)
        return
    await callback.answer("Отправляю…")
    if callback.message:
        await callback.message.answer_document(FSInputFile(LOG_FILE), caption=f"📄 logs/bot.log · {size_kb} КБ")


@router.callback_query(F.data == "dev_log_clear")
async def deploy_log_clear(callback: CallbackQuery):
    ok, info = clear_log_file()
    await callback.answer(("✅ " if ok else "❌ ") + info, show_alert=True)
