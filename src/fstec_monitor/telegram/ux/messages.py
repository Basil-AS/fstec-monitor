from __future__ import annotations

import logging
from collections.abc import Iterable

from ..lifecycle import MessageLifecycleManager
from .errors import TelegramErrorKind, classify_telegram_error
from .models import MessageKind, MessageRef, ViewModel

log = logging.getLogger(__name__)


class MessageLedger:
    """DubnaCams-style ledger backed by the single existing lifecycle owner."""

    def __init__(self, lifecycle: MessageLifecycleManager) -> None:
        self.lifecycle = lifecycle

    def remember_message(self, chat_id: int, message_id: int, kind: MessageKind = MessageKind.CONTEXT, *, screen: str | None = None) -> MessageRef:
        self.lifecycle.remember_message(
            chat_id,
            message_id,
            screen=screen,
            persistent=kind is MessageKind.PERSISTENT,
            temporary=kind is MessageKind.TEMPORARY,
            context=kind in {MessageKind.CONTEXT, MessageKind.MENU},
        )
        return MessageRef(chat_id, message_id, kind)

    def remember_context_message(self, chat_id: int, message: dict | None) -> None:
        self.lifecycle.remember_context_message(chat_id, message)

    def is_chat_tail(self, chat_id: int, message_id: int | None) -> bool:
        return self.lifecycle.is_chat_tail(chat_id, message_id)

    async def delete_previous_menu(self, chat_id: int, except_message_id: int | None = None) -> None:
        await self.lifecycle.cleanup_old_menu(chat_id, except_message_id)

    async def cleanup_callback(self, chat_id: int, message: dict | None) -> None:
        await self.lifecycle.cleanup_trigger_message(chat_id, message)

    async def edit_or_send_menu(self, chat_id: int, view: ViewModel, *, source_message: dict | None = None, reason: str = "navigation") -> int | None:
        return await self.lifecycle.show_screen(
            chat_id, view.screen, view.text, view.reply_markup,
            source_message=source_message, reason=reason,
        )

    async def send_menu(self, chat_id: int, view: ViewModel) -> int | None:
        await self.delete_previous_menu(chat_id)
        return await self.lifecycle.show_screen(chat_id, view.screen, view.text, view.reply_markup, reason="menu")

    async def send_tracked_message(self, chat_id: int, text: str, kind: MessageKind = MessageKind.CONTEXT, reply_markup: dict | None = None) -> MessageRef | None:
        if kind is MessageKind.PERSISTENT:
            message_id = await self.lifecycle.publish_persistent(chat_id, text, reply_markup)
        elif kind is MessageKind.TEMPORARY:
            message_id = await self.lifecycle.show_temporary(chat_id, text)
        else:
            message_id = await self.lifecycle.transport.send(chat_id, text, reply_markup)
        return self.remember_message(chat_id, message_id, kind) if message_id is not None else None

    async def settle(self, refs: Iterable[MessageRef], text: str) -> None:
        for ref in refs:
            if ref.kind is MessageKind.PERSISTENT:
                continue
            try:
                await self.lifecycle.transport.edit_message(ref.chat_id, ref.message_id, text, {"inline_keyboard": []})
            except (OSError, RuntimeError, TimeoutError) as exc:
                if classify_telegram_error(exc) not in {TelegramErrorKind.NOT_MODIFIED, TelegramErrorKind.MISSING, TelegramErrorKind.NOT_EDITABLE}:
                    log.debug("notification settlement skipped chat=%s message=%s: %s", ref.chat_id, ref.message_id, exc)


class NotificationSettlement:
    """Idempotently closes every admin copy of one decision notification."""

    def __init__(self, ledger: MessageLedger) -> None:
        self.ledger = ledger
        self._settled: set[str] = set()

    async def settle(self, request_id: str, refs: Iterable[MessageRef], verdict: str) -> bool:
        if request_id in self._settled:
            return False
        self._settled.add(request_id)
        await self.ledger.settle(refs, verdict)
        return True
