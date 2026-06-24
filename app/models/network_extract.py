from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NetworkExtract:
    title: str | None = None
    description: str | None = None
    employment_type: str | None = None
    schedule: str | None = None
    first_day: str | None = None
    hours_per_week: str | None = None
    pay: str | None = None
    location: str | None = None
    openings: str | None = None
    apply_enabled: bool | None = None
    apply_url: str | None = None
    job_status: str | None = None
    postcode: str | None = None
    job_id: str | None = None
