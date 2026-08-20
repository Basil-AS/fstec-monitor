from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class MessageTransport(Protocol):
    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None) -> int:
        ...

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> None:
        ...

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        ...


@dataclass
class ScreenSession:
    chat_id: int
    message_id: int | None = None
    screen: str = "main"
    payload: tuple[str, dict | None] | None = None
    persistent_message_ids: set[int] = field(default_factory=set)


class MessageLifecycleManager:
    """Own the lifecycle of reusable UI screens and non-persistent messages.

    Persistent Telegram messages are deliberately tracked separately from the
    screen pointer. Closing a screen can only delete the current screen; it can
    never delete a report or a generated Markdown document.
    """

    def __init__(self, transport: MessageTransport) -> None:
        self.transport = transport
        self._sessions: dict[int, ScreenSession] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._temporary_tasks: dict[int, set[asyncio.Task[None]]] = {}

    def session(self, chat_id: int) -> ScreenSession:
        return self._sessions.setdefault(chat_id, ScreenSession(chat_id=chat_id))

    def adopt_screen(self, chat_id: int, message_id: int, screen: str = "main") -> None:
        session = self.session(chat_id)
        if session.message_id is None:
            session.message_id = message_id
            session.screen = screen
            session.payload = None

    def _lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(chat_id, asyncio.Lock())

    async def show_screen(
        self,
        chat_id: int,
        screen: str,
        text: str,
        reply_markup: dict | None = None,
    ) -> int | None:
        async with self._lock(chat_id):
            session = self.session(chat_id)
            payload = (text, reply_markup)
            if session.message_id is not None and session.payload == payload and session.screen == screen:
                return session.message_id
            if session.message_id is None:
                message_id = await self.transport.send(chat_id, text, reply_markup)
            else:
                try:
                    await self.transport.edit_message(chat_id, session.message_id, text, reply_markup)
                    message_id = session.message_id
                except Exception as exc:
                    if is_not_modified(exc):
                        message_id = session.message_id
                    elif is_missing_message(exc):
                        message_id = await self.transport.send(chat_id, text, reply_markup)
                    else:
                        raise
            session.message_id = message_id
            session.screen = screen
            session.payload = payload
            return message_id

    async def show_progress(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> int | None:
        return await self.show_screen(chat_id, "scan", text, reply_markup)

    async def publish_persistent(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> int | None:
        async with self._lock(chat_id):
            message_id = await self.transport.send(chat_id, text, reply_markup)
            self.session(chat_id).persistent_message_ids.add(message_id)
            return message_id

    async def show_temporary(
        self,
        chat_id: int,
        text: str,
        ttl: float = 8.0,
    ) -> int | None:
        async with self._lock(chat_id):
            message_id = await self.transport.send(chat_id, text)
        if message_id is None:
            return None
        task = asyncio.create_task(self._delete_temporary(chat_id, message_id, ttl))
        self._temporary_tasks.setdefault(chat_id, set()).add(task)
        task.add_done_callback(self._temporary_tasks[chat_id].discard)
        return message_id

    async def _delete_temporary(self, chat_id: int, message_id: int, ttl: float) -> None:
        await asyncio.sleep(ttl)
        try:
            await self.transport.delete_message(chat_id, message_id)
        except (OSError, RuntimeError, TimeoutError):
            return

    async def close_screen(self, chat_id: int) -> None:
        async with self._lock(chat_id):
            session = self.session(chat_id)
            if session.message_id is None:
                return
            message_id = session.message_id
            if message_id in session.persistent_message_ids:
                return
            try:
                await self.transport.delete_message(chat_id, message_id)
            except (OSError, RuntimeError, TimeoutError) as exc:
                log.debug("screen message cleanup failed chat=%s message=%s: %s", chat_id, message_id, exc)
            session.message_id = None
            session.payload = None
            session.screen = "main"

    async def close(self) -> None:
        tasks = [task for group in self._temporary_tasks.values() for task in group]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._temporary_tasks.clear()

    @staticmethod
    def safe_delete_path(path: str | Path) -> bool:
        """Delete a temporary artifact, never a Markdown document."""
        target = Path(path)
        if target.suffix.casefold() == ".md":
            return False
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        return True


class ProgressCoalescer:
    """Render only the latest progress state at a bounded frequency."""

    def __init__(self, renderer, *, interval: float = 2.0) -> None:
        self.renderer = renderer
        self.interval = max(0.0, interval)
        self._latest = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def submit(self, value) -> None:
        if self._closed:
            return
        self._latest = value
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while not self._closed and self._latest is not None:
            if self.interval:
                await asyncio.sleep(self.interval)
            value = self._latest
            self._latest = None
            result = self.renderer(value)
            if asyncio.iscoroutine(result):
                await result

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        if self._latest is not None:
            value, self._latest = self._latest, None
            result = self.renderer(value)
            if asyncio.iscoroutine(result):
                await result


def is_not_modified(error: BaseException) -> bool:
    return "message is not modified" in str(error).casefold()


def is_missing_message(error: BaseException) -> bool:
    message = str(error).casefold()
    return "message to edit not found" in message or "message not found" in message
