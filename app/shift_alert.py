from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
from html import escape as html_escape
import json
import logging
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import (
    FileConfig,
    Settings,
    ShiftAlertConfig,
    ShiftAlertProfile,
    ShiftAlertTurboWindow,
    load_file_config,
)
from app.notifiers import TelegramNotifier
from app.notifiers.telegram import TelegramMessage, TelegramPhoto
from app.shift_analytics import (
    ShiftDropEvent,
    analyze_peak_slots,
    bucket_minute,
    drop_event_from_record,
    format_peak_warning_html,
    predict_peak_warnings,
    resolve_shift_location_key,
)
from app.storage.sqlite import SqliteStore
from app.utils.logging import setup_logging
from app.utils.time import now_utc

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext

CAPTION_LINE_URGENCY = "Slots may fill fast — please open the link when you can."
TELEGRAM_CAPTION_MAX = 1024

_MIN_TEST_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# Direct UK application tracking / shift-selection dashboard (login + run target).
DEFAULT_SHIFT_APPLICATION_DASHBOARD_URL = "https://www.jobsatamazon.co.uk/app#/myApplications"


def _shift_drop_events_from_rows(rows: list[dict[str, Any]]) -> list[ShiftDropEvent]:
    out: list[ShiftDropEvent] = []
    for row in rows:
        raw = row.get("dropped_at_utc")
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        out.append(
            drop_event_from_record(
                location_key=str(row.get("location_key") or "default"),
                profile_id=str(row.get("profile_id") or "default"),
                dropped_at_utc=dt.astimezone(UTC),
                weekday=int(row.get("weekday") or 0),
                hour=int(row.get("hour") or 0),
                minute=int(row.get("minute") or 0),
                confidence=str(row.get("confidence") or "normal"),
            )
        )
    return out


def _record_successful_shift_drop(
    sqlite_path: str,
    *,
    location_key: str,
    profile_id: str,
    dropped_at_utc: datetime,
    tz_name: str,
    dashboard_slots: bool,
    deep_slots: bool,
) -> None:
    local = dropped_at_utc.astimezone(ZoneInfo(tz_name))
    if dashboard_slots and deep_slots:
        confidence = "both"
    elif deep_slots:
        confidence = "deep"
    else:
        confidence = "dashboard"
    store = SqliteStore(sqlite_path)
    try:
        store.record_shift_drop(
            location_key=location_key,
            profile_id=profile_id,
            dropped_at_utc=dropped_at_utc,
            weekday=local.weekday(),
            hour=local.hour,
            minute=bucket_minute(local.minute),
            confidence=confidence,
            timezone=tz_name,
        )
    finally:
        store.close()
    log.info(
        "Shift analytics recorded: location=%s weekday=%s %02d:%02d (%s)",
        location_key,
        local.strftime("%a"),
        local.hour,
        bucket_minute(local.minute),
        confidence,
    )


async def _shift_peak_prediction_loop(
    settings: Settings,
    file_cfg: FileConfig,
) -> None:
    """Admin-only pre-peak warnings (TELEGRAM_CHAT_ID) based on shift_analytics patterns."""
    cfg = file_cfg.shift_alert
    if not cfg.peak_prediction_enabled:
        return
    admin_cid = (settings.telegram_chat_id or "").strip()
    token = (settings.telegram_bot_token or "").strip()
    if not admin_cid or not token:
        log.info("Peak prediction disabled: missing TELEGRAM_CHAT_ID or TELEGRAM_BOT_TOKEN.")
        return
    tz_name = file_cfg.project.timezone
    interval = max(15, int(cfg.peak_prediction_check_interval_seconds))
    log.info(
        "Peak prediction loop started (admin chat %s, check every %ds).",
        admin_cid,
        interval,
    )
    while True:
        await asyncio.sleep(interval)
        if settings.dry_run:
            continue
        try:
            store = SqliteStore(settings.sqlite_path)
            try:
                rows = store.list_shift_drop_events(limit=500)
                events = _shift_drop_events_from_rows(rows)
                if len(events) < cfg.peak_prediction_min_samples:
                    continue
                peaks = analyze_peak_slots(
                    events,
                    min_slot_count=cfg.peak_prediction_min_slot_count,
                )
                warnings = predict_peak_warnings(
                    peaks,
                    now_utc=now_utc(),
                    tz_name=tz_name,
                    lead_minutes_min=cfg.peak_prediction_lead_minutes_min,
                    lead_minutes_max=cfg.peak_prediction_lead_minutes_max,
                    min_slot_count=cfg.peak_prediction_min_slot_count,
                )
                for warning in warnings:
                    if store.peak_warning_already_sent(warning.location_key, warning.slot_key):
                        continue
                    text = format_peak_warning_html(warning, tz_name=tz_name)
                    async with TelegramNotifier(
                        bot_token=token,
                        chat_id=admin_cid,
                        timeout_seconds=settings.http_timeout_seconds,
                    ) as notifier:
                        await notifier.send(
                            TelegramMessage(text=text, parse_mode="HTML", disable_web_page_preview=True)
                        )
                    store.mark_peak_warning_sent(warning.location_key, warning.slot_key)
                    log.info(
                        "Peak pre-alert sent to admin for %s slot %s",
                        warning.location_key,
                        warning.slot_key,
                    )
            finally:
                store.close()
        except Exception:
            log.exception("Peak prediction loop error")


def _load_shift_alert_recipient_chat_ids(sqlite_path: str, fallback_chat_id: str | None) -> list[str]:
    store = SqliteStore(sqlite_path)
    try:
        ids = store.get_subscribers_for_alerts()
    finally:
        store.close()
    if ids:
        return ids
    if fallback_chat_id:
        return [fallback_chat_id.strip()]
    raise ValueError(
        "No Telegram subscribers and TELEGRAM_CHAT_ID unset; use /start while app.main runs, or set TELEGRAM_CHAT_ID"
    )


@dataclass
class ShiftAlertPersistedState:
    last_was_empty: bool = True
    last_panel_fingerprint: str = ""
    empty_state_seen: bool = False
    last_deep_was_empty: bool = True
    deep_empty_seen: bool = False
    last_dashboard_had_empty_copy: bool = True
    last_pre_alert_fingerprint: str = ""
    last_pre_alert_at_iso: str = ""


@dataclass
class ShiftCheckResult:
    state: ShiftAlertPersistedState
    alerted: bool
    outcome: str
    detail: str = ""
    watch: "ShiftWatchHandle | None" = None


_WAF_NAV_COOLDOWN_SECONDS = 600  # after CloudFront block, skip page.goto for 10 minutes
_BROWSER_RECOVERY_SLEEP_SECONDS = 15
_OBSERVER_HEALTH_PING_MS = 15_000
_OBSERVER_STALE_SILENCE_MS = 120_000  # 2 min with zero mutations before stale / soft-reconnect
_STALE_OBSERVER_REASONS = frozenset({"observer_silent", "layout_detached", "stale_health"})


class ShiftBrowserRecoveryNeeded(Exception):
    """Playwright browser/page died; cmd_run should restart Chromium with the same session."""


@dataclass
class ShiftWatchHandle:
    """Persistent browser context + dashboard page reused across poll cycles."""

    profile_id: str
    schedule_url: str
    storage_path: Path
    context: Any
    page: Any
    dashboard_bootstrapped: bool = False
    blocked_cooldown_until: float = 0.0  # time.monotonic() deadline; no network nav until then


def merge_storage_overrides(
    *,
    cli_storage: str | None,
    cli_state: str | None,
    env_storage: str | None,
    env_state: str | None,
) -> tuple[str | None, str | None]:
    storage = cli_storage or env_storage
    state = cli_state or env_state
    return (storage, state)


def resolve_shift_paths(
    cfg: ShiftAlertConfig,
    *,
    storage_override: str | Path | None = None,
    state_override: str | Path | None = None,
) -> tuple[Path, Path]:
    storage = Path(storage_override) if storage_override else Path(cfg.storage_state_path)
    if state_override:
        state = Path(state_override)
    elif storage_override:
        state = storage.parent / f"{storage.stem}.shift_state.json"
    else:
        state = Path(cfg.state_path)
    return storage, state


@dataclass(frozen=True)
class ResolvedWatchProfile:
    id: str
    display_name: str
    schedule_url: str
    storage_path: Path
    state_path: Path


def merge_profile_storage_for_login(
    cfg: ShiftAlertConfig,
    profile_id: str | None,
    storage_override: str | Path | None,
    state_override: str | Path | None,
) -> tuple[str | Path | None, str | Path | None]:
    if not profile_id or storage_override:
        return storage_override, state_override
    for p in cfg.profiles:
        if p.id == profile_id:
            return p.storage_state_path, p.state_path
    raise ValueError(f"Unknown shift_alert.profiles id: {profile_id!r}")


def is_application_tracking_url(url: str) -> bool:
    u = (url or "").lower()
    return "/application/uk" in u or "job-opportunities" in u




def _application_id_from_url(url: str) -> str | None:
    m = re.search(r"applicationid=([a-f0-9-]+)", url or "", re.I)
    return m.group(1).lower() if m else None


def _job_id_from_url(url: str) -> str | None:
    m = re.search(r"jobid=([^&#]+)", url or "", re.I)
    return m.group(1).strip().lower() if m else None


def resolve_shift_watch_url(cfg: ShiftAlertConfig, profile_id: str | None) -> str:
    """URL used for ``run`` bootstrap and post-login cookie scope."""
    if profile_id:
        for p in cfg.profiles:
            if p.id == profile_id and (p.schedule_url or "").strip():
                return p.schedule_url.strip()
    explicit = (cfg.schedule_url or "").strip()
    if explicit:
        return explicit
    start = (cfg.login_start_url or "").strip()
    if start:
        return start
    return DEFAULT_SHIFT_APPLICATION_DASHBOARD_URL


def resolve_login_schedule_url(cfg: ShiftAlertConfig, profile_id: str | None) -> str:
    return resolve_shift_watch_url(cfg, profile_id)


def resolve_login_entry_url(cfg: ShiftAlertConfig, profile_id: str | None) -> str:
    """First URL for ``login`` — direct application dashboard (not generic /login)."""
    start = (cfg.login_start_url or "").strip()
    if start:
        return start
    return resolve_shift_watch_url(cfg, profile_id)


def resolve_watch_profiles(
    cfg: ShiftAlertConfig,
    *,
    storage_override: str | None,
    state_override: str | None,
    profile_id_filter: str | None,
) -> list[ResolvedWatchProfile]:
    if storage_override:
        watch_url = resolve_shift_watch_url(cfg, profile_id_filter)
        st, st_path = resolve_shift_paths(cfg, storage_override=storage_override, state_override=state_override)
        return [
            ResolvedWatchProfile(
                id="cli",
                display_name="CLI / env session",
                schedule_url=watch_url,
                storage_path=st,
                state_path=st_path,
            )
        ]

    active: list[ShiftAlertProfile] = [p for p in cfg.profiles if p.enabled]
    if profile_id_filter:
        active = [p for p in active if p.id == profile_id_filter]
        if not active:
            log.error("No enabled profile with id=%r", profile_id_filter)

    if active:
        out: list[ResolvedWatchProfile] = []
        for p in active:
            if not p.schedule_url.strip():
                log.warning("shift_alert.profiles id=%s: empty schedule_url; skipped", p.id)
                continue
            stor = Path(p.storage_state_path)
            stp = Path(p.state_path) if p.state_path else stor.parent / f"{stor.stem}.shift_state.json"
            name = (p.display_name or "").strip() or p.id
            out.append(
                ResolvedWatchProfile(
                    id=p.id,
                    display_name=name,
                    schedule_url=p.schedule_url.strip(),
                    storage_path=stor,
                    state_path=stp,
                )
            )
        return out

    watch_url = resolve_shift_watch_url(cfg, None)
    st, st_path = resolve_shift_paths(cfg, storage_override=None, state_override=state_override)
    return [
        ResolvedWatchProfile(
            id="default",
            display_name="Default",
            schedule_url=watch_url,
            storage_path=st,
            state_path=st_path,
        )
    ]


def load_shift_state(path: Path) -> ShiftAlertPersistedState:
    if not path.exists():
        return ShiftAlertPersistedState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "empty_state_seen" in raw:
            empty_seen = bool(raw.get("empty_state_seen"))
        else:
            empty_seen = True
        deep_seen = raw.get("deep_empty_seen")
        if deep_seen is None:
            deep_seen = bool(raw.get("empty_state_seen", empty_seen))
        ldhec_raw = raw.get("last_dashboard_had_empty_copy")
        if ldhec_raw is None:
            lw = bool(raw.get("last_was_empty", True))
            if (
                not lw
                and empty_seen
                and str(raw.get("last_panel_fingerprint", "") or "").strip()
            ):
                ldhec = True
            else:
                ldhec = lw
        else:
            ldhec = bool(ldhec_raw)
        state = ShiftAlertPersistedState(
            last_was_empty=bool(raw.get("last_was_empty", True)),
            last_panel_fingerprint=str(raw.get("last_panel_fingerprint", "") or ""),
            empty_state_seen=empty_seen,
            last_deep_was_empty=bool(raw.get("last_deep_was_empty", True)),
            deep_empty_seen=bool(deep_seen),
            last_dashboard_had_empty_copy=bool(ldhec),
            last_pre_alert_fingerprint=str(raw.get("last_pre_alert_fingerprint", "") or ""),
            last_pre_alert_at_iso=str(raw.get("last_pre_alert_at_iso", "") or ""),
        )
        if state.empty_state_seen and not state.deep_empty_seen:
            state = ShiftAlertPersistedState(
                last_was_empty=state.last_was_empty,
                last_panel_fingerprint=state.last_panel_fingerprint,
                empty_state_seen=state.empty_state_seen,
                last_deep_was_empty=state.last_deep_was_empty,
                deep_empty_seen=True,
                last_dashboard_had_empty_copy=state.last_dashboard_had_empty_copy,
                last_pre_alert_fingerprint=state.last_pre_alert_fingerprint,
                last_pre_alert_at_iso=state.last_pre_alert_at_iso,
            )
        return state
    except Exception:
        log.warning("Could not read shift state file %s; using defaults", path)
        return ShiftAlertPersistedState()


def save_shift_state(path: Path, state: ShiftAlertPersistedState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_was_empty": state.last_was_empty,
        "last_panel_fingerprint": state.last_panel_fingerprint,
        "empty_state_seen": state.empty_state_seen,
        "last_deep_was_empty": state.last_deep_was_empty,
        "deep_empty_seen": state.deep_empty_seen,
        "last_dashboard_had_empty_copy": state.last_dashboard_had_empty_copy,
        "last_pre_alert_fingerprint": state.last_pre_alert_fingerprint,
        "last_pre_alert_at_iso": state.last_pre_alert_at_iso,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def format_alert_time_uk(now: datetime, tz_name: str) -> str:
    local = now.astimezone(ZoneInfo(tz_name))
    return local.strftime("%d %b %Y, %H:%M %Z")


def _semantic_body_fingerprint(html_lower: str) -> str:
    s = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", html_lower, flags=re.I)
    s = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()[:24000]
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:28]


