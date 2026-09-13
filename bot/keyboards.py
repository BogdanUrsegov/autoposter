import json
from urllib.parse import quote

from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

from bot import texts as t
from bot.services.emoji import button_icon_and_text
from config import settings


def hide_reply() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def admin_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t.BTN_ADMIN)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def _btn(
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
    style: str | None = None,
    copy_text: str | None = None,
    web_app: WebAppInfo | None = None,
) -> InlineKeyboardButton:
    data: dict = {"text": text}
    icon, label = button_icon_and_text(text)
    if icon:
        data["text"] = label
        data["icon_custom_emoji_id"] = icon
    if callback_data:
        data["callback_data"] = callback_data
    if url:
        data["url"] = url
    if style:
        data["style"] = style
    if copy_text:
        data["copy_text"] = CopyTextButton(text=copy_text)
    if web_app:
        data["web_app"] = web_app
    return InlineKeyboardButton(**data)


def menu_kb(
    is_admin: bool = False,
    link: str | None = None,
) -> InlineKeyboardMarkup:
    share_url = None
    if link:
        share_url = (
            "https://t.me/share/url?url="
            + quote(link, safe="")
            + "&text="
            + quote(t.MENU_SHARE_TEXT, safe="")
        )
    rows: list[list[InlineKeyboardButton]] = [
        [
            _btn(t.BTN_REFRESH, callback_data="go:refresh"),
            _btn(t.BTN_WITHDRAW, callback_data="go:withdraw", style="success"),
        ],
        [_btn(t.BTN_CLICK, callback_data="go:click", style="primary")],
        [
            _btn(t.BTN_REF, copy_text=link, style="primary")
            if link
            else _btn(t.BTN_REF, callback_data="go:ref", style="primary"),
            _btn(t.BTN_CABINET, callback_data="go:cabinet", style="primary"),
        ],
    ]
    if share_url:
        rows.append([_btn(t.BTN_SHARE, url=share_url, style="danger")])
    if is_admin:
        url = (settings.webapp_url or "").strip()
        if url.startswith("https://"):
            rows.append([_btn(t.BTN_ADMIN, web_app=WebAppInfo(url=url))])
        else:
            rows.append([_btn(t.BTN_ADMIN, callback_data="go:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_menu(user_id: int | None = None, link: str | None = None) -> InlineKeyboardMarkup:
    return menu_kb(bool(user_id and settings.is_admin(user_id)), link=link)


def main_menu(is_admin: bool = False, link: str | None = None) -> InlineKeyboardMarkup:
    return menu_kb(is_admin, link=link)


def clicker_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(t.BTN_CLICK, callback_data="go:click", style="primary")],
            [_btn(t.BTN_BACK, callback_data="go:menu")],
        ]
    )


def _button_style(value) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    raw = str(raw).lower().strip()
    if raw in ("danger", "success", "primary"):
        return raw
    return {"1": "primary", "2": "success", "3": "danger"}.get(raw)


def dump_inline_markup(message) -> list[list[dict]]:
    markup = getattr(message, "reply_markup", None)
    keyboard = getattr(markup, "inline_keyboard", None) if markup else None
    if not keyboard:
        return []
    rows: list[list[dict]] = []
    for row in keyboard:
        items: list[dict] = []
        for btn in row:
            item: dict = {"text": btn.text or ""}
            if btn.url:
                item["url"] = btn.url
            if btn.callback_data:
                item["callback_data"] = btn.callback_data
            style = _button_style(getattr(btn, "style", None))
            if style:
                item["style"] = style
            icon = getattr(btn, "icon_custom_emoji_id", None)
            if icon:
                item["icon_custom_emoji_id"] = icon
            if btn.web_app:
                item["web_app"] = btn.web_app.url
            copy = getattr(btn, "copy_text", None)
            if copy and getattr(copy, "text", None):
                item["copy_text"] = copy.text
            if btn.switch_inline_query is not None:
                item["switch_inline_query"] = btn.switch_inline_query
            items.append(item)
        if items:
            rows.append(items)
    return rows


