"""Verify Stripe Payment Link checkout via API (fallback when webhook/redirect param missing)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.stripe_checkout import resolve_checkout_session, stripe_client_reference

log = logging.getLogger(__name__)


async def fetch_paid_checkout_session(
    secret_key: str,
    *,
    client_reference_id: str,
) -> dict[str, Any] | None:
    """Return the newest paid checkout session for this client_reference_id, if any."""
    ref = (client_reference_id or "").strip()
    key = (secret_key or "").strip()
    if not ref or not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                "https://api.stripe.com/v1/checkout/sessions",
                params={"client_reference_id": ref, "limit": 5},
                auth=(key, ""),
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as exc:
        log.warning("Stripe API list sessions failed ref=%s: %s", ref, exc)
        return None
    for session in data.get("data") or []:
        if not isinstance(session, dict):
            continue
        if str(session.get("payment_status") or "").lower() == "paid":
            return session
    return None
