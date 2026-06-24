from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def search_page_url_to_json_api_url(page_url: str) -> str | None:
    """
    React search pages on amazon.jobs return an empty shell over HTTP.

    Job hits are loaded from the sibling JSON endpoint: ``/…/search`` → ``/…/search.json``
    with the same query string.
    """
    p = urlparse(page_url.strip())
    if "amazon.jobs" not in (p.netloc or "").lower():
        return None
    path = (p.path or "").rstrip("/")
    if not path.endswith("/search"):
        return None
    json_path = path + ".json"
    return urlunparse((p.scheme or "https", p.netloc or "www.amazon.jobs", json_path, "", p.query, ""))
