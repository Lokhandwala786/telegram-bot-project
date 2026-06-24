from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.shift_analytics import (
    ShiftDropEvent,
    analyze_peak_slots,
    format_peak_times_report_html,
    predict_peak_warnings,
    resolve_shift_location_key,
)
from app.storage.sqlite import SqliteStore


def _ev(
    *,
    loc: str = "Coventry",
    wd: int = 3,
    hour: int = 18,
    minute: int = 15,
) -> ShiftDropEvent:
    dt = datetime(2026, 5, 22, 17, 15, 0, tzinfo=UTC)
    return ShiftDropEvent(
        location_key=loc,
        profile_id="default",
        dropped_at_utc=dt,
        weekday=wd,
        hour=hour,
        minute=minute,
        confidence="deep",
    )


def test_resolve_shift_location_key_prefers_alert_location() -> None:
    assert resolve_shift_location_key(
        alert_location="Nottingham",
        watch_label="Profile A",
        profile_id="x",
    ) == "Nottingham"


def test_analyze_peak_slots_groups_by_weekday_time() -> None:
    events = [_ev(wd=3, hour=18, minute=14) for _ in range(4)]
    events += [_ev(wd=3, hour=16, minute=10) for _ in range(2)]
    peaks = analyze_peak_slots(events, min_slot_count=2)
    assert peaks
    assert peaks[0].count >= 4
    assert peaks[0].hour == 18


def test_predict_peak_warnings_in_lead_window() -> None:
    peaks = [analyze_peak_slots([_ev() for _ in range(3)], min_slot_count=2)[0]]
    # Thursday 18:15 UK — warn between 18:05 and 18:10 (21 May 2026 = Thu)
    now = datetime(2026, 5, 21, 17, 7, 0, tzinfo=UTC)  # 18:07 London (BST)
    warnings = predict_peak_warnings(
        peaks,
        now_utc=now,
        tz_name="Europe/London",
        lead_minutes_min=5,
        lead_minutes_max=10,
        min_slot_count=2,
    )
    assert len(warnings) == 1
    assert warnings[0].minutes_until_peak == 8


def test_format_peak_times_report_needs_min_samples() -> None:
    text = format_peak_times_report_html([_ev()], tz_name="Europe/London", min_samples=3)
    assert "Not enough data" in text


def test_sqlite_record_shift_drop_and_list(tmp_path: Path) -> None:
    db = tmp_path / "sa.db"
    s = SqliteStore(str(db))
    try:
        now = datetime(2026, 5, 22, 17, 15, 0, tzinfo=UTC)
        s.record_shift_drop(
            location_key="Coventry",
            profile_id="default",
            dropped_at_utc=now,
            weekday=3,
            hour=18,
            minute=15,
            confidence="deep",
            timezone="Europe/London",
        )
        assert s.count_shift_drops() == 1
        rows = s.list_shift_drop_events()
        assert rows[0]["location_key"] == "Coventry"
        s.mark_peak_warning_sent("Coventry", "2026-05-22:3:18:15")
        assert s.peak_warning_already_sent("Coventry", "2026-05-22:3:18:15")
    finally:
        s.close()
