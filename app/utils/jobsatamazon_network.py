from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from app.models.job import JobListing
from app.models.network_extract import NetworkExtract
from app.parsers.jobsatamazon_graphql import best_graphql_extract
from app.utils.text import clean_description_plain, is_garbled_scraped_line, normalize_whitespace


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _as_text(val: Any, *, max_len: int = 400) -> str | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return "Yes" if val else "No"
    if isinstance(val, (int, float)):
        s = str(val)
        return s if s else None
    if isinstance(val, str):
        s = val.strip()
        return s[:max_len] if s else None
    if isinstance(val, list):
        parts: list[str] = []
        for it in val[:40]:
            t = _as_text(it, max_len=120)
            if t:
                parts.append(t)
        if not parts:
            return None
        out = ", ".join(parts)
        return out[:max_len] if out else None
    if isinstance(val, dict):
        # Prefer common "display" shapes
        for k in ("displayName", "label", "value", "name", "text", "formatted", "formattedValue"):
            if k in val:
                t = _as_text(val.get(k), max_len=max_len)
                if t:
                    return t
        inner = val.get("amount") or val.get("monetaryAmount")
        if inner is not None:
            return _as_text(inner, max_len=max_len)
        return None
    return None


# Map normalized keys (no punctuation) -> canonical bucket
_KEY_BUCKETS: dict[str, frozenset[str]] = {
    "title": frozenset(
        {"jobtitle", "title", "jobname", "positiontitle", "rolename"}
    ),
    "description": frozenset(
        {"jobdescription", "description", "shortdescription", "summary", "whatyoullbedoing"}
    ),
    "employment_type": frozenset(
        {"employmenttype", "jobtype", "positiontype", "workertype", "typeofemployment"}
    ),
    "schedule": frozenset(
        {
            "schedule",
            "shiftschedule",
            "weeklyschedule",
            "workschedule",
            "shiftpattern",
            "workpattern",
            "shift",
            "shifttimes",
            "scheduletext",
        }
    ),
    "first_day": frozenset(
        {"firstday", "startdate", "firststartdate", "anticipatedstartdate", "joindate", "firstdayonsite"}
    ),
    "hours_per_week": frozenset(
        {"hoursperweek", "weeklyhours", "hoursweek", "hoursweeek", "weeklyhour"}
    ),
    "pay": frozenset(
        {
            "payrate",
            "hourlyrate",
            "basepay",
            "compensation",
            "wage",
            "hourlycompensation",
            "advertisedbasepay",
            "pay",
        }
    ),
    "location": frozenset(
        {
            "location",
            "locationname",
            "address",
            "worklocation",
            "site",
            "city",
            "postalcode",
            "postcode",
        }
    ),
    "openings": frozenset(
        {"openings", "numberofpositions", "headcount", "positions", "numopenings"}
    ),
}


def _bucket_for_norm_key(nk: str) -> str | None:
    for bucket, needles in _KEY_BUCKETS.items():
        if nk in needles:
            return bucket
    return None


def _pick_str(primary: str | None, fallback: str | None) -> str | None:
    return primary if primary else fallback


def merge_network_extracts(primary: NetworkExtract | None, fallback: NetworkExtract | None) -> NetworkExtract:
    if primary is None:
        return fallback if fallback is not None else NetworkExtract()
    if fallback is None:
        return primary

    return NetworkExtract(
        title=_pick_str(primary.title, fallback.title),
        description=_pick_str(primary.description, fallback.description),
        employment_type=_pick_str(primary.employment_type, fallback.employment_type),
        schedule=_pick_str(primary.schedule, fallback.schedule),
        first_day=_pick_str(primary.first_day, fallback.first_day),
        hours_per_week=_pick_str(primary.hours_per_week, fallback.hours_per_week),
        pay=_pick_str(primary.pay, fallback.pay),
        location=_pick_str(primary.location, fallback.location),
        openings=_pick_str(primary.openings, fallback.openings),
        apply_enabled=primary.apply_enabled
        if primary.apply_enabled is not None
        else fallback.apply_enabled,
        apply_url=_pick_str(primary.apply_url, fallback.apply_url),
        job_status=_pick_str(primary.job_status, fallback.job_status),
        postcode=_pick_str(primary.postcode, fallback.postcode),
        job_id=_pick_str(primary.job_id, fallback.job_id),
    )


