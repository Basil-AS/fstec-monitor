from types import SimpleNamespace
from typing import ClassVar

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import fstec_monitor.telegram_bot as telegram_bot_module
from fstec_monitor.models import Base, Document, Event, Snapshot
from fstec_monitor.notify import (
    format_change_digest,
    format_event,
    should_notify_event,
    split_new_errors,
)
from fstec_monitor.reports import event_report, event_report_md
from fstec_monitor.storage import ObjectStore
from fstec_monitor.telegram_bot import (
    ScanProgress,
    TelegramBot,
    admin_keyboard,
    api_url,
    category_token,
    is_admin,
    settings_keyboard,
    telegram_commands,
    user_keyboard,
)


def test_api_url_uses_shared_local_bot_api():
    assert api_url("http://127.0.0.1:8081", "token", "getUpdates") == (
        "http://127.0.0.1:8081/bottoken/getUpdates"
    )


def test_edit_and_delete_message_use_telegram_native_methods():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    calls = []

    async def call(method, payload):
        calls.append((method, payload))
        return True

    bot.call = call
    asyncio.run(bot.edit_message(123, 45, "Обновлено", {"inline_keyboard": []}))
    asyncio.run(bot.delete_message(123, 45))

    assert calls == [
        ("editMessageText", {
            "chat_id": 123,
            "message_id": 45,
            "text": "Обновлено",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {"inline_keyboard": []},
        }),
        ("deleteMessage", {"chat_id": 123, "message_id": 45}),
    ]


def test_callback_edits_origin_message_instead_of_sending_another():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    edited = []
    sent = []

    async def call(method, payload):
        if method == "answerCallbackQuery":
            return True
        edited.append((method, payload))
        return True

    async def send(*args, **kwargs):
        sent.append((args, kwargs))

    bot.call = call
    bot.send = send
    bot.set_schedule_mode = lambda _mode: None
    bot.notifications_enabled = lambda: True
    asyncio.run(bot.handle_callback({
        "id": "callback-1",
        "from": {"id": 151599744},
        "message": {"message_id": 88, "chat": {"id": 151599744}},
        "data": "settings:set:disabled",
    }))

    assert sent == []
    assert edited[0][0] == "editMessageText"
    assert edited[0][1]["chat_id"] == 151599744
    assert edited[0][1]["message_id"] == 88


def test_scan_progress_refresh_edits_one_saved_message():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    bot.scan_status_message = (123, 77)
    bot.scan_progress = ScanProgress(state="running", stage="Документы", completed=3, total=10)
    edited = []

    async def edit_message(*args):
        edited.append(args)

    bot.edit_message = edit_message
    asyncio.run(bot.refresh_scan_status())

    assert len(edited) == 1
    assert edited[0][:2] == (123, 77)
    assert "3/10" in edited[0][2]


def test_temporary_message_is_deleted_after_ttl():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    calls = []

    async def call(method, payload):
        calls.append((method, payload))
        return {"message_id": 77} if method == "sendMessage" else True

    bot.call = call
    async def run():
        await bot.send_temporary(123, "Временное сообщение", ttl=0.01)
        await asyncio.sleep(0.02)

    asyncio.run(run())

    assert calls[0][0] == "sendMessage"
    assert "reply_markup" not in calls[0][1]
    assert calls[1] == ("deleteMessage", {"chat_id": 123, "message_id": 77})


def test_only_configured_admin_is_authorized():
    assert is_admin(151599744, 151599744)
    assert not is_admin(151599745, 151599744)


def test_admin_menu_contains_only_expected_commands():
    commands = telegram_commands()
    assert [item["command"] for item in commands] == ["start", "status", "changes", "report", "errors", "clear_errors", "scan", "users", "ignore", "settings", "help"]
    assert admin_keyboard()["keyboard"][0] == [{"text": "📊 Статус"}, {"text": "📰 Изменения"}]
    assert admin_keyboard()["is_persistent"] is True


def test_admin_menu_exposes_settings_and_manual_scan():
    labels = [button["text"] for row in admin_keyboard()["keyboard"] for button in row]

    assert "⚙️ Настройки" in labels
    assert "🔍 Проверить сейчас" in labels
    assert all(not label.startswith("/") for label in labels)


def test_user_menu_contains_only_read_only_actions():
    labels = [button["text"] for row in user_keyboard()["keyboard"] for button in row]

    assert labels == ["📰 Последние изменения", "🚫 Мои категории", "ℹ️ Помощь"]
    assert all(not label.startswith("/") for label in labels)


