from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import httpx
import json

from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

# Read-only dashboard (separate process): uvicorn app.dashboard:app --host 127.0.0.1 --port 8080
from app.config import BehaviorConfig, FileConfig, Settings, load_file_config
from app.fetchers import HttpFetcher, PlaywrightFetcher, PlaywrightNotInstalledError
from app.models.job import JobListing
from app.notifiers import TelegramNotifier
from app.notifiers.telegram import TelegramMessage
from app.parsers import AmazonJobsSearchParser, JobsAtAmazonSearchParser
from app.storage.sqlite import SqliteStore
from app.stripe_webhook import run_stripe_webhook_server
from app.subscriber_bot import run_subscriber_bot
from app.utils.filters import passes_filters
from app.utils.alert_locations import subscriber_accepts_job_alert
from app.utils.amazon_jobs_urls import search_page_url_to_json_api_url
from app.utils.jobsatamazon_network import enrich_listing, extract_from_network_json
from app.utils.logging import setup_logging
from app.utils.text import (
    canonicalize_url,
    clean_description_plain,
    is_garbled_scraped_line,
    normalize_whitespace,
    stable_hash,
)
from app.utils.time import ensure_utc, format_dt, now_utc

log = logging.getLogger(__name__)


def build_parser() -> AmazonJobsSearchParser:
    return AmazonJobsSearchParser()

def build_jobsatamazon_parser() -> JobsAtAmazonSearchParser:
    return JobsAtAmazonSearchParser()


def _expected_pay_text(settings: Settings) -> str | None:
    lo = settings.default_expected_pay_gbp_per_hour_min
    hi = settings.default_expected_pay_gbp_per_hour_max
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        return f"£{lo:.2f}–£{hi:.2f}/hr (approx.)"
    if lo is not None:
        return f"£{lo:.2f}+/hr (approx.)"
    return f"Up to £{hi:.2f}/hr (approx.)"  # type: ignore[arg-type]


def _job_key(job: JobListing) -> str:
    """
    Dedupe across sources: the same Amazon listing often appears on both ``amazon.jobs`` search
    and ``jobsatamazon.co.uk`` with the same ``job_id`` — one key avoids duplicate Telegram alerts.
    """
    jid = (job.job_id or "").strip()
    if jid:
        return f"jid:{jid.casefold()}"
    return f"url:{stable_hash(job.url)}"


def dedupe_job_listings(jobs: list[JobListing]) -> list[JobListing]:
    """
    Collapse the same logical job when it appears from multiple configured source URLs.
    Prefers ``jobsatamazon.co.uk`` rows (richer apply / network metadata) over ``amazon.jobs``.
    """
    by_jid: dict[str, JobListing] = {}
    by_url: dict[str, JobListing] = {}
    for j in jobs:
        jid = (j.job_id or "").strip()
        if jid:
            k = jid.casefold()
            cur = by_jid.get(k)
            if cur is None:
                by_jid[k] = j
            elif j.source == "jobsatamazon.co.uk" and cur.source != "jobsatamazon.co.uk":
                by_jid[k] = j
            continue
        u = (j.url or "").strip()
        if not u:
            continue
        if u not in by_url:
            by_url[u] = j
    return list(by_jid.values()) + list(by_url.values())


def _suppress_telegram_for_apply_policy(job: JobListing, behavior: BehaviorConfig) -> bool:
    """
    For jobsatamazon detail listings, avoid spamming Telegram when Apply is disabled or unknown.
    Public amazon.jobs search hits are still notified (those listings do not carry apply_enabled).
    """
    if not behavior.alert_only_when_apply_enabled:
        return False
    if job.source != "jobsatamazon.co.uk":
        return False
    return job.raw_metadata.get("apply_enabled") != "true"