def _heuristic_extract_from_network_json(captures: list[Any], *, max_nodes: int = 25_000) -> NetworkExtract:
    acc: dict[str, str] = {}
    nodes = 0

    def walk(obj: Any, depth: int) -> None:
        nonlocal nodes
        if depth > 30 or nodes > max_nodes:
            return
        nodes += 1
        if isinstance(obj, dict):
            for k, v in obj.items():
                nk = _norm_key(str(k))
                bucket = _bucket_for_norm_key(nk)
                if bucket:
                    txt = _as_text(v)
                    if txt and bucket not in acc:
                        acc[bucket] = txt
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for it in obj[:200]:
                walk(it, depth + 1)

    for cap in captures[:200]:
        walk(cap, 0)

    location = acc.get("location")

    pay = acc.get("pay")
    if pay and re.search(r"\d", pay):
        low = pay.lower()
        if "£" not in pay and "gbp" not in low:
            pay = f"£{pay}"

    return NetworkExtract(
        title=acc.get("title"),
        description=acc.get("description"),
        employment_type=acc.get("employment_type"),
        schedule=acc.get("schedule"),
        first_day=acc.get("first_day"),
        hours_per_week=acc.get("hours_per_week"),
        pay=pay,
        location=location,
        openings=acc.get("openings"),
    )


def extract_from_network_json(captures: list[Any], *, max_nodes: int = 25_000) -> NetworkExtract:
    """
    Prefer structured GraphQL/AppSync shapes when present, then merge with heuristic key-walking.
    """
    gql = best_graphql_extract(captures)
    heur = _heuristic_extract_from_network_json(captures, max_nodes=max_nodes)
    return merge_network_extracts(gql, heur)


def enrich_listing(job: JobListing, net: NetworkExtract) -> JobListing:
    """
    Prefer network-derived fields when present; keep HTML-derived values as fallback.
    """
    meta = dict(job.raw_metadata)
    title = net.title or job.title
    location = net.location or job.location
    pay_text = net.pay or job.pay_text

    shift = net.schedule or job.shift

    first_day = net.first_day or job.posted_date_text

    if net.employment_type:
        meta.setdefault("Employment type", net.employment_type)
    if net.schedule and not is_garbled_scraped_line(net.schedule):
        meta.setdefault("Schedule", normalize_whitespace(net.schedule))

    if net.hours_per_week:
        meta.setdefault("Hours/Week", net.hours_per_week)
    if net.description:
        cleaned = clean_description_plain(net.description)
        if cleaned:
            meta["Description"] = cleaned[:2000]
    if net.openings:
        meta.setdefault("Openings", net.openings)

    if net.apply_enabled is not None:
        meta["apply_enabled"] = "true" if net.apply_enabled else "false"
    if net.apply_url:
        # Prefer API apply URL over any DOM guess so Telegram "Apply" taps go to the real flow.
        meta["Apply link"] = net.apply_url
    if net.job_status:
        meta.setdefault("Job status", net.job_status)
    if net.postcode:
        meta.setdefault("Postcode", net.postcode)

    if meta.get("Description"):
        c = clean_description_plain(meta["Description"])
        if c:
            meta["Description"] = c[:2000]
        else:
            meta.pop("Description", None)
    if meta.get("Schedule") and is_garbled_scraped_line(meta["Schedule"]):
        meta.pop("Schedule", None)
    if meta.get("Pay rate") and is_garbled_scraped_line(meta["Pay rate"]):
        meta.pop("Pay rate", None)

    return replace(
        job,
        title=title,
        location=location,
        pay_text=pay_text,
        shift=shift,
        posted_date_text=first_day,
        raw_metadata=meta,
    )
