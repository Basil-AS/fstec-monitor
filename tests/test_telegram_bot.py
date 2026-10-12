from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import fstec_monitor.telegram_bot as telegram_bot_module
from fstec_monitor.models import (
    Base,
    Document,
    Event,
    EventDelivery,
    Snapshot,
    UserAccess,
    UserAccessEvent,
)
from fstec_monitor.notify import (
    format_change_digest,
    format_event,
    should_notify_event,
    split_new_errors,
)
from fstec_monitor.reports import event_report, event_report_md
from fstec_monitor.storage import ObjectStore
from fstec_monitor.telegram.lifecycle import MessageLifecycleManager
from fstec_monitor.telegram.list_views import paginate_lines
from fstec_monitor.telegram.navigation import NavigationStack
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


def test_callback_access_boundary_rejects_admin_screens_for_non_admin():
    bot = TelegramBot.__new__(TelegramBot)

    assert bot._callback_access_allowed("screen", "users", 42) is False
    assert bot._callback_access_allowed("screen", "changes", 42) is True


@pytest.mark.parametrize("action", ["scan", "ignore", "users", "errors", "settings", "access"])
def test_non_admin_callback_actions_are_denied_by_default(action):
    bot = TelegramBot.__new__(TelegramBot)

    assert bot._callback_access_allowed(action, "run", 42) is False


@pytest.mark.parametrize("action", ["nav", "userignore"])
def test_non_admin_callback_navigation_and_personal_filters_remain_allowed(action):
    bot = TelegramBot.__new__(TelegramBot)

    assert bot._callback_access_allowed(action, "value", 42) is True


def test_non_admin_menu_callback_is_limited_to_user_screens():
    bot = TelegramBot.__new__(TelegramBot)

    assert bot._callback_access_allowed("menu", "main", 42)
    assert bot._callback_access_allowed("menu", "changes", 42)
    assert bot._callback_access_allowed("menu", "my-ignore", 42)
    assert not bot._callback_access_allowed("menu", "users", 42)
    assert not bot._callback_access_allowed("menu", "settings", 42)


def test_update_envelope_routing_keeps_callback_and_message_paths_separate():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    routed = []

    async def callback_path(value):
        routed.append(("callback", value))

    async def message_path(value):
        routed.append(("message", value))

    bot._handle_callback_update = callback_path
    bot._handle_message_update = message_path

    asyncio.run(bot.handle({"callback_query": {"id": "c1"}}))
    asyncio.run(bot.handle({"message": {"message_id": 7}}))

    assert routed == [
        ("callback", {"id": "c1"}),
        ("message", {"message_id": 7}),
    ]


def test_back_callback_renders_previous_payload_context():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    stack = NavigationStack()
    stack.reset("main")
    stack.push("changes", {"page": 3})
    stack.push("event", {"event_id": 512})
    bot.navigation = {7: stack}
    rendered = []

    async def render_screen(chat_id, screen, **kwargs):
        rendered.append((chat_id, screen, kwargs))

    bot._render_screen = render_screen

    async def reply(*_args, **_kwargs):
        raise AssertionError("Back should render the previous screen")

    handled = asyncio.run(bot._dispatch_decoded_callback(
        "nav", "back", 7, 7, {}, 22, reply
    ))

    assert handled is True
    assert rendered == [(7, "changes", {
        "reset": True,
        "source_message": {},
        "reason": "back",
        "payload": {"page": 3},
    })]


@pytest.mark.parametrize("method,payload", [
    ("editMessageText", {"chat_id": 1, "message_id": 2, "text": "updated"}),
    ("sendMessage", {"chat_id": 1, "text": "hello"}),
])
def test_aiogram_transport_does_not_retry_ambiguous_outcome(method, payload, monkeypatch):
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    calls = []

    class API:
        async def call(self, method_name, request):
            calls.append((method_name, request))
            raise RuntimeError("Telegram transport outcome unknown")

        async def call_rich(self, method_name, rich, fallback, *, fallback_method=None):
            calls.append((method_name, rich))
            raise RuntimeError("Telegram transport outcome unknown")

    bot.api = API()
    monkeypatch.setattr(telegram_bot_module.settings, "telegram_rich_messages_enabled", False)

    with pytest.raises(RuntimeError, match="outcome unknown"):
        asyncio.run(bot.call(method, payload))
    assert len(calls) == 1


def test_report_command_description_allows_latest_event_default():
    commands = {item["command"]: item["description"] for item in telegram_commands()}

    assert "последн" in commands["report"].casefold()
    assert "id" in commands["report"].casefold()


def test_help_command_dispatches_to_help_screen():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    rendered = []

    async def render(chat_id, screen, **kwargs):
        rendered.append((chat_id, screen, kwargs))
        return 1

    bot._render_screen = render
    asyncio.run(bot._dispatch_command("/help", ["/help"], 151599744, 151599744))

    assert rendered == [(151599744, "help", {"reset": True})]


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


def test_long_send_attaches_keyboard_only_to_tail_fragment():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    calls = []

    async def call(method, payload):
        calls.append((method, payload))
        return {"message_id": len(calls)}

    bot.call = call
    asyncio.run(bot.send(123, "x" * 8000, {"inline_keyboard": []}))

    assert len(calls) == 3
    assert all("reply_markup" not in payload for _, payload in calls[:-1])
    assert calls[-1][1]["reply_markup"] == {"inline_keyboard": []}


