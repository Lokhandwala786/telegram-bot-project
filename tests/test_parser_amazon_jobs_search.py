from __future__ import annotations

from pathlib import Path

from app.parsers.amazon_jobs_search import AmazonJobsSearchParser


def test_parser_extracts_json_ld_jobs() -> None:
    html = Path("tests/fixtures/amazon_search_sample.html").read_text(encoding="utf-8")
    p = AmazonJobsSearchParser()
    jobs = p.parse(html=html, source_url="https://www.amazon.jobs/en/search?q=warehouse", max_items=50, expected_pay_text=None)
    assert any(j.job_id == "1234567" for j in jobs)
    j = next(j for j in jobs if j.job_id == "1234567")
    assert "Fulfil" in (j.title or "")
    assert "Leicester" in (j.location or "")


def test_parser_html_fallback_finds_links() -> None:
    html = Path("tests/fixtures/amazon_search_sample.html").read_text(encoding="utf-8")
    p = AmazonJobsSearchParser()
    jobs = p._parse_html_cards(  # type: ignore[attr-defined]
        __import__("bs4").BeautifulSoup(html, "lxml"),
        "https://www.amazon.jobs/en/search?q=warehouse",
        expected_pay_text=None,
    )
    assert any(j.job_id == "7654321" for j in jobs)

