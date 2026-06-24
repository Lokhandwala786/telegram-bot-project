from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import AlertLocationItem, PaidPlanConfig, SubscriptionConfig
from app.stripe_checkout import stripe_checkout_url, stripe_client_reference
from app.shift_analytics import (
    ShiftDropEvent,
    drop_event_from_record,
    format_peak_times_report_html,
)
from app.storage.sqlite import EngagementStats, LapsedTrialLead, SqliteStore, SubscriberAdminRow

log = logging.getLogger(__name__)

# Admin tapped "Reference grant" — next plain message from admin is treated as target chat_id.
_admin_awaiting_reference_grant: set[str] = set()
# Admin tapped "Promo → lapsed" — next plain message is broadcast to saved lapsed-trial leads.
_admin_awaiting_lapsed_broadcast: set[str] = set()

_HELP_TEXT_USER = """🤖 Amazon Jobs Alert Bot

Commands:
/start — Welcome + 1-day free trial (tap Subscribe)
/stop — Unsubscribe
/mute — Pause job + shift alerts (you stay subscribed)
/unmute — Turn alerts back on
/status — Check your subscription status
/setlocation — Choose which area you want alerts for (inline buttons)
/myfilters — Show your current alert filters
/pay — Payment plans (1 day / 15 days / 1 month)
/help — Show this message"""


_HELP_TEXT_ADMIN = """
Admin only (owner chat = TELEGRAM_CHAT_ID):
/subscribers — Subscriber count
/list — Subscribers with Telegram name/username (getChat)
/stats — Subscriber + tracked job row counts with timestamp
/broadcast <text> — Message all subscribers
/admin — Admin panel (Users testimonials: trial / paid / lapsed)
/kick <chat_id> — Remove and ban a user by chat ID
/broadcast_lapsed <text> — Promo message to users who tried trial but never paid"""

# (command, button label, short description for button tap toast)
_HELP_USER_COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("/start", "▶️ /start", "Welcome + 1-day free trial (Subscribe button)"),
    ("/stop", "⏹ /stop", "Unsubscribe from alerts"),
    ("/mute", "🔕 /mute", "Pause job + shift alerts (stay subscribed)"),
    ("/unmute", "🔔 /unmute", "Turn alerts back on"),
    ("/status", "📋 /status", "Check subscription status"),
    ("/setlocation", "📍 /setlocation", "Pick location filter (inline buttons)"),
    ("/myfilters", "🎛 /myfilters", "Show your current filters"),
    ("/pay", "🙋 Help me — payment", "Choose a plan and pay (Stripe)"),
    ("/help", "❓ /help", "Show this help menu"),
)

_HELP_ADMIN_COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("/subscribers", "👥 /subscribers", "Total subscriber count"),
    ("/list", "📜 /list", "Subscriber list with Telegram names"),
    ("/stats", "📊 /stats", "Subscriber + job DB stats"),
    ("/broadcast", "📣 /broadcast", "Message all subscribers (add text after command)"),
    ("/admin", "🛠 /admin", "Admin panel + Users testimonials (trial / paid / lapsed)"),
    ("/kick", "🚫 /kick", "Ban user — usage: /kick CHAT_ID"),
    ("/broadcast_lapsed", "📉 /broadcast_lapsed", "Promo to trial users who never paid"),
    ("/engagement", "📈 /engagement", "Active users & reactions (not read receipts)"),
    ("/trials", "⏳ /trials", "Free trial users — time left until expiry"),
    ("/trial_all", "🎁 /trial_all", "Give everyone +24h free access (admin promo)"),
    ("/trial_all_msg", "📣 /trial_all_msg", "Give everyone +24h + send announcement"),
)


def _is_admin_chat(chat_id: str, admin_chat_id: str | None) -> bool:
    uid = _norm_chat_id(str(chat_id)) or str(chat_id).strip()
    admin = _norm_chat_id(admin_chat_id)
    return bool(admin and uid == admin)


def _is_bot_owner(chat_id: str, admin_chat_id: str | None) -> bool:
    """TELEGRAM_CHAT_ID = VIP owner; never payment, trial, or expiry."""
    return _is_admin_chat(chat_id, admin_chat_id)


def _owner_vip_message_html(*, first_name: str) -> str:
    name = html.escape(first_name.strip() or "there")
    return (
        f"👑 Hi <b>{name}</b>!\n\n"
        "<b>You are VIP — you are the bot owner.</b>\n"
        "Lifetime access is enabled for you. No payment, no trial limit, no expiry.\n\n"
        "You will always receive job + shift alerts (unless you send /mute).\n"
        "Send /admin for the admin panel."
    )


def _owner_status_html(*, muted: bool) -> str:
    lines = [
        "👑 <b>VIP — Bot owner</b>",
        "Lifetime access · no payment required",
    ]
    if muted:
        lines.append("🔕 Alerts: <b>muted</b> — send /unmute to turn on")
    else:
        lines.append("🔔 Alerts: <b>on</b>")
    return "\n".join(lines)


def _help_text_for(chat_id: str, admin_chat_id: str | None) -> str:
    """Plain-text help (legacy). Admin sees combined user + admin blocks."""
    if _is_admin_chat(chat_id, admin_chat_id):
        return _HELP_TEXT_USER + _HELP_TEXT_ADMIN
    return _HELP_TEXT_USER


def _help_user_intro_text() -> str:
    lines = ["🤖 <b>Amazon Jobs Alert Bot</b>", "", "<b>User commands</b> — tap a button to run:"]
    for cmd, _, desc in _HELP_USER_COMMANDS:
        lines.append(f"• <code>{html.escape(cmd)}</code> — {html.escape(desc)}")
    return "\n".join(lines)


def _help_admin_intro_text() -> str:
    lines = ["🔧 <b>Admin commands</b>", "", "Owner chat only — tap a button to run:"]
    for cmd, _, desc in _HELP_ADMIN_COMMANDS:
        if cmd in ("/broadcast", "/broadcast_lapsed"):
            lines.append(f"• <code>{html.escape(cmd)} &lt;text&gt;</code> — {html.escape(desc)}")
        else:
            lines.append(f"• <code>{html.escape(cmd)}</code> — {html.escape(desc)}")
    return "\n".join(lines)


def _help_admin_root_keyboard(
    *,
    trial_count: int | None = None,
    paid_count: int | None = None,
    lapsed_count: int | None = None,
) -> dict[str, Any]:
    ut_label = "👥 Users testimonials"
    if trial_count is not None and paid_count is not None and lapsed_count is not None:
        ut_label = f"👥 Users testimonials (⏳{trial_count} · 💳{paid_count} · 📉{lapsed_count})"
    return {
        "inline_keyboard": [
            [
                {"text": "👤 User", "callback_data": "help:pick:user"},
                {"text": "🔧 Admin", "callback_data": "help:pick:admin"},
            ],
            [{"text": ut_label, "callback_data": "help:users_testimonials"}],
            [{"text": "🛠 Admin panel", "callback_data": "admin:panel"}],
        ]
    }


@dataclass(frozen=True, slots=True)
class SubscriberChatMeta:
    display_name: str
    username: str | None
    chat_url: str | None


def _admin_panel_keyboard(
    *,
    trial_count: int | None = None,
    paid_count: int | None = None,
    lapsed_count: int | None = None,
) -> dict[str, Any]:
    ut_label = "👥 Users testimonials"
    if trial_count is not None and paid_count is not None and lapsed_count is not None:
        ut_label = f"👥 Users testimonials (⏳{trial_count} · 💳{paid_count} · 📉{lapsed_count})"
    return {
        "inline_keyboard": [
            [{"text": ut_label, "callback_data": "admin:users_testimonials"}],
            [
                {"text": "📊 Peak times", "callback_data": "admin:peak_times"},
                {"text": "📈 Engagement", "callback_data": "admin:engagement"},
            ],
            [
                {"text": "🚫 Kick user", "callback_data": "admin:kick_help"},
                {"text": "🎁 Reference grant", "callback_data": "admin:ref_grant"},
            ],
            [{"text": "◀️ Help menu", "callback_data": "help:pick:menu"}],
        ]
    }


async def _users_testimonials_counts(store: SqliteStore) -> tuple[int, int, int]:
    await asyncio.to_thread(store.sync_lapsed_trial_leads)
    trial_n, paid_n, lapsed_n = await asyncio.gather(
        asyncio.to_thread(lambda: len(store.list_trial_only_subscribers())),
        asyncio.to_thread(lambda: len(store.list_paid_subscribers())),
        asyncio.to_thread(store.count_lapsed_trial_leads),
    )
    return trial_n, paid_n, lapsed_n


def _users_testimonials_menu_text(*, trial_count: int, paid_count: int, lapsed_count: int) -> str:
    return (
        "👥 <b>Users testimonials</b>\n\n"
        "Subscriber groups — tap a row to open the full list:\n"
        f"• ⏳ <b>Trial active</b> (unpaid): <b>{trial_count}</b>\n"
        f"• 💳 <b>Paid</b>: <b>{paid_count}</b>\n"
        f"• 📉 <b>Trial chhod diye</b> (trial end, no pay): <b>{lapsed_count}</b>"
    )


def _users_testimonials_keyboard(
    *,
    trial_count: int,
    paid_count: int,
    lapsed_count: int,
    back_callback: str,
) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": f"⏳ Trial active ({trial_count})", "callback_data": "admin:trial"}],
            [{"text": f"💳 Paid subscribers ({paid_count})", "callback_data": "admin:paid"}],
            [
                {
                    "text": f"📉 Trial chhod diye ({lapsed_count})",
                    "callback_data": "admin:lapsed",
                }
            ],
            [{"text": "🎁 1-day free + message", "callback_data": "admin:trial_all_msg"}],
            [{"text": "📣 Promo → lapsed", "callback_data": "admin:lapsed_promo"}],
            [{"text": "◀️ Back", "callback_data": back_callback}],
        ]
    }


async def _send_users_testimonials_menu(
    token: str,
    chat_id: str,
    store: SqliteStore,
    *,
    back_callback: str,
) -> None:
    trial_n, paid_n, lapsed_n = await _users_testimonials_counts(store)
    await _send_with_reply_markup(
        token,
        chat_id,
        _users_testimonials_menu_text(
            trial_count=trial_n,
            paid_count=paid_n,
            lapsed_count=lapsed_n,
        ),
        _users_testimonials_keyboard(
            trial_count=trial_n,
            paid_count=paid_n,
            lapsed_count=lapsed_n,
            back_callback=back_callback,
        ),
        parse_mode="HTML",
    )


def _chat_open_url(chat_id: str, chat: dict[str, Any] | None) -> str | None:
    """URL to open a private chat in Telegram (t.me username or tg://user)."""
    if chat:
        un = (chat.get("username") or "").strip().lstrip("@")
        if un:
            return f"https://t.me/{un}"
    try:
        uid = int(str(chat_id).strip())
        if uid > 0:
            return f"tg://user?id={uid}"
    except ValueError:
        pass
    return None


def _admin_contact_line_html(chat_id: str, meta: SubscriberChatMeta) -> str:
    """Clickable name + chat id — opens DM in Telegram when chat_url is set."""
    cid = html.escape(chat_id)
    if meta.chat_url:
        url = html.escape(meta.chat_url, quote=True)
        name = html.escape(meta.display_name)
        un = f" @{html.escape(meta.username)}" if meta.username else ""
        return (
            f'• <a href="{url}"><b>{name}</b></a>{un} — '
            f'<a href="{url}"><code>{cid}</code></a> (tap to chat)'
        )
    name = html.escape(meta.display_name)
    return f"• <b>{name}</b> — <code>{cid}</code>"


def _admin_subscriber_list_keyboard(
    chat_ids: list[str],
    meta: dict[str, SubscriberChatMeta],
    *,
    show_kick: bool = True,
    max_users: int = 15,
    footer_rows: list[list[dict[str, str]]] | None = None,
) -> dict[str, Any]:
    """Per-user 💬 Chat (URL) + optional 🚫 kick; then footer navigation."""
    buttons: list[list[dict[str, str]]] = []
    for cid in chat_ids[:max_users]:
        m = meta.get(cid)
        if not m or not m.chat_url:
            continue
        label = _truncate_button_label(f"💬 {m.display_name}", 28)
        row: list[dict[str, str]] = [{"text": label, "url": m.chat_url}]
        if show_kick:
            row.append({"text": "🚫", "callback_data": f"admin:kick:{cid}"})
        buttons.append(row)
    if footer_rows:
        buttons.extend(footer_rows)
    else:
        buttons.append(
            [{"text": "◀️ Users testimonials", "callback_data": "admin:users_testimonials"}]
        )
    return {"inline_keyboard": buttons}


def _admin_lapsed_list_footer_rows() -> list[list[dict[str, str]]]:
    return [
        [{"text": "🔄 Refresh", "callback_data": "admin:lapsed"}],
        [
            {"text": "📣 Promo → lapsed", "callback_data": "admin:lapsed_promo"},
            {"text": "◀️ Users testimonials", "callback_data": "admin:users_testimonials"},
        ],
    ]


def _format_admin_dt(dt: datetime | None, tz_name: str) -> str:
    if dt is None:
        return "—"
    try:
        return dt.astimezone(ZoneInfo(tz_name)).strftime("%d %b %Y %H:%M %Z")
    except Exception:
        return dt.strftime("%d %b %Y %H:%M UTC")


def _subscription_active_label(ends: datetime | None, *, now: datetime) -> str:
    if ends is None:
        return "no end date"
    if ends > now:
        return "active"
    return "expired"