def test_callback_screen_action_reuses_origin_message_context(monkeypatch):
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    monkeypatch.setattr(telegram_bot_module.settings, "telegram_admin_id", 151599744)
    calls = []

    async def call(method, payload):
        calls.append((method, payload))
        return True

    async def render(chat_id, screen, **kwargs):
        calls.append(("render", chat_id, screen, kwargs))

    bot.call = call
    bot._render_screen = render
    message = {"message_id": 88, "chat": {"id": 151599744, "type": "private"}}
    asyncio.run(bot.handle_callback({
        "id": "callback-1",
        "from": {"id": 151599744},
        "message": message,
        "data": "v1:screen:settings",
    }))

    assert calls[0][0] == "answerCallbackQuery"
    assert calls[1] == ("render", 151599744, "settings", {"source_message": message})


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


def test_scan_progress_refresh_swallows_transport_failure_without_losing_state():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    bot.scan_status_message = (123, 77)
    bot.scan_progress = ScanProgress(state="running", stage="Документы")

    async def edit_message(*_args):
        raise httpx.ReadTimeout("telegram unavailable")

    bot.edit_message = edit_message
    asyncio.run(bot.refresh_scan_status())

    assert bot.scan_status_message == (123, 77)


def test_scan_progress_rebinds_pointer_when_deleted_ui_is_recreated():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    bot.scan_status_message = (123, 77)
    bot.scan_progress = ScanProgress(state="running", stage="Документы")

    class _Lifecycle:
        def adopt_screen(self, *_args, **_kwargs):
            return None

        async def show_progress(self, *_args, **_kwargs):
            return 88

    bot.lifecycle = _Lifecycle()
    asyncio.run(bot.refresh_scan_status())

    assert bot.scan_status_message == (123, 88)


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


def test_pending_access_request_does_not_spam_user_on_repeated_updates(tmp_db):
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    sent = []

    async def send(*args, **kwargs):
        sent.append((args, kwargs))

    bot.send = send
    request = {"user_id": 42, "chat_id": 42, "username": "user", "display_name": "User"}

    asyncio.run(bot.request_access(**request))
    asyncio.run(bot.request_access(**request))

    assert len(sent) == 2  # one admin alert and one initial user acknowledgement
    assert [call[0][0] for call in sent].count(telegram_bot_module.settings.telegram_admin_id) == 1
    callback_data = sent[0][0][2]["inline_keyboard"][0][0]["callback_data"]
    assert callback_data.startswith("v1:access:approve-42-pending-")
    assert [call[0][0] for call in sent].count(42) == 1
    with tmp_db() as session:
        assert session.get(UserAccess, 42).attempt_count == 2


def test_concurrent_pending_access_requests_notify_once(tmp_db):
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    sent = []

    async def send(*args, **kwargs):
        sent.append((args, kwargs))
        await asyncio.sleep(0)

    bot.send = send
    request = {"user_id": 43, "chat_id": 43, "username": "racer", "display_name": "Racer"}

    async def run():
        await asyncio.gather(bot.request_access(**request), bot.request_access(**request))

    asyncio.run(run())

    assert [call[0][0] for call in sent].count(telegram_bot_module.settings.telegram_admin_id) == 1
    assert [call[0][0] for call in sent].count(43) == 1
    with tmp_db() as session:
        assert session.get(UserAccess, 43).attempt_count == 2


def test_only_configured_admin_is_authorized():
    assert is_admin(151599744, 151599744)
    assert not is_admin(151599745, 151599744)


def test_admin_menu_contains_only_expected_commands():
    commands = telegram_commands()
    assert [item["command"] for item in commands] == ["start", "status", "changes", "report", "errors", "clear_errors", "scan", "users", "ignore", "settings", "help"]
    buttons = [button for row in admin_keyboard()["inline_keyboard"] for button in row]
    assert {button["text"] for button in buttons} >= {"📊 Статус", "📰 Изменения", "⚙️ Настройки"}
    assert all(button["callback_data"].startswith("v1:menu:") for button in buttons)
    assert "keyboard" not in admin_keyboard()  # dashboard navigation is inline


def test_admin_menu_exposes_settings_and_manual_scan():
    buttons = [button for row in admin_keyboard()["inline_keyboard"] for button in row]
    labels = [button["text"] for button in buttons]

    assert "⚙️ Настройки" in labels
    assert "🔍 Проверка" in labels
    assert all(button["callback_data"].startswith("v1:menu:") for button in buttons)


def test_start_command_renders_reusable_screen_instead_of_new_message(monkeypatch):
    import asyncio

    monkeypatch.setattr(telegram_bot_module.settings, "telegram_admin_id", 151599744)
    bot = TelegramBot.__new__(TelegramBot)
    rendered = []

    async def render(chat_id, screen, **kwargs):
        rendered.append((chat_id, screen, kwargs))

    async def send(*_args, **_kwargs):
        raise AssertionError("navigation commands must reuse the lifecycle screen")

    bot._render_screen = render
    bot.send = send

    asyncio.run(bot.handle({
        "message": {
            "from": {"id": 151599744},
            "chat": {"id": 151599744},
            "text": "/start",
        }
    }))

    assert rendered == [(151599744, "main", {"reset": True})]


