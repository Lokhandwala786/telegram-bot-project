from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import sys

from app.config import ShiftAlertConfig, ShiftAlertProfile, ShiftAlertTurboWindow
from app.storage.sqlite import SqliteStore

from app.shift_alert import (
    CAPTION_LINE_URGENCY,
    DEFAULT_SHIFT_APPLICATION_DASHBOARD_URL,
    _deep_screen_signals,
    build_dom_watcher_eval_config,
    dom_observer_result_is_stale,
    dom_watch_timeout_ms,
    is_playwright_browser_crash,
    is_application_tracking_url,
    is_direct_schedule_picker_url,
    page_on_schedule_route,
    resolve_login_entry_url,
    resolve_shift_watch_url,
    _LOGIN_KEEPALIVE_INTERVAL_SECONDS,
    _load_shift_alert_recipient_chat_ids,
    _next_last_dashboard_had_empty_copy,
    blocked_page_match_reason,
    login_dashboard_looks_ready,
    load_shift_state,
    _login_persistent_context_kwargs,
    _LOGIN_SPA_STABILITY_INIT_SCRIPT,
    _POST_OTP_JOBS_LOGIN_CTA_RE,
    _already_on_app_dashboard_url,
    _jobsatamazon_login_bounce_url,
    _shift_page_corpus,
    _saw_hiring_auth_flow_url,
    build_shift_alert_caption,
    chromium_launch_attempt_variants,
    chromium_launch_options,
    chromium_launch_options_for_login,
    effective_poll_intervals,
    in_active_poll_window,
    is_direct_schedule_picker_url,
    merge_storage_overrides,
    resolve_active_turbo_window,
    resolve_login_entry_url,
    resolve_login_schedule_url,
    select_shift_click_budget,
    shift_alert_telegram_burst_count,
    should_pre_alert_on_fingerprint,
    normalize_shift_alert_argv,
    page_looks_blocked,
    page_looks_empty,
    page_looks_ready,
    resolve_shift_paths,
    resolve_watch_profiles,
)


def test_login_spa_stability_init_script_caps_reload_only() -> None:
    assert "location.reload" in _LOGIN_SPA_STABILITY_INIT_SCRIPT
    assert "visibilityState" not in _LOGIN_SPA_STABILITY_INIT_SCRIPT


def test_post_otp_url_heuristics() -> None:
    assert _saw_hiring_auth_flow_url("https://auth.hiring.amazon.com/x")
    assert _saw_hiring_auth_flow_url("https://www.amazon.co.uk/ap/cvf/display")
    assert not _saw_hiring_auth_flow_url("https://www.jobsatamazon.co.uk/login")
    assert _jobsatamazon_login_bounce_url("https://www.jobsatamazon.co.uk/login#/foo")
    assert _jobsatamazon_login_bounce_url("https://www.jobsatamazon.co.uk/")
    assert not _jobsatamazon_login_bounce_url("https://auth.hiring.amazon.com/")
    tgt = "https://www.jobsatamazon.co.uk/app#/myApplications"
    assert _already_on_app_dashboard_url("https://www.jobsatamazon.co.uk/app#/myApplications", tgt)
    assert not _already_on_app_dashboard_url("https://www.jobsatamazon.co.uk/login", tgt)


def test_post_otp_jobs_login_cta_regex_matches_button_label() -> None:
    assert _POST_OTP_JOBS_LOGIN_CTA_RE.search("Click here to login")
    assert _POST_OTP_JOBS_LOGIN_CTA_RE.search("CLICK HERE TO LOGIN")
    assert _POST_OTP_JOBS_LOGIN_CTA_RE.search("Log in to continue")


def test_login_dashboard_looks_ready() -> None:
    cfg = ShiftAlertConfig()
    tgt = "https://www.jobsatamazon.co.uk/app#/myApplications"
    ok_url = "https://www.jobsatamazon.co.uk/app#/myApplications"
    ok_corpus = "my jobs application no shifts available"
    assert login_dashboard_looks_ready(ok_url, ok_corpus, tgt, cfg) is True
    assert login_dashboard_looks_ready("https://www.jobsatamazon.co.uk/login", ok_corpus, tgt, cfg) is False


