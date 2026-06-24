from __future__ import annotations

from collections.abc import Iterable

from app.config import AlertLocationItem
from app.models.job import JobListing


def job_matches_subscriber_location_choice(
    job: JobListing,
    location_choice: str,
    alert_locations: Iterable[AlertLocationItem],
) -> bool:
    """
    If choice is ``all`` (or empty), always match.
    Otherwise match when any keyword for that preset appears in job location or title
    (case-insensitive substring). Unknown preset names match everything (fail-open).
    """
    lc = (location_choice or "all").strip().casefold()
    if lc in ("", "all"):
        return True
    hay_parts = [job.location or "", job.title or ""]
    hay = " ".join(p for p in hay_parts if p).casefold()
    if not hay:
        return False
    locs = list(alert_locations)
    for item in locs:
        if item.name.strip().casefold() != lc:
            continue
        kws = [k.strip().casefold() for k in item.keywords if k and str(k).strip()]
        if not kws:
            return True
        return any(k in hay for k in kws)
    return True


def subscriber_accepts_job_alert(
    job: JobListing,
    location_choice: str,
    location_match_substr: str | None,
    alert_locations: Iterable[AlertLocationItem],
) -> bool:
    """
    Per-subscriber filter: if ``location_match_substr`` is set (from /setlocation picking a real
    ``jobs.location`` row), require case-insensitive substring in job location or title.
    Otherwise fall back to ``location_choice`` + ``alert_locations`` presets (legacy).
    """
    ms = (location_match_substr or "").strip()
    if ms:
        hay = " ".join(p for p in [job.location or "", job.title or ""] if p).casefold()
        return ms.casefold() in hay
    return job_matches_subscriber_location_choice(job, location_choice, alert_locations)