def test_user_menu_contains_only_read_only_actions():
    buttons = [button for row in user_keyboard()["inline_keyboard"] for button in row]
    labels = [button["text"] for button in buttons]

    assert labels == ["📰 Изменения", "🚫 Мои категории"]
    assert all(button["callback_data"].startswith("v1:menu:") for button in buttons)
    assert all(not label.startswith("/") for label in labels)


def test_non_admin_command_whitelist_excludes_operations():
    from fstec_monitor.telegram_bot import is_user_command_allowed

    assert is_user_command_allowed("/changes")
    assert is_user_command_allowed("/my_ignore")
    assert not is_user_command_allowed("/scan")
    assert not is_user_command_allowed("/errors")
    assert not is_user_command_allowed("/settings")


def test_stale_callback_cleans_old_navigation_message_without_dispatching():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    bot.scan_task = None
    cleaned = []
    answered = []

    class Session:
        message_id = 99

    class Lifecycle:
        def session(self, _chat_id):
            return Session()

        @staticmethod
        def is_media_message(_message):
            return False

        @staticmethod
        def is_current_screen_message(_chat_id, _message_id):
            return False

        async def cleanup_trigger_message(self, chat_id, message):
            cleaned.append((chat_id, message["message_id"]))

    bot.lifecycle = Lifecycle()

    async def answer(callback_id, text, **_kwargs):
        answered.append((callback_id, text))

    bot.answer_callback = answer
    callback = {
        "id": "stale-1",
        "from": {"id": 151599744},
        "data": "v1:nav:back",
        "message": {"message_id": 10, "chat": {"id": 123}},
    }

    asyncio.run(bot.handle_callback(callback))

    assert answered == [("stale-1", "Экран устарел")]
    assert cleaned == [(123, 10)]


def test_non_admin_restricted_command_uses_temporary_feedback(monkeypatch):
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    temporary = []
    regular = []

    async def send_temporary(*args, **kwargs):
        temporary.append((args, kwargs))

    async def send(*args, **kwargs):
        regular.append((args, kwargs))

    async def send_chat_action(*_args, **_kwargs):
        return None

    bot.send_temporary = send_temporary
    bot.send = send
    bot.send_chat_action = send_chat_action
    monkeypatch.setattr(telegram_bot_module.settings, "telegram_admin_id", 1)

    asyncio.run(bot._dispatch_command("/scan", ["/scan"], 42, 42))

    assert regular == []
    assert temporary and temporary[0][0][1].startswith("Доступно только")


def test_admin_inline_dashboard_has_unique_role_scoped_menu_actions():
    buttons = [button for row in admin_keyboard()["inline_keyboard"] for button in row]
    labels = [button["text"] for button in buttons]

    assert len(labels) == len(set(labels))
    assert all(button["callback_data"].startswith("v1:menu:") for button in buttons)
    assert {"⚙️ Настройки", "👥 Пользователи", "🔍 Проверка"} <= set(labels)
    assert "keyboard" not in admin_keyboard()


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


def test_settings_callback_changes_mode_and_renders_settings_screen(monkeypatch):
    bot = TelegramBot.__new__(TelegramBot)
    monkeypatch.setattr(telegram_bot_module.settings, "telegram_admin_id", 151599744)
    bot.set_schedule_mode = lambda mode: setattr(bot, "selected_mode", mode)
    renders = []

    async def call(*_args):
        return None

    async def render(*args, **kwargs):
        renders.append((args, kwargs))

    bot.call = call
    bot._render_screen = render

    import asyncio
    asyncio.run(bot.handle_callback({
        "id": "callback-1",
        "from": {"id": 151599744},
        "message": {"message_id": 88, "chat": {"id": 151599744, "type": "private"}},
        "data": "v1:settings:set-disabled",
    }))

    assert bot.selected_mode == "disabled"
    assert renders == [((151599744, "settings"), {"reset": True})]


def test_legacy_settings_callback_adapter_handles_toggle():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    bot.notifications_enabled = lambda: True
    bot.set_notifications_enabled = lambda enabled: setattr(bot, "saved_enabled", enabled)
    replies = []

    async def reply(text, markup=None, fallback_chat_id=None):
        replies.append((text, markup, fallback_chat_id))

    handled = asyncio.run(bot._dispatch_legacy_settings_callback(["settings", "notifications", "toggle"], reply))

    assert handled is True
    assert bot.saved_enabled is False
    assert replies and "Уведомления выключены" in replies[0][0]


def test_legacy_admin_callback_adapter_handles_main_menu():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    replies = []

    async def reply(text, markup=None, fallback_chat_id=None):
        replies.append((text, markup, fallback_chat_id))

    handled = asyncio.run(bot._dispatch_legacy_admin_callback(["menu", "main"], reply))

    assert handled is True
    assert replies == [("Главное меню готово. Выберите действие:", None, None)]


def test_legacy_user_filter_callback_adapter_handles_toggle(monkeypatch):
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    replies = []
    bot.toggle_user_ignored_category = lambda user_id, token: f"user={user_id}; token={token}"

    class _AllowedSession(_FakeSettingsSession):
        def get(self, _model, _key):
            return SimpleNamespace(status="approved")

    monkeypatch.setattr(telegram_bot_module, "SessionLocal", _AllowedSession)

    async def reply(text, markup=None, fallback_chat_id=None):
        replies.append((text, markup, fallback_chat_id))

    handled = asyncio.run(bot._dispatch_legacy_user_callback(["userignore", "t", "cat"], 42, reply))

    assert handled is True
    assert replies == [("user=42; token=cat", None, 42)]


