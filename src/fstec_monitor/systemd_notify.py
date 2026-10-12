"""Minimal systemd sd_notify support for the long-running bot process."""

from __future__ import annotations

import asyncio
import os
import socket


class SystemdWatchdog:
    """Notify systemd readiness and keep its watchdog fed while the loop runs."""

    def __init__(self) -> None:
        self._notify_socket = os.environ.get("NOTIFY_SOCKET")
        self._watchdog_interval = self._read_watchdog_interval()
        self.task: asyncio.Task[None] | None = None
        self._started = False

    @staticmethod
    def _read_watchdog_interval() -> float | None:
        raw_usec = os.environ.get("WATCHDOG_USEC")
        if not raw_usec:
            return None
        try:
            interval = int(raw_usec) / 2_000_000
        except ValueError:
            return None
        if interval <= 0:
            return None

        raw_pid = os.environ.get("WATCHDOG_PID")
        if raw_pid:
            try:
                if int(raw_pid) != os.getpid():
                    return None
            except ValueError:
                return None
        return interval

    def _notify(self, state: str) -> bool:
        address = self._notify_socket
        if not address:
            return False

        if address.startswith("@"):  # systemd's abstract-namespace socket syntax
            address = "\0" + address[1:]

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify_socket:
                notify_socket.connect(address)
                notify_socket.sendall(state.encode("utf-8"))
        except OSError:
            # Manual runs and test environments commonly have no systemd listener.
            return False
        return True

    async def start(self) -> None:
        if not self._notify_socket:
            return
        if not self._notify("READY=1"):
            return
        self._started = True
        if self._watchdog_interval is not None:
            self.task = asyncio.create_task(self._heartbeat(), name="systemd-watchdog")

    async def _heartbeat(self) -> None:
        assert self._watchdog_interval is not None
        while True:
            await asyncio.sleep(self._watchdog_interval)
            if not self._notify("WATCHDOG=1"):
                return

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self._started:
            self._notify("STOPPING=1")
            self._started = False
