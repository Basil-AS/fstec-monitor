from types import SimpleNamespace
from typing import ClassVar

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import fstec_monitor.telegram_bot as telegram_bot_module
from fstec_monitor.models import Base, Document, Event, Snapshot
from fstec_monitor.notify import format_event, should_notify_event
from fstec_monitor.reports import event_report, event_report_md
from fstec_monitor.storage import ObjectStore
from fstec_monitor.telegram_bot import (
    TelegramBot,
    admin_keyboard,
    api_url,
    category_token,
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
    assert [item["command"] for item in commands] == ["start", "status", "changes", "report", "diff", "errors", "clear_errors", "scan", "users", "ignore", "settings", "help"]
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


def test_getupdates_timeout_does_not_alert_admin(monkeypatch):
    import asyncio

    import httpx

    class StopRun(Exception):
        pass

    bot = TelegramBot.__new__(TelegramBot)
    bot.offset = None
    bot.get_schedule_mode = lambda: "disabled"
    bot.start_scan = lambda *_args: None
    alerts = []

    async def configure_menu():
        return None

    async def report_error(context, exc):
        alerts.append(context)

    calls = []

    async def call(method, _payload):
        calls.append(method)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow poll")
        raise StopRun

    bot.configure_menu = configure_menu
    bot.report_error = report_error
    bot.call = call
    monkeypatch.setattr(telegram_bot_module, "init_db", lambda: None)

    with pytest.raises(StopRun):
        asyncio.run(bot.run())

    assert calls == ["getUpdates", "getUpdates"]
    assert alerts == []


def test_event_message_escapes_diff_for_telegram_html():
    message = format_event(SimpleNamespace(severity="info", summary="x < y", kind="diff", details="a < b"))
    assert "&lt;" in message
    assert "a < b" not in message


def test_event_message_contains_document_link():
    message = format_event(SimpleNamespace(severity="warning", summary="изменение", kind="diff", details="x"), "https://fstec.ru/doc?a=1")
    assert 'href="https://fstec.ru/doc?a=1"' in message


def test_markup_only_events_are_not_sent():
    assert not should_notify_event(SimpleNamespace(kind="html_markup_changed"))
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


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/bot-test.db")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(telegram_bot_module, "SessionLocal", session_factory)
    monkeypatch.setattr(telegram_bot_module, "init_db", lambda: None)
    return session_factory


def test_event_report_md_wraps_diff_in_fence():
    event = Event(
        id=7,
        kind="html_content_changed",
        severity="critical",
        summary="изменена страница",
        details="--- old\n+++ new\n@@\n-old\n+new",
    )
    md = event_report_md(event, "Документ", "https://example.test/doc")
    assert "# Отчёт об изменении · событие #7" in md
    assert "```diff" in md
    assert "-old" in md and "+new" in md


def test_clear_errors_removes_only_error_events(tmp_db):
    with tmp_db() as session:
        session.add(Event(kind="fetch_error", severity="warning", summary="e1"))
        session.add(Event(kind="storage_error", severity="critical", summary="e2"))
        session.add(Event(kind="document_added", severity="warning", summary="keep"))
        session.commit()
    bot = TelegramBot.__new__(TelegramBot)

    text, markup = bot.clear_errors_text()
    assert "2 событий" in text
    assert markup["inline_keyboard"][0][0]["callback_data"] == "errors:clear:confirm"

    assert bot.clear_errors() == 2
    with tmp_db() as session:
        remaining = [e.kind for e in session.scalars(select(Event)).all()]
    assert remaining == ["document_added"]
    assert bot.clear_errors_text()[1] is None


def test_ignore_toggle_persists_category(tmp_db):
    with tmp_db() as session:
        session.add(Document(canonical_url="https://example.test/1", category="Приказы"))
        session.commit()
    bot = TelegramBot.__new__(TelegramBot)

    token = category_token("Приказы")
    assert "добавлена в игнор" in bot.toggle_ignored_category(token)
    assert bot.ignored_categories_db() == ["Приказы"]
    text, markup = bot.ignore_text()
    assert "Приказы" in text
    assert any(f"ignore:t:{token}" == b["callback_data"] for row in markup["inline_keyboard"] for b in row)
    assert "снова отслеживается" in bot.toggle_ignored_category(token)
    assert bot.ignored_categories_db() == []
    assert bot.toggle_ignored_category("0" * 16) is None


def test_send_report_always_attaches_markdown_diff(tmp_db, tmp_path, monkeypatch):
    store = ObjectStore(root=tmp_path / "objects", quota_root=tmp_path, quota_bytes=10**9)
    _, old_key = store.put(b"old line\n", ".txt")
    _, new_key = store.put(b"new line\n", ".txt")
    with tmp_db() as session:
        doc = Document(canonical_url="https://example.test/doc", title="Doc")
        session.add(doc)
        session.flush()
        for key in (old_key, new_key):
            session.add(Snapshot(
                document_id=doc.id, status_code=200, final_url=doc.canonical_url,
                raw_sha256="a" * 64, semantic_sha256="b" * 64, html_sha256="c" * 64,
                raw_object=key, normalized_html_object=key, normalized_text_object=key,
            ))
        event = Event(
            document_id=doc.id, kind="html_content_changed", severity="critical",
            summary="изменена страница", details="--- old\n+++ new\n-old line\n+new line",
        )
        session.add(event)
        session.commit()
        event_id = event.id
    monkeypatch.setattr(telegram_bot_module, "ObjectStore", lambda: store)
    bot = TelegramBot.__new__(TelegramBot)
    files = []

    async def send_file(_chat_id, name, data, caption=""):
        files.append((name, data))

    async def send(*_args, **_kwargs):
        return None

    bot.send_file = send_file
    bot.send = send

    import asyncio
    asyncio.run(bot.send_report(123, event_id))

    by_name = dict(files)
    assert f"diff_{event_id}.md" in by_name
    md = by_name[f"diff_{event_id}.md"].decode()
    assert "```diff" in md
    assert "-old line" in md and "+new line" in md
    assert "old-Doc.txt" in by_name and "new-Doc.txt" in by_name


def test_scan_requires_confirmation_and_callback_starts_it():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    bot.scan_task = None
    started = []
    bot.start_scan = lambda: started.append(True) or True
    sent = []

    async def send(_chat_id, text, markup=None):
        sent.append((text, markup))

    async def call(*_args, **_kwargs):
        return {}

    bot.send = send
    bot.call = call

    admin_update = {"message": {"from": {"id": 151599744}, "chat": {"id": 151599744}, "text": "/scan"}}
    asyncio.run(bot.handle(admin_update))
    assert started == []
    assert "Запустить полную проверку" in sent[-1][0]
    assert sent[-1][1]["inline_keyboard"][0][0]["callback_data"] == "scan:run:confirm"

    asyncio.run(bot.handle_callback({"id": "c1", "from": {"id": 151599744}, "data": "scan:run:cancel"}))
    assert started == []
    assert "отмен" in sent[-1][0].lower()

    asyncio.run(bot.handle_callback({"id": "c2", "from": {"id": 151599744}, "data": "scan:run:confirm"}))
    assert started == [True]
    assert "запущена" in sent[-1][0].lower()


def test_fmt_duration():
    from fstec_monitor.telegram_bot import _fmt_duration

    assert _fmt_duration(5) == "5 с"
    assert _fmt_duration(65) == "1 мин 05 с"
    assert _fmt_duration(3661) == "1 ч 01 мин"


def test_status_text_shows_scan_duration(tmp_db):
    import asyncio
    import time
    from datetime import UTC, datetime, timedelta

    from fstec_monitor.models import ScanRun

    with tmp_db() as session:
        finished = datetime.now(UTC)
        session.add(ScanRun(started_at=finished - timedelta(seconds=125), finished_at=finished, documents=42, trigger="manual"))
        session.commit()
    bot = TelegramBot.__new__(TelegramBot)
    bot._quota_cache = (time.monotonic(), (0, 5 * 1024**3))
    bot.scan_is_running = lambda: False

    text = asyncio.run(bot.status_text())

    assert "Длительность последней проверки: 2 мин 05 с" in text
    assert "Средняя длительность (1 зап.)" in text
