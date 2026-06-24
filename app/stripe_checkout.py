"""Stripe Payment Link URLs and client_reference_id helpers."""

from __future__ import annotations

import re
from urllib.parse import urlencode

from app.config import PaidPlanConfig, SubscriptionConfig

_CLIENT_REF_RE = re.compile(r"^tg_(-?\d+)_([a-z0-9]+)$", re.IGNORECASE)


def stripe_client_reference(chat_id: str, plan_id: str) -> str:
    """Stripe-allowed reference: alphanumeric, dashes, underscores (max 200 chars)."""
    return f"tg_{chat_id}_{plan_id}"


def parse_stripe_client_reference(ref: str | None) -> tuple[str, str] | None:
    if not ref or not str(ref).strip():
        return None
    m = _CLIENT_REF_RE.match(str(ref).strip())
    if not m:
        return None
    return m.group(1), m.group(2).lower()


def price_pence_from_gbp(price_gbp: float) -> int:
    return max(1, int(round(float(price_gbp) * 100)))


def stripe_checkout_url(plan: PaidPlanConfig, chat_id: str) -> str:
    base = (plan.stripe_url or "").strip()
    if not base:
        return base
    ref = stripe_client_reference(chat_id, plan.id)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode({'client_reference_id': ref})}"


def plan_from_checkout_amount(
    subscription: SubscriptionConfig,
    *,
    amount_total: int | None,
    currency: str | None,
) -> PaidPlanConfig | None:
    if amount_total is None or (currency or "").lower() != "gbp":
        return None
    for plan in subscription.paid_plans:
        if price_pence_from_gbp(plan.price_gbp) == int(amount_total):
            return plan
    return None


def resolve_checkout_session(
    session: dict,
    subscription: SubscriptionConfig,
) -> tuple[str, PaidPlanConfig] | None:
    """
    Map a Stripe checkout.session object to (chat_id, plan).
    Prefers client_reference_id; falls back to amount_total vs plan prices.
    """
    ref = session.get("client_reference_id")
    parsed = parse_stripe_client_reference(str(ref) if ref is not None else None)
    if parsed:
        chat_id, plan_id = parsed
        plan = subscription.plan_by_id(plan_id)
        if plan is not None:
            return chat_id, plan

    amount = session.get("amount_total")
    try:
        amount_int = int(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount_int = None
    plan = plan_from_checkout_amount(
        subscription,
        amount_total=amount_int,
        currency=str(session.get("currency") or ""),
    )
    if plan is None:
        return None
    if parsed:
        return parsed[0], plan
    return None
