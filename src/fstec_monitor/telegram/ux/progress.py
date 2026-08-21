from __future__ import annotations

import asyncio
from typing import ClassVar

from ..lifecycle import MessageLifecycleManager, ProgressCoalescer


class ProgressMessage:
    """Single-flight, edit-in-place progress card."""

    _active: ClassVar[dict[tuple[int, str], ProgressMessage]] = {}

    def __init__(self, lifecycle: MessageLifecycleManager, chat_id: int, operation: str = "default", *, interval: float = 2.0) -> None:
        self.lifecycle = lifecycle
        self.chat_id = chat_id
        self.operation = operation
        self._coalescer = ProgressCoalescer(self._render, interval=interval)
        self.message_id: int | None = None
        self.state = "idle"

    @classmethod
    async def start(cls, lifecycle: MessageLifecycleManager, chat_id: int, text: str, operation: str = "default", *, reply_markup: dict | None = None, interval: float = 2.0) -> ProgressMessage:
        key = (chat_id, operation)
        active = cls._active.get(key)
        if active is not None and active.state == "running":
            raise RuntimeError("operation already running")
        card = cls(lifecycle, chat_id, operation, interval=interval)
        card.state = "running"
        card.message_id = await lifecycle.show_progress(chat_id, text, reply_markup)
        cls._active[key] = card
        return card

    async def update(self, text: str, reply_markup: dict | None = None) -> None:
        if self.state != "running":
            return
        self._coalescer.submit((text, reply_markup))
        await asyncio.sleep(0)

    async def _render(self, value: tuple[str, dict | None]) -> None:
        text, reply_markup = value
        self.message_id = await self.lifecycle.show_progress(self.chat_id, text, reply_markup)

    async def success(self, text: str, reply_markup: dict | None = None) -> None:
        await self._finish("success", text, reply_markup)

    async def fail(self, text: str, reply_markup: dict | None = None) -> None:
        await self._finish("failed", text, reply_markup)

    async def cancel(self, text: str = "Проверка остановлена", reply_markup: dict | None = None) -> None:
        await self._finish("cancelled", text, reply_markup)

    async def _finish(self, state: str, text: str, reply_markup: dict | None) -> None:
        if self.state not in {"running", "success", "failed", "cancelled"}:
            return
        self.state = state
        await self._coalescer.close()
        self.message_id = await self.lifecycle.show_progress(self.chat_id, text, reply_markup)
        self._active.pop((self.chat_id, self.operation), None)