def _content_hash(job: JobListing) -> str:
    meta = job.raw_metadata or {}
    canonical_meta: dict[str, str] = {}
    for k in (
        "Work address",
        "Employment type",
        "Schedule",
        "Hours/Week",
        "Openings",
        "Pay rate",
        "Job status",
        "Postcode",
        "apply_enabled",
    ):
        v = meta.get(k)
        if v is not None:
            norm_v = normalize_whitespace(str(v))
            if norm_v and norm_v.upper() not in ("N/A", "TBC", "—", "-") and not is_garbled_scraped_line(norm_v):
                canonical_meta[k] = norm_v

    parts = {
        "job_id": (job.job_id or "").strip(),
        "url": canonicalize_url(job.url),
        "title": normalize_whitespace(job.title or ""),
        "location": normalize_whitespace(job.location or ""),
        "pay": str(job.pay_gbp_per_hour or ""),
        "pay_text": normalize_whitespace(job.pay_text or ""),
        "shift": normalize_whitespace(job.shift or ""),
        "posted": normalize_whitespace(job.posted_date_text or ""),
        "meta": canonical_meta,
    }
    return stable_hash(json.dumps(parts, sort_keys=True, ensure_ascii=False))


def _fmt_alert_value(s: str | None) -> str | None:
    """Return text for Telegram line, or None to omit the line."""
    if s is None:
        return None
    v = normalize_whitespace(str(s))
    if not v or v.upper() in ("N/A", "TBC", "—", "-"):
        return None
    if is_garbled_scraped_line(v):
        return None
    return v


def _fmt(
    job: JobListing,
    *,
    tz_name: str,
    first_seen_utc,
    alert_at_utc,
    status: str,
    alert_header: str | None = None,
) -> str:
    title = job.title or "Unknown title"
    pay_fallback = job.pay_text or (
        f"£{job.pay_gbp_per_hour:.2f}/hr" if job.pay_gbp_per_hour is not None else None
    )
    location = (job.location or "").strip() or None
    alert_at = ensure_utc(alert_at_utc)
    first_tracked = ensure_utc(first_seen_utc)
    status_line = "New" if status == "new" else "Updated"

    meta = job.raw_metadata
    work_address = (meta.get("Work address") or meta.get("work_address") or "").strip() or None
    if not work_address:
        work_address = location

    employment = _fmt_alert_value(meta.get("Employment type")) or None
    schedule = _fmt_alert_value(meta.get("Schedule")) or _fmt_alert_value(job.shift)
    hours = _fmt_alert_value(meta.get("Hours/Week"))
    openings = (meta.get("Openings") or "").strip()
    first_day = _fmt_alert_value(job.posted_date_text)

    pay_display = _fmt_alert_value(meta.get("Pay rate")) or _fmt_alert_value(pay_fallback)

    desc_src = meta.get("Description")
    description = clean_description_plain(desc_src, min_len=20) if desc_src else None
    if description and len(description) > 450:
        description = description[:447].rstrip() + "..."

    job_status_m = _fmt_alert_value(meta.get("Job status"))
    postcode = _fmt_alert_value(meta.get("Postcode"))

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def href_esc(u: str) -> str:
        return esc(u).replace('"', "&quot;")

    title_line = esc(title)
    if openings:
        title_line = f"{title_line} | {esc(openings)}"

    blocks: list[str] = []
    if alert_header:
        blocks.append(esc(alert_header))

    loc_a = location or ""
    wa = work_address or ""
    postcode_suffix = f" {postcode}" if postcode else ""

    if loc_a and wa and normalize_whitespace(loc_a.casefold()) == normalize_whitespace(wa.casefold()):
        blocks.append(f"📍 {esc(loc_a)}{esc(postcode_suffix)}")
    else:
        if loc_a:
            blocks.append(f"📍 {esc(loc_a)}{esc(postcode_suffix)}")
        if wa and normalize_whitespace(wa.casefold()) != normalize_whitespace(loc_a.casefold()):
            blocks.append(f"📬 <b>Work address:</b> {esc(wa)}")

    blocks.append(f"🏷️ {title_line}")
    if employment:
        blocks.append(f"💼 {esc(employment)}")
    if description:
        blocks.append(f"💬 <b>Summary:</b> {esc(description)}")
    if pay_display:
        blocks.append(f"💰 {esc(pay_display)}")
    if first_day:
        blocks.append(f"📅 First Day: {esc(first_day)}")
    if schedule:
        blocks.append(f"⏰ Schedule: {esc(schedule)}")
    if hours:
        blocks.append(f"🕐 Hours/Week: {esc(hours)}")
    if job_status_m:
        blocks.append(f"📋 Listing: {esc(job_status_m)}")
    blocks.append(f"🆕 Update: {esc(status_line)}")
    blocks.append(f"🕰️ Time: {esc(format_dt(alert_at, tz_name))}")
    if status == "updated" and (alert_at - first_tracked).total_seconds() > 90:
        blocks.append(f"📌 Listed since: {esc(format_dt(first_tracked, tz_name))}")
    blocks.append("")
    blocks.append(f'<a href="{href_esc(job.url)}">{esc(job.url)}</a>')
    return "\n".join(blocks)