def test_v1_settings_toggle_answers_callback_and_only_rerenders(monkeypatch):
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    monkeypatch.setattr(telegram_bot_module.settings, "telegram_admin_id", 151599744)
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
        "message": {"message_id": 42, "chat": {"id": 151599744, "type": "private"}},
        "data": "v1:settings:notifications",
    }))

    assert bot.saved_enabled is False
    assert calls[0] == ("answerCallbackQuery", {
        "callback_query_id": "callback-settings",
        "text": "Сохраняю настройки…",
    })
    assert calls[1][0] == "render"
    assert all(item[0] != "sendMessage" for item in calls if item[0] != "render")


def test_duplicate_scan_callback_uses_already_running_toast():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    bot.scan_task = SimpleNamespace(done=lambda: False)
    calls = []

    async def call(method, payload):
        calls.append((method, payload))
        return {}

    bot.call = call
    bot.start_scan = lambda: False
    bot._render_screen = lambda *args, **kwargs: asyncio.sleep(0)

    asyncio.run(bot.handle_callback({
        "id": "callback-duplicate-scan",
        "from": {"id": 151599744},
        "message": {"message_id": 42, "chat": {"id": 151599744}},
        "data": "v1:scan:run",
    }))

    assert calls[0] == ("answerCallbackQuery", {
        "callback_query_id": "callback-duplicate-scan",
        "text": "Уже выполняется",
    })


def test_unexpected_scan_error_marks_progress_failed_and_reports_it():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    bot.scan_progress = ScanProgress(state="running", stage="Проверка документов")
    bot.scan_cancel_event = asyncio.Event()
    refreshed = []
    reported = []

    async def scan(**_kwargs):
        raise KeyError("broken state")

    async def refresh():
        refreshed.append(True)

    async def report(context, exc):
        reported.append((context, exc))

    bot.scan = scan
    bot.refresh_scan_status = refresh
    bot.report_error = report

    with pytest.raises(KeyError):
        asyncio.run(bot._scan_task())

    assert bot.scan_progress.state == "failed"
    assert "Проверка завершилась с ошибкой" == bot.scan_progress.stage
    assert bot.scan_progress.last_error == "'broken state'"
    assert refreshed == [True]
    assert reported and reported[0][0] == "ошибка фоновой проверки"


def test_stale_callback_is_acknowledged_but_does_not_dispatch():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    bot.lifecycle = MessageLifecycleManager(SimpleNamespace())
    bot.lifecycle.remember_message(151599744, 101, screen="main")
    bot.lifecycle.remember_message(151599744, 102, context=True)
    calls = []

    async def call(method, payload):
        calls.append((method, payload))
        return {}

    async def unexpected_render(*_args, **_kwargs):
        raise AssertionError("stale callback was dispatched")

    bot.call = call
    bot._render_screen = unexpected_render

    asyncio.run(bot.handle_callback({
        "id": "callback-stale",
        "from": {"id": 151599744},
        "message": {"message_id": 101, "chat": {"id": 151599744}},
        "data": "v1:menu:main",
    }))

    assert calls[0] == ("answerCallbackQuery", {
        "callback_query_id": "callback-stale",
        "text": "Экран устарел",
    })


def test_dispatcher_polling_is_single_sequential_loop_and_does_not_start_scan(monkeypatch):
    import asyncio

    calls = []
    started = []

    class Watchdog:
        async def start(self):
            calls.append("watchdog-start")

        async def stop(self):
            calls.append("watchdog-stop")

    class Dispatcher:
        async def start_polling(self, _bot, **kwargs):
            calls.append(("poll", kwargs))

    async def no_op():
        return None

    bot = TelegramBot.__new__(TelegramBot)
    bot.bot = SimpleNamespace(get_me=no_op)
    bot.watchdog = Watchdog()
    bot.configure_menu = no_op
    bot.create_dispatcher = lambda: Dispatcher()
    bot._run_scheduler = no_op
    bot.start_scan = lambda: started.append(True)
    monkeypatch.setattr(telegram_bot_module, "init_db", lambda: None)
    monkeypatch.setattr(telegram_bot_module, "next_scheduled_at", lambda *_args: None)

    asyncio.run(bot.run())

    assert calls[0] == "watchdog-start"
    polling = calls[1][1]
    assert polling["handle_as_tasks"] is False
    assert polling["polling_timeout"] == 25
    assert polling["allowed_updates"] == ["message", "callback_query"]
    assert calls[-1] == "watchdog-stop"
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


def test_legacy_scan_callback_adapter_handles_cancel_without_new_message():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    bot.scan_progress = ScanProgress(state="running", stage="Проверка документов")
    replies = []

    async def reply(text, markup=None, fallback_chat_id=None):
        replies.append((text, markup, fallback_chat_id))

    handled = asyncio.run(bot._dispatch_legacy_scan_callback(["scan", "stop", "cancel"], reply))

    assert handled is True
    assert replies and "Остановка отменена" in replies[0][0]


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