def _format_trial_time_left(ends: datetime | None, *, now: datetime) -> str:
    """Human-readable countdown until trial/subscription ends."""
    if ends is None:
        return "no end date"
    delta = ends - now
    secs = int(delta.total_seconds())
    if secs > 0:
        days, rem = divmod(secs, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        if days:
            return f"{days}d {hours}h left"
        if hours:
            return f"{hours}h {minutes}m left"
        return f"{minutes}m left"
    ago = -secs
    hours, rem = divmod(ago, 3600)
    minutes, _ = divmod(rem, 60)
    if hours:
        return f"ended {hours}h {minutes}m ago"
    return f"ended {minutes}m ago"


async def _telegram_display_name(token: str, chat_id: str, fallback: str | None) -> str:
    meta = await _fetch_tg_meta_for_chat_ids(token, [chat_id], fallbacks={chat_id: fallback})
    return meta.get(chat_id, SubscriberChatMeta(fallback or chat_id, None, None)).display_name


async def _fetch_tg_meta_for_chat_ids(
    token: str,
    chat_ids: list[str],
    *,
    fallbacks: dict[str, str | None] | None = None,
) -> dict[str, SubscriberChatMeta]:
    fb = fallbacks or {}
    out: dict[str, SubscriberChatMeta] = {}
    async with httpx.AsyncClient(timeout=12.0) as client:
        for cid in chat_ids:
            chat = await _telegram_get_chat(client, token, cid)
            fn = (chat.get("first_name") or "").strip() if chat else ""
            ln = (chat.get("last_name") or "").strip() if chat else ""
            un = (chat.get("username") or "").strip().lstrip("@") if chat else ""
            parts = [" ".join(x for x in (fn, ln) if x).strip()]
            if un:
                parts.append(f"@{un}")
            label = " ".join(p for p in parts if p).strip() or (fb.get(cid) or "").strip() or cid
            out[cid] = SubscriberChatMeta(
                display_name=label,
                username=un or None,
                chat_url=_chat_open_url(cid, chat),
            )
            await asyncio.sleep(0.04)
    return out


async def _fetch_tg_meta_for_rows(
    token: str, rows: list[SubscriberAdminRow]
) -> dict[str, SubscriberChatMeta]:
    return await _fetch_tg_meta_for_chat_ids(
        token,
        [r.chat_id for r in rows],
        fallbacks={r.chat_id: r.first_name for r in rows},
    )


async def _fetch_tg_meta_for_lapsed(
    token: str, leads: list[LapsedTrialLead]
) -> dict[str, SubscriberChatMeta]:
    return await _fetch_tg_meta_for_chat_ids(
        token,
        [lead.chat_id for lead in leads],
        fallbacks={lead.chat_id: lead.first_name for lead in leads},
    )


def _trial_rows_by_expiry(rows: list[SubscriberAdminRow], *, now: datetime) -> list[SubscriberAdminRow]:
    """Active trials first (soonest expiry on top), then expired."""

    def sort_key(r: SubscriberAdminRow) -> tuple[int, datetime]:
        ends = r.subscription_ends_at
        if ends is None:
            return (2, datetime.max.replace(tzinfo=UTC))
        expired = 1 if ends <= now else 0
        return (expired, ends)

    return sorted(rows, key=sort_key)


def _trial_user_lines(
    rows: list[SubscriberAdminRow],
    *,
    meta: dict[str, SubscriberChatMeta],
    tz_name: str,
    now: datetime,
) -> list[str]:
    if not rows:
        return [
            "🆓 <b>Trial subscribers</b>",
            "",
            "(koi nahi — abhi kisi ne free trial activate nahi kiya)",
        ]
    sorted_rows = _trial_rows_by_expiry(rows, now=now)
    active_n = sum(
        1 for r in sorted_rows if r.subscription_ends_at is not None and r.subscription_ends_at > now
    )
    expired_n = len(sorted_rows) - active_n
    lines = [
        f"⏳ <b>Trial time left</b> ({len(sorted_rows)} unpaid trials)",
        "",
        f"🟢 Active now: <b>{active_n}</b> · Expired: <b>{expired_n}</b>",
        "<i>Sorted soonest expiry first. Tap name to open chat.</i>",
        "",
    ]
    for r in sorted_rows:
        m = meta.get(r.chat_id) or SubscriberChatMeta(r.first_name or "?", None, None)
        left = html.escape(_format_trial_time_left(r.subscription_ends_at, now=now))
        ends_s = html.escape(_format_admin_dt(r.subscription_ends_at, tz_name))
        lines.append(
            f"{_admin_contact_line_html(r.chat_id, m)}\n"
            f"  ⏳ <b>{left}</b> · ends {ends_s}"
        )
    lines.append("")
    lines.append("🚫 Kick: <code>/kick CHAT_ID</code> or 🚫 button on list.")
    return lines


def _paid_user_lines(
    rows: list[SubscriberAdminRow],
    *,
    meta: dict[str, SubscriberChatMeta],
    tz_name: str,
    now: datetime,
) -> list[str]:
    if not rows:
        return ["💳 <b>Paid subscribers</b>", "", "(abhi koi paid subscriber nahi)"]
    lines = [
        f"💳 <b>Paid subscribers</b> ({len(rows)})",
        "",
        "<i>Tap name or chat ID to open chat. Or use 💬 buttons below.</i>",
        "",
    ]
    for r in rows:
        m = meta.get(r.chat_id) or SubscriberChatMeta(r.first_name or "?", None, None)
        status = _subscription_active_label(r.subscription_ends_at, now=now)
        paid_s = html.escape(_format_admin_dt(r.paid_at, tz_name))
        ends_s = html.escape(_format_admin_dt(r.subscription_ends_at, tz_name))
        amt = r.last_payment_total_amount
        cur = r.last_payment_currency or "?"
        pay_line = f"£{amt / 100:.2f}" if cur.upper() == "GBP" and amt is not None else (
            f"{amt} {cur}" if amt is not None else "—"
        )
        mute = "yes" if r.alerts_muted else "no"
        loc = html.escape(r.location_match_substr or r.location_choice or "all")
        lines.append(
            f"{_admin_contact_line_html(r.chat_id, m)} (<i>{status}</i>)\n"
            f"  paid: {paid_s} | {html.escape(pay_line)} | ends: {ends_s} | muted: {mute} | loc: {loc}"
        )
    return lines


async def _send_admin_panel(token: str, chat_id: str, *, store: SqliteStore | None = None) -> None:
    trial_n = paid_n = lapsed_n = None
    if store is not None:
        trial_n, paid_n, lapsed_n = await _users_testimonials_counts(store)
    await _send_with_reply_markup(
        token,
        chat_id,
        "🛠 <b>Admin panel</b>\n\n"
        "Open <b>👥 Users testimonials</b> for trial, paid, and lapsed-trial lists (with counts).",
        _admin_panel_keyboard(
            trial_count=trial_n,
            paid_count=paid_n,
            lapsed_count=lapsed_n,
        ),
        parse_mode="HTML",
    )


def _lapsed_trial_lines(
    leads: list[LapsedTrialLead],
    *,
    meta: dict[str, SubscriberChatMeta],
    tz_name: str,
    newly_synced: int = 0,
) -> list[str]:
    if not leads:
        intro = "📉 <b>Trial chhod diye</b> (trial use kiya, pay nahi kiya)\n\n"
        if newly_synced:
            intro += f"Synced: <b>{newly_synced}</b> new.\n\n"
        intro += "(list empty — koi saved lead nahi)"
        return [intro]
    lines = [
        f"📉 <b>Trial chhod diye</b> ({len(leads)})",
        "",
        "Trial start kiya, expire hua, <b>paid plan nahi</b> liya.",
        "<i>Tap name / chat ID to open chat. Or 💬 buttons below.</i>",
    ]
    if newly_synced:
        lines.append(f"Just synced: <b>{newly_synced}</b> new lead(s).")
    lines.append("")
    for lead in leads[:80]:
        m = meta.get(lead.chat_id) or SubscriberChatMeta(lead.first_name or "?", None, None)
        ended_s = html.escape(_format_admin_dt(lead.trial_ended_at, tz_name))
        promo_s = html.escape(_format_admin_dt(lead.last_promo_sent_at, tz_name))
        count = lead.promo_send_count
        lines.append(
            f"{_admin_contact_line_html(lead.chat_id, m)}\n"
            f"  trial ended: {ended_s} | promos: {count} | last: {promo_s}"
        )
    if len(leads) > 80:
        lines.append(f"\n… and {len(leads) - 80} more (use /broadcast_lapsed for all).")
    lines.append("")
    lines.append(
        "Promo: <b>📣 Promo → lapsed</b> or <code>/broadcast_lapsed text</code>"
    )
    return lines


def _shift_events_from_store_rows(rows: list[dict]) -> list[ShiftDropEvent]:
    out: list[ShiftDropEvent] = []
    for row in rows:
        raw = row.get("dropped_at_utc")
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        out.append(
            drop_event_from_record(
                location_key=str(row.get("location_key") or "default"),
                profile_id=str(row.get("profile_id") or "default"),
                dropped_at_utc=dt,
                weekday=int(row.get("weekday") or 0),
                hour=int(row.get("hour") or 0),
                minute=int(row.get("minute") or 0),
                confidence=str(row.get("confidence") or "normal"),
            )
        )
    return out


async def _send_admin_peak_times_report(
    token: str,
    store: SqliteStore,
    *,
    chat_id: str,
    stats_timezone: str,
) -> None:
    rows = await asyncio.to_thread(store.list_shift_drop_events, limit=500)
    events = _shift_events_from_store_rows(rows)
    report = format_peak_times_report_html(
        events,
        tz_name=stats_timezone,
        min_samples=3,
        min_slot_count=2,
    )
    trial_n, paid_n, lapsed_n = await _users_testimonials_counts(store)
    await _send_with_reply_markup(
        token,
        chat_id,
        report,
        _admin_panel_keyboard(
            trial_count=trial_n,
            paid_count=paid_n,
            lapsed_count=lapsed_n,
        ),
        parse_mode="HTML",
    )


async def _send_lapsed_trial_list(
    token: str,
    store: SqliteStore,
    *,
    chat_id: str,
    stats_timezone: str,
) -> None:
    newly = await asyncio.to_thread(store.sync_lapsed_trial_leads)
    leads = await asyncio.to_thread(store.list_lapsed_trial_leads)
    meta = await _fetch_tg_meta_for_lapsed(token, leads[:40])
    lines = _lapsed_trial_lines(leads, meta=meta, tz_name=stats_timezone, newly_synced=newly)
    kb = _admin_subscriber_list_keyboard(
        [lead.chat_id for lead in leads],
        meta,
        footer_rows=_admin_lapsed_list_footer_rows(),
    )
    await _send_admin_chunked_lists(token, chat_id, lines, reply_markup=kb)


async def _admin_broadcast_lapsed(
    token: str,
    store: SqliteStore,
    *,
    admin_chat_id: str,
    message: str,
) -> str:
    payload = message.strip()
    if not payload:
        return "⚠️ Message is empty. Example:\n<code>/broadcast_lapsed Special: £99 for 30 days — tap Subscribe!</code>"
    await asyncio.to_thread(store.sync_lapsed_trial_leads)
    targets = await asyncio.to_thread(store.get_lapsed_trial_chat_ids)
    if not targets:
        return "📭 No lapsed-trial leads saved yet. Open <b>Trial lapsed</b> in admin panel after trials expire."
    sent = 0
    failed = 0
    for cid in targets:
        try:
            await _send_plain(token, cid, payload)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await asyncio.to_thread(store.mark_lapsed_trial_promo_sent, targets)
    fail_note = f" ({failed} failed)" if failed else ""
    return (
        f"✅ Lapsed-trial promo sent to <b>{sent}</b> user(s){fail_note}.\n"
        "They stay in the saved list for future offers."
    )


async def _send_admin_chunked_lists(
    token: str,
    chat_id: str,
    lines: list[str],
    *,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    chunks = _chunk_lines(lines, max_chars=3800)
    for i, part in enumerate(chunks):
        if i:
            part = f"(part {i + 1}/{len(chunks)})\n{part}"
        if i == len(chunks) - 1 and reply_markup is not None:
            await _send_with_reply_markup(token, chat_id, part, reply_markup, parse_mode="HTML")
        else:
            await _send_plain(token, chat_id, part, parse_mode="HTML")


def _parse_target_chat_id(text: str) -> str | None:
    t = text.strip()
    if t.lstrip("-").isdigit():
        return t
    return None


def _reference_grant_notify_user_text(*, subscription: SubscriptionConfig) -> str:
    return subscription.reference_grant_user_message.strip() or (
        "Ahhaha — you're special to the admin! You get temporary free alerts. Enjoy! 🎉"
    )


async def _admin_grant_reference_access(
    token: str,
    store: SqliteStore,
    *,
    admin_chat_id: str,
    target_chat_id: str,
    subscription: SubscriptionConfig,
    stats_timezone: str,
) -> str:
    tid = target_chat_id.strip()
    if not _parse_target_chat_id(tid):
        return "⚠️ Invalid chat ID. Send digits only, e.g. <code>8674859284</code>"
    if _is_admin_chat(tid, admin_chat_id):
        return "⛔ Admin already has VIP lifetime access."
    if await asyncio.to_thread(store.subscriber_is_owner, tid):
        return "⛔ That user is the bot owner (VIP)."
    if await asyncio.to_thread(store.is_banned, tid):
        pass  # grant unbans via grant_admin_reference_access

    tg_name = await _telegram_display_name(token, tid, None)
    ends = await asyncio.to_thread(
        store.grant_admin_reference_access,
        tid,
        grant_hours=subscription.reference_grant_hours,
        first_name=tg_name.split()[0] if tg_name else None,
    )
    if ends is None:
        return "⛔ Could not grant (owner account)."

    user_msg = _reference_grant_notify_user_text(subscription=subscription)
    await _send_plain(token, tid, user_msg)
    ends_s = html.escape(_format_admin_dt(ends, stats_timezone))
    hours = int(subscription.reference_grant_hours)
    return (
        f"✅ Reference access granted to <code>{html.escape(tid)}</code> "
        f"({html.escape(tg_name)}) for <b>{hours}h</b> — until {ends_s}.\n"
        "They were notified."
    )


async def _admin_kick_user(
    token: str,
    store: SqliteStore,
    *,
    admin_chat_id: str,
    target_chat_id: str,
) -> str:
    tid = target_chat_id.strip()
    if not tid or not tid.lstrip("-").isdigit():
        return "⚠️ Invalid chat ID. Use: <code>/kick 123456789</code>"
    if _is_admin_chat(tid, admin_chat_id) or await asyncio.to_thread(store.subscriber_is_owner, tid):
        return "⛔ Cannot kick the bot owner (VIP)."
    was_sub = tid in await asyncio.to_thread(store.get_all_subscribers)
    await asyncio.to_thread(store.kick_subscriber, tid, reason="admin_kick")
    try:
        await _send_plain(
            token,
            tid,
            "⛔ You have been removed from this bot by the administrator.",
        )
    except Exception:
        pass
    if was_sub:
        return f"✅ Kicked <code>{html.escape(tid)}</code> — removed from subscribers and banned."
    return f"✅ Banned <code>{html.escape(tid)}</code> (was not in subscriber list)."


async def _handle_admin_callback(
    token: str,
    store: SqliteStore,
    *,
    chat_id: str,
    data: str,
    callback_query_id: str,
    admin_chat_id: str | None,
    stats_timezone: str,
    subscription: SubscriptionConfig,
) -> bool:
    if not data.startswith("admin:"):
        return False
    if not _is_admin_chat(chat_id, admin_chat_id):
        await _answer_callback_query(
            token,
            callback_query_id,
            "⛔ Admin only.",
            show_alert=True,
        )
        return True

    await _answer_callback_query(token, callback_query_id)

    if data == "admin:panel":
        await _send_admin_panel(token, chat_id, store=store)
        return True

    if data == "admin:users_testimonials":
        await _send_users_testimonials_menu(
            token, chat_id, store, back_callback="admin:panel"
        )
        return True

    if data == "admin:trial_all_msg":
        hours = float(subscription.trial_duration_hours)
        total_updated, trial_started = await asyncio.to_thread(
            store.grant_free_trial_to_all, trial_hours=hours
        )
        targets = await asyncio.to_thread(store.get_all_non_banned_subscribers)
        text_msg = "🎁 We're giving everyone a 1-day free trial. Enjoy the alerts!"
        sent = 0
        for sid in targets:
            try:
                await _send_plain(token, sid, text_msg)
                sent += 1
            except Exception:
                log.exception("admin:trial_all_msg send failed (chat_id=%s)", sid)
        await _send_plain(
            token,
            chat_id,
            "🎁 <b>Admin promo: free access + announcement</b>\n\n"
            f"Hours: <b>{hours:g}h</b>\n"
            f"Subscribers updated: <b>{total_updated}</b>\n"
            f"Trial started (unpaid, first-time): <b>{trial_started}</b>\n"
            f"Announcement sent: <b>{sent}</b> / {len(targets)}\n\n"
            f"Message: <code>{html.escape(text_msg)}</code>",
            parse_mode="HTML",
        )
        return True

    if data == "admin:lapsed":
        await _send_lapsed_trial_list(token, store, chat_id=chat_id, stats_timezone=stats_timezone)
        return True

    if data == "admin:peak_times":
        await _send_admin_peak_times_report(
            token,
            store,
            chat_id=chat_id,
            stats_timezone=stats_timezone,
        )
        return True

    if data == "admin:engagement":
        await _send_admin_engagement_report(
            token,
            store,
            chat_id=chat_id,
            stats_timezone=stats_timezone,
        )
        return True

    if data == "admin:lapsed_promo":
        admin_norm = _norm_chat_id(admin_chat_id)
        if admin_norm:
            _admin_awaiting_lapsed_broadcast.add(admin_norm)
            _admin_awaiting_reference_grant.discard(admin_norm)
        await _send_with_reply_markup(
            token,
            chat_id,
            "📣 <b>Promo → lapsed trial users</b>\n\n"
            "Send your <b>next message</b> (offer, lower price, reminder to subscribe).\n"
            "It goes only to users saved in <b>Trial lapsed</b> (trial ended, never paid).\n\n"
            "Or use: <code>/broadcast_lapsed your text here</code>\n\n"
            "Send <code>/cancel</code> to abort.",
            {
                "inline_keyboard": [
                    [{"text": "◀️ Users testimonials", "callback_data": "admin:users_testimonials"}],
                ]
            },
            parse_mode="HTML",
        )
        return True

    if data == "admin:lapsed_cancel":
        admin_norm = _norm_chat_id(admin_chat_id)
        if admin_norm:
            _admin_awaiting_lapsed_broadcast.discard(admin_norm)
        await _send_plain(token, chat_id, "Lapsed promo broadcast cancelled.")
        return True

    if data == "admin:kick_help":
        trial_n, paid_n, lapsed_n = await _users_testimonials_counts(store)
        await _send_with_reply_markup(
            token,
            chat_id,
            "🚫 <b>Kick / ban a user</b>\n\n"
            "Send: <code>/kick CHAT_ID</code>\n"
            "Example: <code>/kick 8674859284</code>\n\n"
            "Or open <b>Users testimonials</b> → trial/paid list and tap 🚫 next to a chat ID.",
            _admin_panel_keyboard(
                trial_count=trial_n,
                paid_count=paid_n,
                lapsed_count=lapsed_n,
            ),
            parse_mode="HTML",
        )
        return True

    if data == "admin:ref_grant":
        admin_norm = _norm_chat_id(admin_chat_id)
        if admin_norm:
            _admin_awaiting_reference_grant.add(admin_norm)
        hours = int(subscription.reference_grant_hours)
        trial_n, paid_n, lapsed_n = await _users_testimonials_counts(store)
        await _send_with_reply_markup(
            token,
            chat_id,
            "🎁 <b>Reference grant</b> — free temporary alerts\n\n"
            f"Send the user's <b>chat ID</b> in your next message (numbers only).\n"
            f"They get <b>{hours} hours</b> of free alerts + a special notification.\n\n"
            "Or use: <code>/grant CHAT_ID</code>\n"
            "Example: <code>/grant 8674859284</code>\n\n"
            "Send <code>/cancel</code> to abort.",
            _admin_panel_keyboard(
                trial_count=trial_n,
                paid_count=paid_n,
                lapsed_count=lapsed_n,
            ),
            parse_mode="HTML",
        )
        return True

    if data == "admin:ref_cancel":
        admin_norm = _norm_chat_id(admin_chat_id)
        if admin_norm:
            _admin_awaiting_reference_grant.discard(admin_norm)
        await _send_plain(token, chat_id, "Reference grant cancelled.")
        return True

    if data.startswith("admin:kick:") and data != "admin:kick_help":
        target = data[len("admin:kick:") :].strip()
        if target:
            msg = await _admin_kick_user(
                token, store, admin_chat_id=admin_chat_id or "", target_chat_id=target
            )
            await _send_plain(token, chat_id, msg, parse_mode="HTML")
        return True

    now = datetime.now(UTC)
    if data == "admin:trial":
        rows = await asyncio.to_thread(store.list_trial_only_subscribers)
        meta = await _fetch_tg_meta_for_rows(token, rows)
        lines = _trial_user_lines(rows, meta=meta, tz_name=stats_timezone, now=now)
        kb = _admin_subscriber_list_keyboard([r.chat_id for r in rows], meta)
        await _send_admin_chunked_lists(token, chat_id, lines, reply_markup=kb)
        return True

    if data == "admin:paid":
        rows = await asyncio.to_thread(store.list_paid_subscribers)
        meta = await _fetch_tg_meta_for_rows(token, rows)
        lines = _paid_user_lines(rows, meta=meta, tz_name=stats_timezone, now=now)
        kb = _admin_subscriber_list_keyboard([r.chat_id for r in rows], meta)
        await _send_admin_chunked_lists(token, chat_id, lines, reply_markup=kb)
        return True

    return True


def _help_commands_keyboard(section: str, *, show_back: bool) -> dict[str, Any]:
    cmds = _HELP_USER_COMMANDS if section == "user" else _HELP_ADMIN_COMMANDS
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for cmd, label, _desc in cmds:
        key = cmd.lstrip("/")
        row.append({"text": label, "callback_data": f"help:run:{section}:{key}"})
        if len(row) >= 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if section == "user":
        rows.append([{"text": "🙋 Help me — payment plans", "callback_data": "sub:pay_menu"}])
    if show_back:
        rows.append([{"text": "◀️ Back", "callback_data": "help:pick:menu"}])
    return {"inline_keyboard": rows}


def _help_run_command_from_callback(data: str) -> tuple[str, str] | None:
    """Parse ``help:run:user:start`` → (section, '/start')."""
    if not data.startswith("help:run:"):
        return None
    rest = data[len("help:run:") :]
    section, sep, key = rest.partition(":")
    if not sep or section not in ("user", "admin") or not key:
        return None
    pool = _HELP_USER_COMMANDS if section == "user" else _HELP_ADMIN_COMMANDS
    for cmd, _label, _desc in pool:
        if cmd.lstrip("/") == key:
            return section, cmd
    return None


def _command_token(text: str) -> str:
    if not text:
        return ""
    token = text.split(None, 1)[0].strip()
    if "@" in token:
        token = token.split("@", 1)[0]
    return token


def _norm_chat_id(chat_id: str | None) -> str | None:
    if chat_id is None:
        return None
    s = str(chat_id).strip()
    return s if s else None


def _format_subscriber_line(chat_id: str, chat: dict[str, Any] | None) -> str:
    """One line for /list (chat is Telegram getChat result)."""
    if not chat:
        return f"• {chat_id} — (profile not available)"
    un = (chat.get("username") or "").strip()
    fn = (chat.get("first_name") or "").strip()
    ln = (chat.get("last_name") or "").strip()
    title = (chat.get("title") or "").strip()
    ctype = (chat.get("type") or "").strip()
    parts: list[str] = []
    if fn or ln:
        parts.append(" ".join(x for x in (fn, ln) if x).strip())
    if un:
        parts.append(f"@{un}")
    if title and ctype in ("group", "supergroup", "channel"):
        parts.append(f'"{title}"')
    label = " — ".join(parts) if parts else ""
    extra = f" [{ctype}]" if ctype and ctype != "private" else ""
    if label:
        return f"• {chat_id} — {label}{extra}"
    return f"• {chat_id}{extra if extra else ' — (no name)'}"


def _engagement_report_html(stats: EngagementStats, *, tz_name: str) -> str:
    try:
        local_note = f"Times in {html.escape(tz_name)} where shown."
    except Exception:
        local_note = ""
    lines = [
        "📈 <b>User engagement</b>",
        "",
        "<i>Telegram bots cannot see “message seen” ✓✓ like WhatsApp. "
        "Below: users who used the bot or reacted to alerts recently.</i>",
        "",
        f"👥 Total subscribers: <b>{stats.total_subscribers}</b>",
        f"✅ Subscription active now: <b>{stats.subscription_active}</b>",
        f"🟢 Active last 24h: <b>{stats.active_24h}</b>",
        f"🟢 Active last 7 days: <b>{stats.active_7d}</b>",
        f"😀 Reacted to an alert (ever): <b>{stats.reacted_ever}</b>",
        f"😀 Reacted last 7 days: <b>{stats.reacted_7d}</b>",
        f"🔕 Alerts muted: <b>{stats.muted}</b>",
        f"📊 Avg taps/messages (active 7d): <b>{stats.avg_interactions_active_7d}</b>",
        "",
    ]
    if stats.top_active_7d:
        lines.append("<b>Most active (7 days):</b>")
        for cid, name, count, last_at in stats.top_active_7d[:8]:
            label = html.escape((name or "?").strip())
            last_s = ""
            if last_at:
                try:
                    dt = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
                    last_s = dt.astimezone(ZoneInfo(tz_name)).strftime("%d %b %H:%M")
                except Exception:
                    last_s = last_at[:16]
            lines.append(
                f"• {label} — <code>{html.escape(cid)}</code> · {count} actions · last {html.escape(last_s)}"
            )
    else:
        lines.append("<i>No tracked activity in the last 7 days yet.</i>")
    if local_note:
        lines.extend(["", f"<i>{local_note}</i>"])
    return "\n".join(lines)


async def _send_admin_engagement_report(
    token: str,
    store: SqliteStore,
    *,
    chat_id: str,
    stats_timezone: str,
) -> None:
    stats = await asyncio.to_thread(store.get_engagement_stats)
    trial_n, paid_n, lapsed_n = await _users_testimonials_counts(store)
    await _send_with_reply_markup(
        token,
        chat_id,
        _engagement_report_html(stats, tz_name=stats_timezone),
        _admin_panel_keyboard(
            trial_count=trial_n,
            paid_count=paid_n,
            lapsed_count=lapsed_n,
        ),
        parse_mode="HTML",
    )


def _stats_text(*, subscriber_count: int, jobs_count: int, tz_name: str) -> str:
    now = datetime.now(UTC)
    try:
        local = now.astimezone(ZoneInfo(tz_name))
        local_s = local.strftime("%d %b %Y, %H:%M %Z")
    except Exception:
        local_s = now.strftime("%d %b %Y, %H:%M UTC")
    utc_s = now.strftime("%d %b %Y, %H:%M UTC")
    return (
        "📊 Bot stats\n"
        f"• Subscribers: {subscriber_count}\n"
        f"• Tracked job rows (SQLite): {jobs_count}\n"
        f"• Generated: {local_s}\n"
        f"  ({utc_s})"
    )


def _chunk_lines(lines: list[str], max_chars: int = 3500) -> list[str]:
    """Keep each chunk under max_chars for Telegram sendMessage (limit 4096)."""
    chunks: list[str] = []
    buf: list[str] = []
    n = 0
    for line in lines:
        add = len(line) + (1 if buf else 0)
        if buf and n + add > max_chars:
            chunks.append("\n".join(buf))
            buf = [line]
            n = len(line)
        else:
            if buf:
                n += 1
            buf.append(line)
            n += len(line)
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def _truncate_button_label(text: str, max_len: int = 34) -> str:
    t = text.strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _subscribe_trial_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "✅ Subscribe — 1 day free trial", "callback_data": "sub:activate"}],
        ]
    }