def shift_alert_telegram_burst_count(cfg: ShiftAlertConfig, *, high_confidence: bool) -> int:
    if high_confidence:
        return max(1, int(cfg.high_confidence_alert_repeat_count))
    return 1


async def _send_shift_availability_telegram(
    *,
    token: str,
    chat_ids: list[str],
    screenshot_bytes: bytes,
    caption: str,
    timeout_seconds: float,
    repeat_count: int = 1,
    repeat_delay_ms: int = 0,
) -> int:
    if not chat_ids:
        return 0
    bursts = max(1, repeat_count)
    delay_s = max(0.0, repeat_delay_ms) / 1000.0
    photo = TelegramPhoto(
        photo_bytes=screenshot_bytes,
        filename="amazon_shifts.png",
        caption=caption,
        parse_mode=None,
    )
    sent_ok = 0
    for cid in chat_ids:
        try:
            async with TelegramNotifier(
                bot_token=token, chat_id=cid, timeout_seconds=timeout_seconds
            ) as t:
                for i in range(bursts):
                    if i > 0 and delay_s > 0:
                        await asyncio.sleep(delay_s)
                    await t.send_photo(photo)
            sent_ok += 1
        except Exception:
            log.exception("Shift alert Telegram send failed for chat_id=%s", cid)
    return sent_ok


def build_shift_alert_caption(
    cfg: ShiftAlertConfig,
    schedule_url: str,
    tz_name: str,
    now: datetime,
    *,
    watch_label: str | None = None,
) -> str:
    loc = (cfg.alert_location or "").strip() or (watch_label or "").strip()

    stable_url = schedule_url
    job_id = None

    try:
        if "jobId=" in schedule_url:
            job_id = schedule_url.split("jobId=")[1].split("&")[0].strip()
            if job_id:
                stable_url = f"https://www.jobsatamazon.co.uk/jobDetail/en-GB/{job_id}"
    except Exception:
        stable_url = schedule_url

    lines = [cfg.alert_title]

    if loc:
        lines.extend(["", f"Location / site: {loc}"])

    lines.extend(
        [
            "",
            "You can now pick a work shift. Open the link below on your phone or computer.",
            "",
            CAPTION_LINE_URGENCY,
            "",
            "Tap here to select your shift:",
            stable_url,
            "",
            f"Time (UK): {format_alert_time_uk(now, tz_name)}",
            "",
            "If the page asks you to sign in, use your Amazon jobs login.",
            "",
            "If the link does not open in Telegram, use the menu → Open in browser, or copy the link above.",
        ]
    )

    text = "\n".join(lines)
    if len(text) > TELEGRAM_CAPTION_MAX:
        return text[: TELEGRAM_CAPTION_MAX - 3] + "..."
    return text


def _shift_page_corpus(html: str) -> str:
    s = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", html, flags=re.I)
    s = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&(nbsp|#160);?", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip().lower()


def page_looks_empty(html_lower: str, substrings: list[str]) -> bool:
    if not substrings:
        return False
    return any(s.lower() in html_lower for s in substrings)


_STRONG_WAF_MARKERS: tuple[str, ...] = (
    "generated by cloudfront",
    "request could not be satisfied",
    "the request could not be satisfied",
)
_WEAK_WAF_MARKERS: frozenset[str] = frozenset(
    {"403 error", "403 forbidden", "access denied", "error 403"}
)


def blocked_page_match_reason(html_lower: str, substrings: list[str]) -> str | None:
    if not substrings:
        return None
    has_strong = any(m in html_lower for m in _STRONG_WAF_MARKERS)
    short_error_shell = len(html_lower) < 4_000 and bool(re.search(r"\b403\b", html_lower))
    for pat in substrings:
        p = (pat or "").strip()
        if not p:
            continue
        pl = p.lower()
        if pl not in html_lower:
            continue
        if pl in _WEAK_WAF_MARKERS:
            if has_strong or short_error_shell:
                return p
            continue
        return p
    return None


def _blocked_match_context_snippet(corpus: str, phrase: str, *, radius: int = 60) -> str:
    idx = corpus.lower().find(phrase.lower())
    if idx < 0:
        return ""
    start = max(0, idx - radius)
    end = min(len(corpus), idx + len(phrase) + radius)
    return corpus[start:end].replace("\n", " ")


def page_looks_blocked(html_lower: str, substrings: list[str]) -> bool:
    return blocked_page_match_reason(html_lower, substrings) is not None


async def _log_and_save_blocked_page_debug(
    page: Any,
    *,
    corpus: str,
    hit: str,
    schedule_url: str,
    debug_path: Path | None,
) -> None:
    title = ""
    try:
        title = await page.title()
    except Exception:
        pass
    page_url = ""
    try:
        page_url = page.url or ""
    except Exception:
        pass
    snippet = _blocked_match_context_snippet(corpus, hit)
    looks_like_spa = any(
        m in corpus for m in ("my jobs", "select shift", "no shifts available", "application")
    )
    log.warning(
        "Blocked check: url=%s | title=%r | corpus_len=%d | matched=%r | context=%r | spa_markers=%s",
        page_url[:200] or schedule_url[:200],
        title[:120],
        len(corpus),
        hit,
        snippet[:200],
        looks_like_spa,
    )
    if looks_like_spa and hit.lower() in _WEAK_WAF_MARKERS:
        log.warning(
            "This may be a FALSE POSITIVE (My jobs text found but weak phrase %r matched). "
            "Remove %r from shift_alert.blocked_page_substrings in config.yaml.",
            hit,
            hit,
        )
    if debug_path is None:
        return
    try:
        html = await _page_content_retry(page, attempts=4, gap_ms=200)
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(html, encoding="utf-8")
        log.info("Saved blocked-page HTML for inspection: %s", debug_path.resolve())
    except Exception as e:
        log.debug("Could not save blocked-page debug HTML: %s", e)


def page_looks_ready(html_lower: str, substrings: list[str]) -> bool:
    if not substrings:
        return True
    return any(s.lower() in html_lower for s in substrings)


_SELECT_SHIFT_BUTTON_RE = re.compile(r"^\s*select shift\s*$", re.I)
_SELECT_SHIFT_LOOSE_RE = re.compile(r"select\s+shift", re.I)

_SELECT_SHIFT_JS = """
() => {
  const want = ['select shift', 'select your shift'];
  for (const sel of ['button', 'a', '[role="button"]', '[role="link"]']) {
    for (const n of document.querySelectorAll(sel)) {
      const t = (n.innerText || n.textContent || '').trim().toLowerCase();
      if (!t) continue;
      if (want.some(w => t === w || t.includes(w))) {
        try { n.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (_) {}
        n.click();
        return true;
      }
    }
  }
  return false;
}
"""


def is_direct_schedule_picker_url(url: str) -> bool:
    u = (url or "").lower()
    if not u:
        return False
    if is_application_tracking_url(u):
        return True
    if "available-schedule" in u:
        return True
    if "selfservice" in u and "schedule" in u:
        return True
    if "jobsatamazon" in u and "/schedule/" in u:
        return True
    return False


def should_pre_alert_on_fingerprint(cfg: ShiftAlertConfig) -> bool:
    return bool(cfg.pre_alert_on_fingerprint_change or cfg.alert_on_fingerprint_change)


def resolve_active_turbo_window(
    now: datetime, tz_name: str, cfg: ShiftAlertConfig
) -> ShiftAlertTurboWindow | None:
    if not cfg.turbo_enabled or not cfg.turbo_windows:
        return None
    local = now.astimezone(ZoneInfo(tz_name))
    for w in cfg.turbo_windows:
        if local.weekday() == w.weekday and w.hour_start <= local.hour < w.hour_end:
            return w
    return None


def effective_poll_intervals(
    now: datetime,
    tz_name: str,
    cfg: ShiftAlertConfig,
    base_min: int,
    base_max: int,
) -> tuple[int, int, bool]:
    turbo = resolve_active_turbo_window(now, tz_name, cfg)
    if turbo is not None:
        return turbo.poll_interval_min_seconds, turbo.poll_interval_max_seconds, True
    return base_min, base_max, False


def select_shift_click_budget(cfg: ShiftAlertConfig, *, turbo_active: bool) -> tuple[int, bool]:
    if cfg.aggressive_select_shift:
        return cfg.select_shift_aggressive_attempts, True
    if turbo_active and cfg.select_shift_aggressive_in_turbo:
        return cfg.select_shift_aggressive_attempts, True
    return cfg.select_shift_click_attempts, False


async def _try_click_select_shift_once(page: Any, *, aggressive: bool) -> bool:
    order = [page.main_frame, *[f for f in page.frames if f is not page.main_frame]]
    patterns = (_SELECT_SHIFT_BUTTON_RE, _SELECT_SHIFT_LOOSE_RE) if aggressive else (_SELECT_SHIFT_BUTTON_RE,)
    click_timeout = 4_500 if aggressive else 12_000
    for frame in order:
        for role in ("button", "link"):
            for pattern in patterns:
                try:
                    loc = frame.get_by_role(role, name=pattern)
                    if await loc.count() == 0:
                        continue
                    el = loc.first
                    try:
                        if not await el.is_visible():
                            continue
                    except Exception:
                        continue
                    try:
                        await el.scroll_into_view_if_needed(timeout=2_500)
                    except Exception:
                        pass
                    await el.click(timeout=click_timeout, force=aggressive)
                    await page.wait_for_timeout(180 if aggressive else 600)
                    return True
                except Exception:
                    continue
    for frame in order:
        try:
            if await frame.evaluate(_SELECT_SHIFT_JS):
                await page.wait_for_timeout(220 if aggressive else 600)
                return True
        except Exception:
            continue
    return False


async def _try_click_select_shift(page: Any, *, max_attempts: int = 1, aggressive: bool = False) -> bool:
    gap_ms = 140 if aggressive else 350
    for attempt in range(max(1, max_attempts)):
        if await _try_click_select_shift_once(page, aggressive=aggressive):
            if attempt > 0:
                log.info("Select Shift clicked on attempt %d/%d.", attempt + 1, max_attempts)
            return True
        if attempt + 1 < max_attempts:
            await page.wait_for_timeout(gap_ms)
    return False


async def _wait_for_corpus_markers(
    page: Any,
    markers: list[str],
    *,
    timeout_ms: int,
    poll_ms: int = 450,
) -> bool:
    if not markers:
        return True
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    while time.monotonic() <= deadline:
        try:
            html = await _page_content_retry(page, attempts=8, gap_ms=350)
            corpus = _shift_page_corpus(html)
            if page_looks_ready(corpus, markers):
                return True
        except Exception:
            pass
        await page.wait_for_timeout(poll_ms)
    return False


def _deep_screen_signals(
    cfg: ShiftAlertConfig,
    deep_corpus: str,
    *,
    deep_evaluated: bool,
) -> tuple[bool, bool, bool, bool]:
    if not deep_evaluated or not deep_corpus:
        return (False, True, False, True)
    blocked = page_looks_blocked(deep_corpus, cfg.blocked_page_substrings)
    no_sched = bool(cfg.deep_no_schedule_substrings) and page_looks_empty(
        deep_corpus, cfg.deep_no_schedule_substrings
    )
    positive = bool(cfg.deep_shift_available_substrings) and page_looks_ready(
        deep_corpus, cfg.deep_shift_available_substrings
    )
    if blocked:
        quiet = True
    elif no_sched:
        quiet = True
    elif positive:
        quiet = False
    else:
        quiet = True
    return (blocked, no_sched, positive, quiet)


def _login_keywords_in_url(url: str) -> bool:
    u = url.lower()
    return "login" in u or "sign-in" in u or "signin" in u


def _print_shift_alert_session_login_hint() -> None:
    print("\nPlease log in again (new session):  python -m app.shift_alert login\n", flush=True)


def in_active_poll_window(now: datetime, tz_name: str, cfg: ShiftAlertConfig) -> bool:
    local = now.astimezone(ZoneInfo(tz_name))
    if cfg.active_weekdays_only and local.weekday() >= 5:
        return False
    h = local.hour
    return cfg.active_hour_start <= h < cfg.active_hour_end


def _session_meta_path(storage: Path) -> Path:
    return storage.parent / f"{storage.stem}.session_meta.json"


def read_session_saved_at(storage: Path) -> datetime | None:
    meta_path = _session_meta_path(storage)
    if meta_path.exists():
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            s = str(raw.get("saved_at") or "").strip()
            if s:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            pass
    if storage.exists():
        try:
            return datetime.fromtimestamp(storage.stat().st_mtime, tz=UTC)
        except Exception:
            pass
    return None


def write_session_saved_at(storage: Path, when: datetime | None = None) -> None:
    when = when or now_utc()
    meta_path = _session_meta_path(storage)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps({"saved_at": when.astimezone(UTC).isoformat()}, indent=2) + "\n",
        encoding="utf-8",
    )


def session_age_hours(storage: Path) -> float | None:
    saved = read_session_saved_at(storage)
    if saved is None:
        return None
    if saved.tzinfo is None:
        saved = saved.replace(tzinfo=UTC)
    return max(0.0, (now_utc() - saved.astimezone(UTC)).total_seconds() / 3600.0)


async def page_goto_with_retries(
    page,
    url: str,
    *,
    timeout_ms: int,
    attempts: int = 4,
) -> None:
    last_err: BaseException | None = None
    wait_modes = ("domcontentloaded", "load", "commit")
    for i in range(attempts):
        wait_until = wait_modes[min(i, len(wait_modes) - 1)]
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            return
        except Exception as e:
            last_err = e
            es = str(e).lower()
            if "err_aborted" in es or "net::err" in es:
                log.warning("goto retry %s/%s (%s): %s", i + 1, attempts, wait_until, e)
                await asyncio.sleep(1.0 + i * 0.75)
                continue
            raise
    assert last_err is not None
    raise last_err


_DEFAULT_CHROMIUM_LAUNCH_ARGS: tuple[str, ...] = ("--disable-blink-features=AutomationControlled",)

_SHIFT_ALERT_INIT_SCRIPT = """
(() => {
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get() { return undefined; },
      configurable: true,
    });
  } catch (_) {}
})();
"""

