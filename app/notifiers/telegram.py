from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter
from tenacity.wait import wait_base

log = logging.getLogger(__name__)


def is_retryable_http_error(exception: BaseException) -> bool:
    if isinstance(exception, httpx.HTTPStatusError):
        status = exception.response.status_code
        return status == 429 or (500 <= status < 600)
    return False


def retry_condition(exception: BaseException) -> bool:
    if isinstance(exception, (httpx.TimeoutException, httpx.TransportError)):
        return True
    return is_retryable_http_error(exception)


class wait_retry_after_or_exponential(wait_base):
    def __init__(self, initial: float = 0.5, max_wait: float = 10.0) -> None:
        self.initial = initial
        self.max_wait = max_wait
        self.fallback = wait_exponential_jitter(initial=initial, max=max_wait)

    def __call__(self, retry_state) -> float:
        exc = retry_state.outcome.exception()
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
            retry_after = exc.response.headers.get("Retry-After")
            if retry_after:
                try:
                    return float(retry_after)
                except ValueError:
                    pass
        return self.fallback(retry_state)



@dataclass(frozen=True, slots=True)
class TelegramMessage:
    text: str
    parse_mode: str = "HTML"
    disable_web_page_preview: bool = False


@dataclass(frozen=True, slots=True)
class TelegramPhoto:
    """Photo sent via Telegram sendPhoto (caption is plain text unless parse_mode is set)."""

    photo_bytes: bytes
    filename: str = "screenshot.png"
    caption: str = ""
    parse_mode: str | None = None


class TelegramNotifier:
    def __init__(self, *, bot_token: str, chat_id: str, timeout_seconds: float = 12.0) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "TelegramNotifier":
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self._client:
            await self._client.aclose()
            self._client = None

    @retry(
        retry=retry_if_exception(retry_condition),
        wait=wait_retry_after_or_exponential(initial=0.5, max_wait=10.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def send(self, msg: TelegramMessage) -> None:
        if not self._client:
            raise RuntimeError("TelegramNotifier must be used as an async context manager")

        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": msg.text,
            "parse_mode": msg.parse_mode,
            "disable_web_page_preview": msg.disable_web_page_preview,
        }
        resp = await self._client.post(url, data=payload)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            try:
                err_data = e.response.json()
                desc = err_data.get("description", "")
                if desc:
                    log.error("Telegram API error response: %s", desc)
            except Exception:
                pass
            raise e
        data = resp.json()
        if not data.get("ok", False):
            raise httpx.TransportError(f"Telegram API error: {data}")

    @retry(
        retry=retry_if_exception(retry_condition),
        wait=wait_retry_after_or_exponential(initial=0.5, max_wait=10.0),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def send_photo(self, msg: TelegramPhoto) -> None:
        if not self._client:
            raise RuntimeError("TelegramNotifier must be used as an async context manager")

        url = f"https://api.telegram.org/bot{self._bot_token}/sendPhoto"
        files = {"photo": (msg.filename, msg.photo_bytes, "image/png")}
        data: dict[str, str] = {"chat_id": self._chat_id, "caption": msg.caption}
        if msg.parse_mode:
            data["parse_mode"] = msg.parse_mode
        resp = await self._client.post(url, data=data, files=files)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            try:
                err_data = e.response.json()
                desc = err_data.get("description", "")
                if desc:
                    log.error("Telegram API error response: %s", desc)
            except Exception:
                pass
            raise e
        out = resp.json()
        if not out.get("ok", False):
            raise httpx.TransportError(f"Telegram API error: {out}")