def _inline_button_from_dict(item: dict) -> InlineKeyboardButton | None:
    if not isinstance(item, dict):
        return None
    kwargs: dict = {"text": item.get("text") or "·"}
    if item.get("url"):
        kwargs["url"] = item["url"]
    elif item.get("callback_data"):
        kwargs["callback_data"] = item["callback_data"]
    elif item.get("web_app"):
        kwargs["web_app"] = WebAppInfo(url=item["web_app"])
    elif item.get("copy_text"):
        kwargs["copy_text"] = CopyTextButton(text=item["copy_text"])
    elif "switch_inline_query" in item:
        kwargs["switch_inline_query"] = item.get("switch_inline_query") or ""
    else:
        return None
    style = _button_style(item.get("style"))
    if style:
        kwargs["style"] = style
    if item.get("icon_custom_emoji_id"):
        kwargs["icon_custom_emoji_id"] = item["icon_custom_emoji_id"]
    return InlineKeyboardButton(**kwargs)


def load_inline_markup(raw) -> InlineKeyboardMarkup | None:
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not data or not isinstance(data, list):
        return None
    kb_rows: list[list[InlineKeyboardButton]] = []
    if data and isinstance(data[0], dict):
        data = [[item] for item in data if isinstance(item, dict)]
    for row in data:
        if not isinstance(row, list):
            continue
        kb_row: list[InlineKeyboardButton] = []
        for item in row:
            btn = _inline_button_from_dict(item)
            if btn:
                kb_row.append(btn)
        if kb_row:
            kb_rows.append(kb_row)
    if not kb_rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


def post_markup(
    extra_buttons=None,
    button_type: str = "none",
    button_text: str = "",
    button_url: str = "",
) -> InlineKeyboardMarkup | None:
    loaded = load_inline_markup(extra_buttons)
    if loaded:
        return loaded
    return buttons_from_ad(button_type, button_text, button_url)


def url_button(text: str, url: str, colored: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn(text, url=url, style="success" if colored else "primary")]]
    )


def buttons_from_ad(
    button_type: str,
    button_text: str,
    button_url: str,
    extra: list[dict] | None = None,
) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if button_type in ("url", "colored") and button_text and button_url:
        rows.append(
            [_btn(button_text, url=button_url, style="success" if button_type == "colored" else "primary")]
        )
    for item in extra or []:
        if isinstance(item, list):
            built = [_inline_button_from_dict(x) for x in item]
            built = [b for b in built if b]
            if built:
                rows.append(built)
            continue
        if isinstance(item, dict) and item.get("text") and item.get("url"):
            btn = _inline_button_from_dict(item)
            if btn:
                rows.append([btn])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def task_kb(offer_id: int, url: str | None, button_text: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if url:
        rows.append([_btn(button_text or t.TASK_BTN_SUB, url=url, style="success")])
    rows.append([_btn(t.TASK_BTN_CHECK, callback_data=f"task:check:{offer_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def view_task_kb(offer_id: int, url: str | None, button_text: str | None = None) -> InlineKeyboardMarkup:
    return task_kb(offer_id, url, button_text)


def withdraw_check_kb(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("✅ Готово", callback_data=f"wd:check:{kind}", style="success")],
            [_btn("⬅️ Назад", callback_data="wd:cancel")],
        ]
    )


def withdraw_channels_kb(offers: list, kind: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for offer in offers:
        url = offer.button_url
        if not url and offer.chat_id:
            chat = offer.chat_id.lstrip("@")
            url = f"https://t.me/{chat}"
        if url:
            name = offer.title or offer.button_text or "Канал"
            rows.append([_btn(f"👥 {name}", url=url, style="primary")])
    rows.append([_btn("✅ Готово", callback_data=f"wd:check:{kind}", style="success")])
    rows.append([_btn("⬅️ Назад", callback_data="wd:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pay_check_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("⭐ Отправить 3 ⭐", callback_data="wd:invoice", style="primary")],
            [_btn("⬅️ Назад", callback_data="wd:cancel")],
        ]
    )


def remind_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn(t.BTN_CLICK, callback_data="go:click", style="primary")]]
    )


def contest_op_kb(contest_id: int, buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for label, url in buttons:
        if url:
            rows.append([_btn(label or t.TASK_BTN_SUB, url=url, style="success")])
    rows.append([_btn(t.TASK_BTN_CHECK, callback_data=f"contest:check:{contest_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def contest_status_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(t.BTN_CLICK, callback_data="go:click", style="primary")],
            [_btn(t.BTN_BACK, callback_data="go:menu")],
        ]
    )


def contest_sponsors_kb(back: str = "adm:contest") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🧩 Свои ресурсы", callback_data="adm:ct:kind:own", style="primary")],
            [_btn("🔌 От сервисов", callback_data="adm:ct:kind:service", style="success")],
            [_btn("🚫 Без спонсоров", callback_data="adm:ct:kind:none", style="danger")],
            [_btn("⬅️ Отмена", callback_data=back)],
        ]
    )


def contest_own_pick_kb(count: int, back: str = "adm:contest") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(f"✅ Свои по умолчанию ({count})", callback_data="adm:ct:own:use", style="primary")],
            [_btn("✏️ Ввести другие", callback_data="adm:ct:own:custom")],
            [_btn("⬅️ Назад", callback_data=back)],
        ]
    )