_LOGIN_SPA_STABILITY_INIT_SCRIPT = """
(() => {
  try {
    const KEY = 'shift_alert_post_login';
    const defaultApp = 'https://www.jobsatamazon.co.uk/application/uk/?CS=true&jobId=JOB-UK-0000000449&locale=en-GB&ssoEnabled=1#/job-opportunities?CS=true&jobId=JOB-UK-0000000449&locale=en-GB&ssoEnabled=1&applicationId=be3fcd64-e83d-4edc-840e-fd69ee2219bb';
    const appDest = () => sessionStorage.getItem(KEY) || defaultApp;
    let authStarted = false;
    let recoveries = 0;

    const isHiringAuth = () => /auth\\.hiring|amazoncognito|\\/ap\\/cvf|\\/cvf\\//i.test(location.href);
    const isLoginShell = () => {
      const h = (location.hostname || '').toLowerCase();
      if (!h.includes('jobsatamazon.co.uk')) return false;
      const p = (location.pathname || '') + (location.hash || '') + (location.search || '');
      if (/\\/login|#\\/login/i.test(p)) return true;
      const base = (location.pathname || '').replace(/\\/$/, '');
      if (!base || base === '/' || base.endsWith('jobsatamazon.co.uk') || base.endsWith('www.jobsatamazon.co.uk')) {
        return !/myapplications|job-opportunities|application\\/uk|jobdetail|selfservice/i.test(p);
      }
      return false;
    };

    const recoverToApp = () => {
      if (recoveries > 15) return;
      recoveries += 1;
      const dest = appDest();
      try {
        if (location.href !== dest) location.replace(dest);
      } catch (_) {}
    };

    const capReload = (orig) => {
      const hits = [];
      return function cappedReload() {
        const now = Date.now();
        hits.push(now);
        while (hits.length && now - hits[0] > 15000) hits.shift();
        if (authStarted && hits.length > 3) {
          if (isLoginShell()) recoverToApp();
          return;
        }
        if (hits.length > 10) return;
        return orig.apply(this, arguments);
      };
    };

    try {
      window.location.reload = capReload(window.location.reload.bind(window.location));
    } catch (_) {}

    const wrapNav = (name) => {
      try {
        const orig = location[name].bind(location);
        location[name] = function(url) {
          const s = String(url || '');
          if (authStarted && /login/i.test(s) && /jobsatamazon/i.test(s)) {
            recoverToApp();
            return;
          }
          return orig(s);
        };
      } catch (_) {}
    };
    wrapNav('assign');
    wrapNav('replace');

    setInterval(() => {
      if (isHiringAuth()) authStarted = true;
      if (authStarted && !isHiringAuth() && isLoginShell()) recoverToApp();
    }, 700);
  } catch (_) {}
})();
"""

_LOGIN_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def chromium_launch_options(cfg: ShiftAlertConfig, *, headless: bool | None = None) -> dict[str, object]:
    opts: dict[str, object] = {}
    opts["headless"] = cfg.playwright_headless if headless is None else headless
    if cfg.playwright_executable_path:
        opts["executable_path"] = cfg.playwright_executable_path
    elif cfg.playwright_browser_channel:
        opts["channel"] = cfg.playwright_browser_channel
    merged_args = list(_DEFAULT_CHROMIUM_LAUNCH_ARGS)
    merged_args.extend(cfg.playwright_launch_args or [])
    opts["args"] = merged_args
    opts["ignore_default_args"] = ["--enable-automation"]
    return opts


def _launch_opts_fingerprint(opts: dict[str, object]) -> tuple[object, ...]:
    args = tuple(str(a) for a in (opts.get("args") or []))
    ignore = tuple(str(a) for a in (opts.get("ignore_default_args") or []))
    return (
        bool(opts.get("headless")),
        opts.get("channel"),
        opts.get("executable_path"),
        args,
        ignore,
    )


def chromium_launch_attempt_variants(cfg: ShiftAlertConfig) -> list[tuple[str, dict[str, object]]]:
    primary = chromium_launch_options(cfg)
    variants: list[tuple[str, dict[str, object]]] = [("configured", primary)]
    seen = {_launch_opts_fingerprint(primary)}

    def add(label: str, opts: dict[str, object]) -> None:
        fp = _launch_opts_fingerprint(opts)
        if fp in seen:
            return
        seen.add(fp)
        variants.append((label, opts))

    has_channel = bool(primary.get("channel"))
    has_exe = bool(primary.get("executable_path"))
    is_headless = bool(primary.get("headless"))

    if is_headless and has_channel and not has_exe:
        opts = dict(primary)
        opts["headless"] = False
        args = list(opts.get("args") or [])
        if not any("headless" in str(a) for a in args):
            args.append("--headless=new")
        opts["args"] = args
        add("chrome channel (--headless=new)", opts)

    if is_headless:
        add("chrome channel (visible window)", chromium_launch_options(cfg, headless=False))

    if has_channel or has_exe:
        bundled = chromium_launch_options(cfg)
        bundled.pop("channel", None)
        bundled.pop("executable_path", None)
        add("Playwright bundled Chromium", bundled)
        if is_headless:
            bundled_headed = chromium_launch_options(cfg, headless=False)
            bundled_headed.pop("channel", None)
            bundled_headed.pop("executable_path", None)
            add("Playwright bundled Chromium (visible window)", bundled_headed)

    return variants


_LAUNCH_FAILURE_HINT = (
    "Chromium could not start for shift polling.\n"
    "• Close other Chrome windows using automation, then retry.\n"
    "• Run: python -m playwright install chrome\n"
    "• In config.yaml try shift_alert.playwright_headless: false (keeps a Chrome window open while polling).\n"
    "• Or clear shift_alert.playwright_browser_channel to use Playwright's bundled Chromium.\n"
    "• Antivirus / corporate policy can kill headless Chrome instantly — check Windows Security logs."
)


async def launch_shift_chromium(browser_type: Any, cfg: ShiftAlertConfig) -> "Browser":
    last_err: BaseException | None = None
    variants = chromium_launch_attempt_variants(cfg)
    for idx, (label, opts) in enumerate(variants):
        try:
            if idx > 0:
                log.warning("Retrying browser launch: %s", label)
            browser = await browser_type.launch(**opts)
            if idx > 0:
                log.warning(
                    "Browser started with fallback %r (primary launch failed). "
                    "To skip retries, adjust shift_alert.playwright_headless / playwright_browser_channel in config.yaml.",
                    label,
                )
            return browser
        except Exception as e:
            last_err = e
            log.warning("Chromium launch failed (%s): %s: %s", label, type(e).__name__, e)
    assert last_err is not None
    raise last_err


_LOGIN_FOCUS_STABILITY_ARGS: tuple[str, ...] = (
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)


def chromium_launch_options_for_login(cfg: ShiftAlertConfig) -> dict[str, object]:
    opts: dict[str, object] = {}
    opts["headless"] = False
    if cfg.playwright_executable_path:
        opts["executable_path"] = cfg.playwright_executable_path
    elif cfg.playwright_browser_channel:
        opts["channel"] = cfg.playwright_browser_channel
    elif sys.platform in ("win32", "darwin"):
        opts["channel"] = "chrome"
    merged = list(_DEFAULT_CHROMIUM_LAUNCH_ARGS)
    extra = list(cfg.playwright_launch_args or [])
    if not any("--start-maximized" in str(a) for a in merged + extra) and not any(
        str(a).startswith("--window-size") for a in merged + extra
    ):
        merged.append("--start-maximized")
    merged.extend(extra)
    for flag in _LOGIN_FOCUS_STABILITY_ARGS:
        if not any(str(a) == flag for a in merged):
            merged.append(flag)
    opts["args"] = merged
    return opts


def _login_persistent_context_kwargs(cfg: ShiftAlertConfig, *, timezone_name: str) -> dict[str, Any]:
    base = chromium_launch_options_for_login(cfg)
    ua = (cfg.login_user_agent or "").strip() or _LOGIN_DEFAULT_USER_AGENT
    tz = (timezone_name or "Europe/London").strip() or "Europe/London"
    kw: dict[str, Any] = {
        "headless": bool(base["headless"]),
        "args": list(base["args"]),
        "locale": "en-GB",
        "timezone_id": tz,
        "user_agent": ua,
        "color_scheme": "light",
        "no_viewport": True,
    }
    if base.get("executable_path"):
        kw["executable_path"] = str(base["executable_path"])
    if base.get("channel"):
        kw["channel"] = str(base["channel"])
    return kw


async def new_shift_browser_context(
    browser: "Browser",
    *,
    cfg: ShiftAlertConfig,
    timezone_name: str,
    storage_state: str | Path | None = None,
    apply_stealth_init: bool = True,
) -> "BrowserContext":
    tz = (timezone_name or "Europe/London").strip() or "Europe/London"
    ctx_kw: dict[str, Any] = {
        "locale": "en-GB",
        "timezone_id": tz,
        "viewport": {"width": 1365, "height": 900},
        "color_scheme": "light",
    }
    if storage_state:
        ctx_kw["storage_state"] = str(storage_state)
    context = await browser.new_context(**ctx_kw)
    if apply_stealth_init:
        await context.add_init_script(_SHIFT_ALERT_INIT_SCRIPT)
    return context


async def save_storage_state_from_context(context, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(path))
    write_session_saved_at(path)


async def refresh_playwright_session_if_stale(
    browser: Any,
    *,
    cfg: ShiftAlertConfig,
    file_cfg: FileConfig,
    storage_path: Path,
    schedule_url: str,
) -> bool:
    if cfg.session_refresh_hours <= 0 or not storage_path.exists():
        return True
    age_h = session_age_hours(storage_path)
    if age_h is None or age_h < float(cfg.session_refresh_hours):
        return True
    log.info(
        "Session file is %.1fh old (refresh after %.1fh) — refreshing cookies via %s",
        age_h,
        cfg.session_refresh_hours,
        schedule_url[:80],
    )
    context = None
    try:
        context = await new_shift_browser_context(
            browser,
            cfg=cfg,
            timezone_name=file_cfg.project.timezone,
            storage_state=storage_path,
            apply_stealth_init=False,
        )
        page = await context.new_page()
        page.set_default_timeout(cfg.navigation_timeout_ms)
        await page_goto_with_retries(page, schedule_url, timeout_ms=cfg.navigation_timeout_ms)
        await page.wait_for_timeout(1_200)
        if _login_keywords_in_url(page.url):
            log.warning(
                "Session refresh hit login page (%s). Run: python -m app.shift_alert login",
                page.url[:120],
            )
            _print_shift_alert_session_login_hint()
            return False
        await save_storage_state_from_context(context, storage_path)
        log.info("Session refreshed and saved to %s", storage_path)
        return True
    except Exception as e:
        log.warning("Session refresh failed (%s): %s", type(e).__name__, e)
        return False
    finally:
        if context is not None:
            await context.close()


_DEFAULT_POST_AUTH_JOBS_APP_URL = DEFAULT_SHIFT_APPLICATION_DASHBOARD_URL


def _saw_hiring_auth_flow_url(url: str) -> bool:
    u = url.lower()
    if "auth.hiring" in u:
        return True
    if "hiring.amazon.com" in u and any(x in u for x in ("signin", "sign-in", "verify", "ap/", "cvf")):
        return True
    if "amazoncognito.com" in u:
        return True
    if "/ap/cvf/" in u or "/cvf/" in u:
        return True
    return False


def _jobsatamazon_login_bounce_url(url: str) -> bool:
    u = url.lower()
    if "jobsatamazon.co.uk" not in u:
        return False
    if "auth.hiring.amazon" in u or "amazoncognito.com" in u:
        return False
    if "/login" in u or "#/login" in u:
        return True
    base = u.split("#", 1)[0].rstrip("/")
    if base.endswith("jobsatamazon.co.uk") or base.endswith("www.jobsatamazon.co.uk"):
        return True
    return False


def _already_on_app_dashboard_url(url: str, target: str) -> bool:
    cur = url.lower()
    tgt = target.strip().lower()
    if "myapplications" in cur:
        return True
    if is_application_tracking_url(cur):
        if not tgt or is_application_tracking_url(tgt):
            want_app = _application_id_from_url(target)
            if want_app:
                return _application_id_from_url(url) == want_app
            want_job = _job_id_from_url(target)
            if want_job:
                return _job_id_from_url(url) == want_job
            return True
    if "selfservice" in cur and "schedule" in cur:
        return True
    if tgt:
        frag = tgt.split("#", 1)
        if len(frag) > 1 and frag[1] and frag[1] in cur:
            return True
    return False


async def _goto_app_after_login_bounce(page, target_url: str, *, timeout_ms: int = 60_000) -> None:
    if not (target_url or "").strip():
        return
    dest = target_url.strip()
    try:
        await page_goto_with_retries(page, dest, timeout_ms=timeout_ms)
        log.info("Opened app/dashboard URL after post-OTP login bounce: %s", dest[:100])
        await page.wait_for_timeout(1_500)
        try:
            await page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:
            pass
        await page.wait_for_timeout(800)
    except Exception as e:
        log.debug("goto after login bounce: %s", e)


def login_dashboard_looks_ready(
    url: str,
    corpus: str,
    target_url: str,
    cfg: ShiftAlertConfig,
) -> bool:
    """True when URL + page text look like signed-in My jobs / schedule (not login shell)."""
    if _login_keywords_in_url(url) and _jobsatamazon_login_bounce_url(url):
        return False
    if not _already_on_app_dashboard_url(url, target_url):
        return False
    if page_looks_blocked(corpus, cfg.blocked_page_substrings):
        return False
    if page_looks_ready(corpus, cfg.page_ready_substrings):
        return True
    return "my jobs" in corpus or "myapplications" in corpus.replace(" ", "")


_POST_OTP_JOBS_LOGIN_CTA_RE = re.compile(
    r"(click\s+here\s+to\s+log\s*in|log\s*in\s+to\s+continue|sign\s*in\s+to\s+continue)",
    re.I,
)

_POST_OTP_LOGIN_CTA_JS = """
() => {
  const wants = ['click here to login', 'log in to continue', 'sign in to continue', 'click here to log in'];
  for (const sel of ['a', 'button', '[role="button"]', '[role="link"]', 'span']) {
    for (const n of document.querySelectorAll(sel)) {
      const t = (n.innerText || n.textContent || '').trim().toLowerCase();
      if (!t) continue;
      if (wants.some(w => t.includes(w))) {
        try { n.scrollIntoView({ block: 'center' }); } catch (_) {}
        n.click();
        return true;
      }
    }
  }
  return false;
}
"""


async def _unstick_jobsatamazon_landing(page) -> bool:
    for frame in [page.main_frame, *[f for f in page.frames if f is not page.main_frame]]:
        for role in ("link", "button"):
            try:
                loc = frame.get_by_role(role, name=_POST_OTP_JOBS_LOGIN_CTA_RE)
                if await loc.count() == 0:
                    continue
                el = loc.first
                try:
                    if not await el.is_visible():
                        continue
                except Exception:
                    continue
                await el.click(timeout=2_800)
                log.info("Clicked jobsatamazon '%s' CTA (%s)", _POST_OTP_JOBS_LOGIN_CTA_RE.pattern, role)
                await page.wait_for_timeout(700)
                return True
            except Exception:
                continue
    for frame in [page.main_frame, *[f for f in page.frames if f is not page.main_frame]]:
        try:
            if await frame.evaluate(_POST_OTP_LOGIN_CTA_JS):
                log.info("Clicked jobsatamazon CTA via JS fallback")
                await page.wait_for_timeout(700)
                return True
        except Exception:
            continue
    return False