async def _fetch_parse_one(
    *,
    fetcher: HttpFetcher,
    parser: AmazonJobsSearchParser,
    url: str,
    max_items: int,
    expected_pay_text: str | None,
    snapshots_dir: Path,
    save_snapshots_on_parse_issues: bool,
) -> list[JobListing]:
    fetch_url = search_page_url_to_json_api_url(url) or url
    if fetch_url != url:
        log.debug("Using amazon.jobs search.json endpoint for source %s", url)

    body = await fetcher.get_text(fetch_url)
    jobs = parser.parse(
        html=body,
        source_url=url,
        max_items=max_items,
        expected_pay_text=expected_pay_text,
    )
    if save_snapshots_on_parse_issues and len(jobs) == 0:
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        ts = now_utc().strftime("%Y%m%dT%H%M%SZ")
        ext = ".json" if fetch_url.rstrip("/").endswith(".json") else ".html"
        (snapshots_dir / f"parse_zero_{ts}{ext}").write_text(body, encoding="utf-8")
        log.warning("Parser returned 0 jobs; snapshot saved (%s)", ext.lstrip("."))
    return jobs


def _is_jobsatamazon(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return host.endswith("jobsatamazon.co.uk")


async def poll_once(settings: Settings) -> int:
    file_cfg = load_file_config(settings.config_path)
    expected_pay_text = _expected_pay_text(settings)
    parser = build_parser()
    ja_parser = build_jobsatamazon_parser()

    store = SqliteStore(settings.sqlite_path)
    snapshots_dir = Path("data") / "snapshots"

    urls = list(dict.fromkeys(file_cfg.sources.urls))
    if not urls:
        log.error("No source URLs configured in config.yaml")
        return 0

    sent = 0
    polled_successfully: set[str] = set()
    sem = asyncio.Semaphore(max(1, settings.http_concurrency))

    async with HttpFetcher(timeout_seconds=settings.http_timeout_seconds) as fetcher:
        pw_fetcher: PlaywrightFetcher | None = None
        if file_cfg.behavior.use_playwright_for_jobsatamazon and any(_is_jobsatamazon(u) for u in urls):
            try:
                pw_fetcher = await PlaywrightFetcher().__aenter__()
            except PlaywrightNotInstalledError:
                log.warning(
                    "jobsatamazon.co.uk URL configured but Playwright not installed. "
                    "Install optional deps:\n"
                    "  pip install -r requirements-playwright.txt\n"
                    "  python -m playwright install chromium"
                )
                pw_fetcher = None

        async def worker(source_url: str) -> list[JobListing]:
            async with sem:
                try:
                    if _is_jobsatamazon(source_url) and file_cfg.behavior.use_playwright_for_jobsatamazon:
                        if not pw_fetcher:
                            return []
                        if file_cfg.behavior.capture_jobsatamazon_network_json:
                            html, captures = await pw_fetcher.get_rendered_html_with_json_captures(source_url)
                            log.debug("Captured %d JSON payloads for jobsatamazon URL %s", len(captures), source_url)
                        else:
                            html = await pw_fetcher.get_rendered_html(source_url)
                            captures = []
                        jobs = ja_parser.parse(
                            html=html,
                            source_url=source_url,
                            max_items=file_cfg.behavior.max_items_per_source,
                            expected_pay_text=expected_pay_text,
                        )
                        if captures and jobs:
                            ext = extract_from_network_json(captures)
                            jobs = [enrich_listing(j, ext) for j in jobs]
                        if file_cfg.behavior.save_snapshots_on_parse_issues and len(jobs) == 0:
                            snapshots_dir.mkdir(parents=True, exist_ok=True)
                            ts = now_utc().strftime("%Y%m%dT%H%M%SZ")
                            (snapshots_dir / f"parse_zero_jobsatamazon_{ts}.html").write_text(html, encoding="utf-8")
                            log.warning("jobsatamazon parser returned 0 jobs; snapshot saved.")
                        if len(jobs) > 0:
                            polled_successfully.add(source_url)
                        return jobs

                    res = await _fetch_parse_one(
                        fetcher=fetcher,
                        parser=parser,
                        url=source_url,
                        max_items=file_cfg.behavior.max_items_per_source,
                        expected_pay_text=expected_pay_text,
                        snapshots_dir=snapshots_dir,
                        save_snapshots_on_parse_issues=file_cfg.behavior.save_snapshots_on_parse_issues,
                    )
                    polled_successfully.add(source_url)
                    return res
                except Exception as e:
                    if file_cfg.behavior.save_snapshots_on_parse_issues:
                        snapshots_dir.mkdir(parents=True, exist_ok=True)
                        ts = now_utc().strftime("%Y%m%dT%H%M%SZ")
                        (snapshots_dir / f"fetch_error_{ts}.txt").write_text(f"{source_url}\n{repr(e)}", encoding="utf-8")
                    log.exception("Failed to fetch/parse source: %s", source_url)
                    return []

        try:
            results: list[list[JobListing]] = await asyncio.gather(*(worker(u) for u in urls))
        finally:
            if pw_fetcher:
                await pw_fetcher.__aexit__(None, None, None)

    all_jobs: list[JobListing] = [j for sub in results for j in sub]
    before_dedupe = len(all_jobs)
    all_jobs = dedupe_job_listings(all_jobs)
    if len(all_jobs) != before_dedupe:
        log.info("[poll] De-duplicated listings: %d -> %d (same job_id / URL from multiple sources)", before_dedupe, len(all_jobs))

    filtered_out = 0
    filter_reason_hits: Counter[str] = Counter()
    past_filters = 0
    db_new = 0
    db_updated = 0
    db_unchanged = 0
    apply_suppressed = 0
    skipped_quiet = 0  # past filters + apply OK, but unchanged (or updates muted)

    # Apply filters and persist/alert.
    now = now_utc()
    send_updates = file_cfg.behavior.alert_on_updates

    token: str | None = None
    chat_ids: list[str] = []
    location_prefs: dict[str, tuple[str, str | None]] = {}
    if not settings.dry_run:
        token = settings.telegram_bot_token
        if not token:
            raise ValueError("Missing TELEGRAM_BOT_TOKEN in environment")
        chat_ids = await asyncio.to_thread(store.get_subscribers_for_alerts)
        if not chat_ids:
            if settings.telegram_chat_id:
                chat_ids = [settings.telegram_chat_id]
            else:
                raise ValueError(
                    "No Telegram subscribers and TELEGRAM_CHAT_ID unset; use /start with the bot running or set TELEGRAM_CHAT_ID"
                )
        location_prefs = await asyncio.to_thread(store.get_subscriber_alert_prefs, chat_ids)

    async def send_job_alert(job: JobListing, text: str, *, is_update: bool = False) -> None:
        nonlocal sent
        if settings.dry_run:
            action_desc = "edit" if is_update else "send"
            log.info("[DRY-RUN] Would %s Telegram job alert:\n%s", action_desc, text)
            sent += 1
            return
        assert token
        job_k = _job_key(job)
        for cid in chat_ids:
            choice, msubstr = location_prefs.get(cid, ("all", None))
            if not subscriber_accepts_job_alert(job, choice, msubstr, file_cfg.alert_locations):
                continue

            existing_alert = await asyncio.to_thread(store.get_sent_alert, job_k, cid)

            # If this is an update and we already sent an alert to this chat, edit in-place
            if is_update and existing_alert:
                msg_id = existing_alert["message_id"]
                try:
                    async with TelegramNotifier(
                        bot_token=token, chat_id=cid, timeout_seconds=settings.http_timeout_seconds
                    ) as t:
                        await t.edit(message_id=msg_id, new_text=text)
                    await asyncio.to_thread(store.record_sent_alert, job_k, cid, msg_id, now)
                    log.info("Edited existing Telegram alert in-place for %s (subscriber %s)", job_k, cid)
                    sent += 1
                    continue
                except Exception as edit_err:
                    log.warning("Failed to edit existing alert for %s, will send new message: %s", job_k, edit_err)

            # If marked as new but alert already active for this subscriber, avoid duplicate send
            if not is_update and existing_alert:
                log.debug("Alert for %s already active for subscriber %s, skipping duplicate send", job_k, cid)
                await asyncio.to_thread(store.reset_sent_alert_miss, job_k)
                continue

            try:
                async with TelegramNotifier(
                    bot_token=token, chat_id=cid, timeout_seconds=settings.http_timeout_seconds
                ) as t:
                    msg_id = await t.send(TelegramMessage(text=text, disable_web_page_preview=False))
                await asyncio.to_thread(store.record_sent_alert, job_k, cid, msg_id, now)
                sent += 1
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                log.warning("Failed to send alert to subscriber %s: HTTP %s", cid, status)
                if status in (400, 403):
                    try:
                        await asyncio.to_thread(store.set_subscriber_alerts_muted, cid, muted=True)
                        log.info("Automatically muted subscriber %s due to Telegram delivery failure (%s)", cid, status)
                    except Exception:
                        log.exception("Failed to auto-mute subscriber %s", cid)
            except Exception:
                log.exception("Unexpected error sending alert to subscriber %s", cid)


    for job in all_jobs:
        decision = passes_filters(job, file_cfg.filters)
        if not decision.allowed:
            filtered_out += 1
            for r in decision.reasons:
                filter_reason_hits[r] += 1
            continue

        past_filters += 1

        key = _job_key(job)
        chash = _content_hash(job)
        up = store.upsert_job(
            key=key,
            job_id=job.job_id,
            url=job.url,
            source=job.source,
            source_url=job.source_url,
            title=job.title,
            location=job.location,
            pay_gbp_per_hour=job.pay_gbp_per_hour,
            pay_text=job.pay_text,
            expected_pay_text=job.expected_pay_text,
            shift=job.shift,
            posted_date_text=job.posted_date_text,
            content_hash=chash,
            now_utc=now,
            raw_metadata_json=json.dumps(job.raw_metadata),
        )

        if up.status == "new":
            db_new += 1
        elif up.status == "updated":
            db_updated += 1
        else:
            db_unchanged += 1

        if _suppress_telegram_for_apply_policy(job, file_cfg.behavior):
            apply_suppressed += 1
            continue

        if up.status == "new" or (up.status == "updated" and send_updates):
            msg = _fmt(
                job,
                tz_name=file_cfg.project.timezone,
                first_seen_utc=up.first_seen_utc,
                alert_at_utc=now,
                status=up.status,
                alert_header=file_cfg.project.telegram_alert_header,
            )
            await send_job_alert(job, msg, is_update=(up.status == "updated"))
        else:
            skipped_quiet += 1

    rejects_detail = (
        "; ".join(f"{k}:{v}" for k, v in filter_reason_hits.most_common(12))
        if filter_reason_hits
        else "none"
    )
    reason_note = ""
    if filtered_out > 0 and filter_reason_hits:
        summed = sum(filter_reason_hits.values())
        if summed > filtered_out:
            reason_note = (
                f" (counts sum={summed}>{filtered_out} because one job may add "
                "several rejection tags)"
            )
    log.info(
        "[poll] Step 1 fetch: %d listing(s) from %d URL(s)",
        len(all_jobs),
        len(urls),
    )
    log.info(
        "[poll] Step 2 filters: rejected=%d%s | accepted=%d",
        filtered_out,
        reason_note,
        past_filters,
    )
    if filter_reason_hits:
        log.info("[poll]   reject tags: %s", rejects_detail)
    log.info(
        "[poll] Step 3 database: new=%d updated=%d unchanged=%d",
        db_new,
        db_updated,
        db_unchanged,
    )
    log.info(
        "[poll] Step 4 Telegram: apply_waiting=%d no_ping=%d message(s)_sent=%d",
        apply_suppressed,
        skipped_quiet,
        sent,
    )

    try:
        purged = await asyncio.to_thread(store.purge_old_jobs, days=30, now_utc=now)
        if purged > 0:
            log.info("[poll] Purged %d old job(s) from database", purged)
    except Exception:
        log.exception("Failed to purge old jobs from database")

    # Check for jobs that are no longer available (offline)
    if not settings.dry_run and token:
        try:
            active_alerts = await asyncio.to_thread(store.get_active_alerts)
            all_found_keys = {_job_key(j) for j in all_jobs}

            for alert in active_alerts:
                job_key = alert["job_key"]
                chat_id = alert["chat_id"]
                msg_id = alert["message_id"]

                # If job was found in this poll cycle, it is active
                if job_key in all_found_keys:
                    await asyncio.to_thread(store.reset_sent_alert_miss, job_key)
                    continue

                db_job = await asyncio.to_thread(store.get_job_by_key, job_key)
                if not db_job:
                    continue

                src_url = db_job.source_url
                if src_url in polled_successfully:
                    misses = await asyncio.to_thread(store.increment_sent_alert_miss, job_key, chat_id)
                    if misses < 2:
                        log.debug(
                            "Job %s missing from poll (%d/2 misses); debouncing offline mark",
                            job_key,
                            misses,
                        )
                        continue

                    row = await asyncio.to_thread(
                        lambda: store._conn.execute("SELECT first_seen_utc FROM jobs WHERE key = ?", (job_key,)).fetchone()
                    )
                    first_seen_val = None
                    if row:
                        try:
                            first_seen_val = datetime.fromisoformat(str(row["first_seen_utc"]).replace("Z", "+00:00"))
                        except Exception:
                            pass

                    first_seen_utc = first_seen_val or now

                    orig_text = _fmt(
                        db_job,
                        tz_name=file_cfg.project.timezone,
                        first_seen_utc=first_seen_utc,
                        alert_at_utc=now,
                        status="new",
                        alert_header=file_cfg.project.telegram_alert_header,
                    )

                    try:
                        sent_at = datetime.fromisoformat(alert["sent_at_utc"].replace("Z", "+00:00"))
                        if sent_at.tzinfo is None:
                            sent_at = sent_at.replace(tzinfo=UTC)
                        else:
                            sent_at = sent_at.astimezone(UTC)
                        duration_secs = int((now - sent_at).total_seconds())
                    except Exception:
                        duration_secs = 0

                    if duration_secs < 60:
                        dur_str = f"{max(1, duration_secs)} seconds"
                    elif duration_secs < 3600:
                        dur_str = f"{max(1, duration_secs // 60)} min"
                    else:
                        hrs = duration_secs // 3600
                        mins = (duration_secs % 3600) // 60
                        if mins > 0:
                            dur_str = f"{hrs} hr {mins} min"
                        else:
                            dur_str = f"{hrs} hour{'s' if hrs > 1 else ''}"

                    new_text = f"{orig_text}\n\n❌ No longer available (was live for {dur_str})"

                    try:
                        async with TelegramNotifier(
                            bot_token=token, chat_id=chat_id, timeout_seconds=settings.http_timeout_seconds
                        ) as t:
                            await t.edit(message_id=msg_id, new_text=new_text)
                        log.info("Edited alert message for job %s to mark as offline (live for %s)", job_key, dur_str)
                    except Exception as edit_err:
                        log.warning("Failed to edit alert message for job %s: %s", job_key, edit_err)

                    await asyncio.to_thread(store.delete_sent_alert, job_key, chat_id)
        except Exception:
            log.exception("Error checking/processing offline job alerts")

    store.close()
    return sent


async def _send_main_heartbeat(settings: Settings, last_poll_iso: str | None) -> None:
    token = settings.telegram_bot_token
    cid = settings.telegram_chat_id
    if not token or not cid:
        return
    text = (
        "💓 Job alerts are still running\n"
        f"Last check: {last_poll_iso or 'not yet'}\n"
        f"Sending to Telegram: {'no (preview mode)' if settings.dry_run else 'yes'}"
    )
    async with TelegramNotifier(bot_token=token, chat_id=cid, timeout_seconds=settings.http_timeout_seconds) as t:
        await t.send(TelegramMessage(text=text, disable_web_page_preview=True))


async def _main_heartbeat_loop(settings: Settings, last_poll: dict[str, str | None]) -> None:
    interval = max(60.0, float(settings.heartbeat_interval_hours) * 3600.0)
    while True:
        await asyncio.sleep(interval)
        try:
            await _send_main_heartbeat(settings, last_poll.get("t"))
        except Exception:
            log.exception("Main heartbeat Telegram send failed")


async def run_forever(settings: Settings) -> None:
    interval = max(5, int(settings.poll_interval_seconds))
    log.info("Starting polling loop (interval=%ss, dry_run=%s)", interval, settings.dry_run)

    token = settings.telegram_bot_token
    file_cfg = load_file_config(settings.config_path)

    last_poll: dict[str, str | None] = {"t": None}

    async def poll_loop() -> None:
        while True:
            try:
                sent = await poll_once(settings)
                last_poll["t"] = now_utc().isoformat()
                log.info("[poll] Done (this cycle). Telegram message(s) sent: %d", sent)
            except Exception:
                log.exception("Poll failed")
            await asyncio.sleep(interval)

    if settings.dry_run or not token:
        await poll_loop()
        return

    store = SqliteStore(settings.sqlite_path)
    try:
        coros = [
            poll_loop(),
            run_subscriber_bot(
                token,
                store,
                admin_chat_id=settings.telegram_chat_id,
                stats_timezone=file_cfg.project.timezone,
                notify_admin_on_message_reaction=file_cfg.behavior.notify_admin_on_message_reaction,
                subscription=file_cfg.subscription,
                fallback_location_names=[loc.name for loc in file_cfg.alert_locations],
                setlocation_exclude_substrings=file_cfg.setlocation.exclude_from_menu,
                payment_provider_token=settings.telegram_payment_provider_token,
                stripe_secret_key=settings.stripe_secret_key,
            ),
        ]
        if settings.heartbeat_interval_hours > 0 and settings.telegram_chat_id:
            coros.append(_main_heartbeat_loop(settings, last_poll))
        stripe_wh_secret = (settings.stripe_webhook_secret or "").strip()
        if stripe_wh_secret and file_cfg.subscription.uses_stripe_links():
            coros.append(
                run_stripe_webhook_server(
                    store=store,
                    bot_token=token,
                    subscription=file_cfg.subscription,
                    admin_chat_id=settings.telegram_chat_id,
                    stats_timezone=file_cfg.project.timezone,
                    webhook_secret=stripe_wh_secret,
                    host=settings.stripe_webhook_host,
                    port=settings.stripe_webhook_port,
                )
            )
            log.info(
                "Stripe webhook enabled on http://%s:%s/stripe/webhook",
                settings.stripe_webhook_host,
                settings.stripe_webhook_port,
            )
        elif file_cfg.subscription.uses_stripe_links():
            log.warning(
                "Stripe Payment Links active but STRIPE_WEBHOOK_SECRET is not set — "
                "users only get activated if they return to the bot after paying "
                "(Stripe redirect: https://t.me/BOT?start=paid_PLANID). "
                "Set STRIPE_WEBHOOK_SECRET for automatic confirmation."
            )
        await asyncio.gather(*coros)
    finally:
        store.close()


async def test_alert(settings: Settings) -> None:
    file_cfg = load_file_config(settings.config_path)
    tz_name = file_cfg.project.timezone
    expected_pay_text = _expected_pay_text(settings) or "£12.00–£14.50/hr (approx.)"
    fake = JobListing(
        source="amazon.jobs",
        source_url="https://www.amazon.jobs/en/search?...",
        job_id="0000000",
        url="https://www.amazon.jobs/en/jobs/0000000/example",
        title="Warehouse Operative",
        location="Bognor Regis, England (Bognor Regis)",
        pay_gbp_per_hour=None,
        pay_text="£14.30 /hr",
        expected_pay_text=expected_pay_text,
        shift=None,
        posted_date_text="2026-05-15",
        raw_metadata={
            "Description": "Pick, pack and ship parcels",
            "Employment type": "Seasonal | Part-time",
            "Schedule": "Fri, Sat, Sun, Mon, Tue 9:00 - 13:00",
            "Hours/Week": "20",
            "Openings": "1",
            "Work address": "Bognor Regis PO22 9FJ",
        },
    )
    token, chat_id = settings.require_telegram()
    now = now_utc()
    text = _fmt(
        fake,
        tz_name=tz_name,
        first_seen_utc=now,
        alert_at_utc=now,
        status="new",
        alert_header=file_cfg.project.telegram_alert_header,
    )
    async with TelegramNotifier(bot_token=token, chat_id=chat_id, timeout_seconds=settings.http_timeout_seconds) as t:
        await t.send(TelegramMessage(text=text, disable_web_page_preview=True))
    log.info("Sent test alert.")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amazon-shift-telegram-alert",
        epilog='If you run with no subcommand (e.g. py -m app.main), it defaults to "run".',
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="Run continuously (poll loop)")
    sub.add_parser("once", help="Run a single poll and exit")
    sub.add_parser("test-alert", help="Send a sample Telegram alert")
    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) == 0:
        argv = ["run"]

    args = build_arg_parser().parse_args(argv)
    settings = Settings()
    setup_logging(settings.log_level, redact_telegram_token=settings.telegram_bot_token)

    if args.cmd == "run":
        try:
            asyncio.run(run_forever(settings))
        except KeyboardInterrupt:
            log.info("Interrupted (Ctrl+C) — poller stopped.")
            return 0
        return 0
    if args.cmd == "once":
        sent = asyncio.run(poll_once(settings))
        log.info("[poll] Done (single run). Telegram message(s) sent: %d", sent)
        return 0
    if args.cmd == "test-alert":
        asyncio.run(test_alert(settings))
        return 0
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())

