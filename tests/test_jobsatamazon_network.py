from __future__ import annotations

from app.utils.jobsatamazon_network import enrich_listing, extract_from_network_json
from app.models.job import JobListing


def test_extract_from_nested_json() -> None:
    caps = [{"nested": {"weeklySchedule": "Mon-Fri 09:00 - 13:00", "hourlyRate": "14.30 /hr"}}]
    ext = extract_from_network_json(caps)
    assert ext.schedule == "Mon-Fri 09:00 - 13:00"
    assert ext.pay and "14.30" in ext.pay


def test_enrich_listing_prefers_network_shift_fields() -> None:
    job = JobListing(
        source="jobsatamazon.co.uk",
        source_url="https://www.jobsatamazon.co.uk/app#/jobDetail?jobId=JOB-UK-0000000001",
        job_id="JOB-UK-0000000001",
        url="https://www.jobsatamazon.co.uk/app#/jobDetail?jobId=JOB-UK-0000000001",
        title="Warehouse Operative",
        location="Northampton area",
        pay_gbp_per_hour=None,
        pay_text=None,
        expected_pay_text=None,
        shift="N/A",
        posted_date_text=None,
        raw_metadata={"apply_enabled": "true"},
    )
    ext = extract_from_network_json(
        [
            {
                "employmentType": "Seasonal | Part-time",
                "weeklySchedule": "Fri 9:00 - 13:00",
                "hourlyRate": "14.30 /hr",
                "firstDay": "2026-05-15",
                "hoursPerWeek": 20,
            }
        ]
    )
    out = enrich_listing(job, ext)
    assert out.raw_metadata.get("Employment type") == "Seasonal | Part-time"
    assert out.raw_metadata.get("Schedule") == "Fri 9:00 - 13:00"
    assert out.posted_date_text == "2026-05-15"
    assert out.pay_text and "14.30" in out.pay_text
