"""Versioned, bounded callback-data helpers for Telegram inline buttons."""

from __future__ import annotations

from typing import Final

PREFIX: Final = "v1"
MAX_CALLBACK_BYTES: Final = 64
_ALLOWED_ACTIONS: Final = {
    "menu",
    "settings",
    "scan",
    "ignore",
    "users",
    "errors",
    "screen",
    "nav",
}


def encode_callback(action: str, value: str = "") -> str:
    """Build callback data without secrets or unbounded user input."""
    if action not in _ALLOWED_ACTIONS:
        raise ValueError("unsupported callback action")
    if not action or ":" in action or ":" in value or any(ord(char) < 32 for char in value):
        raise ValueError("invalid callback value")
    encoded = f"{PREFIX}:{action}:{value}" if value else f"{PREFIX}:{action}"
    if len(encoded.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError("callback data is too long")
    return encoded


def decode_callback(data: str) -> tuple[str, str] | None:
    """Validate and decode callback data received from Telegram."""
    if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        return None
    parts = data.split(":")
    if len(parts) not in {2, 3} or parts[0] != PREFIX or parts[1] not in _ALLOWED_ACTIONS:
        return None
    value = parts[2] if len(parts) == 3 else ""
    if not value or any(ord(char) < 32 for char in value):
        return None
    return parts[1], value