def test_dispatcher_polling_failure_is_not_misreported_as_command_error(monkeypatch):
    import asyncio

    import httpx

    alerts = []

    class Watchdog:
        async def start(self):
            return None

        async def stop(self):
            return None

    class Dispatcher:
        async def start_polling(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("long polling timeout")

    async def no_op():
        return None

    async def report_error(context, _exc, **_kwargs):
        alerts.append(context)

    bot = TelegramBot.__new__(TelegramBot)
    bot.bot = SimpleNamespace(get_me=no_op)
    bot.watchdog = Watchdog()
    bot.configure_menu = no_op
    bot.create_dispatcher = lambda: Dispatcher()
    bot._run_scheduler = no_op
    bot.report_error = report_error
    monkeypatch.setattr(telegram_bot_module, "init_db", lambda: None)
    monkeypatch.setattr(telegram_bot_module, "next_scheduled_at", lambda *_args: None)

    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(bot.run())

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


def test_changes_text_groups_by_category_and_hides_initial_attachment_noise(tmp_db):
    with tmp_db() as session:
        first = Document(canonical_url="https://example.test/1", title="Первый", category="Доклады")
        second = Document(canonical_url="https://example.test/2", title="Второй", category="Доклады")
        session.add_all([first, second])
        session.flush()
        session.add_all([
            Event(document_id=first.id, kind="attachment_added", severity="warning", summary="добавлено вложение: Первый.pdf", details="https://example.test/1.pdf"),
            Event(document_id=first.id, kind="document_added", severity="warning", summary="добавлен документ: Первый"),
            Event(document_id=first.id, kind="attachment_content_changed", severity="critical", summary="обновлено вложение: Первый.odt"),
            Event(document_id=second.id, kind="document_added", severity="warning", summary="добавлен документ: Второй"),
        ])
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    text = bot.changes_text(limit=10)

    assert text.count("📁 Доклады") == 1
    assert "добавлено вложение: Первый.pdf" not in text
    assert "добавлен документ: Первый" in text
    assert "обновлено вложение: Первый.odt" in text
    assert "добавлен документ: Второй" in text


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
    assert markup["inline_keyboard"][0][0]["callback_data"] == "v1:errors:clear-confirm"

    assert bot.clear_errors() == 2
    with tmp_db() as session:
        remaining = [e.kind for e in session.scalars(select(Event)).all()]
    assert remaining == ["document_added"]


def test_clear_errors_confirmation_uses_versioned_callback_protocol(tmp_db):
    with tmp_db() as session:
        session.add(Event(kind="fetch_error", summary="Ошибка загрузки", details="temporary"))
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)

    _, markup = bot.clear_errors_text()

    callbacks = [button["callback_data"] for row in (markup or {}).get("inline_keyboard", []) for button in row]
    assert callbacks == ["v1:errors:clear-confirm", "v1:errors:clear-cancel"]


def test_versioned_clear_errors_callback_is_dispatched_without_new_message():
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    rendered = []
    replies = []

    async def render(*args, **kwargs):
        rendered.append((args, kwargs))
        return 1

    async def reply(text, markup=None, fallback_chat_id=None):
        replies.append((text, markup, fallback_chat_id))

    bot._render_screen = render
    bot.clear_errors = lambda: 3

    handled = asyncio.run(
        bot._dispatch_decoded_callback("errors", "clear-confirm", 7, 7, {}, None, reply)
    )

    assert handled is True
    assert replies == [("🧹 Журнал ошибок очищен: удалено 3 событий.", None, None)]
    assert rendered == []


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


def test_admin_user_actions_use_nonce_bound_versioned_callback_protocol(tmp_db):
    with tmp_db() as session:
        session.add(UserAccess(user_id=42, chat_id=42, status="pending", request_nonce="abcdef123456"))
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    _, markup = bot.users_text()

    callbacks = [button["callback_data"] for row in markup["inline_keyboard"] for button in row]
    assert callbacks == [
        "v1:access:approve-42-pending-abcdef123456",
        "v1:access:deny-42-pending-abcdef123456",
        "v1:access:block-42-pending-abcdef123456",
    ]


def test_admin_user_requests_are_paginated(tmp_db):
    with tmp_db() as session:
        session.add_all([
            UserAccess(user_id=100 + index, chat_id=100 + index, status="pending")
            for index in range(6)
        ])
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    text, markup = bot.users_text()

    assert "страница 1/2" in text
    assert len(markup["inline_keyboard"]) == 6  # five users + pager
    pager = markup["inline_keyboard"][-1]
    assert pager[2]["callback_data"] == "v1:screen:users-page-1"


def test_paginate_lines_keeps_page_bounds_and_reports_page_count():
    page, number, total = paginate_lines(["a", "b", "c", "d", "e"], page=1, page_size=2)

    assert page == ["c", "d"]
    assert number == 1
    assert total == 3

    page, number, total = paginate_lines(["a"], page=99, page_size=2)
    assert page == ["a"]
    assert number == 0
    assert total == 1


def test_global_categories_are_paginated(tmp_db):
    with tmp_db() as session:
        session.add_all(
            [
                Document(canonical_url=f"https://example.test/{index}", category=f"Category {index}")
                for index in range(21)
            ]
        )
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    text, markup = bot.ignore_text()

    assert len(markup["inline_keyboard"]) == 9  # eight categories + pager
    assert "страница 1/3" in text
    assert markup["inline_keyboard"][-1][2]["callback_data"] == "v1:screen:filters-page-1"


