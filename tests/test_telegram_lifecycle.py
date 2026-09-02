from __future__ import annotations

import asyncio

import pytest

from fstec_monitor.telegram.lifecycle import MessageLifecycleManager


class FakeTransport:
    def __init__(self) -> None:
        self.next_message_id = 100
        self.sent: list[tuple[int, str, dict | None]] = []
        self.edited: list[tuple[int, int, str, dict | None]] = []
        self.send_metadata: list[tuple[str | None, str]] = []
        self.edit_metadata: list[tuple[str | None, str]] = []
        self.deleted: list[tuple[int, int]] = []
        self.fail_next_edit: Exception | None = None
        self.markup_edits: list[tuple[int, int, dict | None]] = []

    async def send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
        *,
        screen: str | None = None,
        reason: str = "navigation",
    ) -> int:
        self.next_message_id += 1
        self.sent.append((chat_id, text, reply_markup))
        self.send_metadata.append((screen, reason))
        return self.next_message_id

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
        if self.fail_next_edit:
            error, self.fail_next_edit = self.fail_next_edit, None
            raise error
        self.edited.append((chat_id, message_id, text, reply_markup))
        self.edit_metadata.append((screen, reason))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))

    async def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup=None) -> None:
        self.markup_edits.append((chat_id, message_id, reply_markup))


def test_screen_transitions_edit_one_reusable_message() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)

        first = await lifecycle.show_screen(7, "main", "Главное меню", {"inline_keyboard": []})
        second = await lifecycle.show_screen(7, "settings", "Настройки", {"inline_keyboard": []})

        assert first == second
        assert len(transport.sent) == 1
        assert len(transport.edited) == 1

    asyncio.run(scenario())


def test_identical_screen_payload_is_coalesced() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)

        await lifecycle.show_screen(7, "main", "Главное меню", {"inline_keyboard": []})
        await lifecycle.show_screen(7, "main", "Главное меню", {"inline_keyboard": []})

        assert len(transport.sent) == 1
        assert len(transport.edited) == 0

    asyncio.run(scenario())


def test_missing_screen_message_is_recreated_without_losing_state() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)
        await lifecycle.show_screen(7, "main", "Главное меню", {"inline_keyboard": []})
        transport.fail_next_edit = RuntimeError("message to edit not found")

        replacement = await lifecycle.show_screen(7, "settings", "Настройки", {"inline_keyboard": []})

        assert replacement != 0
        assert len(transport.sent) == 2
        assert lifecycle.session(7).screen == "settings"

    asyncio.run(scenario())


def test_non_editable_screen_falls_back_to_new_menu_and_closes_old_one() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)
        await lifecycle.show_screen(7, "main", "Главное меню", {"inline_keyboard": []})
        transport.fail_next_edit = RuntimeError("Bad Request: message can't be edited")

        replacement = await lifecycle.show_screen(7, "settings", "Настройки", {"inline_keyboard": []})

        assert replacement == 102
        assert transport.deleted == [(7, 101)]
        assert lifecycle.session(7).message_id == 102
        assert lifecycle.session(7).screen == "settings"

    asyncio.run(scenario())


def test_persistent_message_is_not_deleted_by_screen_cleanup() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)
        await lifecycle.show_screen(7, "main", "Главное меню", {"inline_keyboard": []})
        persistent = await lifecycle.publish_persistent(7, "diff.md")

        await lifecycle.close_screen(7)

        assert persistent is not None
        assert transport.deleted == [(7, 101)]
        assert all(message_id != persistent for _, message_id in transport.deleted)

    asyncio.run(scenario())


def test_timeout_keeps_screen_pointer_for_retry() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)
        await lifecycle.show_screen(7, "scan", "Проверка", {"inline_keyboard": []})
        transport.fail_next_edit = TimeoutError("telegram timeout")

        with pytest.raises(TimeoutError):
            await lifecycle.show_screen(7, "scan", "Проверка 20%", {"inline_keyboard": []})

        assert lifecycle.session(7).message_id == 101
        assert lifecycle.session(7).screen == "scan"

    asyncio.run(scenario())