def test_non_admin_command_whitelist_excludes_operations():
    from fstec_monitor.telegram_bot import is_user_command_allowed

    assert is_user_command_allowed("/changes")
    assert is_user_command_allowed("/my_ignore")
    assert not is_user_command_allowed("/scan")
    assert not is_user_command_allowed("/errors")
    assert not is_user_command_allowed("/settings")


def test_admin_reply_keyboard_uses_the_same_command_labels_as_command_menu():
    labels = [button["text"] for row in admin_keyboard()["keyboard"] for button in row]

    assert len(labels) == len(set(labels))
    assert set(labels) == set(telegram_bot_module.ADMIN_LABEL_COMMANDS)


def test_settings_keyboard_contains_all_schedule_modes():
    callback_data = [
        button["callback_data"]
        for row in settings_keyboard()["inline_keyboard"]
        for button in row
        if "callback_data" in button
    ]

    assert "v1:settings:set-daily_noon" in callback_data
    assert "v1:settings:set-disabled" in callback_data


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
    bot.notifications_enabled = lambda: True
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


def test_v1_settings_toggle_answers_callback_and_only_rerenders(monkeypatch):
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    bot.notifications_enabled = lambda: True
    bot.set_notifications_enabled = lambda enabled: setattr(bot, "saved_enabled", enabled)
    calls = []

    async def call(method, payload):
        calls.append((method, payload))
        return {}

    async def render(*args, **kwargs):
        calls.append(("render", args, kwargs))
        return 42

    bot.call = call
    bot._render_screen = render

    asyncio.run(bot.handle_callback({
        "id": "callback-settings",
        "from": {"id": 151599744},
        "message": {"message_id": 42, "chat": {"id": 151599744}},
        "data": "v1:settings:notifications",
    }))

    assert bot.saved_enabled is False
    assert calls[0] == ("answerCallbackQuery", {
        "callback_query_id": "callback-settings",
        "text": "Сохраняю настройки…",
    })
    assert calls[1][0] == "render"
    assert all(item[0] != "sendMessage" for item in calls if item[0] != "render")


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


def test_one_bad_update_is_reported_without_stopping_polling():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    reported = []

    async def broken_handle(_update):
        raise KeyError("malformed update")

    async def report_error(context, exc):
        reported.append((context, type(exc).__name__))

    bot.handle = broken_handle
    bot.report_error = report_error

    asyncio.run(bot.handle_update_safely({"update_id": 1}))

    assert reported == [("ошибка обработки update", "KeyError")]


def test_scan_is_not_running_before_first_background_task():
    bot = TelegramBot.__new__(TelegramBot)
    bot.scan_task = None
    assert not bot.scan_is_running()


def test_scan_progress_text_shows_stage_progress_and_controls():
    from datetime import UTC, datetime

    bot = TelegramBot.__new__(TelegramBot)
    bot.scan_progress = ScanProgress(
        state="running",
        stage="Проверка документов",
        completed=7,
        total=20,
        errors=2,
        started_at=datetime.now(UTC),
    )
    bot.scan_task = SimpleNamespace(done=lambda: False)

    text, markup = bot.scan_progress_card()

    assert "Проверка документов" in text
    assert "7/20" in text
    assert "35%" in text
    callbacks = [button["callback_data"] for row in markup["inline_keyboard"] for button in row]
    assert "v1:scan:status" in callbacks
    assert "v1:scan:stop" in callbacks


def test_active_scan_cannot_be_started_twice_and_can_be_stopped():
    import asyncio

    async def run():
        bot = TelegramBot.__new__(TelegramBot)
        bot.scan_task = None
        bot.scan_progress = ScanProgress()
        bot.scan_cancel_event = asyncio.Event()
        started = asyncio.Event()

        async def fake_scan_task(_trigger="manual"):
            started.set()
            await bot.scan_cancel_event.wait()
            raise asyncio.CancelledError

        bot._scan_task = fake_scan_task
        assert bot.start_scan()
        await started.wait()
        assert not bot.start_scan()
        assert bot.stop_scan()
        await asyncio.sleep(0)
        assert bot.scan_progress.state == "cancelled"
        await asyncio.gather(bot.scan_task, return_exceptions=True)
        assert bot.start_scan()
        bot.stop_scan()
        await asyncio.gather(bot.scan_task, return_exceptions=True)

    asyncio.run(run())


def test_completed_background_scan_task_is_consumed_and_cleared():
    import asyncio

    async def run():
        bot = TelegramBot.__new__(TelegramBot)
        bot.scan_task = None
        task = asyncio.create_task(asyncio.sleep(0, result=3))
        bot.scan_task = task
        task.add_done_callback(bot._scan_task_done)
        await task
        await asyncio.sleep(0)
        assert bot.scan_task is None

    asyncio.run(run())


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


