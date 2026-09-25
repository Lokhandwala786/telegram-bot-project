from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.models.job import JobListing
from app.utils.text import canonicalize_url, is_garbled_scraped_line, normalize_whitespace


_TITLE_MIN_LEN = 6
_JOB_ID_RE = re.compile(r"(?:jobId=|job_id=|/jobs/)([A-Za-z0-9-]+)", re.IGNORECASE)
_KV_RE = re.compile(r"\b([A-Za-z][A-Za-z /]{1,30}):\s*([^|]{1,80})")
_WORK_ADDRESS_RE = re.compile(r"\bWork address:\s*((?:(?!Type:|Duration:|Pay rate:|Location|See what|Apply|Share|Similar).){2,120})", re.IGNORECASE)
_PAY_RATE_RE = re.compile(r"\bPay rate:\s*((?:(?!Type:|Duration:|Location|Work address:|See what|Apply|Share|Similar).){1,40})", re.IGNORECASE)
_TYPE_RE = re.compile(r"\bType:\s*((?:(?!Duration:|Pay rate:|Location|Work address:|See what|Apply|Share|Similar).){1,40})", re.IGNORECASE)
_DURATION_RE = re.compile(r"\bDuration:\s*((?:(?!Type:|Pay rate:|Location|Work address:|See what|Apply|Share|Similar).){1,40})", re.IGNORECASE)
_NOT_AVAILABLE_RE = re.compile(
    r"\b(?:"
    r"not available for application|"
    r"no longer available|"
    r"no longer accepting applications|"
    r"job not found|"
    r"doesn't have available shifts|"
    r"does not have available shifts|"
    r"no available shifts|"
    r"no shifts available|"
    r"currently no shifts|"
    r"all shifts are currently filled|"
    r"this position has been filled|"
    r"check back later for new shifts|"
    r"please choose another job below"
    r")\b",
    re.IGNORECASE,
)


_ALLOWED_DETAIL_KEYS: dict[str, str] = {
    "work address": "Work address",
    "employment type": "Employment type",
    "schedule": "Schedule",
    "hours/week": "Hours/Week",
    "openings": "Openings",
    "pay rate": "Pay rate",
    "description": "Description",
    "job status": "Job status",
    "postcode": "Postcode",
}


def _clean_val(v: str | None) -> str | None:
    if not v:
        return None
    s = normalize_whitespace(v).strip(" -|,")
    if not s or s.upper() in ("N/A", "TBC", "—", "-"):
        return None
    if "loading" in s.lower():
        return None
    if is_garbled_scraped_line(s):
        return None
    return s


