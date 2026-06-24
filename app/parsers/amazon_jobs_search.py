from __future__ import annotations

import json
import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from app.models.job import JobListing
from app.utils.text import canonicalize_url, normalize_whitespace

log = logging.getLogger(__name__)


_JOB_SEG_RE = re.compile(r"/(?:en(?:-[a-z]{2})?/)?jobs/([^/?#]+)", re.IGNORECASE)


class AmazonJobsSearchParser:
    """
    Parses public Amazon Jobs search/list pages.

    Design goals:
    - tolerate markup changes using layered fallbacks
    - extract as much visible metadata as possible
    - never rely on private/employee-only endpoints
    """

    def parse(
        self,
        *,
        html: str,
        source_url: str,
        max_items: int,
        expected_pay_text: str | None,
    ) -> list[JobListing]:
        jobs: list[JobListing] = []
        jobs.extend(self._parse_search_json(html, source_url, max_items, expected_pay_text))
        if not jobs:
            soup = BeautifulSoup(html, "lxml")
            jobs.extend(self._parse_json_ld(soup, source_url, expected_pay_text))
            if not jobs:
                jobs.extend(self._parse_html_cards(soup, source_url, expected_pay_text))
            if not jobs:
                jobs.extend(self._parse_links_fallback(soup, source_url, expected_pay_text))

        # De-dup within a single page (same URL/job_id)
        seen: set[str] = set()
        out: list[JobListing] = []
        for j in jobs:
            key = j.job_id or j.url
            if key in seen:
                continue
            seen.add(key)
            out.append(j)
            if len(out) >= max_items:
                break
        return out

    def _parse_search_json(
        self,
        body: str,
        source_url: str,
        max_items: int,
        expected_pay_text: str | None,
    ) -> list[JobListing]:
        if not body.strip().startswith("{") or '"jobs"' not in body[:800]:
            return []
        try:
            payload = json.loads(body)
        except Exception:
            return []
        rows = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        out: list[JobListing] = []
        for row in rows[:max_items]:
            if not isinstance(row, dict):
                continue
            job_path = _as_str(row.get("job_path"))
            if not job_path:
                continue
            listing_url = canonicalize_url(job_path, base="https://www.amazon.jobs")
            job_id = _extract_job_id(listing_url) or _as_str(row.get("id"))

            title = _as_str(row.get("title"))
            loc = _as_str(row.get("normalized_location")) or _as_str(row.get("location"))
            if not loc:
                city = _as_str(row.get("city"))
                state = _as_str(row.get("state"))
                cc = _as_str(row.get("country_code"))
                bits = ", ".join(b for b in (city, state, cc) if b)
                loc = bits or None

            shift = _as_str(row.get("job_schedule_type"))
            meta: dict[str, str] = {}
            if jd := _as_str(row.get("job_category")):
                meta.setdefault("Job category", jd)
            if team := _as_str(row.get("team")):
                meta.setdefault("Team", team)

            out.append(
                JobListing(
                    source="amazon.jobs",
                    source_url=source_url,
                    job_id=job_id,
                    url=listing_url,
                    title=normalize_whitespace(title) if title else None,
                    location=normalize_whitespace(loc) if loc else None,
                    pay_gbp_per_hour=None,
                    pay_text=None,
                    expected_pay_text=expected_pay_text,
                    shift=shift,
                    posted_date_text=_as_str(row.get("posted_date")),
                    raw_metadata=meta,
                )
            )
        return out

    def _parse_json_ld(
        self, soup: BeautifulSoup, source_url: str, expected_pay_text: str | None
    ) -> list[JobListing]:
        out: list[JobListing] = []
        for script in soup.select('script[type="application/ld+json"]'):
            raw = script.string or script.get_text(strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue

            for item in _iter_json_ld_items(data):
                if not isinstance(item, dict):
                    continue
                if item.get("@type") not in ("JobPosting", "JobPostingPosting", "JobPostingSchema"):
                    continue

                title = _as_str(item.get("title"))
                url = _as_str(item.get("url")) or source_url
                url = canonicalize_url(url, base=source_url)
                job_id = _extract_job_id(url)

                loc = None
                job_loc = item.get("jobLocation")
                if isinstance(job_loc, dict):
                    loc = _extract_ld_location(job_loc)
                elif isinstance(job_loc, list) and job_loc:
                    loc = _extract_ld_location(job_loc[0]) if isinstance(job_loc[0], dict) else None

                posted = _as_str(item.get("datePosted"))

                out.append(
                    JobListing(
                        source="amazon.jobs",
                        source_url=source_url,
                        job_id=job_id,
                        url=url,
                        title=normalize_whitespace(title) if title else None,
                        location=normalize_whitespace(loc) if loc else None,
                        pay_gbp_per_hour=None,
                        pay_text=None,
                        expected_pay_text=expected_pay_text,
                        shift=None,
                        posted_date_text=posted,
                        raw_metadata={},
                    )
                )
        return out

    def _parse_html_cards(
        self, soup: BeautifulSoup, source_url: str, expected_pay_text: str | None
    ) -> list[JobListing]:
        out: list[JobListing] = []

        # Common patterns:
        # - results often contain anchors to /en/jobs/<id>/...
        # - card containers may be <div> with role/listitem or data-test attributes
        anchors = soup.select('a[href*="/jobs/"], a[href*="/en/jobs/"]')
        for a in anchors:
            href = a.get("href")
            if not href:
                continue
            url = canonicalize_url(href, base=source_url)
            job_id = _extract_job_id(url)
            if not job_id:
                continue

            title = normalize_whitespace(a.get_text(" ", strip=True)) or None

            # Heuristic: nearby text might contain location.
            card = a
            for _ in range(5):
                if card.parent is None:
                    break
                card = card.parent
                txt = card.get_text(" ", strip=True)
                if txt and ("Location" in txt or "United Kingdom" in txt or "," in txt):
                    break

            location = _guess_location_from_text(card.get_text(" ", strip=True))

            out.append(
                JobListing(
                    source="amazon.jobs",
                    source_url=source_url,
                    job_id=job_id,
                    url=url,
                    title=title,
                    location=location,
                    pay_gbp_per_hour=None,
                    pay_text=None,
                    expected_pay_text=expected_pay_text,
                    shift=None,
                    posted_date_text=None,
                    raw_metadata={},
                )
            )
        return out

    def _parse_links_fallback(
        self, soup: BeautifulSoup, source_url: str, expected_pay_text: str | None
    ) -> list[JobListing]:
        out: list[JobListing] = []
        for a in soup.find_all("a"):
            href = a.get("href")
            if not href:
                continue
            if "/jobs/" not in href and "/en/jobs/" not in href:
                continue
            url = canonicalize_url(href, base=source_url)
            job_id = _extract_job_id(url)
            if not job_id:
                continue
            title = normalize_whitespace(a.get_text(" ", strip=True)) or None
            out.append(
                JobListing(
                    source="amazon.jobs",
                    source_url=source_url,
                    job_id=job_id,
                    url=url,
                    title=title,
                    location=None,
                    pay_gbp_per_hour=None,
                    pay_text=None,
                    expected_pay_text=expected_pay_text,
                    shift=None,
                    posted_date_text=None,
                    raw_metadata={},
                )
            )
        return out


def _iter_json_ld_items(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # some pages wrap inside @graph
        g = data.get("@graph")
        if isinstance(g, list):
            return g
        return [data]
    return []


def _as_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return None


def _extract_job_id(url: str) -> str | None:
    m = _JOB_SEG_RE.search(url)
    return m.group(1) if m else None


def _extract_ld_location(job_loc: dict[str, Any]) -> str | None:
    addr = job_loc.get("address")
    if isinstance(addr, dict):
        parts = []
        for k in ("addressLocality", "addressRegion", "addressCountry", "streetAddress", "postalCode"):
            v = _as_str(addr.get(k))
            if v:
                parts.append(v)
        if parts:
            return ", ".join(dict.fromkeys(parts))
    name = _as_str(job_loc.get("name"))
    return name


def _guess_location_from_text(text: str) -> str | None:
    t = normalize_whitespace(text)
    # Basic heuristic: prefer "City, Region, Country" like patterns.
    # Keep it simple and non-brittle.
    for token in ("United Kingdom", "UK", "England", "Scotland", "Wales", "Northern Ireland"):
        if token in t:
            # Return a clipped portion around the country token.
            idx = t.find(token)
            start = max(0, idx - 60)
            loc = t[start : idx + len(token)]
            return loc.strip(" -|,")
    return None