def _payment_plans_menu_text(subscription: SubscriptionConfig) -> str:
    lines = [
        "🙋 <b>Help me — choose a payment plan</b>",
        "",
        "Tap your plan below. You will open secure Stripe checkout. "
        "After payment, you return here and your subscription is activated automatically.",
        "",
    ]
    for p in subscription.paid_plans:
        lines.append(f"• {html.escape(p.button_label)}")
    return "\n".join(lines)


def _trial_repeat_blocked_html(*, first_name: str, body: str) -> str:
    name = html.escape(first_name.strip() or "there")
    return f"Hi <b>{name}</b>!\n\n{html.escape(body)}"


async def _send_trial_activation_blocked(
    *,
    token: str,
    store: SqliteStore,
    chat_id: str,
    subscription: SubscriptionConfig,
    stats_timezone: str,
    first_name: str | None = None,
) -> bool:
    """
    If the one-time free trial was already claimed, send payment options instead of re-activating.
    Returns True when activation must not proceed.
    """
    claimed = await asyncio.to_thread(store.subscriber_free_trial_already_claimed, chat_id)
    if not claimed:
        return False

    fn = (first_name or "").strip() or "there"
    if await asyncio.to_thread(store.subscriber_has_paid, chat_id):
        active = await asyncio.to_thread(store.subscriber_has_active_subscription, chat_id)
        ends = await asyncio.to_thread(store.get_subscription_ends_at, chat_id)
        if active and ends:
            try:
                end_s = ends.astimezone(ZoneInfo(stats_timezone)).strftime("%d %b %Y, %H:%M %Z")
            except Exception:
                end_s = ends.strftime("%d %b %Y, %H:%M UTC")
            text = (
                f"Hi <b>{html.escape(fn)}</b>!\n\n"
                f"✅ You have a <b>paid subscription</b> until <b>{html.escape(end_s)}</b>.\n\n"
                "Choose a plan below to extend when you are ready."
            )
        else:
            text = (
                f"Hi <b>{html.escape(fn)}</b>!\n\n"
                "✅ You have paid before. Your free trial cannot be started again.\n\n"
                "Choose a paid plan below to subscribe again."
            )
        await _send_with_reply_markup(
            token,
            chat_id,
            text,
            _subscribe_paid_keyboard(subscription, chat_id=chat_id),
            parse_mode="HTML",
        )
        return True

    active = await asyncio.to_thread(store.subscriber_has_active_subscription, chat_id)
    ends = await asyncio.to_thread(store.get_subscription_ends_at, chat_id)
    if active and ends:
        try:
            end_s = ends.astimezone(ZoneInfo(stats_timezone)).strftime("%d %b %Y, %H:%M %Z")
        except Exception:
            end_s = ends.strftime("%d %b %Y, %H:%M UTC")
        text = (
            f"Hi <b>{html.escape(fn)}</b>!\n\n"
            "⏳ Your <b>free trial is still active</b> — you cannot start another one.\n"
            f"Alerts continue until <b>{html.escape(end_s)}</b>.\n\n"
            "💳 Want paid access now? Choose a plan below:"
        )
        await _send_with_reply_markup(
            token,
            chat_id,
            text,
            _subscribe_paid_keyboard(subscription, chat_id=chat_id),
            parse_mode="HTML",
        )
        return True

    body = subscription.trial_repeat_blocked_body.strip() or (
        "Your free trial has ended. Please buy a subscription to continue."
    )
    await _send_with_reply_markup(
        token,
        chat_id,
        _trial_repeat_blocked_html(first_name=fn, body=body) + "\n\n💳 <b>Choose a paid plan:</b>",
        _subscribe_paid_keyboard(subscription, chat_id=chat_id),
        parse_mode="HTML",
    )
    return True


