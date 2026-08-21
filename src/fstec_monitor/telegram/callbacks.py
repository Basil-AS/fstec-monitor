"""Versioned, bounded callback-data helpers for Telegram inline buttons."""

from __future__ import annotations

from typing import Final

from .ux.callbacks import CallbackCodec

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
    "userignore",
}


_CODEC = CallbackCodec(namespace=PREFIX, actions=_ALLOWED_ACTIONS)


def encode_callback(action: str, value: str = "") -> str:
    """Compatibility wrapper over the single namespaced callback codec."""
    return _CODEC.encode(action, *(value,) if value else ())


def decode_callback(data: str) -> tuple[str, str] | None:
    """Validate and decode callback data received from Telegram."""
    decoded = _CODEC.decode(data)
    if decoded is None or len(decoded.arguments) > 1:
        return None
    return decoded.action, decoded.arguments[0] if decoded.arguments else ""
