"""Shared Stripe paid checkout → DB + Telegram notifications."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import PaidPlanConfig, SubscriptionConfig
from app.stripe_checkout import resolve_checkout_session, stripe_client_reference
from app.storage.sqlite import SqliteStore

log = logging.getLogger(__name__)


def payment_record_from_session(session: dict, *, plan_id: str, chat_id: str) -> dict[str, Any]:
    currency = str(session.get("currency") or "gbp").upper()
    amount = session.get("amount_total")
    try:
        amount_int = int(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount_int = None
    return {
        "currency": currency,
        "total_amount": amount_int,
        "invoice_payload": f"stripe_sub_{plan_id}_{chat_id}",
        "telegram_payment_charge_id": str(session.get("id") or ""),
    }


async def activate_from_checkout_session(
    session: dict,
    *,
    store: SqliteStore,
    token: str,
    subscription: SubscriptionConfig,
    stats_timezone: str,
    admin_chat_id: str | None,
    notify_admin: bool = True,
) -> bool:
    """
    Apply a paid Stripe checkout.session to the subscriber DB and send Telegram messages.
    Returns True when activation ran (False if unresolved, duplicate session, or banned).
    """
    from app.subscriber_bot import notify_paid_subscription_activated

    status = str(session.get("payment_status") or "").lower()
    if status != "paid":
        log.info("checkout session ignored (payment_status=%s)", status)
        return False

    resolved = resolve_checkout_session(session, subscription)
    if resolved is None:
        log.warning(
            "checkout session: could not resolve plan/chat (ref=%s amount=%s)",
            session.get("client_reference_id"),
            session.get("amount_total"),
        )
        return False

    chat_id, plan = resolved
    if await asyncio.to_thread(store.is_banned, chat_id):
        log.warning("Stripe payment for banned chat_id=%s ignored", chat_id)
        return False

    session_id = str(session.get("id") or "").strip()
    if session_id:
        is_new = await asyncio.to_thread(
            store.record_stripe_checkout_session, session_id, chat_id, plan.id
        )
        if not is_new:
            log.info("checkout session %s already applied", session_id)
            ends = await asyncio.to_thread(store.get_subscription_ends_at, chat_id)
            if ends is not None:
                first_name = await asyncio.to_thread(store.get_subscriber_first_name, chat_id)
                await notify_paid_subscription_activated(
                    token,
                    chat_id=chat_id,
                    plan=plan,
                    ends_at=ends,
                    stats_timezone=stats_timezone,
                    admin_chat_id=admin_chat_id,
                    user_first_name=first_name,
                    notify_admin=False,
                )
            return True

    await asyncio.to_thread(store.add_subscriber, chat_id)
    paid_days = float(plan.days)
    payment = payment_record_from_session(session, plan_id=plan.id, chat_id=chat_id)
    ends = await asyncio.to_thread(
        store.activate_paid_subscription,
        chat_id,
        paid_days=paid_days,
        payment=payment,
    )
    first_name = await asyncio.to_thread(store.get_subscriber_first_name, chat_id)
    await notify_paid_subscription_activated(
        token,
        chat_id=chat_id,
        plan=plan,
        ends_at=ends,
        stats_timezone=stats_timezone,
        admin_chat_id=admin_chat_id,
        user_first_name=first_name,
        notify_admin=notify_admin,
    )
    log.info(
        "Stripe paid activated chat_id=%s plan=%s days=%s until=%s",
        chat_id,
        plan.id,
        paid_days,
        ends.isoformat(),
    )
    return True
