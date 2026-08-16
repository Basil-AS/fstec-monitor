from __future__ import annotations

from .models import UserAccess


def is_allowed(user: UserAccess | None) -> bool:
    return bool(user and user.status == "approved")


def access_request_text(user_id: int, username: str, display_name: str) -> str:
    identity = f"@{username}" if username else display_name or "без имени"
    return f"🔐 Запрос доступа\nПользователь: {identity}\nID: {user_id}\n\nРазрешить доступ?"
