from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.models.network_extract import NetworkExtract
from app.utils.text import clean_description_plain

_DAY_ABBR = {
    "MONDAY": "Mon",
    "TUESDAY": "Tue",
    "WEDNESDAY": "Wed",
    "THURSDAY": "Thu",
    "FRIDAY": "Fri",
    "SATURDAY": "Sat",
    "SUNDAY": "Sun",
}

_JOBTYPE_DISPLAY = {
    "FULL_TIME": "Full-Time",
    "PART_TIME": "Part-Time",
    "SEASONAL": "Seasonal",
    "TEMP": "Temporary",
    "TEMPORARY": "Temporary",
}


def safe_get(obj: Any, *keys: str, default: Any | None = None) -> Any:
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def unwrap_job_detail_node(raw: dict[str, Any]) -> dict[str, Any] | None:
    """
    Find the canonical job object inside a GraphQL-style JSON envelope.
    Handles multiple aliases seen across Amazon hiring frontends.
    """
    data = raw.get("data") if isinstance(raw, dict) else None
    if isinstance(data, dict):
        preferred = (
            "getJobDetails",
            "getJobDetail",
            "getJob",
            "job",
            "jobDetails",
        )
        for k in preferred:
            node = data.get(k)
            if isinstance(node, dict) and ("jobId" in node or "title" in node or "jobTitle" in node):
                return node
        for node in data.values():
            if isinstance(node, dict) and ("jobId" in node or "title" in node or "jobTitle" in node):
                return node
    # Bare object
    if isinstance(raw, dict) and ("jobId" in raw or "title" in raw):
        return raw
    return None


def _format_iso_date_short(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        if re.match(r"^\d{4}-\d{2}-\d{2}", s):
            return s[:10]
        return s


def parse_employment_type(data: dict[str, Any]) -> str | None:
    employment = safe_get(data, "employmentType")
    schedule = safe_get(data, "scheduleType")
    job_type = safe_get(data, "jobType")

    parts: list[str] = []
    for p in (employment, schedule):
        if isinstance(p, str) and p.strip():
            parts.append(p.strip())
    if parts:
        return " | ".join(dict.fromkeys(parts))

    if isinstance(job_type, str) and job_type.strip():
        jt = job_type.strip().upper().replace(" ", "_")
        return _JOBTYPE_DISPLAY.get(jt, job_type.strip())
    return None


def parse_pay_display(data: dict[str, Any]) -> str | None:
    comp = safe_get(data, "compensation")
    if not isinstance(comp, dict):
        return None

    display = safe_get(comp, "displayCompensation")
    if isinstance(display, str) and display.strip():
        return display.strip()

    min_pay = safe_get(comp, "minPay")
    max_pay = safe_get(comp, "maxPay")
    currency = (safe_get(comp, "currencyCode", default="GBP") or "GBP").upper()
    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, currency + " ")

    try:
        if min_pay is not None and max_pay is not None and float(min_pay) != float(max_pay):
            return f"{symbol}{float(min_pay):.2f}–{symbol}{float(max_pay):.2f}/hr"
        if min_pay is not None:
            return f"{symbol}{float(min_pay):.2f}/hr"
    except (TypeError, ValueError):
        pass
    return None


def parse_schedule_text(data: dict[str, Any]) -> str | None:
    sched = safe_get(data, "jobSchedule")
    if not isinstance(sched, dict):
        sched = safe_get(data, "schedule")
    if not isinstance(sched, dict):
        return None

    text = safe_get(sched, "scheduleText")
    if isinstance(text, str) and text.strip():
        return text.strip()

    days = safe_get(sched, "daysOfWeek")
    if isinstance(days, list) and days:
        abbrs = []
        for d in days:
            if not isinstance(d, str):
                continue
            abbrs.append(_DAY_ABBR.get(d.upper(), d))
        day_str = ", ".join(abbrs) if abbrs else ""

        start = safe_get(sched, "startTime") or safe_get(sched, "shiftStart")
        end = safe_get(sched, "endTime") or safe_get(sched, "shiftEnd")
        time_part = ""
        if isinstance(start, str) and isinstance(end, str) and start and end:
            time_part = f"{start}–{end}"
        shift_type = safe_get(sched, "shiftType")
        parts = [p for p in (day_str, time_part, shift_type) if isinstance(p, str) and p.strip()]
        if parts:
            return " ".join(parts)
    return None


