from __future__ import annotations

import asyncio

from fstec_monitor.telegram.lifecycle import ProgressCoalescer


def test_progress_updates_are_coalesced_to_latest_state() -> None:
    async def scenario() -> None:
        rendered: list[int] = []
        coalescer = ProgressCoalescer(lambda value: rendered.append(value), interval=0.01)

        coalescer.submit(0)
        await asyncio.sleep(0)
        for value in range(1, 20):
            coalescer.submit(value)
        await asyncio.sleep(0.03)
        await coalescer.close()

        assert rendered
        assert rendered[-1] == 19
        assert len(rendered) <= 3

    asyncio.run(scenario())


def test_progress_close_does_not_cancel_latest_render() -> None:
    async def scenario() -> None:
        rendered: list[str] = []
        coalescer = ProgressCoalescer(lambda value: rendered.append(value), interval=0)
        coalescer.submit("готово")
        await coalescer.close()

        assert rendered == ["готово"]

    asyncio.run(scenario())