async def _send_payment_plans_menu(
    token: str,
    store: SqliteStore,
    *,
    chat_id: str,
    subscription: SubscriptionConfig,
    admin_chat_id: str | None,
) -> None:
    if await asyncio.to_thread(store.is_banned, chat_id):
        await _send_plain(token, chat_id, "⛔ You are not allowed to use this bot.")
        return
    if _is_bot_owner(chat_id, admin_chat_id):
        await _send_plain(
            token,
            chat_id,
            "👑 You are the bot owner (VIP). No payment needed.",
            parse_mode="HTML",
        )
        return
    known = chat_id in await asyncio.to_thread(store.get_all_subscribers)
    if not known:
        await _send_plain(
            token,
            chat_id,
            "Pehle /start bhejo aur free trial activate karo — phir payment plans khulenge.",
            parse_mode="HTML",
        )
        return
    await _send_with_reply_markup(
        token,
        chat_id,
        _payment_plans_menu_text(subscription),
        _subscribe_paid_keyboard(subscription, chat_id=chat_id),
        parse_mode="HTML",
    )


def _subscribe_paid_keyboard(
    subscription: SubscriptionConfig,
    *,
    chat_id: str | None = None,
) -> dict[str, Any]:
    """Paid plan buttons — Stripe Payment Link (url) or Telegram invoice (callback)."""
    plans = subscription.paid_plans
    if not plans:
        return {
            "inline_keyboard": [
                [{"text": "💳 Subscribe (paid)", "callback_data": "sub:pay"}],
            ]
        }
    use_stripe = subscription.uses_stripe_links()
    rows: list[list[dict[str, str]]] = []
    for plan in plans:
        label = _truncate_button_label(plan.button_label, 64)
        stripe_url = (plan.stripe_url or "").strip()
        if use_stripe and stripe_url and chat_id:
            stripe_url = stripe_checkout_url(plan, chat_id) or stripe_url
        if use_stripe and stripe_url:
            rows.append([{"text": label, "url": stripe_url}])
        else:
            rows.append([{"text": label, "callback_data": f"sub:pay:{plan.id}"}])
    return {"inline_keyboard": rows}


def _parse_start_deep_link(text: str) -> str | None:
    """``/start paid_15d`` → ``paid_15d`` (Stripe redirect back to bot)."""
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[0].casefold().startswith("/start"):
        return None
    payload = parts[1].strip()
    if "@" in payload:
        payload = payload.split("@", 1)[0].strip()
    return payload or None


def _stripe_activation_retry_keyboard(
    subscription: SubscriptionConfig,
    *,
    plan_id: str | None = None,
) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    if plan_id:
        plan = subscription.plan_by_id(plan_id)
        if plan:
            rows.append(
                [{"text": "✅ I've paid — activate my subscription", "callback_data": f"sub:stripe_done:{plan.id}"}]
            )
    else:
        for plan in subscription.paid_plans:
            rows.append(
                [
                    {
                        "text": _truncate_button_label(f"Activate — {plan.button_label}", 64),
                        "callback_data": f"sub:stripe_done:{plan.id}",
                    }
                ]
            )
    return {"inline_keyboard": rows}


def _parse_payment_plan_id_from_callback(data: str) -> str | None:
    if data == "sub:pay":
        return None
    if data.startswith("sub:pay:"):
        return data[len("sub:pay:") :].strip().lower() or None
    return None


def _parse_payment_plan_id_from_payload(payload: str) -> str | None:
    """Invoice payload: paid_sub_{plan_id}_{chat_id}_{timestamp}"""
    if not payload.startswith("paid_sub_"):
        return None
    parts = payload.split("_")
    if len(parts) < 4:
        return None
    return parts[2].strip().lower() or None


def _paid_plan_from_payload(payload: str, subscription: SubscriptionConfig) -> PaidPlanConfig | None:
    plan_id = _parse_payment_plan_id_from_payload(payload)
    if plan_id:
        return subscription.plan_by_id(plan_id)
    return subscription.primary_paid_plan()


def _legacy_paid_plan(subscription: SubscriptionConfig) -> PaidPlanConfig:
    return PaidPlanConfig(
        id="legacy",
        button_label=f"Subscribe — £{subscription.paid_price_gbp:.2f}",
        days=subscription.paid_subscription_days,
        price_gbp=subscription.paid_price_gbp,
        invoice_title=subscription.paid_invoice_title,
        invoice_description=subscription.paid_invoice_description,
    )


def _price_pence_from_gbp(price_gbp: float) -> int:
    return max(1, int(round(float(price_gbp) * 100)))


def _final_expiry_reminder_html(*, first_name: str) -> str:
    name = html.escape(first_name.strip() or "there")
    return (
        f"Hi <b>{name}</b>!\n\n"
        "⏳ <b>20 minutes left</b> on your subscription.\n\n"
        "If you want to continue, choose a <b>paid plan</b> below.\n\n"
        "If you land an Amazon warehouse job, on your <b>first induction day</b> alone you can earn "
        "roughly <b>£145–£178</b> — these alerts are a strong investment."
    )


def _trial_ending_reminder_html(*, first_name: str, body: str) -> str:
    name = html.escape(first_name.strip() or "there")
    return (
        f"Hi <b>{name}</b>!\n\n"
        "⏳ <b>1 minute left</b> on your free trial.\n\n"
        f"{html.escape(body)}"
    )


def _format_subscription_end_local(ends_at: datetime, tz_name: str) -> str:
    try:
        return ends_at.astimezone(ZoneInfo(tz_name)).strftime("%d %b %Y, %H:%M %Z")
    except Exception:
        return ends_at.strftime("%d %b %Y, %H:%M UTC")


def _paid_subscription_confirmation_html(
    *,
    paid_days: float,
    ends_at: datetime,
    tz_name: str,
    plan: PaidPlanConfig | None = None,
) -> str:
    days_label = f"{int(paid_days)} days" if paid_days >= 1 else f"{int(paid_days * 24)} hours"
    end_s = _format_subscription_end_local(ends_at, tz_name)
    plan_line = ""
    if plan is not None:
        plan_line = (
            f"Plan: <b>{html.escape(plan.button_label)}</b> "
            f"(<b>{html.escape(days_label)}</b> access)\n\n"
        )
    return (
        "✅ <b>Payment received — you are subscribed.</b>\n\n"
        f"{plan_line}"
        f"Your subscription is active until <b>{html.escape(end_s)}</b>.\n\n"
        "<b>Tips:</b>\n"
        "• Tap each job link within <b>2–3 seconds</b> of the notification.\n"
        "• Stay signed in to Amazon Jobs so you can apply straight away.\n"
        "• Keep notifications on and send /unmute if alerts are paused.\n"
        "• Send /setlocation to choose your area."
    )


def _admin_paid_subscription_notify_html(
    *,
    chat_id: str,
    plan: PaidPlanConfig,
    ends_at: datetime,
    tz_name: str,
    first_name: str | None = None,
) -> str:
    name = html.escape((first_name or "").strip() or "Unknown")
    end_s = html.escape(_format_subscription_end_local(ends_at, tz_name))
    days = int(plan.days)
    return (
        "💳 <b>New paid subscription</b>\n\n"
        f"User: <b>{name}</b> (<code>{html.escape(chat_id)}</code>)\n"
        f"Plan: <b>{html.escape(plan.button_label)}</b> — "
        f"<b>{days} day</b> access · <b>£{plan.price_gbp:.2f}</b>\n"
        f"Active until: <b>{end_s}</b>"
    )


async def notify_paid_subscription_activated(
    token: str,
    *,
    chat_id: str,
    plan: PaidPlanConfig,
    ends_at: datetime,
    stats_timezone: str,
    admin_chat_id: str | None = None,
    user_first_name: str | None = None,
    notify_admin: bool = True,
) -> None:
    """Send payment confirmation to user and purchase alert to admin."""
    paid_days = float(plan.days)
    confirm = _paid_subscription_confirmation_html(
        paid_days=paid_days,
        ends_at=ends_at,
        tz_name=stats_timezone,
        plan=plan,
    )
    await _send_plain(token, chat_id, confirm, parse_mode="HTML")
    admin = _norm_chat_id(admin_chat_id)
    if notify_admin and admin and admin != chat_id:
        admin_text = _admin_paid_subscription_notify_html(
            chat_id=chat_id,
            plan=plan,
            ends_at=ends_at,
            tz_name=stats_timezone,
            first_name=user_first_name,
        )
        await _send_plain(token, admin, admin_text, parse_mode="HTML")


def _display_name_from_message(msg: dict[str, Any]) -> str:
    user = msg.get("from") if isinstance(msg.get("from"), dict) else {}
    fn = (user.get("first_name") or "").strip()
    return fn or "there"


def _display_name_from_chat(chat: dict[str, Any] | None) -> str:
    if not chat:
        return "there"
    fn = (chat.get("first_name") or "").strip()
    return fn or "there"


def _filter_location_name_list(names: list[str], exclude_substrings: list[str] | None) -> list[str]:
    from app.utils.setlocation_menu import filter_location_strings

    return filter_location_strings(names, exclude_substrings)


async def _resolve_available_locations(
    store: SqliteStore,
    *,
    limit: int,
    fallback_names: list[str],
    setlocation_exclude: list[str] | None = None,
) -> list[str]:
    exclude = setlocation_exclude or []
    db_locs = await asyncio.to_thread(
        store.get_distinct_job_locations,
        limit,
        exclude_substrings=exclude,
    )
    if db_locs:
        return db_locs
    filtered = _filter_location_name_list(
        [n.strip() for n in fallback_names if n.strip()],
        exclude,
    )
    return filtered[:limit]


def _format_locations_block(locations: list[str]) -> str:
    return "\n".join(f"• {html.escape(loc)}" for loc in locations)


def _welcome_message_html(*, first_name: str, locations: list[str], trial_hours: float) -> str:
    trial_label = "1 day" if trial_hours >= 24 and trial_hours % 24 == 0 else f"{int(trial_hours)} hours"
    parts = [f"Hi <b>{html.escape(first_name)}</b>! 👋\n"]
    if locations:
        parts.append("\nWe have available locations right now:\n")
        parts.append(_format_locations_block(locations))
        parts.append("\n")
    parts.append(f"\n🎁 We offer a <b>{html.escape(trial_label)} free trial</b>.\n\n")
    parts.append(
        "If you are ready to subscribe, press the button below for your "
        f"<b>{html.escape(trial_label)} free trial</b>."
    )
    return "".join(parts)


def _subscribed_confirmation_html(*, trial_hours: float, ends_at: datetime, tz_name: str) -> str:
    trial_label = "24 hours" if trial_hours >= 24 and trial_hours % 24 == 0 else f"{int(trial_hours)} hours"
    try:
        local_end = ends_at.astimezone(ZoneInfo(tz_name))
        end_s = local_end.strftime("%d %b %Y, %H:%M %Z")
    except Exception:
        end_s = ends_at.strftime("%d %b %Y, %H:%M UTC")
    return (
        "✅ <b>You are subscribed.</b>\n\n"
        f"Your free trial is active for <b>{html.escape(trial_label)}</b>.\n"
        f"You will receive Amazon warehouse job alerts until <b>{html.escape(end_s)}</b>.\n\n"
        "Tip: use /setlocation to pick your area."
    )


def _expiry_reminder_html(*, first_name: str, body: str) -> str:
    name = html.escape(first_name.strip() or "there")
    return (
        f"Hi <b>{name}</b>!\n\n"
        "⏳ You have <b>1 hour remaining</b> on your subscription.\n\n"
        f"{html.escape(body)}"
    )


def _subscription_status_plain(
    *,
    active: bool,
    ends_at: datetime | None,
    muted: bool,
    tz_name: str,
) -> str:
    if not active:
        return (
            "❌ No active subscription.\n"
            "Send /start and tap <b>Subscribe</b> for your 1-day free trial."
        )
    assert ends_at is not None
    try:
        local_end = ends_at.astimezone(ZoneInfo(tz_name))
        end_s = local_end.strftime("%d %b %Y, %H:%M %Z")
    except Exception:
        end_s = ends_at.strftime("%d %b %Y, %H:%M UTC")
    lines = [f"✅ You are subscribed until {end_s}."]
    if muted:
        lines.append("🔕 Alerts are muted — send /unmute to receive job + shift messages again.")
    else:
        lines.append("🔔 Alerts are on.")
    return "\n".join(lines)


def _inline_setlocation_keyboard_from_strings(
    locations: list[str],
    *,
    exclude_substrings: list[str] | None = None,
) -> dict[str, Any]:
    from app.utils.setlocation_menu import filter_location_strings

    locations = filter_location_strings(locations, exclude_substrings)
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for i, loc in enumerate(locations):
        row.append({"text": _truncate_button_label(loc), "callback_data": f"setloc:{i}"})
        if len(row) >= 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "✅ All Locations", "callback_data": "setloc:all"}])
    return {"inline_keyboard": rows}


async def _telegram_get_chat(client: httpx.AsyncClient, token: str, chat_id: str) -> dict[str, Any] | None:
    try:
        r = await client.get(
            f"https://api.telegram.org/bot{token}/getChat",
            params={"chat_id": chat_id},
        )
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            return None
        res = body.get("result")
        return res if isinstance(res, dict) else None
    except httpx.HTTPError:
        return None


async def _admin_list_subscriber_lines(token: str, store: SqliteStore) -> list[str]:
    subs = await asyncio.to_thread(store.get_all_subscribers)
    if not subs:
        return ["(no subscribers)"]
    lines = ["👥 Subscribers (Telegram getChat):"]
    async with httpx.AsyncClient(timeout=15.0) as client:
        for sid in subs:
            info = await _telegram_get_chat(client, token, sid)
            lines.append(_format_subscriber_line(sid, info))
            await asyncio.sleep(0.04)
    return lines


