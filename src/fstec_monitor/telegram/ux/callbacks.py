from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

MAX_CALLBACK_BYTES = 64


@dataclass(frozen=True)
class CallbackData:
    namespace: str
    action: str
    arguments: tuple[str, ...] = ()


class CallbackCodec:
    """Encode and validate compact callback data without secrets."""

    def __init__(self, *, namespace: str = "ux", actions: Iterable[str] | None = None) -> None:
        self.namespace = self._token(namespace, "namespace")
        self.actions = frozenset(actions or ())

    def encode(self, action: str, *arguments: object) -> str:
        action = self._token(action, "action")
        if self.actions and action not in self.actions:
            raise ValueError("unsupported callback action")
        values = [self._value(argument) for argument in arguments]
        encoded = ":".join((self.namespace, action, *values))
        if len(encoded.encode("utf-8")) > MAX_CALLBACK_BYTES:
            raise ValueError("callback data is too long")
        return encoded

    def decode(self, data: str) -> CallbackData | None:
        if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
            return None
        parts = data.split(":")
        if len(parts) < 2 or parts[0] != self.namespace:
            return None
        try:
            action = self._token(parts[1], "action")
            args = tuple(self._value(value) for value in parts[2:])
        except ValueError:
            return None
        if self.actions and action not in self.actions:
            return None
        return CallbackData(self.namespace, action, args)

    def is_current(self, callback: CallbackData, generation: int | None, current_generation: int | None) -> bool:
        """Reject callbacks carrying an explicit stale generation argument."""
        if generation is None or current_generation is None:
            return True
        return generation == current_generation

    @staticmethod
    def _token(value: object, label: str) -> str:
        if not isinstance(value, str) or not value or ":" in value or any(ord(char) < 32 for char in value):
            raise ValueError(f"invalid callback {label}")
        return value

    @classmethod
    def _value(cls, value: object) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TypeError("callback arguments must be strings or integers")
        return cls._token(str(value), "argument")