def test_transport_error_can_be_logged_without_recursive_admin_notification():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    bot.last_error_notice = 0.0

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("transport diagnostics must not call the same broken API")

    bot.send = fail_if_called

    asyncio.run(bot.report_error("ошибка Telegram API", RuntimeError("upstream timeout"), notify_admin=False))


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
    assert not should_notify_event(SimpleNamespace(kind="fetch_error"))
    assert not should_notify_event(SimpleNamespace(kind="storage_error"))


def test_repeated_error_is_silenced_after_first_delivery():
    first = SimpleNamespace(kind="fetch_error", document_id=7, summary="каталог недоступен", notified=False)
    repeat = SimpleNamespace(kind="fetch_error", document_id=7, summary="каталог недоступен", notified=False)

    unique, duplicates = split_new_errors([repeat], [first])

    assert unique == []
    assert duplicates == [repeat]


def test_change_digest_combines_real_changes_into_one_compact_message():
    events = [
        SimpleNamespace(id=1, kind="document_added", severity="warning", summary="Новый документ", document_id=4),
        SimpleNamespace(id=2, kind="html_content_changed", severity="critical", summary="Изменён документ", document_id=4),
    ]
    documents = {4: SimpleNamespace(canonical_url="https://example.test/doc")}

    text = format_change_digest(events, documents)

    assert "Изменения ФСТЭК: 2" in text
    assert "Новый документ" in text
    assert text.count("https://example.test/doc") == 2


def test_document_lifecycle_events_are_included_in_bot_status_and_changes():
    assert telegram_bot_module.MEANINGFUL_KINDS >= {"document_removed", "document_restored"}


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
    assert any(f"v1:ignore:{token}" == b["callback_data"] for row in markup["inline_keyboard"] for b in row)
    assert "снова отслеживается" in bot.toggle_ignored_category(token)
    assert bot.ignored_categories_db() == []
    assert bot.toggle_ignored_category("0" * 16) is None


def test_user_ignore_toggle_is_private_to_that_user(tmp_db):
    with tmp_db() as session:
        session.add_all([
            Document(canonical_url="https://example.test/1", category="Приказы"),
            Document(canonical_url="https://example.test/2", category="Письма"),
        ])
        session.commit()
    bot = TelegramBot.__new__(TelegramBot)

    token = category_token("Приказы")
    assert "скрыта из ваших уведомлений" in bot.toggle_user_ignored_category(42, token)
    assert bot.user_ignored_categories(42) == ["Приказы"]
    assert bot.user_ignored_categories(43) == []
    text, markup = bot.user_ignore_text(42)
    assert "Приказы" in text
    assert any(f"v1:userignore:{token}" == button["callback_data"] for row in markup["inline_keyboard"] for button in row)


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
    assert sent[-1][1]["inline_keyboard"][0][0]["callback_data"] == "v1:scan:run"

    asyncio.run(bot.handle_callback({"id": "c1", "from": {"id": 151599744}, "data": "scan:run:cancel"}))
    assert started == []
    assert "отмен" in sent[-1][0].lower()

    asyncio.run(bot.handle_callback({"id": "c2", "from": {"id": 151599744}, "data": "scan:run:confirm"}))
    assert started == [True]
    assert "запущена" in sent[-1][0].lower()


def test_scan_control_callbacks_show_progress_and_confirm_stop():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    bot.scan_task = SimpleNamespace(done=lambda: False)
    bot.scan_progress = ScanProgress(state="running", stage="Проверка документов", completed=1, total=2)
    bot.stop_scan = lambda: True
    sent = []

    async def send(_chat_id, text, markup=None):
        sent.append((text, markup))

    async def call(*_args, **_kwargs):
        return {}

    bot.send = send
    bot.call = call
    asyncio.run(bot.handle_callback({"id": "c1", "from": {"id": 151599744}, "data": "scan:status"}))
    assert "1/2" in sent[-1][0]
    assert sent[-1][1]["inline_keyboard"][0][0]["callback_data"] == "v1:scan:stop"

    asyncio.run(bot.handle_callback({"id": "c2", "from": {"id": 151599744}, "data": "scan:stop"}))
    assert "Остановить" in sent[-1][0]
    assert sent[-1][1]["inline_keyboard"][0][0]["callback_data"] == "scan:stop:confirm"

    asyncio.run(bot.handle_callback({"id": "c3", "from": {"id": 151599744}, "data": "scan:stop:confirm"}))
    assert "остановка" in sent[-1][0].lower()


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
