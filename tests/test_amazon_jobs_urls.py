from __future__ import annotations

from app.utils.amazon_jobs_urls import search_page_url_to_json_api_url


def test_amazon_search_rewrite_to_json() -> None:
    u = "https://www.amazon.jobs/en/search?country=GBR&base_query=warehouse&loc_query=Coventry%2C%20UK"
    j = search_page_url_to_json_api_url(u)
    assert j is not None
    assert "/search.json" in j
    assert "country=GBR" in j
    assert "Coventry" in j