class JobsAtAmazonSearchParser:
    """
    Best-effort parser for jobsatamazon.co.uk rendered HTML.

    The site is a SPA, so we rely on Playwright-rendered HTML and then extract
    job detail links and nearby text as metadata. Keep this tolerant.
    """

    def parse(self, *, html: str, source_url: str, max_items: int, expected_pay_text: str | None) -> list[JobListing]:
        soup = BeautifulSoup(html, "lxml")
        # If this is a job *detail* page, extract a single record from the page itself.
        detail = self._parse_detail_page(soup, source_url, expected_pay_text)
        if detail:
            return [detail]

        out: list[JobListing] = []

        for a in soup.select("a[href]"):
            href = a.get("href")
            if not href:
                continue
            if "job" not in href.lower():
                continue

            url = canonicalize_url(href, base=source_url)
            job_id = _extract_job_id(url)
            if not job_id and "jobdetail" not in url.lower() and "/job" not in url.lower():
                continue

            title = normalize_whitespace(a.get_text(" ", strip=True)) or None
            if title and len(title) < _TITLE_MIN_LEN:
                title = None

            # Heuristic: search near anchor for location-like text.
            container = a
            for _ in range(4):
                if not container.parent:
                    break
                container = container.parent
            text = normalize_whitespace(container.get_text(" ", strip=True))
            location = _guess_uk_location(text)

            out.append(
                JobListing(
                    source="jobsatamazon.co.uk",
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
            if len(out) >= max_items:
                break

        # De-dupe by url/job_id
        uniq: list[JobListing] = []
        seen: set[str] = set()
        for j in out:
            k = j.job_id or j.url
            if k in seen:
                continue
            seen.add(k)
            uniq.append(j)
        return uniq

    def _parse_detail_page(
        self, soup: BeautifulSoup, source_url: str, expected_pay_text: str | None
    ) -> JobListing | None:
        # Heuristic: the detail route includes "#/jobDetail" and usually has a single H1 title.
        if "jobdetail" not in source_url.lower():
            return None

        text = normalize_whitespace(soup.get_text(" ", strip=True))
        if not text:
            return None

        h1 = soup.find(["h1", "h2"])
        title = normalize_whitespace(h1.get_text(" ", strip=True)) if h1 else None
        if title:
            tl = title.casefold()
            if (
                len(title) < _TITLE_MIN_LEN
                or "loading" in tl
                or "jobs found" in tl
                or "error" in tl
                or "no recommendations" in tl
                or re.search(r"^\d+\s+jobs?\b", tl)
            ):
                title = None

        if not title:
            return None

        job_id = _extract_job_id(source_url) or _extract_job_id(text)

        # Extract key-value fields commonly shown on the detail page.
        work_address = _clean_val(_first_match(_WORK_ADDRESS_RE, text))
        pay_rate = _clean_val(_first_match(_PAY_RATE_RE, text))
        jtype = _clean_val(_first_match(_TYPE_RE, text))
        duration = _clean_val(_first_match(_DURATION_RE, text))

        location = work_address or _guess_uk_location(text)

        raw_meta: dict[str, str] = {}
        for m in _KV_RE.finditer(text):
            raw_k = normalize_whitespace(m.group(1)).casefold()
            if raw_k in _ALLOWED_DETAIL_KEYS:
                canonical_k = _ALLOWED_DETAIL_KEYS[raw_k]
                cleaned_v = _clean_val(m.group(2))
                if cleaned_v and len(cleaned_v) <= 120:
                    raw_meta.setdefault(canonical_k, cleaned_v)

        shift = None
        shift_parts = [p for p in [jtype, duration] if p]
        if shift_parts:
            shift = " / ".join(shift_parts)

        pay_text = pay_rate

        if work_address:
            raw_meta["Work address"] = work_address
        if pay_text:
            raw_meta["Pay rate"] = pay_text
        if shift and "Schedule" not in raw_meta:
            raw_meta["Schedule"] = shift

        # Apply enabled detection:
        # A job is ONLY available if:
        # 1. No unavailable/no-shifts phrase is present in text
        # 2. An active Apply or Select Shift button/link is found, and is NOT disabled
        apply_enabled = False
        if not _NOT_AVAILABLE_RE.search(text):
            for el in soup.find_all(["button", "a"]):
                label = normalize_whitespace(el.get_text(" ", strip=True)).casefold()
                if any(
                    btn_word in label
                    for btn_word in (
                        "apply",
                        "select shift",
                        "choose shift",
                        "start application",
                        "book shift",
                        "continue application",
                    )
                ):
                    # Check for disabled attributes or classes
                    is_dis = el.has_attr("disabled")
                    aria_dis = str(el.get("aria-disabled") or "").strip().lower() == "true"
                    classes = el.get("class") or []
                    class_dis = (
                        any("disabled" in str(c).lower() for c in classes)
                        if isinstance(classes, list)
                        else "disabled" in str(classes).lower()
                    )
                    if not (is_dis or aria_dis or class_dis):
                        apply_enabled = True
                        break

        raw_meta["apply_enabled"] = "true" if apply_enabled else "false"


        return JobListing(
            source="jobsatamazon.co.uk",
            source_url=source_url,
            job_id=job_id,
            url=source_url,
            title=title,
            location=location,
            pay_gbp_per_hour=None,
            pay_text=pay_text,
            expected_pay_text=expected_pay_text,
            shift=shift,
            posted_date_text=None,
            raw_metadata=raw_meta,
        )


def _extract_job_id(url: str) -> str | None:
    m = _JOB_ID_RE.search(url)
    return m.group(1) if m else None


def _first_match(rx: re.Pattern[str], text: str) -> str | None:
    m = rx.search(text)
    if not m:
        return None
    v = normalize_whitespace(m.group(1))
    return v or None


def _guess_uk_location(text: str) -> str | None:
    # Lightweight UK-centric heuristic: return a snippet containing "United Kingdom" or "UK".
    for token in ("United Kingdom", "UK", "England", "Scotland", "Wales", "Northern Ireland"):
        if token in text:
            idx = text.find(token)
            start = max(0, idx - 60)
            return text[start : idx + len(token)].strip(" -|,")
    return None

