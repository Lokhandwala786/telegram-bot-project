from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.main import _content_hash, _job_key, dedupe_job_listings
from app.models.job import JobListing


def _job(**kw) -> JobListing:
    return JobListing(
        source=kw.get("source", "amazon.jobs"),
        source_url="https://www.amazon.jobs/en/search?q=warehouse",
        job_id=kw.get("job_id"),
        url=kw.get("url", "https://www.amazon.jobs/en/jobs/1234567/fulfilment-associate"),
        title=kw.get("title", "Fulfilment Associate"),
        location=kw.get("location", "Leicester, United Kingdom"),
        pay_gbp_per_hour=kw.get("pay_gbp_per_hour"),
        pay_text=kw.get("pay_text"),
        expected_pay_text=kw.get("expected_pay_text"),
        shift=kw.get("shift"),
        posted_date_text=kw.get("posted_date_text"),
        raw_metadata=kw.get("raw_metadata", {}),
    )


def test_job_key_prefers_job_id() -> None:
    j = _job(job_id="1234567")
    assert _job_key(j) == "jid:1234567"


def test_job_key_same_for_different_sources_with_same_job_id() -> None:
    a = _job(job_id="JOB-UK-0000000340", source="amazon.jobs")
    b = _job(job_id="JOB-UK-0000000340", source="jobsatamazon.co.uk")
    assert _job_key(a) == _job_key(b)


def test_dedupe_job_listings_prefers_jobsatamazon_when_same_job_id() -> None:
    a = _job(job_id="JOB-UK-1", source="amazon.jobs", title="A")
    b = _job(job_id="JOB-UK-1", source="jobsatamazon.co.uk", title="B")
    out = dedupe_job_listings([a, b])
    assert len(out) == 1
    assert out[0].source == "jobsatamazon.co.uk"


def test_dedupe_job_listings_keeps_distinct_urls_without_job_id() -> None:
    x = _job(job_id=None, url="https://a.example/j1")
    y = _job(job_id=None, url="https://a.example/j2")
    out = dedupe_job_listings([x, y])
    assert len(out) == 2


def test_content_hash_changes_on_title_update() -> None:
    j1 = _job(job_id="1234567", title="Fulfilment Associate")
    j2 = _job(job_id="1234567", title="Fulfilment Associate (Nights)")
    assert _content_hash(j1) != _content_hash(j2)


def test_content_hash_ignores_irrelevant_meta_and_ordering() -> None:
    meta_a = {
        "apply_enabled": "true",
        "Hours/Week": "40",
        "Random Carousel Job Type": "Full Time",
        "Another Unrelated Key": "123",
    }
    meta_b = {
        "Hours/Week": "40",
        "apply_enabled": "true",
        "Different Carousel Job Type": "Part Time",
    }
    j1 = _job(job_id="JOB-123", raw_metadata=meta_a)
    j2 = _job(job_id="JOB-123", raw_metadata=meta_b)
    assert _content_hash(j1) == _content_hash(j2)


def test_content_hash_changes_on_meaningful_meta_update() -> None:
    j1 = _job(job_id="JOB-123", raw_metadata={"Hours/Week": "40", "apply_enabled": "true"})
    j2 = _job(job_id="JOB-123", raw_metadata={"Hours/Week": "20", "apply_enabled": "true"})
    assert _content_hash(j1) != _content_hash(j2)


def test_sqlite_sent_alerts_miss_count_and_debounce(tmp_path: Path) -> None:
    from app.storage.sqlite import SqliteStore

    db = tmp_path / "sent_alerts.db"
    s = SqliteStore(str(db))
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    s.record_sent_alert("jid:job-1", "chat-1", 1001, now)
    alert = s.get_sent_alert("jid:job-1", "chat-1")
    assert alert is not None
    assert alert["message_id"] == 1001
    assert alert["miss_count"] == 0

    miss1 = s.increment_sent_alert_miss("jid:job-1", "chat-1")
    assert miss1 == 1
    alert = s.get_sent_alert("jid:job-1", "chat-1")
    assert alert["miss_count"] == 1

    s.reset_sent_alert_miss("jid:job-1")
    alert = s.get_sent_alert("jid:job-1", "chat-1")
    assert alert["miss_count"] == 0

    s.close()


def test_sqlite_upsert_migrates_legacy_row_key_by_job_id(tmp_path: Path) -> None:
    from app.storage.sqlite import SqliteStore

    db = tmp_path / "migrate.db"
    s = SqliteStore(str(db))
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    s._conn.execute(
        """
        INSERT INTO jobs (
          key, job_id, url, source, source_url, title, location, pay_gbp_per_hour, pay_text,
          expected_pay_text, shift, posted_date_text, content_hash, first_seen_utc, last_seen_utc
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "amazon.jobs:JOB-UK-9",
            "JOB-UK-9",
            "https://x",
            "amazon.jobs",
            "https://src",
            "t",
            "L",
            None,
            None,
            None,
            None,
            None,
            "hash1",
            now.isoformat(),
            now.isoformat(),
        ),
    )
    s._conn.commit()
    up = s.upsert_job(
        key="jid:job-uk-9",
        job_id="JOB-UK-9",
        url="https://x",
        source="jobsatamazon.co.uk",
        source_url="https://src",
        title="t2",
        location="L",
        pay_gbp_per_hour=None,
        pay_text=None,
        expected_pay_text=None,
        shift=None,
        posted_date_text=None,
        content_hash="hash2",
        now_utc=now,
    )
    assert up.status == "updated"
    row = s._conn.execute("SELECT key FROM jobs").fetchone()
    assert row is not None
    assert str(row[0]) == "jid:job-uk-9"
    s.close()


def test_sqlite_purge_old_jobs(tmp_path: Path) -> None:
    from app.storage.sqlite import SqliteStore

    db = tmp_path / "purge.db"
    s = SqliteStore(str(db))
    now = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)

    # Insert a job seen 31 days ago
    old_time = now - timedelta(days=31)
    s.upsert_job(
        key="jid:job-old",
        job_id="JOB-OLD",
        url="https://x",
        source="jobsatamazon.co.uk",
        source_url="https://src",
        title="t-old",
        location="L",
        pay_gbp_per_hour=None,
        pay_text=None,
        expected_pay_text=None,
        shift=None,
        posted_date_text=None,
        content_hash="hash-old",
        now_utc=old_time,
    )

    # Insert a recent job (seen 1 day ago)
    recent_time = now - timedelta(days=1)
    s.upsert_job(
        key="jid:job-new",
        job_id="JOB-NEW",
        url="https://y",
        source="jobsatamazon.co.uk",
        source_url="https://src",
        title="t-new",
        location="L",
        pay_gbp_per_hour=None,
        pay_text=None,
        expected_pay_text=None,
        shift=None,
        posted_date_text=None,
        content_hash="hash-new",
        now_utc=recent_time,
    )

    # Purge jobs older than 30 days
    purged = s.purge_old_jobs(days=30, now_utc=now)
    assert purged == 1

    # Verify job-old is deleted, job-new is kept
    row_old = s._conn.execute("SELECT 1 FROM jobs WHERE key = 'jid:job-old'").fetchone()
    assert row_old is None
    row_new = s._conn.execute("SELECT 1 FROM jobs WHERE key = 'jid:job-new'").fetchone()
    assert row_new is not None
    s.close()