def contest_own_kb(contest_id: int, buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for label, url in buttons:
        if url:
            rows.append([_btn(label or t.TASK_BTN_SUB, url=url, style="success")])
    rows.append([_btn(t.CONTEST_OWN_BTN, callback_data=f"contest:join:{contest_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def contest_end_type_kb(back: str = "adm:contest") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("👥 По участникам", callback_data="adm:ct:end:people", style="primary")],
            [_btn("⏰ По времени", callback_data="adm:ct:end:time", style="success")],
            [_btn("♾ Без итогов", callback_data="adm:ct:end:none", style="danger")],
            [_btn("⬅️ Отмена", callback_data=back)],
        ]
    )


def service_task_kb(service: str, buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if buttons:
        label, url = buttons[0]
        if url:
            rows.append([_btn(label or t.TASK_BTN_SUB, url=url, style="success")])
    rows.append([_btn(t.TASK_BTN_CHECK, callback_data=f"task:svc:{service}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_root_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            _btn("📊 Статистика", callback_data="adm:stats:day"),
            _btn("⚙️ Настройки", callback_data="adm:settings"),
        ],
        [
            _btn("📢 Реклама", callback_data="adm:ads"),
            _btn("👁 Показы", callback_data="adm:shows"),
        ],
        [
            _btn("📋 Задания", callback_data="adm:offers"),
            _btn("🔌 Сервисы", callback_data="adm:svc"),
        ],
        [
            _btn("🔗 Реф-ссылки", callback_data="adm:camps"),
            _btn("📨 Рассылка", callback_data="adm:br"),
        ],
        [
            _btn("👤 Юзеры", callback_data="adm:users"),
            _btn("💸 Выводы", callback_data="adm:wd"),
        ],
        [
            _btn("🎨 Прем-эмодзи", callback_data="adm:emoji"),
            _btn("🛠 Разработчик", callback_data="adm:dev"),
        ],
        [
            _btn("🏆 Конкурсы", callback_data="adm:contest"),
            _btn("📣 Спамер", callback_data="adm:spam"),
        ],
        [
            _btn("🎯 Авто-конкурс", callback_data="adm:cmb"),
        ],
        [
            _btn("👋 Приветка", callback_data="adm:greet"),
            _btn("🏷 Имена", callback_data="adm:namer"),
        ],
        [
            _btn("🤖 Юзер-бот", callback_data="adm:ub"),
        ],
    ]
    url = (settings.webapp_url or "").strip()
    if url.startswith("https://"):
        rows.insert(
            0,
            [_btn("🌐 Открыть веб-админку", web_app=WebAppInfo(url=url))],
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)