def test_normalize_shift_alert_argv_hyphen_login() -> None:
    assert normalize_shift_alert_argv(["-login"]) == ["login"]
    assert normalize_shift_alert_argv(["--login", "--storage-state", "x.json"]) == ["login", "--storage-state", "x.json"]
    assert normalize_shift_alert_argv(["-run", "--once"]) == ["run", "--once"]
    assert normalize_shift_alert_argv(["login"]) == ["login"]


def test_shift_alert_config_normalizes_jobsatamazon_apex_to_www() -> None:
    c = ShiftAlertConfig(
        login_url="https://jobsatamazon.co.uk/login#/x",
        schedule_url="https://jobsatamazon.co.uk/app#/y",
    )
    assert c.login_url.startswith("https://www.jobsatamazon.co.uk/")
    assert c.schedule_url.startswith("https://www.jobsatamazon.co.uk/")


def test_shift_alert_profile_schedule_normalizes_apex_to_www() -> None:
    p = ShiftAlertProfile(
        id="t",
        schedule_url="https://jobsatamazon.co.uk/app#/z",
        storage_state_path="data/t.json",
    )
    assert p.schedule_url.startswith("https://www.jobsatamazon.co.uk/")


def test_shift_alert_config_login_user_agent_default_empty() -> None:
    assert ShiftAlertConfig().login_user_agent == ""


def test_page_looks_empty_matches_configured_phrases() -> None:
    html = "Sorry, WE DO NOT HAVE ANY SCHEDULES MATCHING your preferences."
    assert page_looks_empty(html.lower(), ["we do not have any schedules matching"]) is True


def test_page_looks_empty_when_no_shifts_split_across_tags() -> None:
    html = "<div>Shift:</div><span>No shifts</span> available at this time."
    corpus = _shift_page_corpus(html)
    assert "no shifts available at this time" in corpus
    assert page_looks_empty(corpus, ["no shifts available at this time"]) is True


def test_page_looks_empty_raw_html_can_miss_when_tags_split_phrase() -> None:
    html = "<div>Shift:</div><span>No shifts</span> available at this time."
    raw_lower = html.lower()
    assert page_looks_empty(raw_lower, ["no shifts available at this time"]) is False


def test_page_looks_empty_when_no_match() -> None:
    html = "<div>Monday 06:00 - 14:00</div>"
    assert page_looks_empty(html.lower(), ["we do not have any schedules matching"]) is False


def test_page_looks_ready_any_marker() -> None:
    html = "<html><body>My jobs — Active (1)</body></html>"
    assert page_looks_ready(html.lower(), ["my jobs", "select shift"]) is True


def test_page_looks_ready_empty_list_is_true() -> None:
    assert page_looks_ready("".lower(), []) is True


def test_deep_screen_signals_skipped_when_not_evaluated() -> None:
    cfg = ShiftAlertConfig()
    b, ns, pos, quiet = _deep_screen_signals(cfg, "anything", deep_evaluated=False)
    assert (b, ns, pos, quiet) == (False, True, False, True)


def test_load_shift_state_migration_without_fingerprint_does_not_force_waiting(tmp_path: Path) -> None:
    p = tmp_path / "st.json"
    p.write_text(
        '{"empty_state_seen": true, "last_panel_fingerprint": "", "last_was_empty": false}\n',
        encoding="utf-8",
    )
    st = load_shift_state(p)
    assert st.last_dashboard_had_empty_copy is False


def test_shift_alert_config_fingerprint_alert_defaults_off() -> None:
    assert ShiftAlertConfig().alert_on_fingerprint_change is False


def test_load_shift_state_migrates_last_dashboard_waiting_when_key_missing(tmp_path: Path) -> None:
    """Old JSON: last_was_empty false but empty_state_seen true — do not block shift alerts forever."""
    p = tmp_path / "st.json"
    p.write_text(
        '{"deep_empty_seen": true, "empty_state_seen": true, "last_deep_was_empty": true, '
        '"last_panel_fingerprint": "x", "last_was_empty": false}\n',
        encoding="utf-8",
    )
    st = load_shift_state(p)
    assert st.last_dashboard_had_empty_copy is True


def test_next_last_dashboard_had_empty_copy_preserves_via_deep_no_schedule() -> None:
    assert (
        _next_last_dashboard_had_empty_copy(
            True,
            blocked_dash=False,
            has_empty_copy=False,
            deep_evaluated=True,
            deep_has_no_schedule=True,
            deep_positive=False,
        )
        is True
    )


