from __future__ import annotations

import asyncio

import pytest

from fstec_monitor.telegram.lifecycle import MessageLifecycleManager


class FakeTransport:
    def __init__(self) -> None:
        self.next_message_id = 100
        self.sent: list[tuple[int, str, dict | None]] = []
        self.edited: list[tuple[int, int, str, dict | None]] = []
        self.deleted: list[tuple[int, int]] = []
        self.fail_next_edit: Exception | None = None

    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None) -> int:
        self.next_message_id += 1
        self.sent.append((chat_id, text, reply_markup))
        return self.next_message_id

    async def edit_message(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        if self.fail_next_edit:
            error, self.fail_next_edit = self.fail_next_edit, None
            raise error
        self.edited.append((chat_id, message_id, text, reply_markup))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))


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
