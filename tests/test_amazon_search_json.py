from __future__ import annotations

from app.parsers.amazon_jobs_search import AmazonJobsSearchParser


def test_parse_search_json_sf_job_id_slug() -> None:
    payload = '{"jobs":[{"title":"Warehouse Operative","job_path":"/en/jobs/SF250119787/x","normalized_location":"Rochester, England, GBR","posted_date":"July 23, 2025","job_schedule_type":"Part"}]}'
    p = AmazonJobsSearchParser()
    jobs = p.parse(
        html=payload,
        source_url="https://www.amazon.jobs/en/search?q=warehouse",
        max_items=10,
        expected_pay_text=None,
    )
    assert len(jobs) == 1
    assert jobs[0].job_id == "SF250119787"
    assert jobs[0].url.endswith("/en/jobs/SF250119787/x")


def test_numeric_job_slug_still_supported() -> None:
    payload = '{"jobs":[{"job_path":"/en/jobs/987654321/role","title":"Associate"}]}'
    p = AmazonJobsSearchParser()
    jobs = p.parse(
        html=payload,
        source_url="https://www.amazon.jobs/en/search?q=x",
        max_items=10,
        expected_pay_text=None,
    )
    assert jobs[0].job_id == "987654321"