def test_concurrent_screen_updates_are_serialized() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)

        await asyncio.gather(
            lifecycle.show_screen(7, "scan", "10%", None),
            lifecycle.show_screen(7, "scan", "20%", None),
        )

        assert len(transport.sent) == 1
        assert len(transport.edited) == 1

    asyncio.run(scenario())


def test_five_navigation_transitions_reuse_one_tail_message() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)

        for screen in ("main", "status", "settings", "filters", "changes"):
            await lifecycle.show_screen(7, screen, screen, {"inline_keyboard": []})

        assert len(transport.sent) == 1
        assert len(transport.edited) == 4
        assert lifecycle.session(7).message_id == 101

    asyncio.run(scenario())


def test_stale_tail_creates_one_new_screen_instead_of_editing_history() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)
        await lifecycle.show_screen(7, "main", "main", None)
        lifecycle.remember_message(7, 102, context=True)

        await lifecycle.show_screen(7, "settings", "settings", None)

        assert len(transport.edited) == 0
        assert len(transport.sent) == 2
        assert transport.deleted == [(7, 101)]

    asyncio.run(scenario())


def test_cleanup_transport_errors_never_break_navigation() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)
        await lifecycle.show_screen(7, "main", "main", None)

        async def broken_delete(*_args):
            raise ValueError("telegram cleanup failure")

        transport.delete_message = broken_delete

        await lifecycle.cleanup_old_menu(7)

        assert lifecycle.session(7).message_id is None

    asyncio.run(scenario())


def test_stale_screen_message_is_not_current_chat_tail() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)
        current = await lifecycle.show_screen(7, "main", "main", None)
        lifecycle.remember_message(7, 102, context=True)

        assert current == 101
        assert not lifecycle.is_current_screen_message(7, current)
        assert lifecycle.is_current_screen_message(7, 102) is False

    asyncio.run(scenario())


def test_media_trigger_is_never_deleted_and_menu_is_recreated_below_it() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)
        await lifecycle.show_screen(7, "main", "main", None)
        lifecycle.remember_message(7, 102, context=True)

        await lifecycle.show_screen(
            7,
            "changes",
            "changes",
            None,
            source_message={"message_id": 102, "document": {"file_id": "x"}},
        )

        assert (7, 102) not in transport.deleted
        assert transport.markup_edits == [(7, 102, {"inline_keyboard": []})]
        assert len(transport.sent) == 2

    asyncio.run(scenario())


def test_temporary_message_is_tracked_separately_from_screen() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)
        temporary = await lifecycle.show_temporary(7, "toast", ttl=0.01)

        assert temporary in lifecycle.session(7).temporary_message_ids
        await asyncio.sleep(0.02)
        assert (7, temporary) in transport.deleted

    asyncio.run(scenario())


def test_lifecycle_labels_api_calls_for_tgux_journal() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)

        await lifecycle.show_screen(7, "main", "Главное меню")
        await lifecycle.show_progress(7, "Проверка 10%")
        await lifecycle.show_temporary(7, "Сохранено", ttl=60)
        await lifecycle.publish_persistent(7, "report.md")

        assert transport.send_metadata == [
            ("main", "navigation"),
            ("temporary", "temporary"),
            ("persistent", "persistent"),
        ]
        assert transport.edit_metadata == [("scan", "progress")]

    asyncio.run(scenario())


def test_temporary_message_cleanup_restores_reusable_menu_tail() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        lifecycle = MessageLifecycleManager(transport)

        menu = await lifecycle.show_screen(7, "main", "Главное меню", None)
        temporary = await lifecycle.show_temporary(7, "Обновлено", ttl=0.01)
        await asyncio.sleep(0.02)
        await lifecycle.show_screen(7, "settings", "Настройки", None)

        assert temporary is not None
        assert menu == lifecycle.session(7).message_id
        assert len(transport.sent) == 2
        assert len(transport.edited) == 1
        assert transport.edited[0][1] == menu

    asyncio.run(scenario())
