from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlparse


_WS_RE = re.compile(r"\s+")
_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


def normalize_whitespace(s: str) -> str:
    return _WS_RE.sub(" ", s).strip()


def html_to_plain_text(s: str) -> str:
    """Strip tags / script; collapse whitespace. For job description HTML from APIs."""
    if not s or not str(s).strip():
        return ""
    t = _SCRIPT_STYLE_RE.sub(" ", str(s))
    t = _HTML_TAG_RE.sub(" ", t)
    t = re.sub(r"&nbsp;|&#160;", " ", t, flags=re.I)
    t = re.sub(r"&(?:#\d+|[a-z]+);", " ", t, flags=re.I)
    return normalize_whitespace(t)


def is_garbled_scraped_line(s: str) -> bool:
    """
    Heuristic: UI dump where several labels got concatenated (N/A chains, multiple field names).
    """
    if not s or len(s) < 40:
        return False
    if s.count("N/A") >= 3:
        return True
    markers = ("Duration:", "Pay rate:", "Location /", "Location Work address:", "Work address:")
    hit = sum(1 for m in markers if m in s)
    return hit >= 2


def clean_description_plain(raw: str | None, *, min_len: int = 30) -> str | None:
    """Plain text suitable for storage/Telegram; None if junk or empty."""
    if not raw or not str(raw).strip():
        return None
    s = str(raw)
    plain = html_to_plain_text(s) if ("<" in s and ">" in s) else normalize_whitespace(s)
    if len(plain) < min_len:
        return None
    if is_garbled_scraped_line(plain):
        return None
    low = plain.lower()
    if "stylesheet" in low or "cloudfront.net" in low or "flex-container" in low:
        return None
    return plain


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonicalize_url(url: str, base: str | None = None) -> str:
    if base:
        url = urljoin(base, url)
    p = urlparse(url)
    # Remove fragments; keep query because it can encode location filters etc.
    return p._replace(fragment="").geturl()

