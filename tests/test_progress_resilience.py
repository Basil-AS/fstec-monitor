from __future__ import annotations

import asyncio

from fstec_monitor.telegram.lifecycle import ProgressCoalescer


def test_progress_renderer_failure_is_recovered_without_unhandled_task():
    calls = 0

    async def renderer(_value):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("telegram unavailable")

    async def scenario():
        coalescer = ProgressCoalescer(renderer, interval=0)
        coalescer.submit("first")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        coalescer.submit("second")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await coalescer.close()

    asyncio.run(scenario())
    assert calls == 2
