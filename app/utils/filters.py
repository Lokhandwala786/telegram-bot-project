from __future__ import annotations

from dataclasses import dataclass

from app.config import FiltersConfig
from app.models.job import JobListing


@dataclass(frozen=True, slots=True)
class FilterDecision:
    allowed: bool
    reasons: list[str]


def _contains_any(haystack: str, needles: list[str]) -> bool:
    h = haystack.casefold()
    return any(n.casefold() in h for n in needles if n.strip())


def _contains_none(haystack: str, forbidden: list[str]) -> bool:
    h = haystack.casefold()
    return all(f.casefold() not in h for f in forbidden if f.strip())


def passes_filters(job: JobListing, cfg: FiltersConfig) -> FilterDecision:
    reasons: list[str] = []

    title = (job.title or "").strip()
    loc = (job.location or "").strip()

    if cfg.title_keywords_any:
        if not _contains_any(title, cfg.title_keywords_any):
            reasons.append("title_no_match_any")

    if cfg.title_keywords_none:
        if not _contains_none(title, cfg.title_keywords_none):
            reasons.append("title_contains_excluded")

    if cfg.location_keywords:
        if loc:
            if not _contains_any(loc, cfg.location_keywords):
                reasons.append("location_no_match")
        else:
            # If location isn't visible, do not hard-reject; it may still be relevant.
            reasons.append("location_missing")

    if cfg.min_pay_gbp_per_hour and job.pay_gbp_per_hour is not None:
        if job.pay_gbp_per_hour < cfg.min_pay_gbp_per_hour:
            reasons.append("pay_below_min")

    allowed = not any(r in reasons for r in ("title_no_match_any", "title_contains_excluded", "location_no_match", "pay_below_min"))
    return FilterDecision(allowed=allowed, reasons=reasons)

