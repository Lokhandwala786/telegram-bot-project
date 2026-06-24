from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

import pytest

from app.config import SubscriptionConfig
from app.stripe_checkout import (
    parse_stripe_client_reference,
    resolve_checkout_session,
    stripe_checkout_url,
    stripe_client_reference,
)
from app.stripe_webhook import configure_stripe_webhook, verify_stripe_signature
from app.storage.sqlite import SqliteStore


def test_stripe_client_reference_roundtrip() -> None:
    ref = stripe_client_reference("8674859284", "15d")
    assert ref == "tg_8674859284_15d"
    assert parse_stripe_client_reference(ref) == ("8674859284", "15d")


def test_stripe_checkout_url_includes_client_reference_id() -> None:
    cfg = SubscriptionConfig()
    plan = cfg.paid_plans[0]
    object.__setattr__(plan, "stripe_url", "https://buy.stripe.com/test_1d")
    url = stripe_checkout_url(plan, "12345")
    assert "client_reference_id=tg_12345_1d" in url
    assert url.startswith("https://buy.stripe.com/test_1d")


def test_resolve_checkout_session_from_reference() -> None:
    cfg = SubscriptionConfig()
    session = {
        "client_reference_id": "tg_999_30d",
        "payment_status": "paid",
        "amount_total": 5000,
        "currency": "gbp",
    }
    resolved = resolve_checkout_session(session, cfg)
    assert resolved is not None
    chat_id, plan = resolved
    assert chat_id == "999"
    assert plan.id == "30d"
    assert plan.days == 30


def test_resolve_checkout_session_from_amount_fallback() -> None:
    cfg = SubscriptionConfig()
    session = {
        "amount_total": 225,
        "currency": "gbp",
    }
    resolved = resolve_checkout_session(session, cfg)
    assert resolved is None


def test_verify_stripe_signature() -> None:
    secret = "whsec_test_secret"
    payload = b'{"id":"evt_1"}'
    ts = str(int(time.time()))
    signed = f"{ts}.{payload.decode()}"
    sig = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    header = f"t={ts},v1={sig}"
    assert verify_stripe_signature(payload, header, secret)
    assert not verify_stripe_signature(payload, header, "wrong")


def test_stripe_webhook_dedup(tmp_path) -> None:
    db = tmp_path / "stripe.db"
    s = SqliteStore(str(db))
    try:
        assert s.record_stripe_webhook_event("evt_abc")
        assert not s.record_stripe_webhook_event("evt_abc")
        assert s.record_stripe_webhook_event("evt_xyz")
    finally:
        s.close()


@pytest.mark.asyncio
async def test_checkout_session_completed_activates_subscription(tmp_path, respx_mock) -> None:
    from app.stripe_webhook import _handle_checkout_session_completed

    db = tmp_path / "wh.db"
    store = SqliteStore(str(db))
    token = "123456:ABC"
    admin = "1"
    try:
        store.add_subscriber("555", first_name="Test")
        store.activate_trial_subscription("555", trial_hours=24, now_utc=datetime(2026, 5, 23, 12, 0, tzinfo=UTC))
        configure_stripe_webhook(
            store=store,
            bot_token=token,
            subscription=SubscriptionConfig(),
            admin_chat_id=admin,
            stats_timezone="Europe/London",
            webhook_secret="whsec_x",
        )
        respx_mock.post(f"https://api.telegram.org/bot{token}/sendMessage").respond(
            json={"ok": True, "result": {"message_id": 1}}
        )
        session = {
            "id": "cs_test_123",
            "payment_status": "paid",
            "client_reference_id": "tg_555_1d",
            "amount_total": 225,
            "currency": "gbp",
        }
        await _handle_checkout_session_completed(session)
        assert store.subscriber_has_paid("555")
        ends = store.get_subscription_ends_at("555")
        assert ends is not None
    finally:
        store.close()
