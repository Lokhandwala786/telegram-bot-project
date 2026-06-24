"""Shift drop timing analytics — peak detection and admin reports."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True, slots=True)
class ShiftDropEvent:
    location_key: str
    profile_id: str
    dropped_at_utc: datetime
    weekday: int
    hour: int
    minute: int
    confidence: str


@dataclass(frozen=True, slots=True)
class PeakTimeSlot:
    location_key: str
    weekday: int
    hour: int
    minute: int
    count: int


@dataclass(frozen=True, slots=True)
class PeakWarning:
    location_key: str
    weekday: int
    hour: int
    minute: int
    count: int
    minutes_until_peak: int
    slot_key: str


def resolve_shift_location_key(
    *,
    alert_location: str,
    watch_label: str | None,
    profile_id: str,
) -> str:
    loc = (alert_location or "").strip() or (watch_label or "").strip() or (profile_id or "").strip()
    return loc or "default"


def bucket_minute(minute: int, *, step: int = 5) -> int:
    step = max(1, int(step))
    return (int(minute) // step) * step


def analyze_peak_slots(
    events: list[ShiftDropEvent],
    *,
    min_slot_count: int = 2,
    top_per_location: int = 8,
) -> list[PeakTimeSlot]:
    """Group drops by location + weekday + 5-minute bucket; return top slots by count."""
    counts: dict[tuple[str, int, int, int], int] = defaultdict(int)
    for ev in events:
        bm = bucket_minute(ev.minute)
        key = (ev.location_key, ev.weekday, ev.hour, bm)
        counts[key] += 1
    slots: list[PeakTimeSlot] = []
    for (loc, wd, hr, mn), cnt in counts.items():
        if cnt < min_slot_count:
            continue
        slots.append(
            PeakTimeSlot(location_key=loc, weekday=wd, hour=hr, minute=mn, count=cnt)
        )
    by_loc: dict[str, list[PeakTimeSlot]] = defaultdict(list)
    for s in slots:
        by_loc[s.location_key].append(s)
    out: list[PeakTimeSlot] = []
    for loc in sorted(by_loc.keys()):
        ranked = sorted(
            by_loc[loc],
            key=lambda x: (-x.count, x.weekday, x.hour, x.minute),
        )
        out.extend(ranked[:top_per_location])
    return out


def _local_dt_for_slot(
    now_utc: datetime,
    tz_name: str,
    *,
    weekday: int,
    hour: int,
    minute: int,
) -> datetime | None:
    """Today's local datetime for this weekday/hour/minute, or None if weekday mismatch."""
    tz = ZoneInfo(tz_name)
    local_now = now_utc.astimezone(tz)
    if local_now.weekday() != weekday:
        return None
    return local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def predict_peak_warnings(
    peaks: list[PeakTimeSlot],
    *,
    now_utc: datetime,
    tz_name: str,
    lead_minutes_min: int = 5,
    lead_minutes_max: int = 10,
    min_slot_count: int = 2,
) -> list[PeakWarning]:
    """
    If local time is within [peak - lead_max, peak - lead_min) on a matching weekday,
    return a warning to send once per slot per calendar day.
    """
    lead_min = max(0, int(lead_minutes_min))
    lead_max = max(lead_min, int(lead_minutes_max))
    tz = ZoneInfo(tz_name)
    local_now = now_utc.astimezone(tz)
    out: list[PeakWarning] = []
    date_key = local_now.date().isoformat()

    for slot in peaks:
        if slot.count < min_slot_count:
            continue
        target_local = _local_dt_for_slot(
            now_utc, tz_name, weekday=slot.weekday, hour=slot.hour, minute=slot.minute
        )
        if target_local is None:
            continue
        window_start = target_local - timedelta(minutes=lead_max)
        window_end = target_local - timedelta(minutes=lead_min)
        if not (window_start <= local_now < window_end):
            continue
        mins_left = max(0, int((target_local - local_now).total_seconds() // 60))
        slot_key = f"{date_key}:{slot.weekday}:{slot.hour:02d}:{slot.minute:02d}"
        out.append(
            PeakWarning(
                location_key=slot.location_key,
                weekday=slot.weekday,
                hour=slot.hour,
                minute=slot.minute,
                count=slot.count,
                minutes_until_peak=mins_left,
                slot_key=slot_key,
            )
        )
    return out


def _bar(count: int, max_count: int, width: int = 10) -> str:
    if max_count <= 0:
        return ""
    n = max(1, round(count / max_count * width)) if count > 0 else 0
    return "█" * n


def format_peak_times_report_html(
    events: list[ShiftDropEvent],
    *,
    tz_name: str,
    min_samples: int = 3,
    min_slot_count: int = 2,
) -> str:
    """HTML report for Telegram (admin Get Peak Times)."""
    if len(events) < min_samples:
        return (
            "📊 <b>Peak shift times</b>\n\n"
            f"Not enough data yet ({len(events)} openings recorded; need at least {min_samples}).\n\n"
            "Keep the shift watcher running — patterns appear after a few real openings."
        )

    peaks = analyze_peak_slots(events, min_slot_count=min_slot_count)
    if not peaks:
        return (
            "📊 <b>Peak shift times</b>\n\n"
            f"{len(events)} drop(s) logged, but no repeating time pattern yet "
            f"(need {min_slot_count}+ drops in the same weekday/time bucket)."
        )

    by_loc: dict[str, list[PeakTimeSlot]] = defaultdict(list)
    for p in peaks:
        by_loc[p.location_key].append(p)

    lines = [
        "📊 <b>Peak shift times</b>",
        f"<i>{len(events)} successful drops recorded</i>",
        "",
    ]

    for loc in sorted(by_loc.keys()):
        slots = by_loc[loc]
        max_c = max(s.count for s in slots)
        lines.append(f"📍 <b>{_escape(loc)}</b>")
        for s in slots[:6]:
            t12 = _format_time_12h(s.hour, s.minute)
            wd = _WEEKDAY_NAMES[s.weekday % 7]
            bar = _bar(s.count, max_c)
            lines.append(
                f"  {wd} {bar} <code>{t12}</code> ({s.count}×)"
            )
        best = slots[0]
        best_t = _format_time_12h(best.hour, best.minute)
        best_wd = _WEEKDAY_NAMES[best.weekday % 7]
        lines.append(
            f"  💡 Often around <b>{best_wd} ~{best_t}</b> ({tz_name})"
        )
        lines.append("")

    lines.append(
        "<i>Admin only: you get a heads-up about 5–10 minutes before these peak times.</i>"
    )
    return "\n".join(lines).strip()


def format_peak_warning_html(warning: PeakWarning, *, tz_name: str) -> str:
    wd = _WEEKDAY_NAMES[warning.weekday % 7]
    t12 = _format_time_12h(warning.hour, warning.minute)
    mins = warning.minutes_until_peak
    return (
        "⏰ <b>Peak shift window soon</b>\n\n"
        f"📍 {_escape(warning.location_key)}\n"
        f"Past drops often open around <b>{wd} ~{t12}</b> ({_escape(tz_name)}) "
        f"— <b>{warning.count}</b> time(s) recorded.\n\n"
        f"Estimated <b>~{mins} min</b> until that window — stay ready on Amazon My jobs."
    )


def _format_time_12h(hour: int, minute: int) -> str:
    h = int(hour) % 24
    m = int(minute) % 60
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def drop_event_from_record(
    *,
    location_key: str,
    profile_id: str,
    dropped_at_utc: datetime,
    weekday: int,
    hour: int,
    minute: int,
    confidence: str,
) -> ShiftDropEvent:
    return ShiftDropEvent(
        location_key=location_key,
        profile_id=profile_id,
        dropped_at_utc=dropped_at_utc,
        weekday=weekday,
        hour=hour,
        minute=minute,
        confidence=confidence,
    )