def test_error_screen_is_paginated(tmp_db):
    with tmp_db() as session:
        session.add_all(
            [
                Event(kind="fetch_error", severity="error", summary=f"error {index}", details="details")
                for index in range(11)
            ]
        )
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    text, markup = bot.errors_page()

    assert "страница 1/3" in text
    assert len(markup) == 1
    assert markup[0][2]["callback_data"] == "v1:screen:errors-page-1"


def test_error_screen_escapes_source_content(tmp_db):
    with tmp_db() as session:
        session.add(Event(kind="fetch_error", severity="error", summary="<b>bad</b>", details="<script>x</script>"))
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    text, _ = bot.errors_page()

    assert "&lt;b&gt;bad&lt;/b&gt;" in text
    assert "&lt;script&gt;x&lt;/script&gt;" in text


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
    actions = []

    async def send_file(_chat_id, name, data, caption=""):
        files.append((name, data))

    async def send(*_args, **_kwargs):
        return None

    async def send_chat_action(chat_id, action):
        actions.append((chat_id, action))

    bot.send_file = send_file
    bot.send = send
    bot.send_chat_action = send_chat_action

    import asyncio
    asyncio.run(bot.send_report(123, event_id))

    assert actions == [(123, "upload_document")]
    by_name = dict(files)
    assert f"diff_{event_id}.md" in by_name
    md = by_name[f"diff_{event_id}.md"].decode()
    assert "```diff" in md
    assert "-old line" in md and "+new line" in md
    assert "old-Doc.txt" in by_name and "new-Doc.txt" in by_name
    assert (tmp_path / "objects" / "reports" / f"event-{event_id}.md").read_text() == md


def test_missing_report_uses_temporary_feedback(monkeypatch):
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    temporary = []
    regular = []
    bot._build_report = lambda _event_id: None

    async def send_temporary(*args, **kwargs):
        temporary.append((args, kwargs))

    async def send(*args, **kwargs):
        regular.append((args, kwargs))

    bot.send_temporary = send_temporary
    bot.send = send
    bot.send_chat_action = lambda *_args, **_kwargs: asyncio.sleep(0)

    asyncio.run(bot.send_report(123, 999))

    assert regular == []
    assert temporary and "не найдено" in temporary[0][0][1]


def test_missing_report_versions_use_temporary_feedback(monkeypatch):
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    temporary = []
    bot._build_report = lambda _event_id: ("# report", [])
    bot.send_file = lambda *_args, **_kwargs: asyncio.sleep(0)

    async def send_temporary(*args, **kwargs):
        temporary.append((args, kwargs))

    bot.send_temporary = send_temporary
    bot.send_chat_action = lambda *_args, **_kwargs: asyncio.sleep(0)

    asyncio.run(bot.send_report(123, 999))

    assert temporary and "файл отсутствует" in temporary[0][0][1]


def test_report_without_id_uses_latest_meaningful_change(tmp_db, monkeypatch):
    monkeypatch.setattr(telegram_bot_module.settings, "telegram_admin_id", 151599744)
    with tmp_db() as session:
        event = Event(
            kind="attachment_content_changed",
            severity="critical",
            summary="обновлено вложение",
        )
        session.add(event)
        session.commit()
        event_id = event.id

    bot = TelegramBot.__new__(TelegramBot)
    reports = []

    async def send_report(_chat_id, report_id):
        reports.append(report_id)

    bot.send_report = send_report
    bot.send = lambda *_args, **_kwargs: None

    import asyncio
    asyncio.run(bot.handle({
        "message": {
            "from": {"id": 151599744},
            "chat": {"id": 151599744},
            "text": "/report",
        }
    }))

    assert reports == [event_id]


def test_report_without_history_uses_temporary_feedback(monkeypatch):
    import asyncio

    bot = TelegramBot.__new__(TelegramBot)
    temporary = []
    bot.latest_change_id = lambda: None

    async def send_temporary(*args, **kwargs):
        temporary.append((args, kwargs))

    bot.send_temporary = send_temporary
    monkeypatch.setattr(telegram_bot_module.settings, "telegram_admin_id", 1)

    asyncio.run(bot._dispatch_command("/report", ["/report"], 1, 1))

    assert temporary and "Изменений для отчёта пока нет" in temporary[0][0][1]


def test_scan_requires_confirmation_and_callback_starts_it(monkeypatch):
    import asyncio

    monkeypatch.setattr(telegram_bot_module.settings, "telegram_admin_id", 151599744)
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