async def _recover_from_post_otp_login_bounce(
    page: Any,
    target_url: str,
    cfg: ShiftAlertConfig,
    *,
    max_rounds: int = 6,
) -> bool:
    """Leave jobsatamazon login shell and load My jobs / schedule before saving session."""
    target = (target_url or _DEFAULT_POST_AUTH_JOBS_APP_URL).strip()
    if not target:
        return False
    for round_i in range(max(1, max_rounds)):
        try:
            u = page.url
            html = await _page_content_retry(page, attempts=6, gap_ms=300)
            corpus = _shift_page_corpus(html)
            if login_dashboard_looks_ready(u, corpus, target, cfg):
                log.info("Login recovery: dashboard ready (round %d).", round_i + 1)
                return True
            await page.bring_to_front()
            try:
                await page.evaluate("window.focus && window.focus()")
            except Exception:
                pass
            await _unstick_jobsatamazon_landing(page)
            await page.wait_for_timeout(600)
            if _jobsatamazon_login_bounce_url(u) or (
                _login_keywords_in_url(u) and "jobsatamazon.co.uk" in u.lower()
            ):
                log.info(
                    "Login recovery round %d: still on login shell (%s) — opening %s",
                    round_i + 1,
                    u[:90],
                    target[:80],
                )
                await _goto_app_after_login_bounce(page, target, timeout_ms=cfg.navigation_timeout_ms)
            elif _saw_hiring_auth_flow_url(u):
                await page.wait_for_timeout(2_000)
            else:
                await _goto_app_after_login_bounce(page, target, timeout_ms=cfg.navigation_timeout_ms)
        except Exception as e:
            log.debug("Login recovery round %d failed: %s", round_i + 1, e)
        await page.wait_for_timeout(1_800)
    try:
        u = page.url
        html = await _page_content_retry(page, attempts=4, gap_ms=250)
        return login_dashboard_looks_ready(u, _shift_page_corpus(html), target, cfg)
    except Exception:
        return False


async def _prime_login_post_otp_target(page: Any, target_url: str) -> None:
    dest = (target_url or _DEFAULT_POST_AUTH_JOBS_APP_URL).strip()
    try:
        await page.evaluate(
            "(dest) => { try { sessionStorage.setItem('shift_alert_post_login', dest); } catch (_) {} }",
            dest,
        )
    except Exception:
        pass


def _attach_login_framenavigated_guard(page: Any, target_url: str) -> dict[str, bool]:
    """If SPA navigates back to /login after OTP, immediately reopen My jobs."""
    state: dict[str, bool] = {"saw_hiring": False, "saw_jobs": False}
    target = (target_url or _DEFAULT_POST_AUTH_JOBS_APP_URL).strip()
    last_recover = 0.0

    async def _on_frame_navigated(frame: Any) -> None:
        nonlocal last_recover
        try:
            if frame != page.main_frame:
                return
            u = frame.url
            if _saw_hiring_auth_flow_url(u):
                state["saw_hiring"] = True
            if "jobsatamazon.co.uk" in u.lower():
                state["saw_jobs"] = True
            if not target or not (state["saw_hiring"] or state["saw_jobs"]):
                return
            if not _jobsatamazon_login_bounce_url(u):
                return
            if _already_on_app_dashboard_url(u, target):
                return
            now = time.monotonic()
            if now - last_recover < 3.0:
                return
            last_recover = now
            log.info("Login guard: post-OTP sent back to login (%s) — opening My jobs", u[:90])
            await _goto_app_after_login_bounce(page, target)
            await _unstick_jobsatamazon_landing(page)
        except Exception:
            pass

    page.on("framenavigated", _on_frame_navigated)
    return state


_LOGIN_KEEPALIVE_INTERVAL_SECONDS = 15.0


async def _simulate_login_session_activity(page: Any) -> None:
    """Subtle mouse/scroll/focus nudges so Amazon does not idle-logout during manual login."""
    try:
        await page.bring_to_front()
    except Exception:
        pass
    try:
        await page.evaluate(
            """
            () => {
              try { window.focus && window.focus(); } catch (_) {}
              try { document.dispatchEvent(new Event('mousemove', { bubbles: true })); } catch (_) {}
            }
            """
        )
    except Exception:
        pass
    try:
        vp = page.viewport_size
        w = int((vp or {}).get("width") or 1365)
        h = int((vp or {}).get("height") or 900)
        x1 = random.randint(100, max(120, w - 100))
        y1 = random.randint(100, max(120, h - 100))
        await page.mouse.move(x1, y1)
        await page.wait_for_timeout(random.randint(50, 150))
        x2 = max(60, min(w - 60, x1 + random.randint(-80, 80)))
        y2 = max(60, min(h - 60, y1 + random.randint(-60, 60)))
        await page.mouse.move(x2, y2)
    except Exception:
        pass
    try:
        scroll_dy = random.choice([-90, -45, 45, 90])
        await page.evaluate(
            """
            (dy) => {
              const el = document.scrollingElement || document.documentElement || document.body;
              if (el) {
                try { el.scrollBy({ top: dy, left: 0, behavior: 'auto' }); } catch (_) {}
              }
              try { window.dispatchEvent(new Event('scroll')); } catch (_) {}
            }
            """,
            scroll_dy,
        )
    except Exception:
        pass


async def _login_idle_recovery_tick(
    page: Any,
    *,
    app_fallback_url: str,
    state: dict[str, Any],
) -> None:
    target = (app_fallback_url or "").strip()
    u = page.url
    ul = u.lower()
    if _saw_hiring_auth_flow_url(u):
        state["saw_hiring"] = True
    if "jobsatamazon.co.uk" in ul:
        state["saw_jobs"] = True
    await _unstick_jobsatamazon_landing(page)
    needs_recover = (
        target
        and (state["saw_hiring"] or state["saw_jobs"])
        and _jobsatamazon_login_bounce_url(u)
        and not _already_on_app_dashboard_url(u, target)
    )
    if needs_recover:
        now = time.monotonic()
        if now - float(state.get("last_recover_mono") or 0.0) > 4.0:
            state["last_recover_mono"] = now
            await _goto_app_after_login_bounce(page, target)


async def _keep_login_browser_awake_while_waiting(page: Any, *, app_fallback_url: str) -> None:
    state: dict[str, Any] = {"saw_hiring": False, "saw_jobs": False, "last_recover_mono": 0.0}
    try:
        while True:
            try:
                await _login_idle_recovery_tick(page, app_fallback_url=app_fallback_url, state=state)
                await _simulate_login_session_activity(page)
            except Exception:
                pass
            await asyncio.sleep(_LOGIN_KEEPALIVE_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        raise


async def _wait_for_login_enter_with_keepalive(page: Any, *, app_fallback_url: str) -> None:
    poller = asyncio.create_task(
        _keep_login_browser_awake_while_waiting(page, app_fallback_url=app_fallback_url)
    )
    try:
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        poller.cancel()
        try:
            await poller
        except asyncio.CancelledError:
            pass


_CMP_ROOT_SELECTORS: tuple[str, ...] = (
    "#onetrust-consent-sdk",
    "#onetrust-banner-sdk",
    "#CybotCookiebotDialog",
    "#usercentrics-root",
    ".glue-cookie-notification-bar",
    "[id^='sp_message_iframe']",
)

_LOGIN_NEUTRALIZE_AND_FOCUS_JS = """
(cmpSelectors) => {
  function walkShadow(root, visit) {
    if (!root || !root.querySelectorAll) return;
    visit(root);
    root.querySelectorAll('*').forEach((host) => {
      if (host.shadowRoot) walkShadow(host.shadowRoot, visit);
    });
  }
  function findEmailLikeInputs() {
    const out = [];
    function visit(r) {
      r.querySelectorAll('input, textarea').forEach((inp) => {
        if (inp.disabled || inp.type === 'hidden') return;
        const t = (inp.type || 'text').toLowerCase();
        if (!['email', 'text', 'tel', ''].includes(t)) return;
        const ph = (inp.getAttribute('placeholder') || '').toLowerCase();
        const id = (inp.id || '').toLowerCase();
        const nm = (inp.name || '').toLowerCase();
        const ac = (inp.getAttribute('autocomplete') || '').toLowerCase();
        if (
          id.includes('ap_email') || id.includes('cvfemail') || id.includes('cvf_email') ||
          nm === 'email' || ac === 'username' || ac === 'email' ||
          ph.includes('email') || ph.includes('mobile') || ph.includes('phone') ||
          t === 'email' || t === 'tel'
        ) {
          out.push(inp);
        }
      });
    }
    const root = document.body || document.documentElement;
    if (!root) return out;
    walkShadow(root, visit);
    return out;
  }
  function neutralizeCoveringLayers(x, y) {
    for (let depth = 0; depth < 28; depth++) {
      const hit = document.elementFromPoint(x, y);
      if (!hit) break;
      if (hit === document.documentElement || hit === document.body) break;
      if (hit.tagName === 'INPUT' || hit.tagName === 'TEXTAREA') break;
      const st = window.getComputedStyle(hit);
      const pos = st.position;
      const zi = parseInt(st.zIndex, 10);
      const z = Number.isFinite(zi) ? zi : 0;
      if (pos === 'fixed' || pos === 'sticky') {
        hit.style.setProperty('pointer-events', 'none', 'important');
        continue;
      }
      if (pos === 'absolute' && z >= 40) {
        const r = hit.getBoundingClientRect();
        if (r.width > innerWidth * 0.65 && r.height > innerHeight * 0.1) {
          hit.style.setProperty('pointer-events', 'none', 'important');
          continue;
        }
      }
      if (pos === 'relative' && z >= 55) {
        const r2 = hit.getBoundingClientRect();
        if (r2.width > innerWidth * 0.72 && r2.height > innerHeight * 0.07) {
          hit.style.setProperty('pointer-events', 'none', 'important');
          continue;
        }
      }
      break;
    }
  }
  const sels = cmpSelectors;
  for (const sel of sels) {
    try {
      document.querySelectorAll(sel).forEach((n) => {
        n.style.setProperty('display', 'none', 'important');
        n.style.setProperty('pointer-events', 'none', 'important');
      });
    } catch (e) {}
  }
  const cands = findEmailLikeInputs();
  cands.sort((a, b) => {
    if ((a.id || '').toLowerCase() === 'ap_email') return -1;
    if ((b.id || '').toLowerCase() === 'ap_email') return 1;
    return 0;
  });
  let el = cands[0] || document.querySelector('input[type="email"], input#ap_email, input[name="email"]');
  if (!el) return false;
  el.scrollIntoView({ block: 'center', behavior: 'instant' });
  const r = el.getBoundingClientRect();
  const x = Math.min(Math.max(r.left + Math.min(r.width * 0.45, 140), 2), innerWidth - 3);
  const y = Math.min(Math.max(r.top + Math.min(22, r.height * 0.35), 2), innerHeight - 3);
  neutralizeCoveringLayers(x, y);
  neutralizeCoveringLayers(x, y);
  const pts = [
    [x, y],
    [Math.min(Math.max(r.left + 6, 2), innerWidth - 4), y],
    [Math.min(Math.max(r.right - 8, 2), innerWidth - 4), y],
    [x, Math.min(Math.max(r.bottom - 10, 2), innerHeight - 4)],
    [Math.floor((r.left + r.right) / 2), Math.floor((r.top + r.bottom) / 2)],
  ];
  for (const [px, py] of pts) {
    neutralizeCoveringLayers(px, py);
  }
  el.focus({ preventScroll: true });
  try {
    el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
    el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
  } catch (e1) {}
  if (typeof el.click === 'function') el.click();
  return true;
}
"""


def _login_frame_neutralize_payload() -> list[str]:
    return list(_CMP_ROOT_SELECTORS)


_LOGIN_BOTTOM_PEEL_JS = """
() => {
  document.querySelectorAll('body *').forEach((node) => {
    try {
      const tag = node.tagName;
      if (['INPUT','TEXTAREA','BUTTON','SELECT','OPTION','LABEL','A','IFRAME','SVG','PATH','CANVAS'].includes(tag)) return;
      const cs = getComputedStyle(node);
      if (cs.position !== 'fixed' && cs.position !== 'sticky') return;
      const r = node.getBoundingClientRect();
      if (r.top < innerHeight * 0.32) return;
      if (r.height < 28 || r.height > 560) return;
      if (r.width < innerWidth * 0.18) return;
      node.style.setProperty('pointer-events', 'none', 'important');
    } catch (e) {}
  });
  return true;
}
"""


async def peel_bottom_fixed_ui_noise(page) -> None:
    for frame in [page.main_frame, *[f for f in page.frames if f is not page.main_frame]]:
        try:
            await frame.evaluate(_LOGIN_BOTTOM_PEEL_JS)
        except Exception:
            continue


async def neutralize_login_dom_via_js(page) -> bool:
    payload = _login_frame_neutralize_payload()
    any_ok = False
    for frame in [page.main_frame, *[f for f in page.frames if f is not page.main_frame]]:
        try:
            if await frame.evaluate(_LOGIN_NEUTRALIZE_AND_FOCUS_JS, payload):
                log.info("Login unblock JS succeeded in a frame")
                any_ok = True
        except Exception:
            continue
    return any_ok


_LOGIN_OVERLAY_BUTTON_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"accept\s+all(\s+cookies)?", re.I),
    re.compile(r"^allow\s+all$", re.I),
    re.compile(r"^reject\s+all$", re.I),
    re.compile(r"^only\s+necessary$", re.I),
)


async def relax_login_overlays_for_human(page) -> None:
    await page.wait_for_timeout(400)
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(120)
        await page.keyboard.press("Escape")
    except Exception:
        pass
    for frame in [page.main_frame, *[f for f in page.frames if f is not page.main_frame]]:
        for pat in _LOGIN_OVERLAY_BUTTON_PATTERNS:
            loc = frame.get_by_role("button", name=pat)
            try:
                if await loc.count() == 0:
                    continue
                btn = loc.first
                await btn.wait_for(state="visible", timeout=1_200)
                await btn.click(timeout=2_500)
                log.info("Clicked overlay/consent button (pattern=%s)", pat.pattern)
                await page.wait_for_timeout(400)
                return
            except Exception:
                continue


_LOGIN_FIELD_SELECTORS: tuple[str, ...] = (
    "input#ap_email",
    "input#cvfEmail",
    "input[name='email']",
    "input[type='email']",
    "input[type='tel']",
    "input[autocomplete='username']",
    "input[autocomplete='email']",
)


async def prime_login_identifier_field_for_human(page) -> None:
    await page.wait_for_timeout(200)
    for frame in [page.main_frame, *[f for f in page.frames if f is not page.main_frame]]:
        for sel in _LOGIN_FIELD_SELECTORS:
            loc = frame.locator(sel).first
            try:
                if await loc.count() == 0:
                    continue
                await loc.click(force=True, timeout=4_000)
                log.info("Focused login identifier (%s)", sel)
                return
            except Exception:
                continue
        try:
            ph = frame.get_by_placeholder(re.compile(r"email|mobile|phone", re.I)).first
            if await ph.count() == 0:
                continue
            await ph.click(force=True, timeout=4_000)
            log.info("Focused login field via placeholder")
            return
        except Exception:
            continue
    log.debug("Could not auto-focus login field; dismiss any cookie bar manually if clicks fail")