def parse_location_line(data: dict[str, Any]) -> tuple[str | None, str | None]:
    loc = safe_get(data, "location")
    if not isinstance(loc, dict):
        full = safe_get(data, "locationName") or safe_get(data, "fullAddress")
        if isinstance(full, str) and full.strip():
            return full.strip(), None
        return None, None

    city = safe_get(loc, "city")
    state = safe_get(loc, "state") or safe_get(loc, "region")
    postcode = safe_get(loc, "postalCode") or safe_get(loc, "postCode") or safe_get(loc, "postcode")
    parts = [p for p in (city, state) if isinstance(p, str) and p.strip()]
    line = ", ".join(parts)
    pc = postcode.strip() if isinstance(postcode, str) else None
    if pc and line:
        line = f"{line} ({pc})"
    elif pc:
        line = pc
    return line or None, pc


def parse_apply_state(data: dict[str, Any]) -> tuple[bool | None, str | None]:
    st = safe_get(data, "applyState")
    if isinstance(st, dict):
        enabled = safe_get(st, "isApplyEnabled")
        if isinstance(enabled, bool):
            pass
        elif enabled is None:
            enabled = None
        else:
            enabled = bool(enabled)

        url = safe_get(st, "applyUrl")
        if isinstance(url, str) and url.strip():
            return enabled, url.strip()
        return enabled, None

    alt = safe_get(data, "applyEnabled")
    if isinstance(alt, bool):
        return alt, None
    return None, None


def parse_hours_per_week(data: dict[str, Any]) -> str | None:
    sched = safe_get(data, "jobSchedule")
    if isinstance(sched, dict):
        h = safe_get(sched, "hoursPerWeek")
        if h is not None:
            return str(h).strip() or None
    h2 = safe_get(data, "weeklyHours")
    if h2 is not None:
        return str(h2).strip() or None
    return None