async def _delete_webhook_for_long_poll(token: str) -> None:
    """
    If a webhook is registered, getUpdates never returns messages until it is removed.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/deleteWebhook",
                json={"drop_pending_updates": False},
            )
            r.raise_for_status()
            body = r.json()
            if body.get("ok"):
                log.info("Telegram webhook cleared; long polling enabled for subscriber bot.")
            else:
                log.warning("deleteWebhook response: %s", body)
    except httpx.HTTPError as exc:
        log.warning("deleteWebhook failed (subscriber bot may still work): %s", exc)


def _format_message_reaction_admin_notice(mr: dict[str, Any]) -> str | None:
    """Build HTML notice for admin when a user adds emoji reaction(s) to a bot message."""
    new_reactions = mr.get("new_reaction") or []
    if not new_reactions:
        return None
    emojis: list[str] = []
    for r in new_reactions:
        if not isinstance(r, dict):
            continue
        if r.get("type") == "emoji":
            emojis.append(str(r.get("emoji") or "?"))
        elif r.get("type") == "custom_emoji":
            emojis.append("✨")
    if not emojis:
        return None

    user = mr.get("user") if isinstance(mr.get("user"), dict) else {}
    actor = mr.get("actor_chat") if isinstance(mr.get("actor_chat"), dict) else {}
    chat = mr.get("chat") if isinstance(mr.get("chat"), dict) else {}

    uid = user.get("id") or actor.get("id")
    un = (user.get("username") or "").strip()
    fn = (user.get("first_name") or "").strip()
    ln = (user.get("last_name") or "").strip()
    name = " ".join(x for x in (fn, ln) if x).strip() or (actor.get("title") or "").strip() or "Someone"

    chat_id = chat.get("id")
    chat_type = (chat.get("type") or "").strip() or "?"
    msg_id = mr.get("message_id")

    who = html.escape(name)
    if un:
        who += f" (@{html.escape(un)})"
    if uid is not None:
        who += f" <code>{uid}</code>"

    emoji_str = " ".join(emojis)
    return (
        f"😀 <b>Reaction on your alert</b>\n"
        f"Emoji: {emoji_str}\n"
        f"From: {who}\n"
        f"Chat: {html.escape(chat_type)} <code>{chat_id}</code>\n"
        f"Message: <code>{msg_id}</code>"
    )


async def _handle_message_reaction(
    token: str,
    store: SqliteStore,
    admin_chat_id: str | None,
    mr: dict[str, Any],
) -> None:
    admin = _norm_chat_id(admin_chat_id)
    if not admin:
        return
    chat = mr.get("chat") if isinstance(mr.get("chat"), dict) else {}
    raw_cid = chat.get("id")
    if raw_cid is not None:
        uid = str(raw_cid).strip()
        if uid and uid != admin:
            await asyncio.to_thread(store.record_subscriber_interaction, uid, reaction=True)
    text = _format_message_reaction_admin_notice(mr)
    if not text:
        return
    await _send_plain(token, admin, text, parse_mode="HTML")
    log.info("Forwarded message reaction to admin (%s)", text.split("\n", 1)[0])


async def _send_plain(token: str, chat_id: str, text: str, *, parse_mode: str | None = None) -> None:
    try:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
            )
            r.raise_for_status()
            body = r.json()
            if not body.get("ok", False):
                log.warning("sendMessage failed for chat_id=%s: %s", chat_id, body)
    except httpx.HTTPError as exc:
        log.warning("sendMessage HTTP error for chat_id=%s: %s", chat_id, exc)


async def _send_with_reply_markup(
    token: str,
    chat_id: str,
    text: str,
    reply_markup: dict[str, Any],
    *,
    parse_mode: str | None = None,
) -> int | None:
    """Returns Telegram ``message_id`` on success."""
    try:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
            )
            r.raise_for_status()
            body = r.json()
            if not body.get("ok", False):
                log.warning("sendMessage+keyboard failed for chat_id=%s: %s", chat_id, body)
                return None
            res = body.get("result") or {}
            mid = res.get("message_id")
            return int(mid) if mid is not None else None
    except httpx.HTTPError as exc:
        log.warning("sendMessage+keyboard HTTP error for chat_id=%s: %s", chat_id, exc)
        return None


async def _answer_callback_query(
    token: str,
    callback_query_id: str,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> None:
    try:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
            payload["show_alert"] = show_alert
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                json=payload,
            )
            r.raise_for_status()
            body = r.json()
            if not body.get("ok"):
                log.warning("answerCallbackQuery not ok: %s", body)
    except httpx.HTTPError as exc:
        log.warning("answerCallbackQuery failed: %s", exc)


async def _edit_message_html(
    token: str,
    *,
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    try:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        else:
            payload["reply_markup"] = None
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/editMessageText",
                json=payload,
            )
            r.raise_for_status()
            body = r.json()
            if body.get("ok"):
                return True
            desc = str(body.get("description") or "")
            if "message is not modified" in desc.lower():
                return True
            log.warning("editMessageText not ok (chat=%s msg=%s): %s", chat_id, message_id, body)
            return False
    except httpx.HTTPError as exc:
        log.warning("editMessageText failed: %s", exc)
        return False


async def _send_interactive_help(
    token: str,
    chat_id: str,
    admin_chat_id: str | None,
    *,
    store: SqliteStore | None = None,
) -> None:
    if _is_admin_chat(chat_id, admin_chat_id):
        trial_n = paid_n = lapsed_n = None
        if store is not None:
            trial_n, paid_n, lapsed_n = await _users_testimonials_counts(store)
        await _send_with_reply_markup(
            token,
            chat_id,
            "📖 <b>Help</b>\n\nYou are the bot admin. Choose a section or open the admin panel:",
            _help_admin_root_keyboard(
                trial_count=trial_n,
                paid_count=paid_n,
                lapsed_count=lapsed_n,
            ),
            parse_mode="HTML",
        )
        return
    await _send_with_reply_markup(
        token,
        chat_id,
        _help_user_intro_text(),
        _help_commands_keyboard("user", show_back=False),
        parse_mode="HTML",
    )


async def _send_start_welcome(
    *,
    token: str,
    store: SqliteStore,
    chat_id: str,
    first_name: str,
    subscription: SubscriptionConfig,
    stats_timezone: str,
    fallback_location_names: list[str],
    setlocation_exclude: list[str] | None = None,
    admin_chat_id: str | None = None,
) -> None:
    if await asyncio.to_thread(store.is_banned, chat_id):
        await _send_plain(token, chat_id, "⛔ You are not allowed to use this bot.")
        return
    if _is_bot_owner(chat_id, admin_chat_id):
        await asyncio.to_thread(store.ensure_owner_vip, chat_id, first_name=first_name)
        await _send_plain(token, chat_id, _owner_vip_message_html(first_name=first_name), parse_mode="HTML")
        log.info("Owner VIP welcome: %s", chat_id)
        return
    await asyncio.to_thread(store.add_subscriber, chat_id, first_name=first_name)
    if await _send_trial_activation_blocked(
        token=token,
        store=store,
        chat_id=chat_id,
        subscription=subscription,
        stats_timezone=stats_timezone,
        first_name=first_name,
    ):
        return
    locations = await _resolve_available_locations(
        store,
        limit=subscription.welcome_locations_limit,
        fallback_names=fallback_location_names,
        setlocation_exclude=setlocation_exclude,
    )
    body = _welcome_message_html(
        first_name=first_name,
        locations=locations,
        trial_hours=subscription.trial_duration_hours,
    )
    await _send_with_reply_markup(
        token,
        chat_id,
        body,
        _subscribe_trial_keyboard(),
        parse_mode="HTML",
    )


async def _send_stripe_payment_link(
    token: str,
    chat_id: str,
    *,
    plan: PaidPlanConfig,
) -> None:
    stripe_url = stripe_checkout_url(plan, chat_id) or (plan.stripe_url or "").strip()
    if not stripe_url:
        await _send_plain(
            token,
            chat_id,
            f"⚠️ Payment link for <b>{html.escape(plan.button_label)}</b> is not set up yet. "
            "Please contact the bot admin.",
            parse_mode="HTML",
        )
        return
    kb = {
        "inline_keyboard": [
            [{"text": f"Pay £{plan.price_gbp:.2f} — {plan.button_label[:40]}", "url": stripe_url}],
        ]
    }
    await _send_with_reply_markup(
        token,
        chat_id,
        f"💳 <b>{html.escape(plan.button_label)}</b>\n\n"
        f"Amount: <b>£{plan.price_gbp:.2f}</b>\n\n"
        "Tap the button below to pay securely with card (Stripe). "
        "You will receive a confirmation here as soon as payment completes.",
        kb,
        parse_mode="HTML",
    )


async def _try_stripe_api_payment_activation(
    *,
    token: str,
    store: SqliteStore,
    chat_id: str,
    subscription: SubscriptionConfig,
    stats_timezone: str,
    admin: str | None,
    stripe_secret_key: str | None,
    plan_id: str | None = None,
    first_name: str | None = None,
) -> bool:
    """Confirm payment via Stripe API (client_reference_id). Returns True if activated."""
    key = (stripe_secret_key or "").strip()
    if not key or not subscription.uses_stripe_links():
        return False
    from app.stripe_activation import activate_from_checkout_session
    from app.stripe_verify import fetch_paid_checkout_session

    if first_name:
        await asyncio.to_thread(store.add_subscriber, chat_id, first_name=first_name)

    plans: list[PaidPlanConfig] = []
    if plan_id:
        p = subscription.plan_by_id(plan_id)
        if p:
            plans = [p]
    if not plans:
        plans = list(subscription.paid_plans)

    for plan in plans:
        ref = stripe_client_reference(chat_id, plan.id)
        session = await fetch_paid_checkout_session(key, client_reference_id=ref)
        if session is None:
            continue
        log.info("Stripe API found paid session for chat_id=%s plan=%s ref=%s", chat_id, plan.id, ref)
        ok = await activate_from_checkout_session(
            session,
            store=store,
            token=token,
            subscription=subscription,
            stats_timezone=stats_timezone,
            admin_chat_id=admin,
        )
        if ok:
            return True
    return False


async def _activate_paid_from_stripe_return(
    *,
    token: str,
    store: SqliteStore,
    chat_id: str,
    plan_id: str,
    subscription: SubscriptionConfig,
    stats_timezone: str,
    admin: str | None,
    first_name: str | None = None,
    stripe_secret_key: str | None = None,
) -> None:
    """After Stripe checkout — verify via API, or trust redirect deep link as fallback."""
    log.info("Stripe return handler chat_id=%s plan_id=%r", chat_id, plan_id)
    if await asyncio.to_thread(store.is_banned, chat_id):
        await _send_plain(token, chat_id, "⛔ You are not allowed to use this bot.")
        return
    if _is_bot_owner(chat_id, admin):
        await asyncio.to_thread(store.ensure_owner_vip, chat_id, first_name=first_name)
        await _send_plain(
            token,
            chat_id,
            _owner_vip_message_html(first_name=first_name or "there"),
            parse_mode="HTML",
        )
        return

    pid = (plan_id or "").strip().lower()
    if pid.startswith("paid_"):
        pid = pid[len("paid_") :].strip()

    if (stripe_secret_key or "").strip():
        activated = await _try_stripe_api_payment_activation(
            token=token,
            store=store,
            chat_id=chat_id,
            subscription=subscription,
            stats_timezone=stats_timezone,
            admin=admin,
            stripe_secret_key=stripe_secret_key,
            plan_id=pid or None,
            first_name=first_name,
        )
        if activated:
            return
        await _send_with_reply_markup(
            token,
            chat_id,
            "⏳ <b>Payment received?</b>\n\n"
            "We could not confirm your payment with Stripe yet. "
            "Wait 30 seconds, then tap the button below to activate your subscription.\n\n"
            "If it still fails, send /start or contact support.",
            _stripe_activation_retry_keyboard(subscription, plan_id=pid or None),
            parse_mode="HTML",
        )
        return

    plan = subscription.plan_by_id(pid) if pid else None
    if plan is None:
        await _send_with_reply_markup(
            token,
            chat_id,
            "⚠️ Could not detect your plan. If you already paid, tap your plan below:",
            _stripe_activation_retry_keyboard(subscription),
            parse_mode="HTML",
        )
        return
    if first_name:
        await asyncio.to_thread(store.add_subscriber, chat_id, first_name=first_name)
    already = await asyncio.to_thread(store.paid_plan_recently_activated, chat_id, plan.id)
    if already:
        ends = await asyncio.to_thread(store.get_subscription_ends_at, chat_id)
        if ends is not None:
            await notify_paid_subscription_activated(
                token,
                chat_id=chat_id,
                plan=plan,
                ends_at=ends,
                stats_timezone=stats_timezone,
                admin_chat_id=admin,
                user_first_name=first_name,
                notify_admin=False,
            )
        return
    paid_days = float(plan.days)
    payment_meta = {
        "currency": "GBP",
        "total_amount": int(round(plan.price_gbp * 100)),
        "invoice_payload": f"stripe_sub_{plan.id}_{chat_id}",
        "telegram_payment_charge_id": f"return_paid_{plan.id}_{chat_id}",
    }
    ends = await asyncio.to_thread(
        store.activate_paid_subscription,
        chat_id,
        paid_days=paid_days,
        payment=payment_meta,
    )
    await notify_paid_subscription_activated(
        token,
        chat_id=chat_id,
        plan=plan,
        ends_at=ends,
        stats_timezone=stats_timezone,
        admin_chat_id=admin,
        user_first_name=first_name,
    )
    log.info("Stripe return (trust) activated %s plan=%s until %s", chat_id, plan.id, ends.isoformat())


async def _send_paid_subscription_invoice(
    token: str,
    chat_id: str,
    *,
    plan: PaidPlanConfig,
    provider_token: str,
) -> bool:
    amount = _price_pence_from_gbp(plan.price_gbp)
    payload_data = f"paid_sub_{plan.id}_{chat_id}_{int(time.time())}"
    title = plan.invoice_title[:32]
    body = {
        "chat_id": chat_id,
        "title": title,
        "description": plan.invoice_description[:255],
        "payload": payload_data,
        "provider_token": provider_token,
        "currency": "GBP",
        "prices": [{"label": title, "amount": amount}],
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendInvoice",
                json=body,
            )
            r.raise_for_status()
            data = r.json()
            if not data.get("ok", False):
                log.warning("sendInvoice failed for %s: %s", chat_id, data)
                return False
            return True
    except httpx.HTTPError as exc:
        log.warning("sendInvoice HTTP error for %s: %s", chat_id, exc)
        return False


async def _answer_pre_checkout_query(token: str, pre_checkout_query_id: str, *, ok: bool, error: str = "") -> None:
    body: dict[str, Any] = {"pre_checkout_query_id": pre_checkout_query_id, "ok": ok}
    if not ok and error:
        body["error_message"] = error[:200]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/answerPreCheckoutQuery",
                json=body,
            )
            r.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("answerPreCheckoutQuery failed: %s", exc)


async def _activate_subscription_from_callback(
    *,
    token: str,
    store: SqliteStore,
    chat_id: str,
    subscription: SubscriptionConfig,
    stats_timezone: str,
    admin: str | None,
    first_name: str | None = None,
) -> None:
    if await asyncio.to_thread(store.is_banned, chat_id):
        await _send_plain(token, chat_id, "⛔ You are not allowed to use this bot.")
        return
    if _is_bot_owner(chat_id, admin):
        await asyncio.to_thread(store.ensure_owner_vip, chat_id, first_name=first_name)
        await _send_plain(
            token,
            chat_id,
            _owner_vip_message_html(first_name=first_name or "there"),
            parse_mode="HTML",
        )
        return
    if first_name:
        await asyncio.to_thread(store.add_subscriber, chat_id, first_name=first_name)
    if await _send_trial_activation_blocked(
        token=token,
        store=store,
        chat_id=chat_id,
        subscription=subscription,
        stats_timezone=stats_timezone,
        first_name=first_name,
    ):
        return
    ends = await asyncio.to_thread(
        store.activate_trial_subscription,
        chat_id,
        trial_hours=subscription.trial_duration_hours,
    )
    body = _subscribed_confirmation_html(
        trial_hours=subscription.trial_duration_hours,
        ends_at=ends,
        tz_name=stats_timezone,
    )
    await _send_with_reply_markup(
        token,
        chat_id,
        body + "\n\n💳 <b>Want paid access now?</b> Choose a plan below (optional — trial is already active):",
        _subscribe_paid_keyboard(subscription, chat_id=chat_id),
        parse_mode="HTML",
    )
    if admin and admin != chat_id:
        label = first_name or chat_id
        await _send_plain(token, admin, f"🔔 New trial subscriber: {label} ({chat_id})")
    log.info("Trial subscription activated: %s until %s", chat_id, ends.isoformat())


async def _handle_paid_plan_callback(
    token: str,
    store: SqliteStore,
    *,
    chat_id: str,
    callback_query_id: str,
    plan_id: str | None,
    subscription: SubscriptionConfig,
    pay_token: str | None,
    admin_chat_id: str | None,
) -> None:
    await _answer_callback_query(token, callback_query_id)
    if await asyncio.to_thread(store.is_banned, chat_id):
        await _send_plain(token, chat_id, "⛔ You are not allowed to use this bot.")
        return
    if _is_bot_owner(chat_id, admin_chat_id):
        await asyncio.to_thread(store.ensure_owner_vip, chat_id)
        await _send_plain(
            token,
            chat_id,
            "👑 You are VIP — bot owner. No payment needed; you already have lifetime access.",
            parse_mode="HTML",
        )
        return
    known = chat_id in await asyncio.to_thread(store.get_all_subscribers)
    if not known:
        await _send_plain(
            token,
            chat_id,
            "Pehle /start bhejo aur free trial activate karo — phir payment plans khulenge.",
            parse_mode="HTML",
        )
        return
    if plan_id is None:
        await _send_payment_plans_menu(
            token,
            store,
            chat_id=chat_id,
            subscription=subscription,
            admin_chat_id=admin_chat_id,
        )
        return
    plan = subscription.plan_by_id(plan_id)
    if plan is None:
        plan = _legacy_paid_plan(subscription) if not subscription.paid_plans else None
    if plan is None:
        await _send_plain(token, chat_id, "⚠️ Invalid plan. Dubara button dabao.")
        return
    if subscription.uses_stripe_links():
        await _send_stripe_payment_link(token, chat_id, plan=plan)
        return
    if not pay_token:
        await _send_plain(
            token,
            chat_id,
            "⚠️ Online payment is not set up yet. Please contact the bot admin.\n\n"
            f"Selected: <b>{html.escape(plan.button_label)}</b>",
            parse_mode="HTML",
        )
        return
    ok = await _send_paid_subscription_invoice(token, chat_id, plan=plan, provider_token=pay_token)
    if not ok:
        await _send_plain(
            token,
            chat_id,
            "⚠️ Could not send the payment link. Please try again in a few minutes.",
        )
        return
    await _send_plain(
        token,
        chat_id,
        f"💳 <b>Payment link sent</b> — {html.escape(plan.button_label)}\n"
        f"Amount: <b>£{plan.price_gbp:.2f}</b> — open it in Telegram and tap <b>Pay</b>.",
        parse_mode="HTML",
    )


async def _process_subscriber_command(
    *,
    token: str,
    store: SqliteStore,
    chat_id: str,
    cmd: str,
    text: str,
    admin: str | None,
    stats_timezone: str,
    subscription: SubscriptionConfig,
    fallback_location_names: list[str],
    setlocation_exclude: list[str] | None = None,
    message: dict[str, Any] | None = None,
    stripe_secret_key: str | None = None,
) -> bool:
    """Handle one bot command. Returns True if ``cmd`` was recognized."""
    if cmd == "/start":
        deep = _parse_start_deep_link(text)
        first_name = _display_name_from_message(message or {})
        if deep and deep.startswith("paid_") and subscription.uses_stripe_links():
            plan_id = deep[len("paid_") :].strip().lower()
            await _activate_paid_from_stripe_return(
                token=token,
                store=store,
                chat_id=chat_id,
                plan_id=plan_id,
                subscription=subscription,
                stats_timezone=stats_timezone,
                admin=admin,
                first_name=first_name,
                stripe_secret_key=stripe_secret_key,
            )
            return True
        if subscription.uses_stripe_links() and (stripe_secret_key or "").strip():
            activated = await _try_stripe_api_payment_activation(
                token=token,
                store=store,
                chat_id=chat_id,
                subscription=subscription,
                stats_timezone=stats_timezone,
                admin=admin,
                stripe_secret_key=stripe_secret_key,
                first_name=first_name,
            )
            if activated:
                return True
        await _send_start_welcome(
            token=token,
            store=store,
            chat_id=chat_id,
            first_name=first_name,
            subscription=subscription,
            stats_timezone=stats_timezone,
            fallback_location_names=fallback_location_names,
            setlocation_exclude=setlocation_exclude,
            admin_chat_id=admin,
        )
        log.info("Start welcome sent: %s (%s)", chat_id, first_name)
        return True

    if cmd == "/stop":
        if _is_bot_owner(chat_id, admin):
            await _send_plain(
                token,
                chat_id,
                "👑 You are the bot owner (VIP). /stop does not remove your access — "
                "you have lifetime access. Use /mute if you want to pause alerts.",
                parse_mode="HTML",
            )
            return True
        await asyncio.to_thread(store.remove_subscriber, chat_id)
        await _send_plain(token, chat_id, "❌ You have been unsubscribed.")
        log.info("Unsubscribed: %s", chat_id)
        return True

    if cmd == "/help":
        await _send_interactive_help(token, chat_id, admin, store=store)
        return True

    if cmd == "/pay":
        await _send_payment_plans_menu(
            token,
            store,
            chat_id=chat_id,
            subscription=subscription,
            admin_chat_id=admin,
        )
        return True

    if cmd == "/status":
        if _is_bot_owner(chat_id, admin):
            muted = await asyncio.to_thread(store.subscriber_alerts_muted, chat_id)
            await _send_plain(token, chat_id, _owner_status_html(muted=muted), parse_mode="HTML")
            return True
        active = await asyncio.to_thread(store.subscriber_has_active_subscription, chat_id)
        ends = await asyncio.to_thread(store.get_subscription_ends_at, chat_id)
        muted = await asyncio.to_thread(store.subscriber_alerts_muted, chat_id)
        body = _subscription_status_plain(
            active=active,
            ends_at=ends,
            muted=muted,
            tz_name=stats_timezone,
        )
        trial_unpaid = await asyncio.to_thread(store.subscriber_is_trial_unpaid, chat_id)
        if trial_unpaid:
            await _send_with_reply_markup(
                token,
                chat_id,
                body + "\n\n💳 <b>Free trial</b> — choose a paid plan to continue after trial:",
                _subscribe_paid_keyboard(subscription, chat_id=chat_id),
                parse_mode="HTML",
            )
        else:
            await _send_plain(token, chat_id, body, parse_mode="HTML")
        return True

    if cmd == "/mute":
        if _is_bot_owner(chat_id, admin):
            await asyncio.to_thread(store.ensure_owner_vip, chat_id)
            await asyncio.to_thread(store.set_subscriber_alerts_muted, chat_id, muted=True)
            await _send_plain(
                token,
                chat_id,
                "🔕 Alerts muted (you remain VIP owner with lifetime access). /unmute to turn alerts back on.",
            )
            return True
        active = await asyncio.to_thread(store.subscriber_has_active_subscription, chat_id)
        if not active:
            await _send_plain(
                token,
                chat_id,
                "Pehle /start karo aur Subscribe dabao — phir /mute se alerts rok sakte ho.",
            )
            return True
        await asyncio.to_thread(store.set_subscriber_alerts_muted, chat_id, muted=True)
        await _send_plain(
            token,
            chat_id,
            "🔕 Alerts muted. Job + shift Telegram messages ab nahi aayenge.\n"
            "Wapas chahiye to /unmute bhejo.",
        )
        log.info("Subscriber muted alerts: %s", chat_id)
        return True

    if cmd == "/unmute":
        if _is_bot_owner(chat_id, admin):
            await asyncio.to_thread(store.ensure_owner_vip, chat_id)
            await asyncio.to_thread(store.set_subscriber_alerts_muted, chat_id, muted=False)
            await _send_plain(token, chat_id, "🔔 Alerts on again (VIP owner — lifetime access).")
            return True
        active = await asyncio.to_thread(store.subscriber_has_active_subscription, chat_id)
        if not active:
            await _send_plain(token, chat_id, "Pehle /start karo aur Subscribe dabao.")
            return True
        await asyncio.to_thread(store.set_subscriber_alerts_muted, chat_id, muted=False)
        await _send_plain(
            token,
            chat_id,
            "🔔 Alerts on again. Job + shift messages ab aayenge (filters jaisa pehle).",
        )
        log.info("Subscriber unmuted alerts: %s", chat_id)
        return True

    if cmd == "/setlocation":
        if _is_bot_owner(chat_id, admin):
            await asyncio.to_thread(store.ensure_owner_vip, chat_id)
        active = await asyncio.to_thread(store.subscriber_has_active_subscription, chat_id)
        if not active:
            await _send_plain(
                token,
                chat_id,
                "Pehle /start karo aur Subscribe dabao — phir /setlocation se location choose kar sakte ho.",
            )
            return True
        exclude = setlocation_exclude or []
        db_locs = await asyncio.to_thread(
            store.get_distinct_job_locations,
            24,
            exclude_substrings=exclude,
        )
        if not db_locs:
            await _send_plain(
                token,
                chat_id,
                "Abhi database mein koi job location nahi mili. Thodi der polling chalao "
                "(``python -m app.main once``) — jab jobs SQLite mein aa jayein tab /setlocation dubara try karo.",
            )
            return True
        kb = _inline_setlocation_keyboard_from_strings(db_locs, exclude_substrings=exclude)
        mid = await _send_with_reply_markup(
            token,
            chat_id,
            "📍 Which area do you want alerts for?\n"
            "Choose one place below — or tap All Locations for every area:",
            kb,
        )
        if mid is not None:
            await asyncio.to_thread(store.save_setlocation_keyboard, chat_id, mid, db_locs)
        return True

    if cmd == "/myfilters":
        if _is_bot_owner(chat_id, admin):
            await asyncio.to_thread(store.ensure_owner_vip, chat_id)
        active = await asyncio.to_thread(store.subscriber_has_active_subscription, chat_id)
        if not active:
            await _send_plain(token, chat_id, "Pehle /start karo aur Subscribe dabao.")
            return True
        loc_choice, min_pay, match_sub = await asyncio.to_thread(store.get_subscriber_filter_row, chat_id)
        muted = await asyncio.to_thread(store.subscriber_alerts_muted, chat_id)
        mute_line = "🔕 Alerts: <b>muted</b> — /unmute to turn on" if muted else "🔔 Alerts: <b>on</b> — /mute to pause"
        if match_sub:
            loc_line = f"📍 Area: <b>{html.escape(match_sub)}</b>"
        elif (loc_choice or "all").strip().casefold() in ("", "all"):
            loc_line = "📍 Area: <b>All locations</b>"
        else:
            loc_line = f"📍 Area: <b>{html.escape(loc_choice)}</b>"
        if min_pay is not None:
            pay_line = f"💷 Minimum pay: <b>£{min_pay:.2f}/hr</b>"
        else:
            pay_line = "💷 Minimum pay: not set"
        body = f"{mute_line}\n{loc_line}\n{pay_line}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": body, "parse_mode": "HTML"},
                )
                r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("sendMessage HTML failed: %s", exc)
        return True

    if cmd == "/subscribers":
        if not admin or chat_id != admin:
            return True
        count = await asyncio.to_thread(store.get_subscriber_count)
        await _send_plain(token, chat_id, f"👥 Total subscribers: {count}")
        return True

    if cmd == "/list":
        if not admin or chat_id != admin:
            return True
        lines = await _admin_list_subscriber_lines(token, store)
        chunks = _chunk_lines(lines, max_chars=3800)
        for i, part in enumerate(chunks):
            if i:
                part = f"(part {i + 1}/{len(chunks)})\n{part}"
            await _send_plain(token, chat_id, part)
        return True

    if cmd == "/stats":
        if not admin or chat_id != admin:
            return True
        sub_n = await asyncio.to_thread(store.get_subscriber_count)
        job_n = await asyncio.to_thread(store.count_jobs)
        eng = await asyncio.to_thread(store.get_engagement_stats)
        await _send_plain(
            token,
            chat_id,
            _stats_text(subscriber_count=sub_n, jobs_count=job_n, tz_name=stats_timezone)
            + f"\n\n🟢 Active (24h): {eng.active_24h} · Active (7d): {eng.active_7d} · "
            f"Reacted (7d): {eng.reacted_7d}\n"
            "Full report: /engagement or Admin panel → Active users",
            parse_mode="HTML",
        )
        return True

    if cmd == "/engagement":
        if not admin or chat_id != admin:
            return True
        await _send_admin_engagement_report(
            token,
            store,
            chat_id=chat_id,
            stats_timezone=stats_timezone,
        )
        return True

    if cmd == "/trials":
        if not admin or chat_id != admin:
            return True
        now = datetime.now(UTC)
        rows = await asyncio.to_thread(store.list_trial_only_subscribers)
        meta = await _fetch_tg_meta_for_rows(token, rows)
        lines = _trial_user_lines(rows, meta=meta, tz_name=stats_timezone, now=now)
        kb = _admin_subscriber_list_keyboard([r.chat_id for r in rows], meta)
        await _send_admin_chunked_lists(token, chat_id, lines, reply_markup=kb)
        return True

    if cmd == "/trial_all":
        if not admin or chat_id != admin:
            return True
        hours = float(subscription.trial_duration_hours)
        total_updated, trial_started = await asyncio.to_thread(
            store.grant_free_trial_to_all, trial_hours=hours
        )
        await _send_plain(
            token,
            chat_id,
            "🎁 <b>Admin promo: free access granted</b>\n\n"
            f"Hours: <b>{hours:g}h</b>\n"
            f"Subscribers updated: <b>{total_updated}</b>\n"
            f"Trial started (unpaid, first-time): <b>{trial_started}</b>\n\n"
            "Note: this never reduces a user's existing paid end time; it only extends where needed.",
            parse_mode="HTML",
        )
        return True

    if cmd == "/trial_all_msg":
        if not admin or chat_id != admin:
            return True
        hours = float(subscription.trial_duration_hours)
        total_updated, trial_started = await asyncio.to_thread(
            store.grant_free_trial_to_all, trial_hours=hours
        )
        targets = await asyncio.to_thread(store.get_all_non_banned_subscribers)
        text_msg = "🎁 We're giving everyone a 1-day free trial. Enjoy the alerts!"
        sent = 0
        for sid in targets:
            try:
                await _send_plain(token, sid, text_msg)
                sent += 1
            except Exception:
                log.exception("trial_all_msg send failed (chat_id=%s)", sid)
        await _send_plain(
            token,
            chat_id,
            "🎁 <b>Admin promo: free access + announcement</b>\n\n"
            f"Hours: <b>{hours:g}h</b>\n"
            f"Subscribers updated: <b>{total_updated}</b>\n"
            f"Trial started (unpaid, first-time): <b>{trial_started}</b>\n"
            f"Announcement sent: <b>{sent}</b> / {len(targets)}\n\n"
            f"Message: <code>{html.escape(text_msg)}</code>",
            parse_mode="HTML",
        )
        return True

    if cmd == "/broadcast":
        if not admin or chat_id != admin:
            return True
        parts = text.split(None, 1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        if not payload:
            await _send_plain(token, chat_id, "⚠️ Usage: /broadcast your message here")
            return True
        subs = await asyncio.to_thread(store.get_all_subscribers)
        for sid in subs:
            await _send_plain(token, sid, payload)
        await _send_plain(token, chat_id, f"✅ Broadcast sent to {len(subs)} subscribers.")
        log.info("Broadcast from admin to %d subscriber(s)", len(subs))
        return True

    if cmd == "/broadcast_lapsed":
        if not admin or chat_id != admin:
            return True
        _admin_awaiting_lapsed_broadcast.discard(admin)
        parts = text.split(None, 1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        if not payload:
            _admin_awaiting_lapsed_broadcast.add(admin)
            await _send_with_reply_markup(
                token,
                chat_id,
                "📣 <b>Lapsed-trial promo</b>\n\n"
                "Usage: <code>/broadcast_lapsed your offer text</code>\n"
                "Or send your message in the next reply after tapping <b>Promo → lapsed</b>.",
                {
                "inline_keyboard": [
                    [{"text": "◀️ Users testimonials", "callback_data": "admin:users_testimonials"}],
                ]
            },
                parse_mode="HTML",
            )
            return True
        msg = await _admin_broadcast_lapsed(
            token, store, admin_chat_id=admin, message=payload
        )
        await _send_plain(token, chat_id, msg, parse_mode="HTML")
        log.info("Lapsed-trial promo broadcast from admin (%d chars)", len(payload))
        return True

    if cmd == "/admin":
        if not admin or chat_id != admin:
            return True
        await _send_admin_panel(token, chat_id, store=store)
        return True

    if cmd == "/kick":
        if not admin or chat_id != admin:
            return True
        parts = text.split(None, 1)
        target = parts[1].strip() if len(parts) > 1 else ""
        if not target:
            trial_n, paid_n, lapsed_n = await _users_testimonials_counts(store)
            await _send_with_reply_markup(
                token,
                chat_id,
                "🚫 <b>Kick user</b>\n\nUsage: <code>/kick CHAT_ID</code>\n"
                "Example: <code>/kick 8674859284</code>",
                _admin_panel_keyboard(
                    trial_count=trial_n,
                    paid_count=paid_n,
                    lapsed_count=lapsed_n,
                ),
                parse_mode="HTML",
            )
            return True
        msg = await _admin_kick_user(
            token, store, admin_chat_id=admin or "", target_chat_id=target
        )
        await _send_plain(token, chat_id, msg, parse_mode="HTML")
        log.info("Admin kicked %s", target)
        return True

    if cmd == "/grant" or cmd == "/reference":
        if not admin or chat_id != admin:
            return True
        parts = text.split(None, 1)
        target = parts[1].strip() if len(parts) > 1 else ""
        _admin_awaiting_reference_grant.discard(admin)
        if not target:
            _admin_awaiting_reference_grant.add(admin)
            trial_n, paid_n, lapsed_n = await _users_testimonials_counts(store)
            await _send_with_reply_markup(
                token,
                chat_id,
                "🎁 Send <code>/grant CHAT_ID</code> or type the chat ID in the next message.",
                _admin_panel_keyboard(
                    trial_count=trial_n,
                    paid_count=paid_n,
                    lapsed_count=lapsed_n,
                ),
                parse_mode="HTML",
            )
            return True
        msg = await _admin_grant_reference_access(
            token,
            store,
            admin_chat_id=admin,
            target_chat_id=target,
            subscription=subscription,
            stats_timezone=stats_timezone,
        )
        await _send_plain(token, chat_id, msg, parse_mode="HTML")
        log.info("Admin reference grant to %s", target)
        return True

    if cmd == "/cancel":
        if admin and chat_id == admin:
            cancelled = False
            if admin in _admin_awaiting_reference_grant:
                _admin_awaiting_reference_grant.discard(admin)
                cancelled = True
            if admin in _admin_awaiting_lapsed_broadcast:
                _admin_awaiting_lapsed_broadcast.discard(admin)
                cancelled = True
            if cancelled:
                await _send_plain(token, chat_id, "Cancelled.")
                return True

    return False


async def _try_admin_lapsed_broadcast_from_message(
    token: str,
    store: SqliteStore,
    *,
    admin_chat_id: str,
    text: str,
) -> bool:
    admin = _norm_chat_id(admin_chat_id)
    if not admin or admin not in _admin_awaiting_lapsed_broadcast:
        return False
    if text.strip().startswith("/"):
        return False
    _admin_awaiting_lapsed_broadcast.discard(admin)
    msg = await _admin_broadcast_lapsed(token, store, admin_chat_id=admin, message=text)
    await _send_plain(token, admin, msg, parse_mode="HTML")
    log.info("Lapsed-trial promo broadcast (typed) from admin")
    return True


async def _try_admin_reference_grant_from_message(
    token: str,
    store: SqliteStore,
    *,
    admin_chat_id: str,
    text: str,
    subscription: SubscriptionConfig,
    stats_timezone: str,
) -> bool:
    """If admin is waiting to enter a chat ID for reference grant, process it."""
    admin = _norm_chat_id(admin_chat_id)
    if not admin or admin not in _admin_awaiting_reference_grant:
        return False
    _admin_awaiting_reference_grant.discard(admin)
    target = _parse_target_chat_id(text)
    if not target:
        await _send_plain(
            token,
            admin,
            "⚠️ That doesn't look like a chat ID. Use digits only, or <code>/grant CHAT_ID</code>.",
            parse_mode="HTML",
        )
        return True
    msg = await _admin_grant_reference_access(
        token,
        store,
        admin_chat_id=admin,
        target_chat_id=target,
        subscription=subscription,
        stats_timezone=stats_timezone,
    )
    await _send_plain(token, admin, msg, parse_mode="HTML")
    log.info("Admin reference grant (typed) to %s", target)
    return True


async def _expiry_reminder_loop(
    token: str,
    store: SqliteStore,
    *,
    subscription: SubscriptionConfig,
) -> None:
    interval = max(15, int(subscription.reminder_check_interval_seconds))
    body_1h = subscription.reminder_1h_body.strip()
    body_trial_end = subscription.trial_ending_body.strip()
    while True:
        try:
            due_1h = await asyncio.to_thread(
                store.get_subscribers_needing_expiry_reminder,
                reminder_hours_before=subscription.reminder_hours_before_expiry,
            )
            for chat_id, first_name, _ends in due_1h:
                text = _expiry_reminder_html(
                    first_name=first_name or "there",
                    body=body_1h,
                )
                await _send_plain(token, chat_id, text, parse_mode="HTML")
                await asyncio.to_thread(store.mark_expiry_reminder_sent, chat_id)
                log.info("1h expiry reminder sent to %s", chat_id)

            due_20m = await asyncio.to_thread(
                store.get_subscribers_needing_final_expiry_reminder,
                reminder_minutes_before=subscription.final_reminder_minutes_before_expiry,
            )
            for chat_id, first_name, _ends in due_20m:
                text = _final_expiry_reminder_html(first_name=first_name or "there")
                trial_unpaid = await asyncio.to_thread(store.subscriber_is_trial_unpaid, chat_id)
                if trial_unpaid:
                    await _send_with_reply_markup(
                        token,
                        chat_id,
                        text,
                        _subscribe_paid_keyboard(subscription, chat_id=chat_id),
                        parse_mode="HTML",
                    )
                else:
                    await _send_plain(token, chat_id, text, parse_mode="HTML")
                await asyncio.to_thread(store.mark_final_expiry_reminder_sent, chat_id)
                log.info("20m paid reminder sent to %s", chat_id)

            due_1m = await asyncio.to_thread(
                store.get_subscribers_needing_trial_ending_reminder,
                reminder_minutes_before=subscription.trial_ending_minutes_before_expiry,
            )
            for chat_id, first_name, _ends in due_1m:
                text = _trial_ending_reminder_html(
                    first_name=first_name or "there",
                    body=body_trial_end,
                )
                trial_unpaid = await asyncio.to_thread(store.subscriber_is_trial_unpaid, chat_id)
                if trial_unpaid:
                    await _send_with_reply_markup(
                        token,
                        chat_id,
                        text,
                        _subscribe_paid_keyboard(subscription, chat_id=chat_id),
                        parse_mode="HTML",
                    )
                else:
                    await _send_plain(token, chat_id, text, parse_mode="HTML")
                await asyncio.to_thread(store.mark_trial_ending_reminder_sent, chat_id)
                log.info("1m trial-ending reminder sent to %s", chat_id)

            synced = await asyncio.to_thread(store.sync_lapsed_trial_leads)
            if synced:
                log.info("Synced %d new lapsed-trial lead(s)", synced)
        except Exception:
            log.exception("Expiry reminder loop error")
        await asyncio.sleep(interval)


async def run_subscriber_bot(
    token: str,
    store: SqliteStore,
    admin_chat_id: str | None = None,
    *,
    stats_timezone: str = "Europe/London",
    notify_admin_on_message_reaction: bool = True,
    subscription: SubscriptionConfig | None = None,
    fallback_location_names: list[str] | None = None,
    setlocation_exclude_substrings: list[str] | None = None,
    payment_provider_token: str | None = None,
    stripe_secret_key: str | None = None,
) -> None:
    sub_cfg = subscription or SubscriptionConfig()
    loc_fallback = fallback_location_names or []
    loc_exclude = setlocation_exclude_substrings or []
    pay_token = (payment_provider_token or "").strip() or None
    stripe_sk = (stripe_secret_key or "").strip() or None
    if sub_cfg.uses_stripe_links():
        pay_token = None
        log.info("Payments: Stripe Payment Links (config subscription.payment_mode / stripe_url)")
        if stripe_sk:
            log.info("Stripe API payment verification enabled (STRIPE_SECRET_KEY)")
        else:
            log.warning(
                "STRIPE_SECRET_KEY not set — set it or STRIPE_WEBHOOK_SECRET so paid users activate"
            )
    elif pay_token:
        log.info("Payments: Telegram BotFather provider token")
    else:
        log.warning("Payments: no Stripe URLs and no TELEGRAM_PAYMENT_PROVIDER_TOKEN")
    admin = _norm_chat_id(admin_chat_id)
    offset = 0
    from app.utils.setlocation_menu import merge_setlocation_excludes

    merged_excludes = merge_setlocation_excludes(loc_exclude)
    log.info(
        "Subscriber bot started (admin_chat_id=%s, reactions_to_admin=%s, trial_hours=%s, "
        "setlocation_excludes=%s)",
        admin or "not set",
        notify_admin_on_message_reaction,
        sub_cfg.trial_duration_hours,
        merged_excludes,
    )
    await _delete_webhook_for_long_poll(token)
    if sub_cfg.uses_stripe_links():
        for plan in sub_cfg.paid_plans:
            url = (plan.stripe_url or "").strip()
            if not url or "PASTE_YOUR" in url.upper():
                log.error(
                    "Set subscription.paid_plans[].stripe_url for plan %s in config.yaml "
                    "(Stripe Dashboard → Payment links)",
                    plan.id,
                )
            elif not url.startswith("https://"):
                log.error("stripe_url for plan %s must start with https://", plan.id)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
                data = r.json()
                if data.get("ok"):
                    uname = (data.get("result") or {}).get("username")
                    if uname:
                        for plan in sub_cfg.paid_plans:
                            if (plan.stripe_url or "").strip() and "PASTE_YOUR" not in (
                                plan.stripe_url or ""
                            ).upper():
                                log.info(
                                    "Stripe redirect (plan %s): https://t.me/%s?start=paid_%s",
                                    plan.id,
                                    uname,
                                    plan.id,
                                )
        except httpx.HTTPError as exc:
            log.warning("getMe for Stripe redirect hints failed: %s", exc)
    if admin:
        await asyncio.to_thread(store.ensure_owner_vip, admin)
        log.info("Owner VIP access ensured for admin chat %s", admin)
    reminder_task = asyncio.create_task(
        _expiry_reminder_loop(token, store, subscription=sub_cfg),
        name="expiry_reminder_loop",
    )
    allowed_updates = ["message", "callback_query", "pre_checkout_query"]
    if notify_admin_on_message_reaction:
        allowed_updates.append("message_reaction")
    try:
        while True:
            try:
                async with httpx.AsyncClient(timeout=35.0) as client:
                    r = await client.get(
                        f"https://api.telegram.org/bot{token}/getUpdates",
                        params={"timeout": 30, "offset": offset, "allowed_updates": allowed_updates},
                    )
                    if r.status_code == 409:
                        log.error(
                            "getUpdates returned 409 Conflict: only one client may poll this bot token. "
                            "Stop duplicate `python -m app.main run` or other bots using the same token. Body: %s",
                            r.text,
                        )
                    r.raise_for_status()
                    data = r.json()
                if not data.get("ok", False):
                    log.warning("getUpdates not ok: %s", data)
                    await asyncio.sleep(2)
                    continue
                for update in data.get("result", []):
                    offset = update["update_id"] + 1

                    pcq = update.get("pre_checkout_query")
                    if isinstance(pcq, dict) and pcq:
                        pcq_id = str(pcq.get("id") or "")
                        from_user = pcq.get("from") if isinstance(pcq.get("from"), dict) else {}
                        payer_id = str(from_user.get("id") or "").strip()
                        inv_payload = str(pcq.get("invoice_payload") or "")
                        if pcq_id and inv_payload.startswith("paid_sub_"):
                            plan = _paid_plan_from_payload(inv_payload, sub_cfg)
                            if plan is None:
                                await _answer_pre_checkout_query(
                                    token, pcq_id, ok=False, error="Unknown payment plan."
                                )
                            else:
                                expected = _price_pence_from_gbp(plan.price_gbp)
                                total = int(pcq.get("total_amount") or 0)
                                if total != expected:
                                    await _answer_pre_checkout_query(
                                        token,
                                        pcq_id,
                                        ok=False,
                                        error=f"Amount mismatch. Expected £{plan.price_gbp:.2f}.",
                                    )
                                else:
                                    await _answer_pre_checkout_query(token, pcq_id, ok=True)
                                    log.info(
                                        "Pre-checkout approved for %s plan=%s",
                                        payer_id,
                                        plan.id,
                                    )
                        elif pcq_id:
                            await _answer_pre_checkout_query(
                                token, pcq_id, ok=False, error="Invalid payment request."
                            )
                        continue

                    cq = update.get("callback_query")
                    if cq:
                        cq_user = cq.get("from") if isinstance(cq.get("from"), dict) else {}
                        cq_cid = str(cq_user.get("id") or "").strip()
                        if cq_cid and cq_cid != (admin or ""):
                            await asyncio.to_thread(store.record_subscriber_interaction, cq_cid)
                        await _handle_callback_query(
                            token,
                            store,
                            cq,
                            admin_chat_id=admin,
                            stats_timezone=stats_timezone,
                            subscription=sub_cfg,
                            fallback_location_names=loc_fallback,
                            setlocation_exclude_substrings=loc_exclude,
                            payment_provider_token=pay_token,
                            stripe_secret_key=stripe_sk,
                        )
                        continue

                    if notify_admin_on_message_reaction:
                        mr = update.get("message_reaction")
                        if isinstance(mr, dict) and mr:
                            await _handle_message_reaction(token, store, admin, mr)
                            continue

                    msg = update.get("message", {})
                    text = (msg.get("text") or "").strip()
                    raw_cid = msg.get("chat", {}).get("id")
                    chat_id = str(raw_cid).strip() if raw_cid is not None else ""
                    if not chat_id:
                        continue

                    if admin and chat_id != admin:
                        await asyncio.to_thread(store.record_subscriber_interaction, chat_id)

                    sp = msg.get("successful_payment")
                    if isinstance(sp, dict) and sp:
                        inv_payload = str(sp.get("invoice_payload") or "")
                        if inv_payload.startswith("paid_sub_"):
                            if _is_bot_owner(chat_id, admin):
                                log.info("Ignored payment from owner VIP chat %s", chat_id)
                                continue
                            payer = msg.get("from") if isinstance(msg.get("from"), dict) else {}
                            fn = (payer.get("first_name") or "").strip() or None
                            await asyncio.to_thread(store.add_subscriber, chat_id, first_name=fn)
                            plan = _paid_plan_from_payload(inv_payload, sub_cfg)
                            if plan is None:
                                plan = _legacy_paid_plan(sub_cfg)
                            paid_days = float(plan.days)
                            ends = await asyncio.to_thread(
                                store.activate_paid_subscription,
                                chat_id,
                                paid_days=paid_days,
                                payment=sp,
                            )
                            await notify_paid_subscription_activated(
                                token,
                                chat_id=chat_id,
                                plan=plan,
                                ends_at=ends,
                                stats_timezone=stats_timezone,
                                admin_chat_id=admin,
                                user_first_name=fn,
                            )
                            log.info(
                                "Paid subscription activated for %s plan=%s (%s days) until %s",
                                chat_id,
                                plan.id,
                                paid_days,
                                ends.isoformat(),
                            )
                        continue

                    if not text:
                        continue

                    start_payload = _parse_start_deep_link(text)
                    if start_payload and start_payload.startswith("paid_"):
                        plan_id = start_payload[len("paid_") :].strip().lower()
                        payer = msg.get("from") if isinstance(msg.get("from"), dict) else {}
                        fn = (payer.get("first_name") or "").strip() or None
                        await _activate_paid_from_stripe_return(
                            token=token,
                            store=store,
                            chat_id=chat_id,
                            plan_id=plan_id,
                            subscription=sub_cfg,
                            stats_timezone=stats_timezone,
                            admin=admin,
                            first_name=fn,
                            stripe_secret_key=stripe_sk,
                        )
                        continue

                    if admin and chat_id == admin:
                        handled = await _try_admin_lapsed_broadcast_from_message(
                            token,
                            store,
                            admin_chat_id=admin,
                            text=text,
                        )
                        if handled:
                            continue
                        handled = await _try_admin_reference_grant_from_message(
                            token,
                            store,
                            admin_chat_id=admin,
                            text=text,
                            subscription=sub_cfg,
                            stats_timezone=stats_timezone,
                        )
                        if handled:
                            continue

                    cmd = _command_token(text)
                    log.debug("Subscriber bot: chat_id=%s cmd=%r", chat_id, cmd)
                    if cmd:
                        await _process_subscriber_command(
                            token=token,
                            store=store,
                            chat_id=chat_id,
                            cmd=cmd,
                            text=text,
                            admin=admin,
                            stats_timezone=stats_timezone,
                            subscription=sub_cfg,
                            fallback_location_names=loc_fallback,
                            setlocation_exclude=loc_exclude,
                            message=msg,
                            stripe_secret_key=stripe_sk,
                        )

            except Exception:
                log.exception("Subscriber bot error, retrying in 5s")
                await asyncio.sleep(5)
    finally:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass


async def _handle_help_callback(
    token: str,
    store: SqliteStore,
    *,
    chat_id: str,
    message_id: int,
    data: str,
    callback_query_id: str,
    admin_chat_id: str | None,
    stats_timezone: str,
    subscription: SubscriptionConfig,
    fallback_location_names: list[str],
    setlocation_exclude: list[str] | None = None,
    callback_query: dict[str, Any] | None = None,
) -> bool:
    """Returns True if ``data`` was a help callback and was handled."""
    if not data.startswith("help:"):
        return False

    admin = _norm_chat_id(admin_chat_id)

    run = _help_run_command_from_callback(data)
    if run is not None:
        section, cmd = run
        if section == "admin" and not _is_admin_chat(chat_id, admin_chat_id):
            await _answer_callback_query(
                token,
                callback_query_id,
                "⛔ Admin commands are only for the bot owner.",
                show_alert=True,
            )
            return True
        await _answer_callback_query(token, callback_query_id, f"Running {cmd}…")
        msg = (callback_query or {}).get("message") if callback_query else None
        await _process_subscriber_command(
            token=token,
            store=store,
            chat_id=chat_id,
            cmd=cmd,
            text=cmd,
            admin=admin,
            stats_timezone=stats_timezone,
            subscription=subscription,
            fallback_location_names=fallback_location_names,
            setlocation_exclude=setlocation_exclude,
            message=msg if isinstance(msg, dict) else None,
        )
        return True

    await _answer_callback_query(token, callback_query_id)

    if data == "help:pick:menu":
        if not _is_admin_chat(chat_id, admin_chat_id):
            return True
        trial_n = paid_n = lapsed_n = None
        if store is not None:
            trial_n, paid_n, lapsed_n = await _users_testimonials_counts(store)
        ok = await _edit_message_html(
            token,
            chat_id=chat_id,
            message_id=message_id,
            text="📖 <b>Help</b>\n\nYou are the bot admin. Choose a section or open the admin panel:",
            reply_markup=_help_admin_root_keyboard(
                trial_count=trial_n,
                paid_count=paid_n,
                lapsed_count=lapsed_n,
            ),
        )
        if not ok:
            await _send_interactive_help(token, chat_id, admin_chat_id, store=store)
        return True

    if data == "help:users_testimonials":
        if not _is_admin_chat(chat_id, admin_chat_id):
            return True
        trial_n, paid_n, lapsed_n = await _users_testimonials_counts(store)
        ok = await _edit_message_html(
            token,
            chat_id=chat_id,
            message_id=message_id,
            text=_users_testimonials_menu_text(
                trial_count=trial_n,
                paid_count=paid_n,
                lapsed_count=lapsed_n,
            ),
            reply_markup=_users_testimonials_keyboard(
                trial_count=trial_n,
                paid_count=paid_n,
                lapsed_count=lapsed_n,
                back_callback="help:pick:menu",
            ),
        )
        if not ok:
            await _send_users_testimonials_menu(
                token, chat_id, store, back_callback="help:pick:menu"
            )
        return True

    if data == "help:pick:user":
        show_back = _is_admin_chat(chat_id, admin_chat_id)
        ok = await _edit_message_html(
            token,
            chat_id=chat_id,
            message_id=message_id,
            text=_help_user_intro_text(),
            reply_markup=_help_commands_keyboard("user", show_back=show_back),
        )
        if not ok:
            await _send_with_reply_markup(
                token,
                chat_id,
                _help_user_intro_text(),
                _help_commands_keyboard("user", show_back=show_back),
                parse_mode="HTML",
            )
        return True

    if data == "help:pick:admin":
        if not _is_admin_chat(chat_id, admin_chat_id):
            await _send_plain(token, chat_id, "⛔ Admin help is only for the bot owner.")
            return True
        ok = await _edit_message_html(
            token,
            chat_id=chat_id,
            message_id=message_id,
            text=_help_admin_intro_text(),
            reply_markup=_help_commands_keyboard("admin", show_back=True),
        )
        if not ok:
            await _send_with_reply_markup(
                token,
                chat_id,
                _help_admin_intro_text(),
                _help_commands_keyboard("admin", show_back=True),
                parse_mode="HTML",
            )
        return True

    return True


async def _handle_callback_query(
    token: str,
    store: SqliteStore,
    cq: dict[str, Any],
    *,
    admin_chat_id: str | None = None,
    stats_timezone: str = "Europe/London",
    subscription: SubscriptionConfig | None = None,
    fallback_location_names: list[str] | None = None,
    setlocation_exclude_substrings: list[str] | None = None,
    payment_provider_token: str | None = None,
    stripe_secret_key: str | None = None,
) -> None:
    sub_cfg = subscription or SubscriptionConfig()
    loc_fallback = fallback_location_names or []
    loc_exclude = setlocation_exclude_substrings or []
    pay_token = (payment_provider_token or "").strip() or None
    if sub_cfg.uses_stripe_links():
        pay_token = None
    admin = _norm_chat_id(admin_chat_id)
    qid = str(cq.get("id") or "")
    data = str(cq.get("data") or "")
    msg = cq.get("message") or {}
    raw_chat = msg.get("chat", {}).get("id")
    chat_id = str(raw_chat).strip() if raw_chat is not None else ""
    mid = msg.get("message_id")
    if not qid or not chat_id or mid is None:
        return

    if data.startswith("admin:"):
        await _handle_admin_callback(
            token,
            store,
            chat_id=chat_id,
            data=data,
            callback_query_id=qid,
            admin_chat_id=admin_chat_id,
            stats_timezone=stats_timezone,
            subscription=sub_cfg,
        )
        return

    if data.startswith("help:"):
        await _handle_help_callback(
            token,
            store,
            chat_id=chat_id,
            message_id=int(mid),
            data=data,
            callback_query_id=qid,
            admin_chat_id=admin_chat_id,
            stats_timezone=stats_timezone,
            subscription=sub_cfg,
            fallback_location_names=loc_fallback,
            setlocation_exclude=loc_exclude,
            callback_query=cq,
        )
        return

    if data == "sub:activate":
        user = cq.get("from") if isinstance(cq.get("from"), dict) else {}
        first_name = (user.get("first_name") or "").strip() or None
        if _is_bot_owner(chat_id, admin_chat_id):
            await _answer_callback_query(token, qid, "You are VIP owner — lifetime access.")
            await asyncio.to_thread(store.ensure_owner_vip, chat_id, first_name=first_name)
            await _send_plain(
                token,
                chat_id,
                _owner_vip_message_html(first_name=first_name or "there"),
                parse_mode="HTML",
            )
            return
        if await _send_trial_activation_blocked(
            token=token,
            store=store,
            chat_id=chat_id,
            subscription=sub_cfg,
            stats_timezone=stats_timezone,
            first_name=first_name,
        ):
            await _answer_callback_query(token, qid, "Free trial already used — see payment options.")
            return
        await _answer_callback_query(token, qid, "Activating your free trial…")
        await _activate_subscription_from_callback(
            token=token,
            store=store,
            chat_id=chat_id,
            subscription=sub_cfg,
            stats_timezone=stats_timezone,
            admin=admin,
            first_name=first_name,
        )
        return

    if data == "sub:pay_menu":
        await _answer_callback_query(token, qid, "Opening payment plans…")
        await _send_payment_plans_menu(
            token,
            store,
            chat_id=chat_id,
            subscription=sub_cfg,
            admin_chat_id=admin_chat_id,
        )
        return

    if data.startswith("sub:stripe_done:"):
        plan_id = data[len("sub:stripe_done:") :].strip().lower()
        user = cq.get("from") if isinstance(cq.get("from"), dict) else {}
        first_name = (user.get("first_name") or "").strip() or None
        await _answer_callback_query(token, qid, "Checking your payment…")
        await _activate_paid_from_stripe_return(
            token=token,
            store=store,
            chat_id=chat_id,
            plan_id=plan_id,
            subscription=sub_cfg,
            stats_timezone=stats_timezone,
            admin=admin,
            first_name=first_name,
            stripe_secret_key=stripe_secret_key,
        )
        return

    if data == "sub:pay" or data.startswith("sub:pay:"):
        plan_id = _parse_payment_plan_id_from_callback(data)
        await _handle_paid_plan_callback(
            token,
            store,
            chat_id=chat_id,
            callback_query_id=qid,
            plan_id=plan_id,
            subscription=sub_cfg,
            pay_token=pay_token,
            admin_chat_id=admin_chat_id,
        )
        return

    await _answer_callback_query(token, qid)

    if not data.startswith("setloc:"):
        return

    active = await asyncio.to_thread(store.subscriber_has_active_subscription, chat_id)
    if not active:
        await _send_plain(token, chat_id, "Pehle /start karo aur Subscribe dabao.")
        return

    rest = data.split(":", 1)[1] if ":" in data else ""

    if rest.strip().casefold() == "all":
        await asyncio.to_thread(store.set_subscriber_location_match_substr, chat_id, None)
        body = "✅ Done! Ab tumhe <b>saari locations</b> ke alerts aayenge (jo global filters allow karte hain)."
        ok = await _edit_message_html(token, chat_id=chat_id, message_id=int(mid), text=body)
        if not ok:
            await _send_plain(token, chat_id, "✅ All locations — filter cleared.")
        return

    try:
        idx = int(rest.strip())
    except ValueError:
        await _edit_message_html(
            token,
            chat_id=chat_id,
            message_id=int(mid),
            text="⚠️ Purana button. Dobara <b>/setlocation</b> bhejo.",
        )
        return

    choices = await asyncio.to_thread(store.get_setlocation_keyboard, chat_id, int(mid))
    if choices is None:
        choices = await asyncio.to_thread(
            store.get_distinct_job_locations,
            24,
            exclude_substrings=loc_exclude,
        )
    else:
        from app.utils.setlocation_menu import filter_location_strings

        choices = filter_location_strings(choices, loc_exclude)

    if idx < 0 or not choices or idx >= len(choices):
        await _edit_message_html(
            token,
            chat_id=chat_id,
            message_id=int(mid),
            text="⚠️ Is message ke liye saved list nahi mili. Dobara <b>/setlocation</b> bhejo.",
        )
        return

    full_location = choices[idx]
    await asyncio.to_thread(store.set_subscriber_location_match_substr, chat_id, full_location)

    short = _truncate_button_label(full_location, 48)
    body = f"✅ Done! Ab sirf un jobs par alert jahan location/title mein yeh text aata hai:\n<b>{html.escape(short)}</b>"
    if len(full_location) > 48:
        body += f"\n<code>{html.escape(full_location)}</code>"

    ok = await _edit_message_html(token, chat_id=chat_id, message_id=int(mid), text=body)
    if not ok:
        await _send_plain(token, chat_id, f"✅ Saved location filter ({len(full_location)} chars).")
