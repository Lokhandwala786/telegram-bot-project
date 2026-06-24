from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import FileConfig
from app.main import _fmt
from app.models.job import JobListing


def test_fmt_alert_time_is_when_message_sent() -> None:
    tz = "Europe/London"
    tracked = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    alert_at = datetime(2026, 5, 18, 12, 30, 0, tzinfo=UTC)
    job = JobListing(
        source="amazon.jobs",
        source_url="https://example.com",
        job_id="1",
        url="https://example.com/j/1",
        title="Warehouse Operative",
        location="Coventry",
        pay_gbp_per_hour=None,
        pay_text=None,
        expected_pay_text=None,
        shift=None,
        posted_date_text=None,
        raw_metadata={},
    )
    text = _fmt(
        job,
        tz_name=tz,
        first_seen_utc=tracked,
        alert_at_utc=alert_at,
        status="new",
    )
    assert "Time:" in text
    assert "2026-05-18 13:30:00 BST" in text
    assert "Listed since:" not in text


def test_fmt_updated_shows_first_tracked_when_older() -> None:
    tz = "Europe/London"
    tracked = datetime(2026, 5, 10, 8, 0, 0, tzinfo=UTC)
    alert_at = tracked + timedelta(days=3)
    job = JobListing(
        source="jobsatamazon.co.uk",
        source_url="https://example.com",
        job_id="JOB-1",
        url="https://www.jobsatamazon.co.uk/app#/jobDetail?jobId=JOB-1",
        title="Warehouse Operative",
        location="Northampton",
        pay_gbp_per_hour=None,
        pay_text=None,
        expected_pay_text=None,
        shift=None,
        posted_date_text=None,
        raw_metadata={"apply_enabled": "true"},
    )
    text = _fmt(job, tz_name=tz, first_seen_utc=tracked, alert_at_utc=alert_at, status="updated")
    assert "Time:" in text
    assert "Listed since:" in text
