"""Canonical inline keyboard builders for role-aware Telegram screens."""

from __future__ import annotations

from typing import Final

from ..schedule import DAILY_MIDNIGHT, DAILY_NOON, DISABLED, EVERY_TWO_HOURS
from .callbacks import encode_callback

ADMIN_ACTIONS: Final = (
    ("📊 Статус", "status"),
    ("📰 Изменения", "changes"),
    ("🔍 Проверка", "scan"),
    ("🧯 Ошибки", "errors"),
    ("👥 Пользователи", "users"),
    ("🚫 Игнор категорий", "ignore"),
    ("⚙️ Настройки", "settings"),
)
USER_ACTIONS: Final = (
    ("📰 Изменения", "changes"),
    ("🚫 Мои категории", "my-ignore"),
)


def _inline(options: tuple[tuple[str, str], ...] | list[tuple[str, str]]) -> dict:
    return {"inline_keyboard": [[{"text": label, "callback_data": data}] for label, data in options]}


def admin_keyboard() -> dict:
    return _inline(tuple((label, encode_callback("menu", action)) for label, action in ADMIN_ACTIONS))


def user_keyboard() -> dict:
    return _inline(tuple((label, encode_callback("menu", action)) for label, action in USER_ACTIONS))


def back_to_panel() -> tuple[str, str]:
    return "🏠 Панель", encode_callback("menu", "panel")


def context_keyboard(*options: tuple[str, str]) -> dict:
    return _inline([*options, back_to_panel()])


def settings_keyboard(notifications_enabled: bool = True) -> dict:
    notification_label = "🔔 Уведомления: включены" if notifications_enabled else "🔕 Уведомления: выключены"
    options = (
        ("✅ Раз в сутки · 12:00", encode_callback("settings", f"set-{DAILY_NOON}")),
        ("🌙 Раз в сутки · 00:00", encode_callback("settings", f"set-{DAILY_MIDNIGHT}")),
        ("⏱ Каждые 2 часа", encode_callback("settings", f"set-{EVERY_TWO_HOURS}")),
        ("⏸ Выключить автозапуск", encode_callback("settings", f"set-{DISABLED}")),
        (notification_label, encode_callback("settings", "notifications")),
    )
    return context_keyboard(*options)


def scan_confirmation_keyboard() -> dict:
    return {
        "inline_keyboard": [[
            {"text": "▶️ Запустить", "callback_data": encode_callback("scan", "run")},
            {"text": "✕ Отмена", "callback_data": encode_callback("scan", "run-cancel")},
        ], [{"text": "🏠 Главное меню", "callback_data": encode_callback("menu", "main")}]],
    }


def scan_keyboard(state: str) -> dict:
    if state == "running":
        options = (
            ("🔄 Обновить", encode_callback("scan", "status")),
            ("⏹ Остановить", encode_callback("scan", "stop")),
        )
    elif state in {"failed", "cancelled", "completed"}:
        options = (("🔁 Повторить", encode_callback("scan", "retry")),)
    else:
        options = (("▶️ Запустить", encode_callback("scan", "run")),)
    return context_keyboard(*options)


def users_keyboard(buttons: list[list[dict]], page: int, total_pages: int) -> dict:
    rows = list(buttons)
    navigation = []
    if page > 0:
        navigation.append({"text": "◀️ Назад", "callback_data": encode_callback("users", f"page-{page - 1}")})
    if page + 1 < total_pages:
        navigation.append({"text": "Далее ▶️", "callback_data": encode_callback("users", f"page-{page + 1}")})
    if navigation:
        rows.append(navigation)
    rows.append([{"text": back_to_panel()[0], "callback_data": back_to_panel()[1]}])
    return {"inline_keyboard": rows}
