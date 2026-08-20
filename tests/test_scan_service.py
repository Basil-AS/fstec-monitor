import asyncio

from fstec_monitor.services.scan_service import ScanService


def test_scan_service_rejects_duplicate_start_and_allows_retry_after_failure():
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(cancel_event, progress):
        progress("fetch", 1, 2, 0)
        started.set()
        await release.wait()
        if cancel_event.is_set():
            raise asyncio.CancelledError
        return 2

    async def scenario():
        service = ScanService(runner)
        first = service.start("manual")
        duplicate = service.start("manual")
        await started.wait()
        release.set()
        await service.wait()
        return first, duplicate, service.snapshot()

    first, duplicate, snapshot = asyncio.run(scenario())
    assert first.started is True
    assert duplicate.started is False
    assert duplicate.reason == "already_running"
    assert snapshot.state == "completed"


def test_scan_service_stop_is_idempotent_and_reports_cancelled_state():
    async def runner(cancel_event, progress):
        while not cancel_event.is_set():
            await asyncio.sleep(0)
        raise asyncio.CancelledError

    async def scenario():
        service = ScanService(runner)
        service.start("manual")
        first_stop = service.stop()
        second_stop = service.stop()
        await service.wait()
        return first_stop, second_stop, service.snapshot()

    first_stop, second_stop, snapshot = asyncio.run(scenario())
    assert first_stop is True
    assert second_stop is False
    assert snapshot.state == "cancelled"
