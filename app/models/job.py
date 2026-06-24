from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class JobListing:
    source: str
    source_url: str
    job_id: str | None
    url: str
    title: str | None
    location: str | None
    pay_gbp_per_hour: float | None
    pay_text: str | None
    expected_pay_text: str | None
    shift: str | None
    posted_date_text: str | None
    raw_metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class SeenJobRecord:
    key: str
    content_hash: str
    first_seen_utc: datetime
    last_seen_utc: datetime

