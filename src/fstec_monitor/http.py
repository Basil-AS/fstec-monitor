from __future__ import annotations

import asyncio
import random
from urllib.parse import urlparse

import httpx

from .config import settings

_ATTACHMENT_SUFFIXES = (".odt", ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".zip", ".rar", ".7z")
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def request_timeout(url: str) -> float:
    """Use the larger configured read budget for binary attachments."""
    if urlparse(url).path.casefold().endswith(_ATTACHMENT_SUFFIXES):
        return settings.attachment_timeout_seconds
    return settings.document_timeout_seconds


def request_headers(url: str, headers: dict[str, str] | None = None) -> dict[str, str] | None:
    """Prepare request headers, avoiding fragile compression for large binaries."""
    result = dict(headers or {})
    if urlparse(url).path.casefold().endswith(_ATTACHMENT_SUFFIXES):
        result.setdefault("Accept-Encoding", "identity")
    return result or None


def conditional_headers(etag: str = "", last_modified: str = "") -> dict[str, str]:
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def retry_delay(attempt: int, response=None) -> float:
    """Return bounded retry delay, honoring an upstream Retry-After hint."""
    raw = getattr(response, "headers", {}).get("retry-after") if response is not None else None
    if raw:
        try:
            return min(60.0, max(0.0, float(raw)))
        except (TypeError, ValueError):
            pass
    return min(60.0, 5 * (2**attempt))


async def close_response(response) -> None:
    """Close async HTTP streams without assuming a synchronous response."""
    close_async = getattr(response, "aclose", None)
    if close_async:
        await close_async()
        return
    close_sync = getattr(response, "close", None)
    if close_sync:
        close_sync()


class Fetcher:
    def __init__(self):
        self.client=httpx.AsyncClient(timeout=settings.timeout_seconds, follow_redirects=True, verify=settings.tls_verify, headers={"User-Agent": settings.user_agent, "Accept-Language":"ru-RU,ru;q=0.9"}, limits=httpx.Limits(max_connections=settings.max_concurrency, max_keepalive_connections=settings.max_concurrency))
    async def close(self): await self.client.aclose()
    async def get(self,url:str, headers:dict[str, str] | None = None, *, timeout: float | None = None) -> httpx.Response:
        total_timeout = timeout if timeout is not None else request_timeout(url)
        try:
            async with asyncio.timeout(total_timeout):
                last=None
                for attempt in range(settings.max_retries):
                    retry_response = None
                    try:
                        delay = settings.request_delay_seconds
                        await asyncio.sleep(delay + random.random() * delay)
                        request_kwargs = {"headers": request_headers(url, headers), "timeout": total_timeout}
                        r=await self.client.get(url, **request_kwargs)
                        if r.status_code in _RETRYABLE_STATUS_CODES:
                            retry_response = r
                            error = httpx.HTTPStatusError("retryable", request=r.request, response=r)
                            await close_response(r)
                            raise error
                        await close_response(r)
                        return r
                    except (httpx.TransportError,httpx.HTTPStatusError) as e:
                        last=e
                        if attempt + 1 < settings.max_retries:
                            await asyncio.sleep(retry_delay(attempt, retry_response))
        except TimeoutError as exc:
            raise RuntimeError(f"failed to fetch {url}: overall timeout after {total_timeout}s") from exc
        if last is None:
            detail = "unknown transport error"
        else:
            detail = f"{type(last).__name__}: {last}".rstrip()
        raise RuntimeError(f"failed to fetch {url} after {settings.max_retries} attempts: {detail}") from last
