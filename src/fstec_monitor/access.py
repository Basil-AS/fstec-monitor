from __future__ import annotations

import secrets
import unicodedata
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .models import UserAccess, UserAccessEvent

ACCESS_STATUSES = {"pending", "approved", "denied", "blocked"}
ACCESS_TRANSITIONS = {
    "approve": {"pending": "approved", "denied": "approved"},
    "deny": {"pending": "denied"},
    "block": {"pending": "blocked", "approved": "blocked", "denied": "blocked"},
    "unblock": {"blocked": "denied"},
    "revoke": {"approved": "denied"},
}


def is_allowed(user: UserAccess | None) -> bool:
    return bool(user and user.status == "approved")


def new_request_nonce() -> str:
    """Create a per-state version used to reject stale inline access buttons."""
    return secrets.token_hex(6)


def record_access_event(
    session: Session,
    user: UserAccess,
    action: str,
    *,
    old_status: str | None,
    new_status: str | None,
    actor_id: int | None,
    details: str = "",
) -> None:
    session.add(UserAccessEvent(
        user_id=user.user_id,
        occurred_at=datetime.now(UTC),
        action=action,
        old_status=old_status,
        new_status=new_status,
        actor_id=actor_id,
        chat_id=user.chat_id,
        username=user.username,
        display_name=user.display_name,
        details=details[:500],
    ))


def resolve_access(
    session: Session,
    user: UserAccess,
    action: str,
    *,
    actor_id: int | None,
    admin_id: int | None,
    expected_nonce: str,
) -> str | None:
    """Apply an admin transition only to the access state shown in its button."""
    allowed_states = ACCESS_TRANSITIONS.get(action)
    if (
        not allowed_states
        or not admin_id
        or actor_id != admin_id
        or user.user_id == admin_id
        or not secrets.compare_digest(user.request_nonce or "", expected_nonce)
    ):
        return None
    new_status = allowed_states.get(user.status)
    if not new_status:
        return None
    old_status = user.status
    user.status = new_status
    user.reviewed_at = datetime.now(UTC)
    user.resolved_by = actor_id
    user.notification_sent = False
    user.request_nonce = new_request_nonce()
    record_access_event(
        session,
        user,
        action,
        old_status=old_status,
        new_status=new_status,
        actor_id=actor_id,
    )
    return new_status


def safe_identity(value: str | None, *, limit: int = 80) -> str:
    """Keep user-controlled names on one readable line without bidi spoofing."""
    bidi_controls = {"LRE", "RLE", "PDF", "LRO", "RLO", "LRI", "RLI", "FSI", "PDI"}
    cleaned = "".join(
        char
        for char in (value or "")
        if unicodedata.category(char) not in {"Cc", "Cs", "Zl", "Zp"}
        and unicodedata.bidirectional(char) not in bidi_controls
    )
    return " ".join(cleaned.split())[:limit]


def access_request_text(user_id: int, username: str, display_name: str) -> str:
    safe_username = safe_identity(username, limit=32)
    safe_display_name = safe_identity(display_name)
    if safe_username and all(char.isascii() and (char.isalnum() or char == "_") for char in safe_username):
        identity = f"@{safe_username}"
    else:
        identity = safe_display_name or "без имени"
    return f"🔐 Запрос доступа\nПользователь: {identity}\nID: {user_id}\n\nРазрешить доступ?"