async def cmd_login(
    file_cfg: FileConfig,
    *,
    storage_override: str | Path | None,
    state_override: str | Path | None,
    profile_id: str | None,
) -> int:
    cfg = file_cfg.shift_alert
    try:
        storage_override, state_override = merge_profile_storage_for_login(
            cfg, profile_id, storage_override, state_override
        )
    except ValueError as e:
        log.error("%s", e)
        return 2
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as e:
        log.error(
            "Playwright is not installed. Install: pip install -r requirements-playwright.txt "
            "and python -m playwright install chromium (%s)",
            e,
        )
        return 2

    storage, derived_state = resolve_shift_paths(cfg, storage_override=storage_override, state_override=state_override)
    watch_url = resolve_login_schedule_url(cfg, profile_id)
    login_entry_url = resolve_login_entry_url(cfg, profile_id)
    login_app_fallback = watch_url if watch_url else _DEFAULT_POST_AUTH_JOBS_APP_URL
    print(
        "\npython -m app.shift_alert login\n"
        "— Opens a **maximized** Chrome window. Sign in, then press Enter here to save the session.\n"
        "• After OTP: keep this window **on top** (don't read OTP only in Telegram — stay on Chrome).\n"
        "  If it reloads to login, the bot returns to your application dashboard and clicks **Click here to login**.\n"
        f"\n• Opens: {login_entry_url}\n"
        f"• After sign-in we save cookies for: {login_app_fallback}\n"
        f"\nSession file: {storage}\n",
        flush=True,
    )
    if storage_override or state_override:
        print(f"Dedupe state for run: {derived_state}\n", flush=True)

    with tempfile.TemporaryDirectory(prefix="shift_alert_login_") as user_data_dir:
        async with async_playwright() as p:
            persist_kw = _login_persistent_context_kwargs(cfg, timezone_name=file_cfg.project.timezone)
            try:
                context = await p.chromium.launch_persistent_context(user_data_dir, **persist_kw)
            except Exception as e:
                log.exception("launch_persistent_context failed: %s", e)
                print(
                    "\nCould not start the browser for login.\n"
                    "• Install Google Chrome, **or** set in config.yaml e.g.\n"
                    '    shift_alert.playwright_browser_channel: "chrome"\n'
                    "  (then: python -m playwright install chrome)\n"
                    "• Linux without Chrome: install `google-chrome-stable` or set playwright_executable_path.\n",
                    flush=True,
                )
                return 2
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(cfg.navigation_timeout_ms)
            await context.add_init_script(_LOGIN_SPA_STABILITY_INIT_SCRIPT)
            await _prime_login_post_otp_target(page, login_app_fallback)
            _attach_login_framenavigated_guard(page, login_app_fallback)
            try:
                await page_goto_with_retries(page, login_entry_url, timeout_ms=cfg.navigation_timeout_ms)
            except Exception:
                if cfg.playwright_executable_path:
                    print(
                        "\nNavigation failed (often net::ERR_ABORTED for custom Chromium builds).\n"
                        "Try in config.yaml:\n"
                        '  shift_alert.playwright_browser_channel: "chrome"\n',
                        flush=True,
                    )
                await context.close()
                raise
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=20_000)
            except Exception:
                pass
            await page.wait_for_timeout(1_800)
            await relax_login_overlays_for_human(page)
            await peel_bottom_fixed_ui_noise(page)
            await neutralize_login_dom_via_js(page)
            await prime_login_identifier_field_for_human(page)
            await page.wait_for_timeout(450)
            await peel_bottom_fixed_ui_noise(page)
            await neutralize_login_dom_via_js(page)
            await prime_login_identifier_field_for_human(page)
            await page.wait_for_timeout(200)
            await neutralize_login_dom_via_js(page)
            try:
                await page.evaluate("window.focus && window.focus()")
            except Exception:
                pass
            await _unstick_jobsatamazon_landing(page)
            print(
                "  After hiring OTP: if the site drops you on jobsatamazon **login/home** again, we navigate to\n"
                f"    {login_app_fallback}\n"
                "  (set shift_alert.schedule_url to your application tracking URL in config.yaml).\n"
                "  Rapid reload() bursts are capped; keep the window focused when you can.\n"
                "  Subtle mouse/scroll keepalive runs every 15s until you press Enter (reduces idle logout).\n",
                flush=True,
            )
            await _wait_for_login_enter_with_keepalive(page, app_fallback_url=login_app_fallback)
            print("\nChecking login — opening your application dashboard if needed…\n", flush=True)
            ready = await _recover_from_post_otp_login_bounce(
                page, login_app_fallback, cfg, max_rounds=8
            )
            while not ready:
                print(
                    "\n⚠ Still on the login page.\n"
                    "  1) In the browser, open your application / job-opportunities page (or wait after 'Click here to login').\n"
                    f"  2) URL should look like: {login_app_fallback}\n"
                    "  3) Press Enter here again when the application dashboard is visible.\n",
                    flush=True,
                )
                await _wait_for_login_enter_with_keepalive(page, app_fallback_url=login_app_fallback)
                ready = await _recover_from_post_otp_login_bounce(
                    page, login_app_fallback, cfg, max_rounds=8
                )
            print("✓ Signed in — saving session.\n", flush=True)
            await save_storage_state_from_context(context, storage)
            await context.close()

    print(f"\nSaved session to {storage.resolve()}\n")
    print(
        "Next: shift_alert.enabled: true, then:\n"
        "  python -m app.shift_alert run\n"
        "If you used --storage-state, pass the same path on run."
    )
    return 0


async def _page_content_retry(page: Any, *, attempts: int = 14, gap_ms: int = 400) -> str:
    last: BaseException | None = None
    for _ in range(attempts):
        try:
            return await page.content()
        except Exception as e:
            last = e
            msg = str(e).lower()
            if any(
                x in msg
                for x in (
                    "navigating",
                    "navigation",
                    "changing the content",
                    "target closed",
                )
            ):
                await page.wait_for_timeout(gap_ms)
                continue
            raise
    assert last is not None
    raise last


async def _screenshot_retry(
    page: Any,
    *,
    per_attempt_timeout_ms: int = 55_000,
    attempts: int = 3,
) -> bytes:
    for attempt in range(attempts):
        try:
            try:
                await page.bring_to_front()
            except Exception:
                pass
            return await page.screenshot(
                full_page=False,
                type="png",
                timeout=per_attempt_timeout_ms,
                animations="disabled",
                caret="hide",
            )
        except Exception as e:
            log.warning(
                "Shift alert screenshot attempt %d/%d failed: %s",
                attempt + 1,
                attempts,
                e,
            )
            if attempt + 1 < attempts:
                await page.wait_for_timeout(1500)
    log.error(
        "Shift alert: screenshot failed after %d attempt(s); using 1×1 placeholder (caption still sent).",
        attempts,
    )
    return base64.b64decode(_MIN_TEST_PNG_B64)


_DOM_WATCH_MIN_SECONDS = 5  # matches MutationObserver JS floor (Math.max(5000, …))


def dom_watch_timeout_ms(
    cfg: ShiftAlertConfig,
    poll_min_seconds: int,
    poll_max_seconds: int,
    *,
    turbo: ShiftAlertTurboWindow | None = None,
) -> int:
    if cfg.dom_watch_timeout_seconds > 0:
        secs = max(_DOM_WATCH_MIN_SECONDS, float(cfg.dom_watch_timeout_seconds))
        return int(secs * 1000)
    if turbo is not None and float(turbo.dom_watch_timeout_seconds) > 0:
        secs = max(_DOM_WATCH_MIN_SECONDS, float(turbo.dom_watch_timeout_seconds))
        return int(secs * 1000)
    base = max(int(poll_min_seconds), int(poll_max_seconds))
    secs = max(_DOM_WATCH_MIN_SECONDS, base)
    return secs * 1000


def page_on_schedule_route(current_url: str, schedule_url: str) -> bool:
    if _login_keywords_in_url(current_url):
        return False
    if is_application_tracking_url(schedule_url):
        return _already_on_app_dashboard_url(current_url, schedule_url)
    if is_direct_schedule_picker_url(schedule_url):
        u = current_url.lower()
        if is_application_tracking_url(u):
            return _already_on_app_dashboard_url(current_url, schedule_url)
        return "selfservice" in u and "schedule" in u
    return _already_on_app_dashboard_url(current_url, schedule_url)


def build_dom_watcher_eval_config(cfg: ShiftAlertConfig, *, timeout_ms: int) -> dict[str, Any]:
    shift_phrases = list(cfg.deep_shift_available_substrings or [])
    shift_phrases.extend(cfg.page_ready_substrings or [])
    return {
        "timeoutMs": int(timeout_ms),
        "healthPingIntervalMs": _OBSERVER_HEALTH_PING_MS,
        "observerStaleMs": _OBSERVER_STALE_SILENCE_MS,
        "emptyPhrases": list(cfg.empty_state_substrings or []),
        "shiftPhrases": shift_phrases,
        "readyPhrases": list(cfg.page_ready_substrings or []),
    }


def dom_observer_result_is_stale(result: dict[str, Any]) -> bool:
    if result.get("stale"):
        return True
    return str(result.get("reason") or "") in _STALE_OBSERVER_REASONS


