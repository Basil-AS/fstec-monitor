"""Single-flight scan orchestration independent from Telegram handlers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

ProgressCallback = Callable[[str, int, int, int], None]
Runner = Callable[[asyncio.Event, ProgressCallback], Awaitable[int]]


@dataclass(frozen=True)
class ScanSnapshot:
    state: str = "idle"
    stage: str = "Проверка не запущена"
    completed: int = 0
    total: int = 0
    errors: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    documents: int = 0
    last_error: str = ""

    @property
    def percent(self) -> int:
        return round(self.completed * 100 / self.total) if self.total else 0


@dataclass(frozen=True)
class StartResult:
    started: bool
    reason: str
    snapshot: ScanSnapshot


class ScanService:
    """Own one scan task and make start/stop/retry transitions idempotent."""

    def __init__(self, runner: Runner) -> None:
        self._runner = runner
        self._task: asyncio.Task[int] | None = None
        self._cancel_event = asyncio.Event()
        self._snapshot = ScanSnapshot()
        self._lock = asyncio.Lock()

    def snapshot(self) -> ScanSnapshot:
        return self._snapshot

    def _progress(self, stage: str, completed: int, total: int, errors: int = 0) -> None:
        self._snapshot = replace(
            self._snapshot,
            stage=stage[:120],
            completed=max(0, completed),
            total=max(0, total),
            errors=max(0, errors),
        )

    def start(self, trigger: str = "manual", *, retry: bool = False) -> StartResult:
        if self._task is not None and not self._task.done():
            return StartResult(False, "already_running", self._snapshot)
        if not retry and self._snapshot.state in {"failed", "cancelled"}:
            return StartResult(False, "retry_required", self._snapshot)
        self._cancel_event = asyncio.Event()
        self._snapshot = ScanSnapshot(state="running", stage=f"Подготовка проверки ({trigger})", started_at=datetime.now(UTC))
        self._task = asyncio.create_task(self._run())
        return StartResult(True, "started", self._snapshot)

    def retry(self) -> StartResult:
        return self.start("retry", retry=True)

    def stop(self) -> bool:
        if self._task is None or self._task.done() or self._snapshot.state != "running":
            return False
        self._cancel_event.set()
        self._snapshot = replace(self._snapshot, state="cancelled", stage="Остановка проверки…", finished_at=datetime.now(UTC))
        return True

    async def _run(self) -> int:
        try:
            count = await self._runner(self._cancel_event, self._progress)
        except asyncio.CancelledError:
            self._snapshot = replace(self._snapshot, state="cancelled", stage="Проверка остановлена", finished_at=datetime.now(UTC))
            return 0
        except Exception as exc:  # noqa: BLE001 - service must convert runner failures to terminal state
            self._snapshot = replace(self._snapshot, state="failed", stage="Проверка завершилась с ошибкой", last_error=type(exc).__name__, finished_at=datetime.now(UTC))
            return 0
        self._snapshot = replace(self._snapshot, state="completed", stage="Проверка завершена", documents=count, finished_at=datetime.now(UTC))
        return count

    async def wait(self) -> int:
        if self._task is None:
            return 0
        return await self._task
