from __future__ import annotations
import asyncio, random
import httpx
from .config import settings

class Fetcher:
    def __init__(self):
        self.client=httpx.AsyncClient(timeout=settings.timeout_seconds, follow_redirects=True, verify=settings.tls_verify, headers={"User-Agent": settings.user_agent, "Accept-Language":"ru-RU,ru;q=0.9"})
    async def close(self): await self.client.aclose()
    async def get(self,url:str) -> httpx.Response:
        last=None
        for attempt in range(settings.max_retries):
            try:
                await asyncio.sleep(settings.request_delay_seconds + random.random())
                r=await self.client.get(url)
                if r.status_code in {429,500,502,503,504}: raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
                return r
            except (httpx.TransportError,httpx.HTTPStatusError) as e:
                last=e
                await asyncio.sleep(min(60, 5*(2**attempt)))
        raise RuntimeError(f"failed to fetch {url}: {last}")