def parse_openings(data: dict[str, Any]) -> str | None:
    for k in ("numberOfOpenings", "openPositions", "openings", "headcount"):
        v = safe_get(data, k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def network_extract_from_graphql_envelope(envelope: dict[str, Any]) -> NetworkExtract | None:
    node = unwrap_job_detail_node(envelope)
    if node is None:
        return None

    title = (
        safe_get(node, "title")
        or safe_get(node, "jobTitle")
        or safe_get(node, "positionTitle")
    )
    if not isinstance(title, str):
        title = None
    else:
        title = title.strip() or None

    job_id = safe_get(node, "jobId") or safe_get(node, "id")
    jid = job_id.strip() if isinstance(job_id, str) else None

    desc = safe_get(node, "description") or safe_get(node, "shortDescription")
    if isinstance(desc, str):
        desc = clean_description_plain(desc.strip(), min_len=25)
    else:
        desc = None

    employment = parse_employment_type(node)
    schedule = parse_schedule_text(node)
    pay = parse_pay_display(node)
    hours = parse_hours_per_week(node)
    loc_line, postcode = parse_location_line(node)
    first_raw = safe_get(node, "firstDayOnSite") or safe_get(node, "startDate")
    sched = safe_get(node, "jobSchedule")
    if not first_raw and isinstance(sched, dict):
        first_raw = sched.get("startDate")
    first_day = _format_iso_date_short(first_raw) if isinstance(first_raw, str) else None

    apply_enabled, apply_url = parse_apply_state(node)
    openings = parse_openings(node)

    if not any(
        [
            title,
            jid,
            employment,
            schedule,
            pay,
            hours,
            loc_line,
            first_day,
            apply_enabled is not None,
            apply_url,
            openings,
        ]
    ):
        return None

    posting_status = safe_get(node, "postingStatus")
    if isinstance(posting_status, str) and posting_status.strip().upper() in ("UNPOSTED", "CLOSED", "FILLED", "INACTIVE"):
        apply_enabled = False

    js = safe_get(node, "jobStatus")
    job_status = js.strip() if isinstance(js, str) and js.strip() else None


    return NetworkExtract(
        title=title,
        description=desc,
        employment_type=employment,
        schedule=schedule,
        first_day=first_day,
        hours_per_week=hours,
        pay=pay,
        location=loc_line,
        openings=openings,
        apply_enabled=apply_enabled,
        apply_url=apply_url,
        job_status=job_status,
        postcode=postcode,
        job_id=jid,
    )


def parse_schedule_cards_extract(envelope: dict[str, Any]) -> NetworkExtract | None:
    """Extract and aggregate shift options from AppSync searchScheduleCards payload."""
    data = safe_get(envelope, "data", "searchScheduleCards")
    if not isinstance(data, dict):
        return None
    cards = data.get("scheduleCards")
    if isinstance(cards, list) and len(cards) == 0:
        return NetworkExtract(apply_enabled=False)
    if not isinstance(cards, list) or not cards:
        return None


    pays = []
    schedules = []
    first_days = []
    hours_list = []

    for card in cards:
        if not isinstance(card, dict):
            continue

        pay = parse_pay_display(card)
        if pay:
            pays.append(pay)

        sched = parse_schedule_text(card)
        if sched:
            schedules.append(sched)

        first_raw = safe_get(card, "firstDayOnSite") or safe_get(card, "startDate")
        sched_obj = safe_get(card, "jobSchedule") or safe_get(card, "schedule")
        if not first_raw and isinstance(sched_obj, dict):
            first_raw = sched_obj.get("startDate")
        fd = _format_iso_date_short(first_raw) if isinstance(first_raw, str) else None
        if fd:
            first_days.append(fd)

        h = parse_hours_per_week(card)
        if h:
            hours_list.append(h)

    # De-duplicate lists
    pays = list(dict.fromkeys(pays))
    schedules = list(dict.fromkeys(schedules))
    first_days = list(dict.fromkeys(first_days))
    hours_list = list(dict.fromkeys(hours_list))

    if not any([pays, schedules, first_days, hours_list]):
        return None

    return NetworkExtract(
        schedule=" / ".join(schedules) if schedules else None,
        first_day=" / ".join(first_days) if first_days else None,
        hours_per_week=" / ".join(hours_list) if hours_list else None,
        pay=" / ".join(pays) if pays else None,
        apply_enabled=True,
    )



def best_graphql_extract(captures: list[Any]) -> NetworkExtract | None:
    best: NetworkExtract | None = None
    best_score = -1
    cards_extract: NetworkExtract | None = None

    for cap in captures:
        if not isinstance(cap, dict):
            continue

        # Extract searchScheduleCards details if present
        c_ext = parse_schedule_cards_extract(cap)
        if c_ext:
            if cards_extract:
                from app.utils.jobsatamazon_network import merge_network_extracts
                cards_extract = merge_network_extracts(c_ext, cards_extract)
            else:
                cards_extract = c_ext
            continue

        ext = network_extract_from_graphql_envelope(cap)
        if ext is None:
            continue
        score = sum(
            1
            for f in (
                ext.title,
                ext.pay,
                ext.schedule,
                ext.employment_type,
                ext.hours_per_week,
                ext.first_day,
                ext.location,
                ext.apply_url,
            )
            if f
        )
        if ext.apply_enabled is not None:
            score += 2
        if score > best_score:
            best_score = score
            best = ext

    if cards_extract:
        if best:
            apply_en = cards_extract.apply_enabled if cards_extract.apply_enabled is not None else best.apply_enabled
            best = NetworkExtract(
                title=best.title,
                description=best.description,
                employment_type=best.employment_type,
                schedule=cards_extract.schedule or best.schedule,
                first_day=cards_extract.first_day or best.first_day,
                hours_per_week=cards_extract.hours_per_week or best.hours_per_week,
                pay=cards_extract.pay or best.pay,
                location=best.location,
                openings=best.openings or cards_extract.openings,
                apply_enabled=apply_en,
                apply_url=best.apply_url,
                job_status=best.job_status,
                postcode=best.postcode,
                job_id=best.job_id,
            )
        else:
            best = cards_extract


    return best

