from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.logging import RichHandler


class _TelegramTokenRedactFilter(logging.Filter):
    """Strip literal bot token from log record text (str.format / % args)."""

    def __init__(self, token: str | None) -> None:
        super().__init__()
        self._token = (token or "").strip()

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if len(self._token) < 12:
            return True
        if isinstance(record.msg, str) and self._token in record.msg:
            record.msg = record.msg.replace(self._token, "<BOT_TOKEN>")
        if record.args:
            record.args = tuple(
                a.replace(self._token, "<BOT_TOKEN>") if isinstance(a, str) and self._token in a else a
                for a in record.args
            )
        return True


def setup_logging(
    level: str = "INFO",
    log_dir: str = "logs",
    *,
    redact_telegram_token: str | None = None,
) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_path = Path(log_dir) / "amazon_shift_alert.log"

    redact = _TelegramTokenRedactFilter(redact_telegram_token)
    rich_h = RichHandler(rich_tracebacks=True, markup=True, show_path=False)
    rich_h.addFilter(redact)
    file_h = RotatingFileHandler(
        log_path,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_h.addFilter(redact)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[rich_h, file_h],
    )

    # Avoid leaking sensitive URLs (e.g. Telegram bot token) via verbose client logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

