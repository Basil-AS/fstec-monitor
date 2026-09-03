from __future__ import annotations

import asyncio

import pytest

from fstec_monitor.telegram.lifecycle import MessageLifecycleManager, ProgressCoalescer
from fstec_monitor.telegram.ux.callbacks import CallbackCodec
from fstec_monitor.telegram.ux.errors import TelegramErrorKind, classify_telegram_error
from fstec_monitor.telegram.ux.keyboards import navigation_row, pagination_row, with_navigation
from fstec_monitor.telegram.ux.messages import MessageLedger, NotificationSettlement
from fstec_monitor.telegram.ux.models import MessageKind, Pagination, ViewModel
from fstec_monitor.telegram.ux.navigation import NavigationController
from fstec_monitor.telegram.ux.progress import ProgressMessage


class Transport:
    def __init__(self) -> None:
        self.next_id = 100
        self.sent: list[tuple[int, str, dict | None]] = []
        self.edits: list[tuple[int, int, str, dict | None]] = []
        self.markup: list[tuple[int, int, dict | None]] = []
        self.deleted: list[tuple[int, int]] = []
        self.fail_edit: Exception | None = None
        self.send_metadata: list[tuple[str | None, str]] = []
        self.edit_metadata: list[tuple[str | None, str]] = []

    async def send(
        self,
        chat_id: int,
        text: str,
        reply_markup=None,
        *,
        screen: str | None = None,
        reason: str = "navigation",
    ) -> int:
        self.next_id += 1
        self.sent.append((chat_id, text, reply_markup))
        self.send_metadata.append((screen, reason))
        return self.next_id

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup=None,
        *,
        screen: str | None = None,
        reason: str = "navigation",
    ) -> None:
        if self.fail_edit:
            error, self.fail_edit = self.fail_edit, None
            raise error
        self.edits.append((chat_id, message_id, text, reply_markup))
        self.edit_metadata.append((screen, reason))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))

    async def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup=None) -> None:
        self.markup.append((chat_id, message_id, reply_markup))


def make_ledger() -> tuple[Transport, MessageLifecycleManager, MessageLedger]:
    transport = Transport()
    lifecycle = MessageLifecycleManager(transport)
    return transport, lifecycle, MessageLedger(lifecycle)


def test_codec_is_namespaced_bounded_and_strict() -> None:
    codec = CallbackCodec(namespace="scan", actions={"run", "page", "noop"})
    value = codec.encode("page", 2)
    assert value == "scan:page:2"
    assert codec.decode(value).arguments == ("2",)
    assert codec.decode("other:page:2") is None
    assert codec.decode("scan:unknown") is None
    with pytest.raises(ValueError):
        codec.encode("run", "contains:separator")


def test_five_navigation_transitions_edit_one_menu() -> None:
    async def scenario() -> None:
        transport, _, ledger = make_ledger()
        for screen in ("home", "status", "settings", "filters", "changes"):
            await ledger.edit_or_send_menu(7, ViewModel(screen, screen, {"inline_keyboard": []}))
        assert len(transport.sent) == 1
        assert len(transport.edits) == 4
        assert len(transport.deleted) == 0

    asyncio.run(scenario())


def test_media_callback_clears_markup_but_never_deletes_media() -> None:
    async def scenario() -> None:
        transport, lifecycle, ledger = make_ledger()
        await ledger.edit_or_send_menu(7, ViewModel("home", "home"))
        lifecycle.remember_message(7, 102, context=True)
        await ledger.edit_or_send_menu(7, ViewModel("changes", "changes"), source_message={"message_id": 102, "document": {"file_id": "x"}})
        assert (7, 102) not in transport.deleted
        assert transport.markup == [(7, 102, {"inline_keyboard": []})]
        assert len(transport.sent) == 2

    asyncio.run(scenario())


def test_stale_menu_is_not_edited() -> None:
    async def scenario() -> None:
        transport, lifecycle, ledger = make_ledger()
        await ledger.edit_or_send_menu(7, ViewModel("home", "home"))
        lifecycle.remember_message(7, 102, context=True)
        await ledger.edit_or_send_menu(7, ViewModel("settings", "settings"))
        assert transport.edits == []
        assert len(transport.sent) == 2
        assert transport.deleted == [(7, 101)]

    asyncio.run(scenario())


def test_markdown_artifact_cannot_be_deleted() -> None:
    assert MessageLifecycleManager.safe_delete_path("report.md") is False


def test_pagination_and_navigation_are_pure() -> None:
    codec = CallbackCodec(namespace="ui")
    pagination = Pagination(2, 4)
    row = pagination_row(pagination, codec)
    assert [button["text"] for button in row] == ["◀️", "2/4", "▶️"]
    markup = with_navigation([], codec, refresh=True)
    assert [button["text"] for button in markup["inline_keyboard"][-1]] == ["← Назад", "🏠 Меню", "🔄 Обновить"]
    assert navigation_row(codec)[0]["callback_data"] == "ui:back"


def test_navigation_retains_context_on_back() -> None:
    navigation = NavigationController()
    navigation.push("changes", {"page": 3})
    navigation.push("event", {"id": 512})
    assert navigation.back().payload == {"page": 3}
    assert navigation.current.screen == "changes"


def test_progress_is_single_flight_and_reuses_message() -> None:
    async def scenario() -> None:
        _, lifecycle, _ = make_ledger()
        first = await ProgressMessage.start(lifecycle, 7, "Запускаю", "scan", interval=0)
        with pytest.raises(RuntimeError, match="already running"):
            await ProgressMessage.start(lifecycle, 7, "Повтор", "scan", interval=0)
        await first.update("Получаю данные")
        await first.success("Готово")
        assert first.state == "success"
        assert first.message_id == 101

    asyncio.run(scenario())


def test_notification_settlement_updates_all_copies_once() -> None:
    async def scenario() -> None:
        transport, _, ledger = make_ledger()
        refs = [MessageRef(1, 11, MessageKind.CONTEXT), MessageRef(2, 12, MessageKind.CONTEXT)]
        settlement = NotificationSettlement(ledger)
        assert await settlement.settle("request-1", refs, "✅ Решение принято") is True
        assert await settlement.settle("request-1", refs, "дубликат") is False
        assert len(transport.edits) == 2
        assert all(edit[3] == {"inline_keyboard": []} for edit in transport.edits)

    from fstec_monitor.telegram.ux.models import MessageRef

    asyncio.run(scenario())


def test_telegram_error_classifier_treats_not_modified_as_noop() -> None:
    assert classify_telegram_error(RuntimeError("Bad Request: message is not modified")) is TelegramErrorKind.NOT_MODIFIED


def test_progress_close_does_not_wait_for_throttle_interval() -> None:
    async def scenario() -> None:
        rendered: list[str] = []

        async def render(value: str) -> None:
            rendered.append(value)

        coalescer = ProgressCoalescer(render, interval=2.0)
        coalescer.submit("first")
        await asyncio.sleep(0)
        coalescer.submit("latest")
        await asyncio.sleep(0)
        await asyncio.wait_for(coalescer.close(), timeout=0.1)
        assert rendered == ["first", "latest"]

    asyncio.run(scenario())
