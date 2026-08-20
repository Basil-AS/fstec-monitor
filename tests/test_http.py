import asyncio

import httpx
import pytest

from fstec_monitor.http import Fetcher


def test_fetcher_does_not_backoff_after_final_attempt(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    class FailingClient:
        async def get(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("upstream timeout")

    monkeypatch.setattr("fstec_monitor.http.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("fstec_monitor.http.random.random", lambda: 0)
    monkeypatch.setattr("fstec_monitor.http.settings.request_delay_seconds", 0)
    monkeypatch.setattr("fstec_monitor.http.settings.max_retries", 2)
    fetcher = Fetcher.__new__(Fetcher)
    fetcher.client = FailingClient()

    with pytest.raises(RuntimeError, match="failed to fetch"):
        asyncio.run(fetcher.get("https://example.test"))

    assert sleeps == [0, 5, 0]