def test_next_last_dashboard_had_empty_copy_clears_when_deep_shows_picker() -> None:
    assert (
        _next_last_dashboard_had_empty_copy(
            True,
            blocked_dash=False,
            has_empty_copy=False,
            deep_evaluated=True,
            deep_has_no_schedule=False,
            deep_positive=True,
        )
        is False
    )


def test_load_shift_state_arms_deep_when_dashboard_was_armed(tmp_path: Path) -> None:
    p = tmp_path / "st.json"
    p.write_text(
        '{"deep_empty_seen": false, "empty_state_seen": true, "last_deep_was_empty": true, '
        '"last_panel_fingerprint": "abc", "last_was_empty": true}\n',
        encoding="utf-8",
    )
    st = load_shift_state(p)
    assert st.empty_state_seen is True
    assert st.deep_empty_seen is True


def test_deep_screen_signals_no_schedule_and_positive() -> None:
    cfg = ShiftAlertConfig()
    html = (
        "<div>Current Schedule Information</div>"
        "<p>You currently do not have a schedule or start date selected.</p>"
    )
    corpus = _shift_page_corpus(html)
    b, ns, pos, quiet = _deep_screen_signals(cfg, corpus, deep_evaluated=True)
    assert b is False and ns is True and pos is False and quiet is True

    html2 = (
        "<div>Current Schedule Information</div>"
        "<button>Save and continue</button><span>Shift pattern</span>"
    )
    c2 = _shift_page_corpus(html2)
    b2, ns2, pos2, quiet2 = _deep_screen_signals(cfg, c2, deep_evaluated=True)
    assert b2 is False and ns2 is False and pos2 is True and quiet2 is False


