import asyncio
from types import SimpleNamespace

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

    with pytest.raises(RuntimeError, match="failed to fetch .* after 2 attempts: ReadTimeout"):
        asyncio.run(fetcher.get("https://example.test"))

    assert sleeps == [0, 5, 0]


def test_fetcher_jitter_is_bounded_by_configured_delay(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    class SuccessfulClient:
        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr("fstec_monitor.http.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("fstec_monitor.http.random.random", lambda: 1)
    monkeypatch.setattr("fstec_monitor.http.settings.request_delay_seconds", 0.5)
    monkeypatch.setattr("fstec_monitor.http.settings.max_retries", 1)
    fetcher = Fetcher.__new__(Fetcher)
    fetcher.client = SuccessfulClient()

    asyncio.run(fetcher.get("https://example.test"))

    assert sleeps == [1.0]


def test_fetcher_accepts_a_per_request_timeout(monkeypatch):
    seen = []

    class SuccessfulClient:
        async def get(self, *_args, **kwargs):
            seen.append(kwargs["timeout"])
            return SimpleNamespace(status_code=200)

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr("fstec_monitor.http.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("fstec_monitor.http.settings.request_delay_seconds", 0)
    monkeypatch.setattr("fstec_monitor.http.settings.max_retries", 1)
    fetcher = Fetcher.__new__(Fetcher)
    fetcher.client = SuccessfulClient()

    asyncio.run(fetcher.get("https://example.test", timeout=120.0))

    assert seen == [120.0]


def test_fetcher_timeout_is_an_overall_deadline(monkeypatch):
    monkeypatch.setattr("fstec_monitor.http.settings.request_delay_seconds", 0)
    monkeypatch.setattr("fstec_monitor.http.settings.max_retries", 3)

    class HangingClient:
        async def get(self, *_args, **_kwargs):
            await asyncio.sleep(60)

    fetcher = Fetcher.__new__(Fetcher)
    fetcher.client = HangingClient()

    with pytest.raises(RuntimeError, match="overall timeout"):
        asyncio.run(fetcher.get("https://example.test", timeout=0.01))


def test_fetcher_closes_response_after_body_is_loaded(monkeypatch):
    closed = []

    class Response:
        status_code = 200

        def close(self):
            closed.append(True)

    class SuccessfulClient:
        async def get(self, *_args, **_kwargs):
            return Response()

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr("fstec_monitor.http.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("fstec_monitor.http.settings.request_delay_seconds", 0)
    monkeypatch.setattr("fstec_monitor.http.settings.max_retries", 1)
    fetcher = Fetcher.__new__(Fetcher)
    fetcher.client = SuccessfulClient()

    asyncio.run(fetcher.get("https://example.test"))

    assert closed == [True]
