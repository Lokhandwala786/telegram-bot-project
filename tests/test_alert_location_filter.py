from __future__ import annotations

from datetime import UTC, datetime

from app.config import AlertLocationItem
from app.models.job import JobListing
from app.storage.sqlite import SqliteStore
from app.utils.alert_locations import (
    job_matches_subscriber_location_choice,
    subscriber_accepts_job_alert,
)


def _job(*, location: str | None, title: str | None = "Warehouse") -> JobListing:
    return JobListing(
        source="amazon.jobs",
        source_url="https://example",
        job_id="1",
        url="https://example/j",
        title=title,
        location=location,
        pay_gbp_per_hour=None,
        pay_text=None,
        expected_pay_text=None,
        shift=None,
        posted_date_text=None,
        raw_metadata={},
    )


def test_choice_all_matches_anywhere() -> None:
    locs = [AlertLocationItem(name="Coventry", keywords=["coventry"])]
    j = _job(location="Derby, UK")
    assert job_matches_subscriber_location_choice(j, "all", locs)
    assert job_matches_subscriber_location_choice(j, "", locs)


def test_choice_coventry_keyword_in_location() -> None:
    locs = [AlertLocationItem(name="Coventry", keywords=["coventry", "cv1"])]
    assert job_matches_subscriber_location_choice(_job(location="Coventry, England"), "Coventry", locs)
    assert not job_matches_subscriber_location_choice(_job(location="Derby, England"), "Coventry", locs)


def test_choice_coventry_keyword_in_title() -> None:
    locs = [AlertLocationItem(name="Coventry", keywords=["coventry"])]
    j = _job(location=None, title="Warehouse — Coventry area")
    assert job_matches_subscriber_location_choice(j, "Coventry", locs)


def test_unknown_choice_fail_open() -> None:
    locs = [AlertLocationItem(name="Coventry", keywords=["coventry"])]
    j = _job(location="Derby")
    assert job_matches_subscriber_location_choice(j, "UnknownCity", locs)


def test_sqlite_subscriber_filters_roundtrip(tmp_path) -> None:
    db = tmp_path / "f.db"
    s = SqliteStore(str(db))
    s.add_subscriber("99")
    assert s.get_subscriber_location_choices_for(["99"]).get("99") == "all"
    s.set_subscriber_location_choice("99", "Coventry")
    assert s.get_subscriber_location_choices_for(["99"])["99"] == "Coventry"
    loc, pay, ms = s.get_subscriber_filter_row("99")
    assert loc == "Coventry"
    assert pay is None
    assert ms is None
    s.remove_subscriber("99")
    assert "99" not in s.get_all_subscribers()
    s.close()


def test_subscriber_accepts_db_substring_filter() -> None:
    locs: list[AlertLocationItem] = []
    j = _job(location="Coventry, England, GBR", title="Warehouse")
    assert subscriber_accepts_job_alert(j, "all", "Coventry", locs)
    assert not subscriber_accepts_job_alert(j, "all", "Derby", locs)


def test_get_distinct_job_locations(tmp_path) -> None:
    db = tmp_path / "d.db"
    s = SqliteStore(str(db))
    now = datetime.now(UTC)
    for i, loc in enumerate(["Coventry, UK", "Derby, UK", "Coventry, UK"]):
        s.upsert_job(
            key=f"k{i}",
            job_id=f"j{i}",
            url="https://u",
            source="amazon.jobs",
            source_url="https://s",
            title="t",
            location=loc,
            pay_gbp_per_hour=None,
            pay_text=None,
            expected_pay_text=None,
            shift=None,
            posted_date_text=None,
            content_hash=f"h{i}",
            now_utc=now,
        )
    d = s.get_distinct_job_locations(10)
    assert d[0] == "Coventry, UK"
    assert "Derby, UK" in d
    s.close()


def test_sqlite_job_insert_still_works_after_schema(tmp_path) -> None:
    db = tmp_path / "j.db"
    s = SqliteStore(str(db))
    now = datetime.now(UTC)
    s.upsert_job(
        key="k1",
        job_id="j1",
        url="https://u",
        source="amazon.jobs",
        source_url="https://s",
        title="t",
        location="L",
        pay_gbp_per_hour=None,
        pay_text=None,
        expected_pay_text=None,
        shift=None,
        posted_date_text=None,
        content_hash="h",
        now_utc=now,
    )
    assert s.count_jobs() == 1
    s.close()
