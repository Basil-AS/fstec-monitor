from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class MessageTransport(Protocol):
    async def send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
        *,
        screen: str | None = None,
        reason: str = "navigation",
    ) -> int:
        ...

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
        *,
        screen: str | None = None,
        reason: str = "navigation",
    ) -> None:
        ...

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        ...

    async def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup: dict | None = None) -> None:
        ...


@dataclass
class ScreenSession:
    chat_id: int
    message_id: int | None = None
    last_message_id: int | None = None
    screen: str = "main"
    payload: tuple[str, dict | None] | None = None
    persistent_message_ids: set[int] = field(default_factory=set)
    temporary_message_ids: set[int] = field(default_factory=set)
    context_message_ids: set[int] = field(default_factory=set)
    generation: int = 0


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

    def remember_message(
        self,
        chat_id: int,
        message_id: int,
        *,
        screen: str | None = None,
        persistent: bool = False,
        temporary: bool = False,
        context: bool = False,
    ) -> None:
        session = self.session(chat_id)
        session.last_message_id = max(session.last_message_id or 0, message_id)
        if persistent:
            session.persistent_message_ids.add(message_id)
        if temporary:
            session.temporary_message_ids.add(message_id)
        if context:
            session.context_message_ids.add(message_id)
        if screen is not None:
            session.message_id = message_id
            session.screen = screen

    def remember_context_message(self, chat_id: int, message: dict | None) -> None:
        message_id = (message or {}).get("message_id")
        if isinstance(message_id, int):
            self.remember_message(chat_id, message_id, context=True)

    @staticmethod
    def is_media_message(message: dict | None) -> bool:
        message = message or {}
        return any(message.get(key) for key in (
            "audio", "document", "photo", "video", "animation", "voice", "video_note", "sticker",
        ))

    def is_chat_tail(self, chat_id: int, message_id: int | None) -> bool:
        if not message_id:
            return False
        known_tail = self.session(chat_id).last_message_id
        return known_tail is None or message_id >= known_tail

    def is_current_screen_message(self, chat_id: int, message_id: int | None) -> bool:
        """Return true only for the reusable screen that is still at chat tail."""
        session = self.session(chat_id)
        return (
            message_id is not None
            and message_id == session.message_id
            and message_id not in session.persistent_message_ids
            and self.is_chat_tail(chat_id, message_id)
        )

    def adopt_screen(
        self,
        chat_id: int,
        message_id: int,
        screen: str = "main",
        *,
        message: dict | None = None,
    ) -> None:
        session = self.session(chat_id)
        if session.message_id is None and not self.is_media_message(message) and self.is_chat_tail(chat_id, message_id):
            session.message_id = message_id
            session.screen = screen
            session.payload = None
        self.remember_message(chat_id, message_id, context=True)

    async def cleanup_trigger_message(self, chat_id: int, message: dict | None) -> None:
        message = message or {}
        message_id = message.get("message_id")
        if not isinstance(message_id, int):
            return
        session = self.session(chat_id)
        if self.is_media_message(message) or message_id in session.persistent_message_ids:
            if hasattr(self.transport, "edit_message_reply_markup"):
                try:
                    await self.transport.edit_message_reply_markup(chat_id, message_id, {"inline_keyboard": []})
                except Exception as exc:  # noqa: BLE001 — cleanup must not mask the operation
                    log.debug("media markup cleanup skipped chat=%s message=%s: %s", chat_id, message_id, exc)
            return
        try:
            await self.transport.delete_message(chat_id, message_id)
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask the operation
            log.debug("callback message cleanup skipped chat=%s message=%s: %s", chat_id, message_id, exc)
        session.context_message_ids.discard(message_id)
        if session.last_message_id == message_id:
            session.last_message_id = session.message_id

    async def cleanup_old_menu(self, chat_id: int, except_message_id: int | None = None) -> None:
        session = self.session(chat_id)
        message_id = session.message_id
        if not message_id or message_id == except_message_id or message_id in session.persistent_message_ids:
            return
        try:
            await self.transport.delete_message(chat_id, message_id)
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask the operation
            log.debug("old menu cleanup skipped chat=%s message=%s: %s", chat_id, message_id, exc)
        session.message_id = None
        session.payload = None

    def _lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(chat_id, asyncio.Lock())

    async def show_screen(
        self,
        chat_id: int,
        screen: str,
        text: str,
        reply_markup: dict | None = None,
        *,
        source_message: dict | None = None,
        reason: str = "navigation",
    ) -> int | None:
        async with self._lock(chat_id):
            session = self.session(chat_id)
            source_message_id = (source_message or {}).get("message_id")
            source_is_media = self.is_media_message(source_message)
            if source_message_id and source_is_media:
                await self.cleanup_trigger_message(chat_id, source_message)
            payload = (text, reply_markup)
            if session.message_id is not None and session.payload == payload and session.screen == screen:
                return session.message_id
            can_edit = (
                session.message_id is not None
                and session.message_id not in session.persistent_message_ids
                and not source_is_media
                and self.is_chat_tail(chat_id, session.message_id)
            )
            if can_edit:
                current_message_id = session.message_id
                try:
                    await self.transport.edit_message(
                        chat_id,
                        current_message_id,
                        text,
                        reply_markup,
                        screen=screen,
                        reason=reason,
                    )
                    message_id = current_message_id
                except Exception as exc:
                    if is_not_modified(exc):
                        message_id = current_message_id
                    elif is_missing_message(exc):
                        message_id = await self.transport.send(
                            chat_id, text, reply_markup, screen=screen, reason=reason
                        )
                    elif is_not_editable(exc):
                        # A message can remain in the chat but lose editability
                        # (for example after it was converted to a different
                        # Telegram message type). Close it best-effort before
                        # placing the replacement at the chat tail.
                        await self.cleanup_old_menu(chat_id)
                        message_id = await self.transport.send(
                            chat_id, text, reply_markup, screen=screen, reason=reason
                        )
                    else:
                        raise
            else:
                if session.message_id is not None and session.message_id not in session.persistent_message_ids:
                    await self.cleanup_old_menu(chat_id)
                message_id = await self.transport.send(
                    chat_id, text, reply_markup, screen=screen, reason=reason
                )
            if message_id is None:
                return None
            session.message_id = message_id
            session.last_message_id = message_id
            session.screen = screen
            session.payload = payload
            session.generation += 1
            log.debug("screen rendered chat=%s message=%s screen=%s reason=%s", chat_id, message_id, screen, reason)
            return message_id

    async def show_progress(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> int | None:
        return await self.show_screen(chat_id, "scan", text, reply_markup, reason="progress")

    async def publish_persistent(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> int | None:
        async with self._lock(chat_id):
            message_id = await self.transport.send(
                chat_id, text, reply_markup, screen="persistent", reason="persistent"
            )
            if message_id is not None:
                self.remember_message(chat_id, message_id, persistent=True)
            return message_id

    async def show_temporary(
        self,
        chat_id: int,
        text: str,
        ttl: float = 8.0,
    ) -> int | None:
        async with self._lock(chat_id):
            message_id = await self.transport.send(
                chat_id, text, screen="temporary", reason="temporary"
            )
        if message_id is None:
            return None
        self.session(chat_id).temporary_message_ids.add(message_id)
        self.remember_message(chat_id, message_id, temporary=True)
        task = asyncio.create_task(self._delete_temporary(chat_id, message_id, ttl))
        self._temporary_tasks.setdefault(chat_id, set()).add(task)
        task.add_done_callback(self._temporary_tasks[chat_id].discard)
        return message_id

    async def _delete_temporary(self, chat_id: int, message_id: int, ttl: float) -> None:
        await asyncio.sleep(ttl)
        session = self.session(chat_id)
        try:
            await self.transport.delete_message(chat_id, message_id)
        except Exception:  # noqa: BLE001 — cleanup must not mask the operation
            log.debug("temporary message cleanup skipped chat=%s message=%s", chat_id, message_id)
        session.temporary_message_ids.discard(message_id)
        if session.last_message_id == message_id:
            known_ids = {
                current_id
                for current_id in (
                    session.message_id,
                    *session.persistent_message_ids,
                    *session.temporary_message_ids,
                    *session.context_message_ids,
                )
                if current_id is not None
            }
            session.last_message_id = max(known_ids, default=None)

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
            except Exception as exc:  # noqa: BLE001 — cleanup must not mask the operation
                log.debug("screen message cleanup failed chat=%s message=%s: %s", chat_id, message_id, exc)
            session.message_id = None
            session.payload = None
            if session.last_message_id == message_id:
                session.last_message_id = None
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
        self._first_render = True

    def submit(self, value) -> None:
        if self._closed:
            return
        self._latest = value
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def reset(self) -> None:
        """Start a new logical operation with an immediate first render."""
        self._first_render = True
        self._latest = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        while not self._closed and self._latest is not None:
            if self.interval and not self._first_render:
                await asyncio.sleep(self.interval)
            self._first_render = False
            value = self._latest
            self._latest = None
            try:
                result = self.renderer(value)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.warning("progress update failed; the next update may recover it", exc_info=True)

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            # Do not make completion wait for the throttle sleep.  The latest
            # value is rendered below, so cancelling the pending worker cannot
            # lose the final progress state.
            if not self._task.done():
                self._task.cancel()
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


def is_not_editable(error: BaseException) -> bool:
    message = str(error).casefold()
    return "can't be edited" in message or "cannot be edited" in message
