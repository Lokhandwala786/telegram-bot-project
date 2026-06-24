from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Normalize DB or parsed datetimes to aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_tz(dt: datetime, tz_name: str) -> datetime:
    return ensure_utc(dt).astimezone(ZoneInfo(tz_name))


def format_dt(dt: datetime, tz_name: str) -> str:
    local = to_tz(dt, tz_name)
    return local.strftime("%Y-%m-%d %H:%M:%S %Z")

