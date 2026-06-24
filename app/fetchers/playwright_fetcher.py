from __future__ import annotations

import asyncio
import logging
from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

log = logging.getLogger(__name__)


class PlaywrightNotInstalledError(RuntimeError):
    pass


class PlaywrightFetcher:
    """
    Lightweight Playwright renderer for dynamic public pages.

    Only used when needed (e.g. jobsatamazon.co.uk SPA). This avoids slowing down
    the normal fast HTTP polling path.
    """

    def __init__(self, *, navigation_timeout_ms: int = 45_000) -> None:
        self._nav_timeout_ms = navigation_timeout_ms
        self._playwright = None
        self._browser = None

    async def __aenter__(self) -> "PlaywrightFetcher":
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except Exception as e:  # pragma: no cover
            raise PlaywrightNotInstalledError(
                "Playwright is not installed. Install optional deps:\n"
                "  pip install -r requirements-playwright.txt\n"
                "  python -m playwright install chromium"
            ) from e

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        try:
            if self._browser:
                await self._browser.close()
        finally:
            self._browser = None
            if self._playwright:
                await self._playwright.stop()
            self._playwright = None

    @retry(
        retry=retry_if_exception_type((TimeoutError,)),
        wait=wait_exponential_jitter(initial=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def get_rendered_html(self, url: str) -> str:
        if not self._browser:
            raise RuntimeError("PlaywrightFetcher must be used as an async context manager")

        context = await self._browser.new_context(
            locale="en-GB",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        page.set_default_navigation_timeout(self._nav_timeout_ms)
        page.set_default_timeout(self._nav_timeout_ms)

        try:
            html, _captures = await self._render_page(page, url)
            return html
        finally:
            await context.close()

    @retry(
        retry=retry_if_exception_type((TimeoutError,)),
        wait=wait_exponential_jitter(initial=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def get_rendered_html_with_json_captures(self, url: str) -> tuple[str, list[Any]]:
        """
        Like get_rendered_html, but also records JSON bodies from public responses
        triggered by the page (often GraphQL/AppSync style). This yields richer fields
        than DOM scraping alone.
        """
        if not self._browser:
            raise RuntimeError("PlaywrightFetcher must be used as an async context manager")

        context = await self._browser.new_context(
            locale="en-GB",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        page.set_default_navigation_timeout(self._nav_timeout_ms)
        page.set_default_timeout(self._nav_timeout_ms)

        try:
            return await self._render_page(page, url, capture_json=True)
        finally:
            await context.close()

    async def _render_page(self, page, url: str, *, capture_json: bool = False) -> tuple[str, list[Any]]:
        captures: list[Any] = []
        pending: list[asyncio.Task] = []

        def on_response(resp) -> None:  # type: ignore[no-untyped-def]
            if not capture_json:
                return

            async def _read() -> None:
                try:
                    ct = (resp.headers.get("content-type") or "").lower()
                    if "json" not in ct:
                        return
                    lu = resp.url.lower()
                    if "jobsatamazon.co.uk" not in lu and ".appsync." not in lu:
                        return
                    try:
                        cl = resp.headers.get("content-length")
                        if cl and int(cl) > 2_000_000:
                            return
                    except Exception:
                        pass
                    body = await resp.json()
                    captures.append(body)
                except Exception:
                    return

            try:
                pending.append(asyncio.create_task(_read()))
            except Exception:
                return

        if capture_json:
            page.on("response", on_response)

        await page.goto(url, wait_until="domcontentloaded")

        try:
            await page.wait_for_function(
                "() => {"
                "  const t = (document.getElementById('root')?.innerText || '').toLowerCase();"
                "  return t.length > 30 && !t.includes('loading');"
                "}",
                timeout=self._nav_timeout_ms,
            )
        except Exception:
            pass

        await page.wait_for_timeout(1500)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        html = await page.content()
        if not html:
            raise TimeoutError("Empty rendered HTML")
        return html, captures