def is_playwright_browser_crash(exc: BaseException) -> bool:
    """True for target closed, crashed browser, or Playwright websocket disconnect."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        name = type(cur).__name__.lower()
        if name in (
            "targetclosederror",
            "browserclosederror",
            "connectionclosederror",
            "websocketerror",
        ):
            return True
        msg = str(cur).lower()
        if any(
            k in msg
            for k in (
                "target closed",
                "target page, context or browser has been closed",
                "browser has been closed",
                "connection closed",
                "websocket",
                "session closed",
                "execution context was destroyed",
                "crash",
                "disconnected",
            )
        ):
            return True
        cur = cur.__cause__ if isinstance(cur.__cause__, BaseException) else None
    return False


_DOM_SHIFT_WATCHER_JS = """
async (cfg) => {
  const timeoutMs = Math.max(5000, Number(cfg.timeoutMs) || 45000);
  const healthPingMs = Math.max(5000, Number(cfg.healthPingIntervalMs) || 15000);
  const staleAfterMs = Math.max(60000, Number(cfg.observerStaleMs) || 120000);
  const emptyPhrases = cfg.emptyPhrases || [];
  const shiftPhrases = cfg.shiftPhrases || [];
  const readyPhrases = cfg.readyPhrases || [];

  const norm = () => {
    const el = document.body;
    if (!el) return '';
    return (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  };
  const hasAny = (phrases, text) => {
    for (const p of phrases) {
      const q = String(p || '').toLowerCase().trim();
      if (q && text.includes(q)) return true;
    }
    return false;
  };
  const snap = () => {
    const text = norm();
    return {
      text,
      empty: hasAny(emptyPhrases, text),
      shiftUi: hasAny(shiftPhrases, text),
      ready: hasAny(readyPhrases, text),
    };
  };

  return await new Promise((resolve) => {
    let settled = false;
    const baseline = snap();
    let lastFp = baseline.text.slice(0, 12000);
    let lastMutationAt = Date.now();
    let observer = null;
    let healthTimer = null;

    const markMutation = () => {
      lastMutationAt = Date.now();
    };

    const finish = (payload) => {
      if (settled) return;
      settled = true;
      try { observer && observer.disconnect(); } catch (_) {}
      clearTimeout(timer);
      if (healthTimer) clearInterval(healthTimer);
      resolve(payload);
    };

    const isLayoutDetached = () => {
      const body = document.body;
      const root = document.documentElement;
      if (!body || !root) return true;
      try {
        if (!root.isConnected) return true;
      } catch (_) {
        return true;
      }
      try {
        const rect = body.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0 && body.childElementCount === 0) return true;
      } catch (_) {}
      const t = norm();
      if (t.length < 8 && body.childElementCount < 2) return true;
      return false;
    };

    const runHealthPing = () => {
      const silentFor = Date.now() - lastMutationAt;
      const detached = isLayoutDetached();
      if (detached && silentFor >= staleAfterMs) {
        return finish({
          triggered: false,
          stale: true,
          reason: 'layout_detached',
          snapshot: norm().slice(0, 8000),
          silentMs: silentFor,
        });
      }
      if (silentFor >= staleAfterMs) {
        return finish({
          triggered: false,
          stale: true,
          reason: 'observer_silent',
          snapshot: norm().slice(0, 8000),
          silentMs: silentFor,
        });
      }
    };

    const hasShiftLayoutNodes = () => {
      const pickers = document.querySelectorAll(
        'button,a,[role="button"],[role="tab"],[class*="shift"],[class*="schedule"],[data-testid*="shift"]'
      );
      for (const el of pickers) {
        const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        if (!t || t.length > 120) continue;
        if (/select shift|choose.*shift|shift pattern|available shift|save and continue/.test(t)) {
          return true;
        }
      }
      return false;
    };

    const check = () => {
      const cur = snap();
      const fp = cur.text.slice(0, 12000);
      const layoutShift = hasShiftLayoutNodes();
      const emptyGone = baseline.empty && !cur.empty;
      const shiftNew = (!baseline.shiftUi && cur.shiftUi) || layoutShift;
      const deepOpen = cur.shiftUi && cur.ready;
      const mutatedWhileEmpty =
        baseline.empty && fp !== lastFp && Math.abs(fp.length - lastFp.length) > 48;
      if (emptyGone) {
        return finish({ triggered: true, reason: 'empty_removed', snapshot: cur.text.slice(0, 8000) });
      }
      if (shiftNew || deepOpen || layoutShift) {
        return finish({ triggered: true, reason: layoutShift ? 'layout_nodes' : 'shift_ui', snapshot: cur.text.slice(0, 8000) });
      }
      if (mutatedWhileEmpty) {
        return finish({ triggered: true, reason: 'dom_mutated', snapshot: cur.text.slice(0, 8000) });
      }
      lastFp = fp;
    };

    observer = new MutationObserver(() => {
      markMutation();
      try { check(); } catch (_) {}
    });
    const root = document.documentElement || document.body;
    if (root) {
      observer.observe(root, {
        childList: true,
        subtree: true,
        characterData: true,
        attributes: true,
      });
    }

    healthTimer = setInterval(() => {
      try { runHealthPing(); } catch (_) {}
    }, healthPingMs);

    const timer = setTimeout(() => {
      finish({ triggered: false, reason: 'timeout', snapshot: norm().slice(0, 8000) });
    }, timeoutMs);

    markMutation();
    setTimeout(() => { try { check(); } catch (_) {} }, 400);
  });
}
"""


async def _close_shift_watch_handle(handle: ShiftWatchHandle | None) -> None:
    if handle is None:
        return
    try:
        await handle.context.close()
    except Exception as e:
        log.debug("Watch context close: %s", e)


async def _close_all_watch_handles(handles: dict[str, ShiftWatchHandle]) -> None:
    for hid in list(handles.keys()):
        await _close_shift_watch_handle(handles.pop(hid, None))


async def _soft_layout_reconnection(
    page: Any,
    schedule_url: str,
    watch: ShiftWatchHandle | None,
) -> None:
    """In-page SPA nudge after observer health failure (no page.goto / reload)."""
    log.info("Soft layout reconnection nudge (observer stale / silent DOM).")
    await soft_spa_dashboard_refresh(page, schedule_url)
    if watch is not None:
        watch.dashboard_bootstrapped = False
    await page.wait_for_timeout(600)


async def _acquire_shift_watch_handle(
    *,
    browser: Any,
    cfg: ShiftAlertConfig,
    file_cfg: FileConfig,
    schedule_url: str,
    storage_path: Path,
    profile_id: str,
    watch: ShiftWatchHandle | None,
) -> tuple[ShiftWatchHandle, bool]:
    """Return (handle, created_new). Reuse handle when storage + schedule match."""
    if watch is not None:
        if (
            watch.profile_id == profile_id
            and watch.schedule_url == schedule_url
            and watch.storage_path.resolve() == storage_path.resolve()
        ):
            try:
                if not watch.page.is_closed():
                    return watch, False
            except Exception:
                pass
        await _close_shift_watch_handle(watch)

    context = await new_shift_browser_context(
        browser,
        cfg=cfg,
        timezone_name=file_cfg.project.timezone,
        storage_state=storage_path,
    )
    page = await context.new_page()
    page.set_default_timeout(cfg.navigation_timeout_ms)
    return (
        ShiftWatchHandle(
            profile_id=profile_id,
            schedule_url=schedule_url,
            storage_path=storage_path,
            context=context,
            page=page,
        ),
        True,
    )


_SPA_INPAGE_NUDGE_JS = """
(hashPart) => {
  const target = hashPart.startsWith('#') ? hashPart : '#' + hashPart;
  const bare = target.replace(/^#/, '');
  try {
    if (location.hash !== target) {
      location.hash = bare;
    }
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    window.dispatchEvent(new PopStateEvent('popstate', { state: history.state }));
  } catch (_) {}
  const labels = /refresh|reload|try again|update|retry/i;
  const nodes = document.querySelectorAll('button,a,[role="button"]');
  for (const el of nodes) {
    const t = (el.innerText || el.textContent || '').trim();
    if (t && t.length < 48 && labels.test(t)) {
      try { el.click(); return 'ui_click'; } catch (_) {}
    }
  }
  return 'hash_nudge';
}
"""


async def soft_spa_dashboard_refresh(page: Any, schedule_url: str) -> None:
    """In-browser SPA nudge only — never page.goto or page.reload (avoids CloudFront WAF)."""
    url = (schedule_url or "").strip() or DEFAULT_SHIFT_APPLICATION_DASHBOARD_URL
    hash_part = "myApplications"
    if is_application_tracking_url(url):
        hash_part = url.split("#", 1)[1] if "#" in url else "/job-opportunities"
    elif "#" in url:
        hash_part = url.split("#", 1)[1]
    try:
        mode = await page.evaluate(_SPA_INPAGE_NUDGE_JS, hash_part)
        log.debug("SPA in-page nudge (%s) for hash fragment.", mode)
    except Exception as e:
        log.debug("SPA in-page nudge failed: %s", e)
    await page.wait_for_timeout(900)


async def _read_dashboard_corpus(page: Any, cfg: ShiftAlertConfig) -> tuple[str, str, bool, bool]:
    html = await _page_content_retry(page)
    corpus = _shift_page_corpus(html)
    blocked = page_looks_blocked(corpus, cfg.blocked_page_substrings)
    markers = cfg.page_ready_substrings
    ready = not markers or page_looks_ready(corpus, markers) or blocked
    return html, corpus, ready, blocked


def _mark_waf_cooldown(watch: ShiftWatchHandle) -> None:
    watch.blocked_cooldown_until = time.monotonic() + float(_WAF_NAV_COOLDOWN_SECONDS)
    log.warning(
        "CloudFront/WAF block — no page.goto for %ds; in-page SPA nudges only.",
        _WAF_NAV_COOLDOWN_SECONDS,
    )


async def _bootstrap_dashboard_once(
    page: Any,
    schedule_url: str,
    cfg: ShiftAlertConfig,
    watch: ShiftWatchHandle,
    *,
    created: bool,
) -> tuple[str, str, bool, bool]:
    """
    Navigate to the hiring dashboard at most once per persistent page.
    After bootstrap, only in-page reads / MutationObserver run (no network reload).
    """
    on_route = page_on_schedule_route(page.url, schedule_url)
    in_cooldown = time.monotonic() < watch.blocked_cooldown_until

    if watch.dashboard_bootstrapped and on_route and not in_cooldown:
        log.debug("Dashboard already bootstrapped — holding in-memory page state.")
        return await _read_dashboard_corpus(page, cfg)

    if in_cooldown:
        log.info("WAF cooldown — skipping page.goto; nudging SPA in-page only.")
        await soft_spa_dashboard_refresh(page, schedule_url)
        html, corpus, ready, blocked = await _read_dashboard_corpus(page, cfg)
        if blocked:
            return html, corpus, ready, blocked
        if ready:
            watch.dashboard_bootstrapped = True
        return html, corpus, ready, blocked

    need_goto = created or not on_route or not watch.dashboard_bootstrapped
    if need_goto:
        log.info("One-time dashboard navigation to %s", schedule_url[:90])
        await page_goto_with_retries(page, schedule_url, timeout_ms=cfg.navigation_timeout_ms)
        await page.wait_for_timeout(1_200)

    html, corpus, ready, blocked = await _wait_dashboard_page_ready(page, cfg, full_wait=need_goto)
    if blocked:
        _mark_waf_cooldown(watch)
        return html, corpus, ready, blocked
    if ready:
        watch.dashboard_bootstrapped = True
        log.info("Dashboard layout initialized — observer-only mode from here.")
    return html, corpus, ready, blocked


async def _wait_dashboard_page_ready(
    page: Any,
    cfg: ShiftAlertConfig,
    *,
    full_wait: bool = True,
) -> tuple[str, str, bool, bool]:
    """Return (html, corpus, ready, blocked). Quick mode = single read (reused bootstrapped page)."""
    if not full_wait and page_on_schedule_route(page.url, ""):
        return await _read_dashboard_corpus(page, cfg)

    ready_deadline_ms = int(cfg.page_ready_timeout_ms)
    poll_ms = 500
    elapsed = 0
    html = ""
    blocked = False
    ready = not bool(cfg.page_ready_substrings)
    while elapsed <= ready_deadline_ms:
        html, corpus, ready, blocked = await _read_dashboard_corpus(page, cfg)
        if blocked or ready:
            break
        await page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
    if not ready:
        html, corpus, ready, blocked = await _read_dashboard_corpus(page, cfg)
    return html, corpus, ready, blocked


async def _run_dom_observer_sequence(
    page: Any,
    cfg: ShiftAlertConfig,
    schedule_url: str,
    *,
    poll_min_seconds: int,
    poll_max_seconds: int,
    tz_name: str,
    watch: ShiftWatchHandle | None = None,
) -> dict[str, Any]:
    """MutationObserver window; on timeout/stale health, in-page nudge then short re-observe (no goto/reload)."""
    turbo_win = resolve_active_turbo_window(now_utc(), tz_name, cfg)
    watch_ms = dom_watch_timeout_ms(
        cfg, poll_min_seconds, poll_max_seconds, turbo=turbo_win
    )
    log.info(
        "DOM MutationObserver watching dashboard (%ds window%s, health ping %dms).",
        watch_ms // 1000,
        " [turbo]" if turbo_win is not None else "",
        _OBSERVER_HEALTH_PING_MS,
    )
    dom_result = await run_dom_shift_watcher(page, cfg, timeout_ms=watch_ms)
    if dom_result.get("triggered"):
        return dom_result

    reason = str(dom_result.get("reason") or "timeout")
    if dom_observer_result_is_stale(dom_result):
        log.warning(
            "Observer health ping: stale DOM (%s, silent %sms) — soft layout reconnection.",
            reason,
            dom_result.get("silentMs", "?"),
        )
    else:
        log.debug("Observer idle (%s) — in-page SPA nudge, then short re-observe.", reason)
    await _soft_layout_reconnection(page, schedule_url, watch)

    follow_ms = min(20_000, max(5_000, watch_ms // 3))
    follow = await run_dom_shift_watcher(page, cfg, timeout_ms=follow_ms)
    if follow.get("triggered"):
        return follow
    if dom_observer_result_is_stale(follow):
        log.debug("Follow-up observer still stale (%s).", follow.get("reason"))
    return dom_result


async def run_dom_shift_watcher(
    page: Any,
    cfg: ShiftAlertConfig,
    *,
    timeout_ms: int,
) -> dict[str, Any]:
    eval_cfg = build_dom_watcher_eval_config(cfg, timeout_ms=timeout_ms)
    try:
        raw = await page.evaluate(_DOM_SHIFT_WATCHER_JS, eval_cfg)
    except Exception as e:
        if is_playwright_browser_crash(e):
            raise ShiftBrowserRecoveryNeeded(str(e)[:400]) from e
        log.warning("DOM watcher evaluate failed (%s) — treating as timeout.", type(e).__name__)
        return {"triggered": False, "reason": "evaluate_error", "snapshot": ""}
    if not isinstance(raw, dict):
        return {"triggered": False, "reason": "bad_payload", "snapshot": ""}
    return raw


def _next_last_dashboard_had_empty_copy(
    prev: bool,
    *,
    blocked_dash: bool,
    has_empty_copy: bool,
    deep_evaluated: bool,
    deep_has_no_schedule: bool,
    deep_positive: bool,
) -> bool:
    if blocked_dash:
        return prev
    if has_empty_copy:
        return True
    if deep_evaluated and deep_has_no_schedule:
        return True
    if deep_evaluated and deep_positive and not deep_has_no_schedule:
        return False
    if deep_evaluated:
        return prev
    return False


def _shift_check_result(
    state: ShiftAlertPersistedState,
    alerted: bool,
    outcome: str,
    detail: str = "",
    *,
    watch: ShiftWatchHandle | None = None,
    keep_watch: bool = True,
) -> ShiftCheckResult:
    return ShiftCheckResult(
        state,
        alerted,
        outcome,
        detail,
        watch=watch if keep_watch else None,
    )


async def _check_once(
    *,
    settings: Settings,
    file_cfg: FileConfig,
    cfg: ShiftAlertConfig,
    state: ShiftAlertPersistedState,
    dry_run: bool,
    storage_path: Path,
    state_path: Path,
    schedule_url: str,
    watch_display_name: str | None = None,
    browser: Any,
    watch: ShiftWatchHandle | None = None,
    profile_id: str = "default",
    poll_min_seconds: int = 30,
    poll_max_seconds: int = 60,
) -> ShiftCheckResult:
    tz_name = file_cfg.project.timezone
    storage = storage_path
    active_watch: ShiftWatchHandle | None = None

    if not storage.exists():
        log.error(
            "Missing Playwright session file: %s — run: python -m app.shift_alert login [--storage-state ...]",
            storage,
        )
        _print_shift_alert_session_login_hint()
        return _shift_check_result(state, False, "session_missing", str(storage), keep_watch=False)

    try:
        from playwright.async_api import async_playwright as _playwright_pm  # type: ignore  # noqa: F401
    except Exception as e:
        log.error("Playwright not installed (%s)", e)
        return _shift_check_result(state, False, "playwright_missing", str(e), keep_watch=False)

    corpus = ""
    deep_slots = False
    dashboard_slots = False
    blocked_dash = False
    has_empty_copy = False
    deep_evaluated = False
    deep_corpus = ""
    blocked_deep = False
    deep_has_no_schedule = False
    deep_positive = False
    deep_quiet = True
    should_send = False
    screenshot_bytes = base64.b64decode(_MIN_TEST_PNG_B64)

    try:
        active_watch, created = await _acquire_shift_watch_handle(
            browser=browser,
            cfg=cfg,
            file_cfg=file_cfg,
            schedule_url=schedule_url,
            storage_path=storage,
            profile_id=profile_id,
            watch=watch,
        )
        page = active_watch.page
        if created:
            log.info("Opened persistent shift watch context for profile %s.", profile_id)
        else:
            log.debug("Reusing persistent dashboard page for profile %s.", profile_id)

        try:
            _html, corpus, ready, blocked = await _bootstrap_dashboard_once(
                page,
                schedule_url,
                cfg,
                active_watch,
                created=created,
            )
        except Exception as e:
            log.exception("Navigation failed for %s", schedule_url)
            active_watch.dashboard_bootstrapped = False
            await _close_shift_watch_handle(active_watch)
            return _shift_check_result(state, False, "navigation_failed", str(e)[:300], keep_watch=False)

        if _login_keywords_in_url(page.url):
            log.warning(
                "Looks like a login page (url=%s). Session may have expired — run login again.",
                page.url,
            )
            _print_shift_alert_session_login_hint()
            active_watch.dashboard_bootstrapped = False
            await _close_shift_watch_handle(active_watch)
            return _shift_check_result(state, False, "login_required", page.url[:500], keep_watch=False)

        blocked_dash = blocked
        if not ready and not blocked_dash:
            ready_deadline_ms = int(cfg.page_ready_timeout_ms)
            log.warning(
                "Shift page did not look ready within %sms (missing page_ready_substrings). "
                "Not treating as shift availability — extend page_ready_timeout_ms or add markers.",
                ready_deadline_ms,
            )
            return _shift_check_result(
                state,
                False,
                "page_not_ready",
                f"timeout {ready_deadline_ms}ms",
                watch=active_watch,
                keep_watch=True,
            )

        if blocked_dash:
            _mark_waf_cooldown(active_watch)
        elif cfg.dom_watch_enabled:
            dom_result = await _run_dom_observer_sequence(
                page,
                cfg,
                schedule_url,
                poll_min_seconds=poll_min_seconds,
                poll_max_seconds=poll_max_seconds,
                tz_name=tz_name,
                watch=active_watch,
            )
            if dom_result.get("triggered"):
                log.info(
                    "DOM watcher triggered (%s) — evaluating shift alert.",
                    dom_result.get("reason", "?"),
                )
        else:
            await page.wait_for_timeout(800)

        html = await _page_content_retry(page)
        corpus = _shift_page_corpus(html)
        blocked_dash = page_looks_blocked(corpus, cfg.blocked_page_substrings)
        if blocked_dash:
            _mark_waf_cooldown(active_watch)
        has_empty_copy = page_looks_empty(corpus, cfg.empty_state_substrings)
        if blocked_dash:
            hit = blocked_page_match_reason(corpus, cfg.blocked_page_substrings) or "unknown"
            debug_html = Path("data") / "shift_last_blocked.html"
            await _log_and_save_blocked_page_debug(
                page,
                corpus=corpus,
                hit=hit,
                schedule_url=schedule_url,
                debug_path=debug_html,
            )
            log.warning(
                "Blocked/error page detected (e.g. CloudFront 403). Matched phrase: %r — "
                "not treating as open shifts. Open data/shift_last_blocked.html in a browser to verify. "
                "If My jobs looks normal, remove that phrase from shift_alert.blocked_page_substrings.",
                hit,
            )

        shot_dash = await _screenshot_retry(page, per_attempt_timeout_ms=int(cfg.screenshot_timeout_ms))

        direct_picker = is_direct_schedule_picker_url(schedule_url)
        turbo_active = resolve_active_turbo_window(now_utc(), tz_name, cfg) is not None
        click_budget, aggressive_click = select_shift_click_budget(cfg, turbo_active=turbo_active)

        deep_evaluated = False
        deep_corpus = ""
        if direct_picker and not blocked_dash:
            deep_evaluated = True
            deep_corpus = corpus
            log.debug("Direct schedule-picker URL — using page corpus without Select Shift.")
        elif cfg.poll_select_shift_for_deep_check and not blocked_dash:
            clicked = await _try_click_select_shift(
                page, max_attempts=click_budget, aggressive=aggressive_click
            )
            if clicked:
                deep_evaluated = True
                showed = await _wait_for_corpus_markers(
                    page,
                    cfg.deep_page_ready_substrings,
                    timeout_ms=int(cfg.deep_page_ready_timeout_ms),
                )
                if not showed:
                    log.warning(
                        "Select Shift: deep markers not matched within %sms — still parsing HTML.",
                        cfg.deep_page_ready_timeout_ms,
                    )
                await page.wait_for_timeout(500)
                if _login_keywords_in_url(page.url):
                    log.warning("After Select Shift, URL looks like login (%s).", page.url)
                    _print_shift_alert_session_login_hint()
                    await _close_shift_watch_handle(active_watch)
                    return _shift_check_result(
                        state, False, "login_required", page.url[:500], keep_watch=False
                    )
                try:
                    deep_html = await _page_content_retry(page)
                    deep_corpus = _shift_page_corpus(deep_html)
                except Exception as e:
                    log.warning("Select Shift: could not read page after click: %s", e)
                    deep_corpus = ""
                    deep_evaluated = False
            else:
                log.debug("Select Shift control not found or not clickable (skipping deep check).")

        blocked_deep, deep_has_no_schedule, deep_positive, deep_quiet = _deep_screen_signals(
            cfg, deep_corpus, deep_evaluated=deep_evaluated
        )

        dashboard_slots = (
            state.empty_state_seen
            and not blocked_dash
            and not has_empty_copy
            and state.last_dashboard_had_empty_copy
        )
        deep_slots = (
            deep_evaluated
            and state.deep_empty_seen
            and not blocked_deep
            and not deep_has_no_schedule
            and state.last_deep_was_empty
            and deep_positive
        )
        should_send = dashboard_slots or deep_slots

        screenshot_bytes = shot_dash
        if deep_slots:
            screenshot_bytes = await _screenshot_retry(page, per_attempt_timeout_ms=int(cfg.screenshot_timeout_ms))

    except ShiftBrowserRecoveryNeeded:
        raise
    except Exception as e:
        if is_playwright_browser_crash(e):
            log.warning(
                "Browser isolation crash during shift check for %s (%s): %s",
                profile_id,
                type(e).__name__,
                e,
            )
            if active_watch is not None:
                await _close_shift_watch_handle(active_watch)
                active_watch = None
            raise ShiftBrowserRecoveryNeeded(str(e)[:400]) from e
        log.exception("Shift check failed for profile %s", profile_id)
        if active_watch is not None:
            await _close_shift_watch_handle(active_watch)
            active_watch = None
        raise

    fp = _semantic_body_fingerprint(corpus)
    empty_seen = state.empty_state_seen or (bool(has_empty_copy) and not blocked_dash)
    deep_seen = state.deep_empty_seen or (
        deep_evaluated and bool(deep_has_no_schedule) and not blocked_deep
    )

    if deep_evaluated:
        if blocked_deep:
            new_last_deep = True
        elif deep_has_no_schedule:
            new_last_deep = True
        elif deep_positive:
            new_last_deep = False
        else:
            new_last_deep = True
    else:
        new_last_deep = state.last_deep_was_empty

    quiet_dashboard = has_empty_copy or blocked_dash
    dashboard_empty = quiet_dashboard
    next_ldhec = _next_last_dashboard_had_empty_copy(
        state.last_dashboard_had_empty_copy,
        blocked_dash=blocked_dash,
        has_empty_copy=has_empty_copy,
        deep_evaluated=deep_evaluated,
        deep_has_no_schedule=deep_has_no_schedule,
        deep_positive=deep_positive,
    )
    deep_still_waiting = (not deep_evaluated) or deep_quiet

    now_check = now_utc()
    pre_alert_fp_changed = (
        should_pre_alert_on_fingerprint(cfg)
        and fp != state.last_panel_fingerprint
        and state.last_panel_fingerprint != ""
        and state.empty_state_seen
        and not blocked_dash
        and not should_send
    )
    pre_alert_sent = False
    if pre_alert_fp_changed:
        cooldown_ok = True
        if cfg.pre_alert_cooldown_seconds > 0 and state.last_pre_alert_at_iso:
            try:
                last_pa = datetime.fromisoformat(state.last_pre_alert_at_iso.replace("Z", "+00:00"))
                if last_pa.tzinfo is None:
                    last_pa = last_pa.replace(tzinfo=UTC)
                elapsed = (now_check - last_pa.astimezone(UTC)).total_seconds()
                cooldown_ok = elapsed >= float(cfg.pre_alert_cooldown_seconds)
            except Exception:
                cooldown_ok = True
        if cooldown_ok and fp != state.last_pre_alert_fingerprint:
            log.info("Shift page fingerprint changed — sending Heads up pre-alert.")

            def _href_esc(u: str) -> str:
                return html_escape(u).replace('"', "&quot;")

            fp_caption = (
                "🔔 <b>Heads up — shift page changed</b>\n\n"
                "Something on your Amazon shifts page changed before slots opened. Check now:\n\n"
                f'<a href="{_href_esc(schedule_url)}">{html_escape(schedule_url)}</a>\n\n'
                f"Time (UK): {html_escape(format_alert_time_uk(now_check, tz_name))}"
            )
            if not (dry_run or settings.dry_run) and settings.telegram_bot_token:
                try:
                    fp_chat_ids = await asyncio.to_thread(
                        _load_shift_alert_recipient_chat_ids,
                        settings.sqlite_path,
                        settings.telegram_chat_id,
                    )
                    for cid in fp_chat_ids:
                        async with TelegramNotifier(
                            bot_token=settings.telegram_bot_token,
                            chat_id=cid,
                            timeout_seconds=settings.http_timeout_seconds,
                        ) as t:
                            await t.send(TelegramMessage(text=fp_caption, disable_web_page_preview=False))
                    pre_alert_sent = True
                except Exception:
                    log.exception("Heads up pre-alert failed")
            else:
                log.info("[dry-run] Would send Heads up pre-alert:\n%s", fp_caption)
                pre_alert_sent = True

    pre_alert_fp = fp if pre_alert_sent else state.last_pre_alert_fingerprint
    pre_alert_at = now_check.isoformat() if pre_alert_sent else state.last_pre_alert_at_iso
    new_state = ShiftAlertPersistedState(
        last_was_empty=dashboard_empty,
        last_panel_fingerprint=fp,
        empty_state_seen=empty_seen,
        last_deep_was_empty=new_last_deep,
        deep_empty_seen=deep_seen,
        last_dashboard_had_empty_copy=next_ldhec,
        last_pre_alert_fingerprint=pre_alert_fp,
        last_pre_alert_at_iso=pre_alert_at,
    )

    if dry_run or settings.dry_run:
        out = Path("data") / f"shift_alert_dry_run_{datetime.now(tz=UTC).strftime('%Y%m%d_%H%M%S')}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(screenshot_bytes)
        log.info("[dry-run] Saved screenshot %s", out.resolve())
        if quiet_dashboard and deep_still_waiting:
            log.info("[dry-run] Dashboard + deep view still look empty / waiting.")
        elif not should_send:
            log.info(
                "[dry-run] Would skip Telegram (need prior empty copy on each surface you use, then transition)."
            )
        else:
            caption = build_shift_alert_caption(
                cfg, schedule_url, tz_name, now_utc(), watch_label=watch_display_name
            )
            log.info("[dry-run] Would send Telegram photo. Caption:\n%s", caption)
        if blocked_dash:
            dr = "blocked"
        elif quiet_dashboard and deep_still_waiting:
            dr = "no_shifts"
        else:
            dr = "dry_run_other"
        return _shift_check_result(state, False, "dry_run", dr, watch=active_watch)

    if not should_send:
        if quiet_dashboard and deep_still_waiting:
            save_shift_state(state_path, new_state)
            if blocked_dash:
                log.info(
                    "No Telegram send: block/WAF page this poll — will retry next interval (no shift alert)."
                )
                return _shift_check_result(
                    new_state,
                    False,
                    "blocked",
                    "CloudFront/WAF or error page",
                    watch=active_watch,
                )
            log.info("Shift page still shows empty-state text (no Telegram send).")
            return _shift_check_result(new_state, False, "no_shifts", "Empty-state copy matched", watch=active_watch)
        save_shift_state(state_path, new_state)
        log.info(
            "No Telegram send: no armed transition from configured empty / no-schedule copy to open slots."
        )
        return _shift_check_result(new_state, False, "shift_ui_unchanged", "", watch=active_watch)

    caption = build_shift_alert_caption(cfg, schedule_url, tz_name, now_utc(), watch_label=watch_display_name)

    if not settings.telegram_bot_token:
        log.error("Missing TELEGRAM_BOT_TOKEN for shift alert.")
        return _shift_check_result(state, False, "missing_bot_token", "", watch=active_watch)

    try:
        chat_ids = await asyncio.to_thread(
            _load_shift_alert_recipient_chat_ids,
            settings.sqlite_path,
            settings.telegram_chat_id,
        )
    except ValueError as e:
        log.error("%s", e)
        return _shift_check_result(state, False, "no_recipients", str(e), watch=active_watch)

    token = settings.telegram_bot_token
    burst_count = shift_alert_telegram_burst_count(cfg, high_confidence=deep_slots)
    sent_ok = await _send_shift_availability_telegram(
        token=token,
        chat_ids=chat_ids,
        screenshot_bytes=screenshot_bytes,
        caption=caption,
        timeout_seconds=settings.http_timeout_seconds,
        repeat_count=burst_count,
        repeat_delay_ms=cfg.high_confidence_alert_repeat_delay_ms if deep_slots else 0,
    )

    if sent_ok == 0:
        log.error("Shift alert failed for every recipient; dedupe state unchanged — next poll may retry.")
        return _shift_check_result(state, False, "telegram_send_failed", "", watch=active_watch)

    if burst_count > 1:
        log.info(
            "High-confidence shift alert: sent %d consecutive photo(s) to %d/%d Telegram chat(s).",
            burst_count,
            sent_ok,
            len(chat_ids),
        )
    else:
        log.info(
            "Sent shift availability alert to %d/%d Telegram chat(s).",
            sent_ok,
            len(chat_ids),
        )
    drop_at = now_utc()
    loc_key = resolve_shift_location_key(
        alert_location=cfg.alert_location,
        watch_label=watch_display_name,
        profile_id=profile_id,
    )
    await asyncio.to_thread(
        _record_successful_shift_drop,
        settings.sqlite_path,
        location_key=loc_key,
        profile_id=profile_id,
        dropped_at_utc=drop_at,
        tz_name=tz_name,
        dashboard_slots=dashboard_slots,
        deep_slots=deep_slots,
    )
    if deep_slots and not dashboard_slots:
        log.info("Alert triggered from post-Select Shift screen (My jobs still showed empty copy).")
    alerted_state = ShiftAlertPersistedState(
        last_was_empty=False,
        last_panel_fingerprint=fp,
        empty_state_seen=True,
        last_deep_was_empty=new_last_deep,
        deep_empty_seen=True,
        last_dashboard_had_empty_copy=False,
    )
    save_shift_state(state_path, alerted_state)
    return _shift_check_result(
        alerted_state,
        True,
        "alert_sent",
        f"{sent_ok}/{len(chat_ids)} chats",
        watch=active_watch,
    )


async def cmd_test_photo(
    settings: Settings,
    file_cfg: FileConfig,
    *,
    image_path: str | None,
    profile_id_filter: str | None,
) -> int:
    tz_name = file_cfg.project.timezone
    cfg = file_cfg.shift_alert
    profiles = resolve_watch_profiles(
        cfg,
        storage_override=None,
        state_override=None,
        profile_id_filter=profile_id_filter,
    )
    if not profiles:
        log.error(
            "No shift profile to preview: set shift_alert.schedule_url (and session path for run), "
            "or add shift_alert.profiles, or fix --profile."
        )
        return 2
    rp = profiles[0]
    if len(profiles) > 1 and not profile_id_filter:
        log.info(
            "Multiple enabled profiles — test caption uses first: %s (%s). Use --profile ID for another.",
            rp.display_name,
            rp.id,
        )

    if image_path:
        p = Path(image_path)
        if not p.is_file():
            log.error("Image path is not a file: %s", p.resolve())
            return 2
        photo_bytes = p.read_bytes()
        fname = p.name
    else:
        photo_bytes = base64.b64decode(_MIN_TEST_PNG_B64)
        fname = "shift_alert_test.png"

    real_caption = build_shift_alert_caption(
        cfg, rp.schedule_url, tz_name, now_utc(), watch_label=rp.display_name
    )
    caption = (
        "Preview only — not a real shift opening.\n\n"
        + real_caption
    )
    if len(caption) > TELEGRAM_CAPTION_MAX:
        caption = caption[: TELEGRAM_CAPTION_MAX - 3] + "..."

    if settings.dry_run:
        log.info("[dry-run] Would send test photo (%s bytes) with caption:\n%s", len(photo_bytes), caption)
        return 0

    if not settings.telegram_bot_token:
        log.error("Missing TELEGRAM_BOT_TOKEN.")
        return 2

    try:
        chat_ids = await asyncio.to_thread(
            _load_shift_alert_recipient_chat_ids,
            settings.sqlite_path,
            settings.telegram_chat_id,
        )
    except ValueError as e:
        log.error("%s", e)
        return 2

    token = settings.telegram_bot_token
    ok = 0
    for cid in chat_ids:
        try:
            async with TelegramNotifier(bot_token=token, chat_id=cid, timeout_seconds=settings.http_timeout_seconds) as t:
                await t.send_photo(
                    TelegramPhoto(
                        photo_bytes=photo_bytes,
                        filename=fname,
                        caption=caption,
                        parse_mode=None,
                    )
                )
            ok += 1
        except Exception:
            log.exception("test-photo failed for chat_id=%s", cid)

    log.info("test-photo delivered to %d/%d chat(s).", ok, len(chat_ids))
    return 0 if ok else 2


async def _send_shift_alert_heartbeat(
    settings: Settings,
    *,
    last_cycle_iso: str | None,
    tz_name: str,
) -> None:
    token = settings.telegram_bot_token
    cid = settings.telegram_chat_id
    if not token or not cid:
        return
    local = format_alert_time_uk(now_utc(), tz_name)
    text = (
        "💓 Shift alerts are still running\n"
        f"Last check: {last_cycle_iso or 'not yet'}\n"
        f"Time (UK): {local}\n"
        f"Sending to Telegram: {'no (preview mode)' if settings.dry_run else 'yes'}"
    )
    async with TelegramNotifier(bot_token=token, chat_id=cid, timeout_seconds=settings.http_timeout_seconds) as t:
        await t.send(TelegramMessage(text=text, disable_web_page_preview=True))


async def _shift_alert_heartbeat_loop(
    settings: Settings,
    last_cycle: dict[str, str | None],
    tz_name: str,
) -> None:
    interval = max(60.0, float(settings.heartbeat_interval_hours) * 3600.0)
    while True:
        await asyncio.sleep(interval)
        try:
            await _send_shift_alert_heartbeat(settings, last_cycle_iso=last_cycle.get("t"), tz_name=tz_name)
        except Exception:
            log.exception("Shift alert heartbeat failed")


async def _safe_browser_close(browser: Any) -> None:
    try:
        await browser.close()
    except Exception as e:
        msg = str(e).lower()
        if "connection closed" in msg or "browser has been closed" in msg or "target closed" in msg:
            log.warning("Browser already disconnected during shutdown (%s); ignoring.", type(e).__name__)
        else:
            log.warning("Browser.close failed during shutdown (%s): %s", type(e).__name__, e)


async def cmd_run(
    settings: Settings,
    file_cfg: FileConfig,
    *,
    once: bool,
    ignore_window: bool,
    storage_override: str | Path | None,
    state_override: str | Path | None,
    profile_id_filter: str | None,
) -> int:
    cfg = file_cfg.shift_alert
    if not cfg.enabled:
        log.error("shift_alert.enabled is false in config.yaml — enable it for this watcher.")
        return 2
    if cfg.poll_interval_min_seconds < 1 or cfg.poll_interval_max_seconds < cfg.poll_interval_min_seconds:
        log.error("Invalid poll interval settings (use min >= 1 second and max >= min).")
        return 2

    base_poll_min = cfg.poll_interval_min_seconds
    base_poll_max = cfg.poll_interval_max_seconds
    if settings.shift_alert_poll_interval_seconds is not None:
        base_poll_min = base_poll_max = settings.shift_alert_poll_interval_seconds
        log.info(
            "SHIFT_ALERT_POLL_SECONDS=%s — fixed delay between full cycles (overrides YAML min/max).",
            base_poll_min,
        )

    stor_ov = str(storage_override).strip() if storage_override else None
    state_ov = str(state_override).strip() if state_override else None

    profiles = resolve_watch_profiles(
        cfg,
        storage_override=stor_ov,
        state_override=state_ov,
        profile_id_filter=profile_id_filter,
    )
    if not profiles:
        log.error(
            "No shift profiles to watch. Add shift_alert.profiles in config.yaml, "
            "or set schedule_url + session paths, or fix --profile."
        )
        return 2

    log.info("Watching %d shift profile(s).", len(profiles))
    for rp in profiles:
        log.info("  • %s (%s) -> %s", rp.display_name, rp.id, rp.storage_path)

    tz_name = file_cfg.project.timezone

    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as e:
        log.error("Playwright not installed (%s)", e)
        return 2

    last_cycle: dict[str, str | None] = {"t": None}

    async with async_playwright() as pw:
        if cfg.playwright_executable_path:
            log.info("Using Playwright executable_path=%s", cfg.playwright_executable_path)
        elif cfg.playwright_browser_channel:
            log.info("Using Playwright channel=%s (installed browser)", cfg.playwright_browser_channel)
        browser: Any = None
        watch_handles: dict[str, ShiftWatchHandle] = {}
        hb_task: asyncio.Task | None = None
        peak_task: asyncio.Task | None = None
        exit_code = 0
        try:
            if (
                settings.heartbeat_interval_hours > 0
                and settings.telegram_bot_token
                and settings.telegram_chat_id
            ):
                hb_task = asyncio.create_task(
                    _shift_alert_heartbeat_loop(settings, last_cycle, tz_name)
                )
            if cfg.peak_prediction_enabled and settings.telegram_bot_token and settings.telegram_chat_id:
                peak_task = asyncio.create_task(
                    _shift_peak_prediction_loop(settings, file_cfg),
                    name="shift_peak_prediction",
                )
            while True:
                if browser is not None:
                    await _safe_browser_close(browser)
                    browser = None
                try:
                    browser = await launch_shift_chromium(pw.chromium, cfg)
                except Exception as e:
                    log.error("%s", _LAUNCH_FAILURE_HINT)
                    log.error("Last launch error: %s: %s", type(e).__name__, e)
                    return 2
                watch_handles.clear()
                log.info(
                    "Shift alert: Chromium ready — dashboard once, MutationObserver + health ping "
                    "(auto browser restart on crash)."
                )
                try:
                    await _shift_monitor_poll_loop(
                        settings=settings,
                        file_cfg=file_cfg,
                        cfg=cfg,
                        profiles=profiles,
                        browser=browser,
                        watch_handles=watch_handles,
                        last_cycle=last_cycle,
                        tz_name=tz_name,
                        base_poll_min=base_poll_min,
                        base_poll_max=base_poll_max,
                        ignore_window=ignore_window,
                        once=once,
                    )
                    exit_code = 0
                    break
                except ShiftBrowserRecoveryNeeded as e:
                    log.warning(
                        "Playwright browser session lost (%s). Closing browser, sleeping %ds, "
                        "then restarting with the same storage state.",
                        e,
                        _BROWSER_RECOVERY_SLEEP_SECONDS,
                    )
                    await _close_all_watch_handles(watch_handles)
                    await _safe_browser_close(browser)
                    browser = None
                    if once:
                        exit_code = 1
                        break
                    await asyncio.sleep(_BROWSER_RECOVERY_SLEEP_SECONDS)
                    log.info("Restarting Chromium after browser recovery...")
                    continue
        except asyncio.CancelledError:
            log.info("Shift watcher cancelled — shutting down.")
            raise
        finally:
            if hb_task is not None:
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass
            if peak_task is not None:
                peak_task.cancel()
                try:
                    await peak_task
                except asyncio.CancelledError:
                    pass
            await _close_all_watch_handles(watch_handles)
            await _safe_browser_close(browser)
        return exit_code


async def _shift_monitor_poll_loop(
    *,
    settings: Settings,
    file_cfg: FileConfig,
    cfg: ShiftAlertConfig,
    profiles: list[ResolvedWatchProfile],
    browser: Any,
    watch_handles: dict[str, ShiftWatchHandle],
    last_cycle: dict[str, str | None],
    tz_name: str,
    base_poll_min: int,
    base_poll_max: int,
    ignore_window: bool,
    once: bool,
) -> None:
    """Main monitoring loop; raises ShiftBrowserRecoveryNeeded on Playwright browser death."""
    while True:
        now = now_utc()
        if not ignore_window and not in_active_poll_window(now, tz_name, cfg):
            wait_s = 900 if not once else 0
            days = "Mon–Fri only" if cfg.active_weekdays_only else "all days"
            log.info(
                "Outside active poll window (%s, local %02d:00–%02d:00). Sleeping %ss. "
                "(Use --ignore-window to check now, or adjust shift_alert.active_* in config.yaml.)",
                days,
                cfg.active_hour_start,
                cfg.active_hour_end,
                wait_s,
            )
            if once:
                return
            await asyncio.sleep(wait_s)
            continue

        cycle_poll_min, cycle_poll_max, turbo_active = effective_poll_intervals(
            now, tz_name, cfg, base_poll_min, base_poll_max
        )
        if turbo_active:
            log.info(
                "Turbo mode active — poll interval %d–%ds (drop window in %s).",
                cycle_poll_min,
                cycle_poll_max,
                tz_name,
            )

        for rp in profiles:
            age_h = session_age_hours(rp.storage_path)
            if (
                cfg.session_refresh_hours > 0
                and age_h is not None
                and age_h >= float(cfg.session_refresh_hours)
            ):
                stale = watch_handles.pop(rp.id, None)
                if stale is not None:
                    await _close_shift_watch_handle(stale)
            await refresh_playwright_session_if_stale(
                browser,
                cfg=cfg,
                file_cfg=file_cfg,
                storage_path=rp.storage_path,
                schedule_url=rp.schedule_url,
            )
            state = load_shift_state(rp.state_path)
            handle = watch_handles.get(rp.id)
            for attempt in range(2):
                result = await _check_once(
                    settings=settings,
                    file_cfg=file_cfg,
                    cfg=cfg,
                    state=state,
                    dry_run=bool(settings.dry_run),
                    storage_path=rp.storage_path,
                    state_path=rp.state_path,
                    schedule_url=rp.schedule_url,
                    watch_display_name=rp.display_name,
                    browser=browser,
                    watch=handle,
                    profile_id=rp.id,
                    poll_min_seconds=cycle_poll_min,
                    poll_max_seconds=cycle_poll_max,
                )
                handle = result.watch
                if handle is not None:
                    watch_handles[rp.id] = handle
                else:
                    watch_handles.pop(rp.id, None)
                state = result.state
                if result.outcome not in ("navigation_failed", "page_not_ready", "blocked"):
                    break
                if attempt == 0:
                    log.warning(
                        "Poll attempt 1 failed (outcome=%s) — retrying in 10s before next full cycle.",
                        result.outcome,
                    )
                    await asyncio.sleep(10)
            await asyncio.sleep(max(0, int(cfg.profile_stagger_seconds)))

        last_cycle["t"] = now_utc().isoformat()

        if once:
            return

        turbo_now = resolve_active_turbo_window(now_utc(), tz_name, cfg) is not None
        if cfg.dom_watch_enabled:
            delay = max(0, int(cfg.profile_stagger_seconds))
            if delay:
                log.info(
                    "DOM watch cycle complete — stagger %ds before next profile pass%s.",
                    delay,
                    " [turbo]" if turbo_now else "",
                )
                await asyncio.sleep(delay)
        else:
            cycle_min, cycle_max, _ = effective_poll_intervals(
                now_utc(), tz_name, cfg, base_poll_min, base_poll_max
            )
            delay = random.randint(cycle_min, cycle_max)
            if delay >= 60:
                log.info(
                    "Next full cycle in %ds (~%d min)%s.",
                    delay,
                    delay // 60,
                    " [turbo]" if turbo_now else "",
                )
            else:
                log.info("Next full cycle in %ds%s.", delay, " [turbo]" if turbo_now else "")
            await asyncio.sleep(delay)


def _add_session_path_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument(
        "--storage-state",
        dest="storage_state",
        metavar="PATH",
        help="Playwright session JSON path (per Amazon login). Same path must be used on run.",
    )
    ap.add_argument(
        "--state-path",
        dest="state_path",
        metavar="PATH",
        help="Dedupe state JSON path (optional). Default: next to --storage-state or config shift_alert.state_path",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amazon-shift-self-service-alert",
        description="Poll Amazon UK jobsatamazon shift UI (My jobs dashboard or self-service schedule URL).",
        epilog='Example: python -m app.shift_alert login   (use the word "login", not -login)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    login_p = sub.add_parser("login", help="Open a browser to sign in and save a Playwright session file")
    _add_session_path_args(login_p)
    login_p.add_argument("--profile", metavar="ID", dest="profile_id", help="Use paths from shift_alert.profiles")
    run_p = sub.add_parser(
        "run",
        help="Poll My jobs / schedule URL; Telegram when configured \"no shifts\" copy disappears after you saw it once",
    )
    _add_session_path_args(run_p)
    run_p.add_argument(
        "--once",
        action="store_true",
        help="Single check then exit (still respects active window unless --ignore-window)",
    )
    run_p.add_argument(
        "--ignore-window",
        action="store_true",
        help="Poll even outside configured active hours / weekday filter (for testing)",
    )
    run_p.add_argument(
        "--profile",
        metavar="ID",
        dest="profile_id",
        help="Only watch this shift_alert.profiles id (multi-profile config)",
    )
    test_p = sub.add_parser(
        "test-photo",
        help="Send a test photo with the same caption format as a real shift alert (plus a TEST banner)",
    )
    test_p.add_argument(
        "--image",
        metavar="PATH",
        dest="test_image",
        help="PNG/JPEG file to send (optional; default is a tiny placeholder image)",
    )
    test_p.add_argument(
        "--profile",
        metavar="ID",
        dest="profile_id",
        help="Which shift_alert.profiles row to use for schedule_url / display_name in the preview caption",
    )
    return p


def normalize_shift_alert_argv(argv: list[str] | None) -> list[str] | None:
    if argv is None:
        return None
    out = list(argv)
    if not out:
        return out
    aliases = {
        "-login": "login",
        "--login": "login",
        "-run": "run",
        "--run": "run",
        "-test-photo": "test-photo",
        "--test-photo": "test-photo",
    }
    key = out[0].lower()
    if key in aliases:
        out[0] = aliases[key]
    return out


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = normalize_shift_alert_argv(argv)
    args = build_arg_parser().parse_args(argv)
    settings = Settings()
    setup_logging(settings.log_level, redact_telegram_token=settings.telegram_bot_token)
    file_cfg = load_file_config(settings.config_path)

    storage_override, state_override = merge_storage_overrides(
        cli_storage=getattr(args, "storage_state", None),
        cli_state=getattr(args, "state_path", None),
        env_storage=settings.shift_alert_storage_state,
        env_state=settings.shift_alert_state_path,
    )

    if args.cmd == "login":
        lp = getattr(args, "profile_id", None)
        lp = str(lp).strip() if lp else None
        return asyncio.run(
            cmd_login(
                file_cfg,
                storage_override=storage_override,
                state_override=state_override,
                profile_id=lp or None,
            )
        )

    if args.cmd == "run":
        once = bool(getattr(args, "once", False))
        ignore_window = bool(getattr(args, "ignore_window", False))
        pid = getattr(args, "profile_id", None)
        pid = str(pid).strip() if pid else None
        try:
            return asyncio.run(
                cmd_run(
                    settings,
                    file_cfg,
                    once=once,
                    ignore_window=ignore_window,
                    storage_override=storage_override,
                    state_override=state_override,
                    profile_id_filter=pid or None,
                )
            )
        except KeyboardInterrupt:
            log.info("Shift watcher stopped (Ctrl+C).")
            return 0

    if args.cmd == "test-photo":
        img = getattr(args, "test_image", None)
        img = str(img).strip() if img else None
        tpid = getattr(args, "profile_id", None)
        tpid = str(tpid).strip() if tpid else None
        return asyncio.run(
            cmd_test_photo(
                settings,
                file_cfg,
                image_path=img or None,
                profile_id_filter=tpid or None,
            )
        )

    return 2


if __name__ == "__main__":
    raise SystemExit(main())