def test_load_shift_alert_recipient_chat_ids_prefers_sqlite_subscribers(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    db = tmp_path / "subs.db"
    store = SqliteStore(str(db))
    try:
        now = datetime.now(UTC)
        store.activate_trial_subscription("111", trial_hours=24, now_utc=now)
        store.activate_trial_subscription("222", trial_hours=24, now_utc=now)
    finally:
        store.close()
    ids = _load_shift_alert_recipient_chat_ids(str(db), "999")
    assert ids == ["111", "222"]


def test_load_shift_alert_recipient_chat_ids_fallback_to_env_chat(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    SqliteStore(str(db)).close()
    ids = _load_shift_alert_recipient_chat_ids(str(db), "999888")
    assert ids == ["999888"]


def test_load_shift_alert_recipient_chat_ids_raises_without_subs_or_fallback(tmp_path: Path) -> None:
    db = tmp_path / "none.db"
    SqliteStore(str(db)).close()
    with pytest.raises(ValueError, match="TELEGRAM_CHAT_ID"):
        _load_shift_alert_recipient_chat_ids(str(db), None)


def test_blocked_page_match_reason_reports_hit() -> None:
    cfg = ShiftAlertConfig()
    corpus = _shift_page_corpus("<html>request could not be satisfied</html>")
    assert blocked_page_match_reason(corpus, cfg.blocked_page_substrings) == "request could not be satisfied"


def test_page_looks_blocked_cloudfront_403() -> None:
    cfg = ShiftAlertConfig()
    html = (
        "<html><title>403 ERROR</title>"
        "<body>Request blocked. Generated by cloudfront (CloudFront)</body></html>"
    )
    assert page_looks_blocked(html.lower(), cfg.blocked_page_substrings) is True


def test_403_error_alone_in_large_spa_not_blocked() -> None:
    """My jobs bundles may mention '403 error' without being a WAF page."""
    cfg = ShiftAlertConfig(
        blocked_page_substrings=["403 error", "generated by cloudfront", "request could not be satisfied"]
    )
    html = _shift_page_corpus(
        "<html><body>"
        + ("widget " * 800)
        + "api returned 403 error for analytics "
        + "my jobs select shift no shifts available application"
        + "</body></html>"
    )
    assert blocked_page_match_reason(html, cfg.blocked_page_substrings) is None
    assert page_looks_blocked(html, cfg.blocked_page_substrings) is False


def test_403_error_on_short_error_page_still_blocked() -> None:
    cfg = ShiftAlertConfig(blocked_page_substrings=["403 error", "generated by cloudfront"])
    html = _shift_page_corpus("<html><title>403 ERROR</title><body>403 error — forbidden</body></html>")
    assert blocked_page_match_reason(html, cfg.blocked_page_substrings) == "403 error"


def test_build_shift_alert_caption_includes_fixed_line_3_and_url() -> None:
    cfg = ShiftAlertConfig(
        alert_title="Amazon UK — shifts are open",
        schedule_url="https://example.com/shift",
    )
    fixed = datetime(2026, 5, 9, 7, 42, tzinfo=UTC)
    text = build_shift_alert_caption(cfg, cfg.schedule_url, "Europe/London", fixed)
    assert "Amazon UK — shifts are open" in text
    assert CAPTION_LINE_URGENCY in text
    assert "https://example.com/shift" in text
    assert "Tap here to select your shift:" in text


def test_build_shift_alert_caption_includes_alert_location() -> None:
    cfg = ShiftAlertConfig(alert_location="Coventry", schedule_url="https://example.com/s")
    t = datetime(2026, 5, 9, 7, 42, tzinfo=UTC)
    text = build_shift_alert_caption(cfg, "https://example.com/s", "Europe/London", t)
    assert "Location / site: Coventry" in text


def test_build_shift_alert_caption_watch_label_when_alert_location_empty() -> None:
    cfg = ShiftAlertConfig(schedule_url="https://example.com/s")
    t = datetime(2026, 5, 9, 7, 42, tzinfo=UTC)
    text = build_shift_alert_caption(
        cfg, "https://example.com/s", "Europe/London", t, watch_label="Profile · Nottingham"
    )
    assert "Location / site: Profile · Nottingham" in text


def test_build_shift_alert_caption_alert_location_beats_watch_label() -> None:
    cfg = ShiftAlertConfig(alert_location="Fixed site", schedule_url="https://example.com/s")
    t = datetime(2026, 5, 9, 7, 42, tzinfo=UTC)
    text = build_shift_alert_caption(
        cfg, "https://example.com/s", "Europe/London", t, watch_label="Ignored"
    )
    assert "Location / site: Fixed site" in text


def test_test_photo_caption_composition_matches_live_alert_body() -> None:
    """test-photo prepends a banner then the same string build_shift_alert_caption returns."""
    cfg = ShiftAlertConfig(
        schedule_url="https://example.com/sched",
        alert_title="Amazon UK — shifts are open",
        alert_location="Coventry",
    )
    t = datetime(2026, 5, 9, 7, 42, tzinfo=UTC)
    real = build_shift_alert_caption(cfg, cfg.schedule_url, "Europe/London", t)
    caption = (
        "TEST — This is not a real shift opening.\n"
        "Everything below matches the live Telegram caption when an alert fires.\n\n"
        + real
    )
    assert "Tap here to select your shift:" in caption
    assert "https://example.com/sched" in caption
    assert "Location / site: Coventry" in caption
    assert real in caption
    assert caption.index("TEST —") < caption.index("Amazon UK")


def test_in_active_poll_window_weekday_hours() -> None:
    cfg = ShiftAlertConfig(active_weekdays_only=True, active_hour_start=6, active_hour_end=18)
    london = ZoneInfo("Europe/London")
    monday_noon = datetime(2026, 1, 5, 12, 0, tzinfo=london)  # Monday 12:00 UK
    assert monday_noon.weekday() == 0
    assert in_active_poll_window(monday_noon, "Europe/London", cfg) is True


def test_chromium_launch_options_for_login_omits_ignore_default_args() -> None:
    """Interactive login avoids ``ignore_default_args`` so hiring forms accept clicks / typing."""
    cfg = ShiftAlertConfig(
        playwright_browser_channel="chrome",
        playwright_launch_args=["--foo"],
    )
    kw = chromium_launch_options_for_login(cfg)
    assert kw["headless"] is False
    assert kw["channel"] == "chrome"
    assert "--disable-blink-features=AutomationControlled" in kw["args"]
    assert "ignore_default_args" not in kw
    assert "--foo" in kw["args"]
    assert "--disable-backgrounding-occluded-windows" in kw["args"]
    assert "--disable-renderer-backgrounding" in kw["args"]


def test_chromium_launch_options_for_login_adds_start_maximized() -> None:
    cfg = ShiftAlertConfig(playwright_browser_channel="chrome")
    kw = chromium_launch_options_for_login(cfg)
    assert any(str(a) == "--start-maximized" for a in kw["args"])


def test_chromium_launch_options_for_login_defaults_chrome_on_desktop_when_unset() -> None:
    cfg = ShiftAlertConfig()
    kw = chromium_launch_options_for_login(cfg)
    if sys.platform in ("win32", "darwin"):
        assert kw.get("channel") == "chrome"
    else:
        assert "channel" not in kw


def test_login_persistent_context_kwargs_no_viewport() -> None:
    cfg = ShiftAlertConfig(playwright_browser_channel="chrome")
    kw = _login_persistent_context_kwargs(cfg, timezone_name="Europe/London")
    assert kw["no_viewport"] is True
    assert kw["locale"] == "en-GB"
    assert "Europe/London" in kw["timezone_id"]


def test_chromium_launch_options_adds_channel_when_set() -> None:
    cfg = ShiftAlertConfig(playwright_headless=True, playwright_browser_channel="chrome")
    kw = chromium_launch_options(cfg)
    assert kw["headless"] is True
    assert kw["channel"] == "chrome"
    assert "--disable-blink-features=AutomationControlled" in kw["args"]
    assert kw.get("ignore_default_args") == ["--enable-automation"]


def test_chromium_launch_options_merges_extra_launch_args() -> None:
    cfg = ShiftAlertConfig(
        playwright_browser_channel="chrome",
        playwright_launch_args=["--extra-flag-for-tests"],
    )
    kw = chromium_launch_options(cfg)
    assert kw["args"][0] == "--disable-blink-features=AutomationControlled"
    assert "--extra-flag-for-tests" in kw["args"]


def test_chromium_launch_attempt_variants_includes_headless_new_for_chrome() -> None:
    cfg = ShiftAlertConfig(playwright_headless=True, playwright_browser_channel="chrome")
    labels = [label for label, _ in chromium_launch_attempt_variants(cfg)]
    assert labels[0] == "configured"
    assert "chrome channel (--headless=new)" in labels
    assert "chrome channel (visible window)" in labels
    assert "Playwright bundled Chromium" in labels


def test_chromium_launch_attempt_variants_no_duplicate_fingerprints() -> None:
    cfg = ShiftAlertConfig(playwright_headless=False, playwright_browser_channel="chrome")
    fps = []
    for _, opts in chromium_launch_attempt_variants(cfg):
        from app.shift_alert import _launch_opts_fingerprint

        fps.append(_launch_opts_fingerprint(opts))
    assert len(fps) == len(set(fps))


def test_chromium_launch_options_executable_overrides_channel() -> None:
    cfg = ShiftAlertConfig(
        playwright_headless=False,
        playwright_browser_channel="chrome",
        playwright_executable_path=r"C:\Apps\Comet.exe",
    )
    kw = chromium_launch_options(cfg, headless=False)
    assert kw["executable_path"] == r"C:\Apps\Comet.exe"
    assert "channel" not in kw
    assert "--disable-blink-features=AutomationControlled" in kw["args"]
    assert kw.get("ignore_default_args") == ["--enable-automation"]


def test_resolve_watch_profiles_legacy_when_profiles_empty() -> None:
    cfg = ShiftAlertConfig(
        schedule_url="https://example.com/sched",
        storage_state_path="data/s.json",
        state_path="data/state.json",
        profiles=[],
    )
    rows = resolve_watch_profiles(cfg, storage_override=None, state_override=None, profile_id_filter=None)
    assert len(rows) == 1
    assert rows[0].id == "default"
    assert rows[0].schedule_url == "https://example.com/sched"


def test_resolve_watch_profiles_multi() -> None:
    cfg = ShiftAlertConfig(
        schedule_url="https://ignored.example/",
        profiles=[
            ShiftAlertProfile(
                id="a",
                display_name="A",
                schedule_url="https://a.example/s",
                storage_state_path="data/a.json",
            ),
            ShiftAlertProfile(
                id="b",
                display_name="B",
                schedule_url="https://b.example/s",
                storage_state_path="data/b.json",
                enabled=False,
            ),
        ],
    )
    rows = resolve_watch_profiles(cfg, storage_override=None, state_override=None, profile_id_filter=None)
    assert len(rows) == 1
    assert rows[0].id == "a"


def test_merge_storage_overrides_cli_beats_env() -> None:
    s, st = merge_storage_overrides(
        cli_storage="cli.json",
        cli_state=None,
        env_storage="env.json",
        env_state="env_state.json",
    )
    assert s == "cli.json"
    assert st == "env_state.json"


def test_merge_storage_overrides_env_fallback() -> None:
    s, st = merge_storage_overrides(
        cli_storage=None,
        cli_state=None,
        env_storage="env.json",
        env_state=None,
    )
    assert s == "env.json"
    assert st is None


def test_resolve_shift_paths_derives_state_next_to_session() -> None:
    cfg = ShiftAlertConfig(
        storage_state_path="data/default_session.json",
        state_path="data/default_shift_state.json",
    )
    storage, state = resolve_shift_paths(cfg, storage_override="data/alice_session.json", state_override=None)
    assert storage == Path("data/alice_session.json")
    assert state == Path("data/alice_session.shift_state.json")


def test_resolve_shift_paths_explicit_state_override() -> None:
    cfg = ShiftAlertConfig()
    storage, state = resolve_shift_paths(
        cfg,
        storage_override="data/a.json",
        state_override="custom/state.json",
    )
    assert state == Path("custom/state.json")


def test_weekend_skipped_when_configured() -> None:
    cfg = ShiftAlertConfig(active_weekdays_only=True, active_hour_start=6, active_hour_end=18)
    saturday = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
    assert in_active_poll_window(saturday, "Europe/London", cfg) is False


def test_weekend_allowed_when_weekdays_only_off() -> None:
    cfg = ShiftAlertConfig(active_weekdays_only=False, active_hour_start=0, active_hour_end=24)
    saturday = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
    assert in_active_poll_window(saturday, "Europe/London", cfg) is True


def test_in_active_poll_window_default_24h_span() -> None:
    cfg = ShiftAlertConfig()
    london = ZoneInfo("Europe/London")
    late = datetime(2026, 1, 5, 23, 30, tzinfo=london)
    assert in_active_poll_window(late, "Europe/London", cfg) is True


def test_is_direct_schedule_picker_url() -> None:
    assert is_direct_schedule_picker_url(
        "https://www.jobsatamazon.co.uk/selfservice/schedule/available-schedule/uuid/JOB-UK-1"
    )
    assert not is_direct_schedule_picker_url("https://www.jobsatamazon.co.uk/app#/myApplications")


def test_resolve_login_entry_url_prefers_schedule() -> None:
    cfg = ShiftAlertConfig(
        schedule_url="https://www.jobsatamazon.co.uk/app#/myApplications",
        login_url="https://www.jobsatamazon.co.uk/login",
    )
    assert "myApplications" in resolve_login_entry_url(cfg, None)


def test_resolve_login_schedule_url_uses_profile() -> None:
    cfg = ShiftAlertConfig(
        schedule_url="https://www.jobsatamazon.co.uk/app#/myApplications",
        profiles=[
            ShiftAlertProfile(
                id="a",
                schedule_url="https://www.jobsatamazon.co.uk/selfservice/schedule/x",
                storage_state_path="data/a.json",
            )
        ],
    )
    assert "selfservice" in resolve_login_schedule_url(cfg, "a")


def test_turbo_window_thursday_4pm() -> None:
    cfg = ShiftAlertConfig(
        turbo_enabled=True,
        turbo_windows=[
            ShiftAlertTurboWindow(
                weekday=3,
                hour_start=16,
                hour_end=17,
                poll_interval_min_seconds=5,
                poll_interval_max_seconds=12,
            )
        ],
    )
    london = ZoneInfo("Europe/London")
    thu_1605 = datetime(2026, 1, 8, 16, 5, tzinfo=london)
    assert resolve_active_turbo_window(thu_1605, "Europe/London", cfg) is not None
    pmin, pmax, turbo = effective_poll_intervals(thu_1605, "Europe/London", cfg, 30, 60)
    assert turbo is True
    assert pmin == 5 and pmax == 12


def test_select_shift_click_budget_turbo() -> None:
    cfg = ShiftAlertConfig(select_shift_aggressive_attempts=8, select_shift_click_attempts=2)
    n, aggressive = select_shift_click_budget(cfg, turbo_active=True)
    assert n == 8 and aggressive is True


def test_shift_alert_telegram_burst_count() -> None:
    cfg = ShiftAlertConfig(high_confidence_alert_repeat_count=3)
    assert shift_alert_telegram_burst_count(cfg, high_confidence=True) == 3
    assert shift_alert_telegram_burst_count(cfg, high_confidence=False) == 1


def test_should_pre_alert_on_fingerprint() -> None:
    assert should_pre_alert_on_fingerprint(ShiftAlertConfig(pre_alert_on_fingerprint_change=True))
    assert should_pre_alert_on_fingerprint(ShiftAlertConfig(alert_on_fingerprint_change=True))
    assert not should_pre_alert_on_fingerprint(
        ShiftAlertConfig(pre_alert_on_fingerprint_change=False, alert_on_fingerprint_change=False)
    )


def test_dom_watch_timeout_ms_from_poll_max() -> None:
    cfg = ShiftAlertConfig(dom_watch_timeout_seconds=0)
    assert dom_watch_timeout_ms(cfg, 30, 50) == 50_000
    assert dom_watch_timeout_ms(cfg, 8, 20) == 20_000
    assert dom_watch_timeout_ms(cfg, 1, 1) == 5_000


def test_dom_watch_timeout_ms_turbo_fixed_18() -> None:
    cfg = ShiftAlertConfig(dom_watch_timeout_seconds=0)
    turbo = ShiftAlertTurboWindow(
        weekday=3,
        hour_start=16,
        hour_end=17,
        poll_interval_min_seconds=8,
        poll_interval_max_seconds=20,
        dom_watch_timeout_seconds=18,
    )
    assert dom_watch_timeout_ms(cfg, 8, 20, turbo=turbo) == 18_000


def test_dom_watch_timeout_ms_explicit() -> None:
    cfg = ShiftAlertConfig(dom_watch_timeout_seconds=42)
    assert dom_watch_timeout_ms(cfg, 30, 60) == 42_000


def test_page_on_schedule_route() -> None:
    dash = "https://www.jobsatamazon.co.uk/app#/myApplications"
    assert page_on_schedule_route(dash, dash)
    assert not page_on_schedule_route("https://www.jobsatamazon.co.uk/login", dash)
    picker = "https://www.jobsatamazon.co.uk/selfservice/schedule/available-schedule/uuid/job"
    assert page_on_schedule_route(picker, picker)
    app = "https://www.jobsatamazon.co.uk/application/uk/job-opportunities?applicationId=be3fcd64"
    assert is_application_tracking_url(app)
    assert is_direct_schedule_picker_url(app)
    assert page_on_schedule_route(app, app)


def test_login_keepalive_interval_is_15_seconds() -> None:
    assert _LOGIN_KEEPALIVE_INTERVAL_SECONDS == 15.0


def test_resolve_login_entry_uses_application_dashboard() -> None:
    cfg = ShiftAlertConfig(
        schedule_url="",
        login_start_url="",
        login_url="https://www.jobsatamazon.co.uk/login",
    )
    entry = resolve_login_entry_url(cfg, None)
    watch = resolve_shift_watch_url(cfg, None)
    assert entry == watch
    assert entry == DEFAULT_SHIFT_APPLICATION_DASHBOARD_URL



def test_dom_observer_result_is_stale() -> None:
    assert dom_observer_result_is_stale({"stale": True, "reason": "x"})
    assert dom_observer_result_is_stale({"reason": "observer_silent"})
    assert dom_observer_result_is_stale({"reason": "layout_detached"})
    assert not dom_observer_result_is_stale({"reason": "timeout", "triggered": False})


def test_is_playwright_browser_crash() -> None:
    class TargetClosedError(Exception):
        pass

    assert is_playwright_browser_crash(TargetClosedError("Target page, context or browser has been closed"))
    assert is_playwright_browser_crash(ConnectionError("websocket connection closed"))
    assert not is_playwright_browser_crash(ValueError("bad config"))


def test_build_dom_watcher_eval_config_merges_phrases() -> None:
    cfg = ShiftAlertConfig(
        empty_state_substrings=["no shifts available"],
        deep_shift_available_substrings=["shift pattern"],
        page_ready_substrings=["my jobs"],
    )
    ev = build_dom_watcher_eval_config(cfg, timeout_ms=45_000)
    assert ev["timeoutMs"] == 45_000
    assert ev["healthPingIntervalMs"] == 15_000
    assert ev["observerStaleMs"] == 120_000
    assert "no shifts available" in ev["emptyPhrases"]
    assert "shift pattern" in ev["shiftPhrases"]
    assert "my jobs" in ev["readyPhrases"]
