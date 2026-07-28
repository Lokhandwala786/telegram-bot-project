from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.models.job import JobListing
from app.utils.text import canonicalize_url, normalize_whitespace


_TITLE_MIN_LEN = 6
_JOB_ID_RE = re.compile(r"(?:jobId=|job_id=|/jobs/)([A-Za-z0-9-]+)", re.IGNORECASE)
_KV_RE = re.compile(r"\b([A-Za-z][A-Za-z ]{1,30}):\s*([^|]{1,80})")
_WORK_ADDRESS_RE = re.compile(r"\bWork address:\s*((?:(?!Type:|Duration:|Pay rate:|Location|See what).){2,120})", re.IGNORECASE)
_PAY_RATE_RE = re.compile(r"\bPay rate:\s*((?:(?!Type:|Duration:|Location|Work address:).){1,40})", re.IGNORECASE)
_TYPE_RE = re.compile(r"\bType:\s*((?:(?!Duration:|Pay rate:|Location|Work address:).){1,40})", re.IGNORECASE)
_DURATION_RE = re.compile(r"\bDuration:\s*((?:(?!Type:|Pay rate:|Location|Work address:).){1,40})", re.IGNORECASE)
_NOT_AVAILABLE_RE = re.compile(r"\bnot available for application\b", re.IGNORECASE)


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
        if title and len(title) < _TITLE_MIN_LEN:
            title = None

        if not title and "loading" in text.casefold():
            return None

        job_id = _extract_job_id(source_url) or _extract_job_id(text)

        # Extract key-value fields commonly shown on the detail page.
        work_address = _first_match(_WORK_ADDRESS_RE, text)
        pay_rate = _first_match(_PAY_RATE_RE, text)
        jtype = _first_match(_TYPE_RE, text)
        duration = _first_match(_DURATION_RE, text)

        location = work_address or _guess_uk_location(text)

        raw_meta: dict[str, str] = {}
        for m in _KV_RE.finditer(text):
            k = normalize_whitespace(m.group(1))
            v = normalize_whitespace(m.group(2))
            if k and v and len(k) <= 32 and len(v) <= 120:
                raw_meta.setdefault(k, v)

        shift = None
        shift_parts = [p for p in [jtype, duration] if p and p.upper() != "N/A"]
        if shift_parts:
            shift = " / ".join(shift_parts)

        pay_text = None
        if pay_rate and pay_rate.upper() != "N/A":
            pay_text = pay_rate

        # Apply enabled detection:
        # - disabled Apply button (aria-disabled/disabled attribute), or
        # - banner text "not available for application now"
        apply_enabled = True
        if _NOT_AVAILABLE_RE.search(text):
            apply_enabled = False
        else:
            # Try DOM-based signals
            for el in soup.find_all(["button", "a"]):
                label = normalize_whitespace(el.get_text(" ", strip=True)).casefold()
                if label == "apply":
                    if el.has_attr("disabled"):
                        apply_enabled = False
                        break
                    aria = str(el.get("aria-disabled") or "").strip().lower()
                    if aria == "true":
                        apply_enabled = False
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

