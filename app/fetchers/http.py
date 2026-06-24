from __future__ import annotations

import logging

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

log = logging.getLogger(__name__)


class HttpFetcher:
    def __init__(self, *, timeout_seconds: float) -> None:
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "HttpFetcher":
        # Browser-like headers help avoid aggressive bot-blocking while staying within public pages.
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
            follow_redirects=True,
            # Keep HTTP/1.1 by default to avoid requiring the optional `h2` package.
            http2=False,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self._client:
            await self._client.aclose()
            self._client = None

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
        wait=wait_exponential_jitter(initial=0.5, max=10),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def get_text(self, url: str) -> str:
        if not self._client:
            raise RuntimeError("HttpFetcher must be used as an async context manager")
        resp = await self._client.get(url)
        resp.raise_for_status()
        text = resp.text
        if not text:
            raise httpx.TransportError("Empty response body")
        return text

