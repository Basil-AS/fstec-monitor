"""Canonical role-aware keyboard builders."""

from __future__ import annotations

from typing import Final

from ..schedule import DAILY_MIDNIGHT, DAILY_NOON, DISABLED, EVERY_TWO_HOURS
from .callbacks import encode_callback

ADMIN_LABELS: Final = (
    ("📊 Статус", "status"),
    ("📰 Изменения", "changes"),
    ("🔍 Проверить сейчас", "scan"),
    ("🧯 Ошибки", "errors"),
    ("👥 Пользователи", "users"),
    ("🚫 Игнор категорий", "ignore"),
    ("⚙️ Настройки", "settings"),
    ("ℹ️ Помощь", "help"),
)
USER_LABELS: Final = (
    ("📰 Последние изменения", "changes"),
    ("🚫 Мои категории", "my_ignore"),
    ("ℹ️ Помощь", "help"),
)


def _reply_keyboard(labels: tuple[tuple[str, str], ...], placeholder: str) -> dict:
    return {
        "keyboard": [
            [{"text": label} for label, _ in labels[index : index + 2]]
            for index in range(0, len(labels), 2)
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": placeholder,
    }


def _inline_keyboard(labels: tuple[tuple[str, str], ...]) -> dict:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": encode_callback("screen", action)}]
            for label, action in labels
        ]
    }


def admin_keyboard() -> dict:
    return _reply_keyboard(ADMIN_LABELS, "Выберите действие или введите команду")


def user_keyboard() -> dict:
    return _reply_keyboard(USER_LABELS, "Выберите действие")


def inline_admin_keyboard() -> dict:
    return _inline_keyboard(ADMIN_LABELS)


def inline_user_keyboard() -> dict:
    return _inline_keyboard(USER_LABELS)


def settings_keyboard(notifications_enabled: bool = True) -> dict:
    notification_label = "🔔 Уведомления: включены" if notifications_enabled else "🔕 Уведомления: выключены"
    options = (
        ("✅ Раз в сутки · 12:00", encode_callback("settings", f"set-{DAILY_NOON}")),
        ("🌙 Раз в сутки · 00:00", encode_callback("settings", f"set-{DAILY_MIDNIGHT}")),
        ("⏱ Каждые 2 часа", encode_callback("settings", f"set-{EVERY_TWO_HOURS}")),
        ("⏸ Выключить автозапуск", encode_callback("settings", f"set-{DISABLED}")),
        (notification_label, encode_callback("settings", "notifications")),
        ("🏠 Главное меню", encode_callback("menu", "main")),
    )
    return {"inline_keyboard": [[{"text": label, "callback_data": callback}] for label, callback in options]}


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
            ("⏹ Остановить", encode_callback("scan", "stop")),
            ("🔄 Обновить", encode_callback("scan", "status")),
        )
    elif state in {"failed", "cancelled"}:
        options = (
            ("🔁 Повторить", encode_callback("scan", "retry")),
            ("🏠 Главное меню", encode_callback("menu", "main")),
        )
    else:
        options = (
            ("▶️ Запустить", encode_callback("scan", "run")),
            ("🏠 Главное меню", encode_callback("menu", "main")),
        )
    return {"inline_keyboard": [[{"text": label, "callback_data": callback} for label, callback in options]]}
