import asyncio
from types import SimpleNamespace

import fstec_monitor.telegram_bot as telegram_bot_module
from fstec_monitor.telegram_bot import TelegramBot


def test_bot_declares_readiness_after_bot_api_check_and_stops_watchdog(monkeypatch):
    calls = []

    class RecordingWatchdog:
        async def start(self):
            calls.append("ready")

        async def stop(self):
            calls.append("stopping")

    class Dispatcher:
        async def start_polling(self, *_args, **_kwargs):
            calls.append("polling")

    async def get_me():
        calls.append("api_checked")

    async def no_op():
        return None

    async def wait_for_shutdown():
        await asyncio.sleep(3600)

    monkeypatch.setattr(telegram_bot_module, "init_db", lambda: None)
    monkeypatch.setattr(telegram_bot_module, "SystemdWatchdog", RecordingWatchdog)
    monkeypatch.setattr(telegram_bot_module, "next_scheduled_at", lambda *_args: None)
    bot = TelegramBot.__new__(TelegramBot)
    bot.bot = SimpleNamespace(get_me=get_me)
    bot.configure_menu = no_op
    bot.get_schedule_mode = lambda: "daily_noon"
    bot.create_dispatcher = lambda: Dispatcher()
    bot._run_scheduler = wait_for_shutdown
    bot.scan_is_running = lambda: False
    bot.scan_status_update_task = None

    asyncio.run(bot.run())

    assert calls == ["api_checked", "ready", "polling", "stopping"]


def test_get_me_failure_fails_startup_without_ready_and_closes_bot(monkeypatch, tmp_path):
    import socket

    import pytest

    calls = []
    notify_path = tmp_path / "notify.sock"
    notify_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify_socket.bind(str(notify_path))
    notify_socket.setblocking(False)
    monkeypatch.setenv("NOTIFY_SOCKET", str(notify_path))
    monkeypatch.setenv("WATCHDOG_USEC", "1000000")

    async def get_me():
        calls.append("api_checked")
        raise RuntimeError("getMe failed")

    async def close():
        calls.append("closed")

    async def no_op():
        return None

    class Dispatcher:
        async def start_polling(self, *_args, **_kwargs):
            calls.append("polling")

    bot = TelegramBot.__new__(TelegramBot)
    bot.bot = SimpleNamespace(get_me=get_me)
    bot.close = close
    bot.configure_menu = no_op
    bot.get_schedule_mode = lambda: "daily_noon"
    bot.create_dispatcher = lambda: Dispatcher()
    bot._run_scheduler = no_op
    bot.scan_is_running = lambda: False
    bot.scan_status_update_task = None
    monkeypatch.setattr(telegram_bot_module, "init_db", lambda: None)
    monkeypatch.setattr(telegram_bot_module, "TelegramBot", lambda: bot)

    try:
        with pytest.raises(RuntimeError, match="getMe failed"):
            asyncio.run(telegram_bot_module.main())

        assert calls == ["api_checked", "closed"]
        with pytest.raises(BlockingIOError):
            notify_socket.recv(1024)
    finally:
        notify_socket.close()
