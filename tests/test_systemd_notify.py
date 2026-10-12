import asyncio
import socket

from fstec_monitor.systemd_notify import SystemdWatchdog


def test_watchdog_sends_ready_heartbeats_and_stopping(monkeypatch, tmp_path):
    notify_path = tmp_path / "notify.sock"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(str(notify_path))
    receiver.setblocking(False)
    monkeypatch.setenv("NOTIFY_SOCKET", str(notify_path))
    monkeypatch.setenv("WATCHDOG_USEC", "20000")

    async def exercise():
        watchdog = SystemdWatchdog()
        await watchdog.start()
        await asyncio.sleep(0.055)
        await watchdog.stop()

    try:
        asyncio.run(exercise())
        messages = []
        while True:
            try:
                messages.append(receiver.recv(1024).decode())
            except BlockingIOError:
                break
    finally:
        receiver.close()

    assert "READY=1" in messages
    assert messages.count("WATCHDOG=1") >= 2
    assert messages[-1] == "STOPPING=1"


def test_watchdog_is_noop_without_systemd_environment(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)

    async def exercise():
        watchdog = SystemdWatchdog()
        await watchdog.start()
        assert watchdog.task is None
        await watchdog.stop()

    asyncio.run(exercise())
