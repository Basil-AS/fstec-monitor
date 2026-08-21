from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..navigation import NavigationStack


@dataclass(frozen=True)
class NavigationFrame:
    screen: str
    payload: Any = None


class NavigationController:
    """Typed navigation facade; the domain payload is retained for Back."""

    def __init__(self, stack: NavigationStack | None = None) -> None:
        self.stack = stack or NavigationStack()

    @property
    def current(self) -> NavigationFrame:
        return NavigationFrame(self.stack.current, self.stack.current_payload)

    def reset(self, screen: str = "main", payload: Any = None) -> NavigationFrame:
        self.stack.reset(screen)
        if payload is not None:
            self.stack.replace(screen, payload)
        return self.current

    def push(self, screen: str, payload: Any = None) -> NavigationFrame:
        self.stack.push(screen, payload)
        return self.current

    def replace(self, screen: str, payload: Any = None) -> NavigationFrame:
        self.stack.replace(screen, payload)
        return self.current

    def back(self) -> NavigationFrame:
        screen, payload = self.stack.back_frame()
        return NavigationFrame(screen, payload)