def test_scan_control_callbacks_show_progress_and_confirm_stop(monkeypatch):
    import asyncio

    monkeypatch.setattr(telegram_bot_module.settings, "telegram_admin_id", 151599744)
    bot = TelegramBot.__new__(TelegramBot)
    bot.scan_task = SimpleNamespace(done=lambda: False)
    bot.scan_progress = ScanProgress(state="running", stage="Проверка документов", completed=1, total=2)
    bot.stop_scan = lambda: True
    calls = []

    async def call(method, payload):
        calls.append((method, payload))
        return {}

    bot.call = call
    private_message = {"message_id": 7, "chat": {"id": 151599744, "type": "private"}}
    asyncio.run(bot.handle_callback({"id": "c1", "from": {"id": 151599744}, "message": private_message, "data": "scan:status"}))
    edit = [payload for method, payload in calls if method == "editMessageText"][-1]
    assert "1/2" in edit["text"]
    assert edit["reply_markup"]["inline_keyboard"][1][0]["callback_data"] == "v1:scan:stop"

    asyncio.run(bot.handle_callback({"id": "c2", "from": {"id": 151599744}, "message": private_message, "data": "scan:stop"}))
    edit = [payload for method, payload in calls if method == "editMessageText"][-1]
    assert "Остановить" in edit["text"]
    assert edit["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "v1:scan:stop-confirm"

    asyncio.run(bot.handle_callback({"id": "c3", "from": {"id": 151599744}, "message": private_message, "data": "scan:stop:confirm"}))
    edit = [payload for method, payload in calls if method == "editMessageText"][-1]
    assert "остановка" in edit["text"].lower()


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


def test_dashboard_builders_dispatch_to_role_scoped_screens(tmp_db, monkeypatch):
    import asyncio

    monkeypatch.setattr(telegram_bot_module.settings, "telegram_admin_id", 99)
    with tmp_db() as session:
        session.add(UserAccess(user_id=42, chat_id=42, status="approved"))
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    calls = []

    async def call(method, payload):
        calls.append((method, payload))
        return True

    async def render(chat_id, screen, **kwargs):
        calls.append(("render", chat_id, screen, kwargs))

    bot.call = call
    bot._render_screen = render

    async def dispatch_keyboard(sender_id, keyboard):
        for row in keyboard["inline_keyboard"]:
            for button in row:
                await bot.handle_callback({
                    "id": f"{sender_id}-{button['callback_data']}",
                    "from": {"id": sender_id},
                    "message": {
                        "message_id": 7,
                        "chat": {"id": sender_id, "type": "private"},
                    },
                    "data": button["callback_data"],
                })

    asyncio.run(dispatch_keyboard(99, admin_keyboard()))
    admin_routes = [call[2] for call in calls if call[0] == "render"]
    assert admin_routes == ["status", "changes", "scan", "errors", "users", "filters", "settings"]

    calls.clear()
    asyncio.run(dispatch_keyboard(42, user_keyboard()))
    user_routes = [call[2] for call in calls if call[0] == "render"]
    assert user_routes == ["changes", "my_ignore"]

    calls.clear()
    asyncio.run(bot.handle_callback({
        "id": "forged-admin-screen",
        "from": {"id": 42},
        "message": {"message_id": 8, "chat": {"id": 42, "type": "private"}},
        "data": "v1:menu:users",
    }))
    assert not [call for call in calls if call[0] == "render"]


def test_scheduler_recovers_from_transient_db_error_and_starts_due_scan(monkeypatch):
    import asyncio
    from datetime import datetime, timedelta

    bot = TelegramBot.__new__(TelegramBot)
    bot.schedule_mode = None
    bot.next_scan_at = None
    modes = iter([RuntimeError("temporary DB failure"), "daily"])
    errors = []
    scans = []
    sleep_count = 0

    def get_schedule_mode():
        value = next(modes)
        if isinstance(value, Exception):
            raise value
        return value

    async def report_error(context, exc):
        errors.append((context, type(exc)))

    def start_scan(trigger):
        scans.append(trigger)
        return True

    async def sleep(_seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(telegram_bot_module, "next_scheduled_at", lambda *_args: datetime.now().astimezone() - timedelta(seconds=1))
    monkeypatch.setattr(telegram_bot_module.asyncio, "sleep", sleep)
    bot.get_schedule_mode = get_schedule_mode
    bot.report_error = report_error
    bot.start_scan = start_scan

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._run_scheduler())

    assert errors == [("ошибка планировщика", RuntimeError)]
    assert scans == ["auto"]


def test_bot_dashboard_history_and_digest_share_canonical_event_policy(tmp_db):
    from fstec_monitor.event_policy import CHANGE_EVENT_KINDS, OPERATIONAL_ERROR_KINDS

    assert telegram_bot_module.MEANINGFUL_KINDS is CHANGE_EVENT_KINDS
    assert set(telegram_bot_module.ERROR_KINDS) == OPERATIONAL_ERROR_KINDS

    with tmp_db() as session:
        document = Document(canonical_url="https://example.test/doc", title="Doc")
        session.add(document)
        session.flush()
        session.add_all([
            Event(
                document_id=document.id,
                kind="attachment_review_required",
                severity="warning",
                summary="review this attachment",
                details="original attachment",
                notified=False,
            ),
            Event(
                document_id=document.id,
                kind="attachment_binary_changed",
                severity="info",
                summary="binary-only noise",
                details="raw binary differs",
                notified=False,
            ),
        ])
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    stats = bot._status_stats()
    history = bot.changes_text()
    review = Event(kind="attachment_review_required", severity="warning", summary="manual review", details="")
    binary = Event(kind="attachment_binary_changed", severity="info", summary="binary", details="")

    assert stats["pending"] == 1
    assert "review this attachment" in history
    assert "binary-only noise" not in history
    assert should_notify_event(review)
    assert not should_notify_event(binary)


def test_context_keyboard_panel_alias_returns_role_dashboard(tmp_db, monkeypatch):
    import asyncio

    from fstec_monitor.telegram.keyboards import context_keyboard

    monkeypatch.setattr(telegram_bot_module.settings, "telegram_admin_id", 99)
    with tmp_db() as session:
        session.add_all([
            UserAccess(user_id=42, chat_id=42, status="approved"),
            UserAccess(user_id=43, chat_id=43, status="pending"),
        ])
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    rendered = []

    async def call(_method, _payload):
        return True

    async def render(chat_id, screen, **kwargs):
        rendered.append((chat_id, screen, kwargs))

    bot.call = call
    bot._render_screen = render
    back = context_keyboard()["inline_keyboard"][-1][0]
    assert back["callback_data"] == "v1:menu:panel"

    def callback(sender_id):
        return {
            "id": f"panel-{sender_id}",
            "from": {"id": sender_id},
            "message": {"message_id": 7, "chat": {"id": sender_id, "type": "private"}},
            "data": back["callback_data"],
        }

    asyncio.run(bot.handle_callback(callback(99)))
    assert rendered[-1][0:2] == (99, "main")
    assert rendered[-1][2]["reset"] is True

    asyncio.run(bot.handle_callback(callback(42)))
    assert rendered[-1][0:2] == (42, "main")
    assert rendered[-1][2]["reset"] is True

    before = len(rendered)
    asyncio.run(bot.handle_callback(callback(43)))
    assert len(rendered) == before


def test_user_pagination_callbacks_dispatch_only_for_approved_users(tmp_db, monkeypatch):
    import asyncio

    monkeypatch.setattr(telegram_bot_module.settings, "telegram_admin_id", 99)
    with tmp_db() as session:
        session.add_all([
            UserAccess(user_id=42, chat_id=42, status="approved"),
            UserAccess(user_id=43, chat_id=43, status="pending"),
        ])
        for index in range(6):
            document = Document(
                canonical_url=f"https://example.test/change/{index}",
                title=f"Document {index}",
                category=f"Category {index}",
            )
            session.add(document)
            session.flush()
            session.add(Event(
                document_id=document.id,
                kind="document_added",
                severity="warning",
                summary=f"Added {index}",
            ))
        for index in range(9):
            session.add(Document(
                canonical_url=f"https://example.test/filter/{index}",
                title=f"Filter {index}",
                category=f"Personal category {index}",
            ))
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    bot.navigation = {}
    rendered = []

    async def call(_method, _payload):
        return True

    async def render(chat_id, screen, **kwargs):
        rendered.append((chat_id, screen, kwargs))

    bot.call = call
    bot._render_screen = render
    _text, change_markup = bot.changes_page(0)
    _text, ignore_markup = bot.user_ignore_text(42, 0)
    callbacks = [
        change_markup[0][2]["callback_data"],
        ignore_markup["inline_keyboard"][-1][2]["callback_data"],
    ]
    assert callbacks == ["v1:screen:changes-page-1", "v1:screen:my-ignore-page-1"]

    async def dispatch(sender_id, data):
        await bot.handle_callback({
            "id": f"{sender_id}-{data}",
            "from": {"id": sender_id},
            "message": {
                "message_id": 7,
                "chat": {"id": sender_id, "type": "private"},
            },
            "data": data,
        })

    for data, screen in zip(callbacks, ("changes", "my_ignore")):
        before = len(rendered)
        awaitable = dispatch(42, data)
        asyncio.run(awaitable)
        assert rendered[before][0:2] == (42, screen)
        assert rendered[before][2]["payload"] == {"page": 1}

        before = len(rendered)
        asyncio.run(dispatch(43, data))
        assert len(rendered) == before

    for data in ("v1:screen:users-page-1", "v1:screen:filters-page-1", "v1:screen:errors-page-1"):
        assert not bot._callback_access_allowed("screen", data.removeprefix("v1:screen:"), 42)


def test_clear_errors_removes_only_matching_deliveries_with_foreign_keys_enabled(tmp_path, monkeypatch):
    from sqlalchemy import event as sqlalchemy_event

    engine = create_engine(f"sqlite:///{tmp_path / 'fk-errors.db'}")

    @sqlalchemy_event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(telegram_bot_module, "SessionLocal", session_factory)
    with session_factory() as session:
        error = Event(kind="fetch_error", severity="warning", summary="failed fetch")
        change = Event(kind="document_added", severity="warning", summary="new document")
        user = UserAccess(user_id=42, chat_id=42, status="approved")
        session.add_all([error, change, user])
        session.flush()
        session.add_all([
            EventDelivery(event_id=error.id, chat_id=42),
            EventDelivery(event_id=change.id, chat_id=42),
        ])
        session.add(UserAccessEvent(user_id=42, action="approved", new_status="approved", actor_id=99))
        session.commit()
        error_id, change_id = error.id, change.id

    bot = TelegramBot.__new__(TelegramBot)
    assert bot.clear_errors() == 1

    with session_factory() as session:
        assert session.get(Event, error_id) is None
        assert session.get(Event, change_id) is not None
        deliveries = session.scalars(select(EventDelivery).order_by(EventDelivery.event_id)).all()
        assert [(delivery.event_id, delivery.chat_id) for delivery in deliveries] == [(change_id, 42)]
        assert len(session.scalars(select(UserAccessEvent)).all()) == 1
    engine.dispose()
