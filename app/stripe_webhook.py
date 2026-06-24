"""Stripe webhook: activate paid subscription after Payment Link checkout."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

from app.config import SubscriptionConfig
from app.stripe_activation import activate_from_checkout_session
from app.storage.sqlite import SqliteStore

log = logging.getLogger(__name__)

app = FastAPI(title="Stripe Webhook", docs_url=None, redoc_url=None)

_state: dict[str, Any] = {}


def configure_stripe_webhook(
    *,
    store: SqliteStore,
    bot_token: str,
    subscription: SubscriptionConfig,
    admin_chat_id: str | None,
    stats_timezone: str,
    webhook_secret: str,
) -> None:
    _state.clear()
    _state.update(
        {
            "store": store,
            "bot_token": bot_token.strip(),
            "subscription": subscription,
            "admin_chat_id": (admin_chat_id or "").strip() or None,
            "stats_timezone": stats_timezone,
            "webhook_secret": webhook_secret.strip(),
        }
    )


def verify_stripe_signature(payload: bytes, stripe_signature: str | None, secret: str) -> bool:
    if not stripe_signature or not secret:
        return False
    parts: dict[str, str] = {}
    for item in stripe_signature.split(","):
        key, _, value = item.partition("=")
        if key and value:
            parts[key.strip()] = value.strip()
    timestamp = parts.get("t")
    v1 = parts.get("v1")
    if not timestamp or not v1:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > 300:
        return False
    signed = f"{timestamp}.{payload.decode('utf-8')}"
    expected = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


async def _handle_checkout_session_completed(session: dict) -> None:
    store: SqliteStore = _state["store"]
    subscription: SubscriptionConfig = _state["subscription"]
    token: str = _state["bot_token"]
    tz_name: str = _state["stats_timezone"]
    admin_chat_id: str | None = _state.get("admin_chat_id")

    await activate_from_checkout_session(
        session,
        store=store,
        token=token,
        subscription=subscription,
        stats_timezone=tz_name,
        admin_chat_id=admin_chat_id,
    )


async def _process_stripe_event(event: dict) -> None:
    etype = str(event.get("type") or "")
    obj = event.get("data", {})
    if not isinstance(obj, dict):
        return
    session = obj.get("object")
    if not isinstance(session, dict):
        return

    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        await _handle_checkout_session_completed(session)
        return

    log.debug("Stripe event ignored: %s", etype)


@app.post("/stripe/webhook")
async def stripe_webhook_endpoint(request: Request) -> Response:
    secret = str(_state.get("webhook_secret") or "")
    if not secret:
        return Response(status_code=503, content="webhook not configured")

    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    if not verify_stripe_signature(payload, sig, secret):
        log.warning("Stripe webhook signature verification failed")
        return Response(status_code=400, content="invalid signature")

    try:
        event = json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return Response(status_code=400, content="invalid json")

    event_id = str(event.get("id") or "")
    store: SqliteStore = _state["store"]
    if event_id:
        is_new = await asyncio.to_thread(store.record_stripe_webhook_event, event_id)
        if not is_new:
            log.debug("Duplicate Stripe event %s skipped", event_id)
            return Response(status_code=200, content="ok")

    try:
        await _process_stripe_event(event)
    except Exception:
        log.exception("Stripe event processing failed id=%s type=%s", event_id, event.get("type"))
        return Response(status_code=500, content="processing error")

    return Response(status_code=200, content="ok")


@app.get("/stripe/health")
async def stripe_health() -> dict[str, str]:
    return {"status": "ok"}


async def run_stripe_webhook_server(
    *,
    store: SqliteStore,
    bot_token: str,
    subscription: SubscriptionConfig,
    admin_chat_id: str | None,
    stats_timezone: str,
    webhook_secret: str,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    configure_stripe_webhook(
        store=store,
        bot_token=bot_token,
        subscription=subscription,
        admin_chat_id=admin_chat_id,
        stats_timezone=stats_timezone,
        webhook_secret=webhook_secret,
    )
    log.info("Stripe webhook listening on http://%s:%s/stripe/webhook", host, port)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    await server.serve()
