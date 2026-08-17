from types import SimpleNamespace
from typing import ClassVar

import pytest

import fstec_monitor.telegram_bot as telegram_bot_module
from fstec_monitor.models import Event
from fstec_monitor.notify import format_event, should_notify_event
from fstec_monitor.reports import event_report
from fstec_monitor.telegram_bot import (
    TelegramBot,
    admin_keyboard,
    api_url,
    is_admin,
    settings_keyboard,
    telegram_commands,
)


def test_api_url_uses_shared_local_bot_api():
    assert api_url("http://127.0.0.1:8081", "token", "getUpdates") == (
        "http://127.0.0.1:8081/bottoken/getUpdates"
    )


def test_only_configured_admin_is_authorized():
    assert is_admin(151599744, 151599744)
    assert not is_admin(151599745, 151599744)


def test_admin_menu_contains_only_expected_commands():
    commands = telegram_commands()
    assert [item["command"] for item in commands] == ["start", "status", "changes", "report", "errors", "scan", "users", "ignore", "settings", "help"]
    assert admin_keyboard()["keyboard"][0] == [{"text": "/status"}, {"text": "/changes"}]
    assert admin_keyboard()["is_persistent"] is True


def test_admin_menu_exposes_settings_and_manual_scan():
    labels = [button["text"] for row in admin_keyboard()["keyboard"] for button in row]

    assert "⚙️ Настройки" in labels
    assert "🔍 Проверить сейчас" in labels


def test_settings_keyboard_contains_all_schedule_modes():
    callback_data = [
        button["callback_data"]
        for row in settings_keyboard()["inline_keyboard"]
        for button in row
        if "callback_data" in button
    ]

    assert "settings:set:daily_noon" in callback_data
    assert "settings:set:disabled" in callback_data


def test_settings_text_shows_schedule_and_next_run():
    bot = TelegramBot.__new__(TelegramBot)
    bot.get_schedule_mode = lambda: "daily_noon"
    bot.scan_is_running = lambda: False

    text = bot.settings_text()

    assert "раз в сутки в 12:00" in text
    assert "Следующая проверка:" in text


class _FakeSettingsSession:
    values: ClassVar[dict] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, model, key):
        return self.values.get(key)

    def add(self, setting):
        self.values[setting.key] = setting

    def commit(self):
        return None


def test_schedule_mode_is_persisted(monkeypatch):
    _FakeSettingsSession.values = {}
    monkeypatch.setattr(telegram_bot_module, "SessionLocal", _FakeSettingsSession)
    monkeypatch.setattr(telegram_bot_module, "init_db", lambda: None)
    bot = TelegramBot.__new__(TelegramBot)

    bot.set_schedule_mode("disabled")

    assert bot.get_schedule_mode() == "disabled"


def test_settings_callback_changes_mode_and_confirms(monkeypatch):
    bot = TelegramBot.__new__(TelegramBot)
    bot.set_schedule_mode = lambda mode: setattr(bot, "selected_mode", mode)
    sent = []
    async def send(*args):
        sent.append(args)

    async def call(*_args):
        return None

    bot.send = send
    bot.call = call

    import asyncio
    asyncio.run(bot.handle_callback({
        "id": "callback-1",
        "from": {"id": 151599744},
        "data": "settings:set:disabled",
    }))

    assert bot.selected_mode == "disabled"
    assert any("Расписание изменено" in args[1] for args in sent)


def test_run_does_not_start_scan_immediately(monkeypatch):
    class StopRun(Exception):
        pass

    bot = TelegramBot.__new__(TelegramBot)
    bot.offset = None
    started = []
    bot.configure_menu = lambda: None
    bot.get_schedule_mode = lambda: "daily_noon"
    bot.start_scan = lambda: started.append(True)

    async def configure_menu():
        return None

    async def call(method, _payload):
        if method == "getUpdates":
            raise StopRun
        return {}

    bot.configure_menu = configure_menu
    bot.call = call
    monkeypatch.setattr(telegram_bot_module, "init_db", lambda: None)

    with pytest.raises(StopRun):
        import asyncio
        asyncio.run(bot.run())

    assert started == []


def test_scan_is_not_running_before_first_background_task():
    bot = TelegramBot.__new__(TelegramBot)
    bot.scan_task = None
    assert not bot.scan_is_running()


def test_event_message_escapes_diff_for_telegram_html():
    message = format_event(SimpleNamespace(severity="info", summary="x < y", kind="diff", details="a < b"))
    assert "&lt;" in message
    assert "a < b" not in message


def test_event_message_contains_document_link():
    message = format_event(SimpleNamespace(severity="warning", summary="изменение", kind="diff", details="x"), "https://fstec.ru/doc?a=1")
    assert 'href="https://fstec.ru/doc?a=1"' in message


def test_markup_only_events_are_not_sent():
    assert should_notify_event(SimpleNamespace(kind="html_markup_changed"))
    assert should_notify_event(SimpleNamespace(kind="html_content_changed"))


def test_event_report_contains_old_new_and_diff():
    event = Event(
        id=42,
        kind="html_content_changed",
        severity="critical",
        summary="изменена страница",
        details="--- old\n+++ new\n@@\n-old line\n+new line",
    )
    report = event_report(event, "Документ", "https://example.test/doc")
    assert "Событие #42" in report
    assert "old line" in report
    assert "new line" in report
    assert "https://example.test/doc" in report
