from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Self

PaymentMode = Literal["auto", "telegram", "stripe_links"]

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProjectConfig(BaseModel):
    timezone: str = "Europe/London"
    telegram_alert_header: str | None = Field(
        default=None,
        description="Optional first line shown on Telegram alerts (for example a channel title).",
    )


class SourcesConfig(BaseModel):
    urls: list[str] = Field(default_factory=list)


class FiltersConfig(BaseModel):
    location_keywords: list[str] = Field(default_factory=list)
    title_keywords_any: list[str] = Field(default_factory=list)
    title_keywords_none: list[str] = Field(default_factory=list)
    min_pay_gbp_per_hour: float = 0.0


class SetLocationConfig(BaseModel):
    exclude_from_menu: list[str] = Field(
        default_factory=list,
        description="Substring matches (case-insensitive) removed from /setlocation choices.",
    )

    @field_validator("exclude_from_menu", mode="before")
    @classmethod
    def _strip_exclude_names(cls, v: object) -> object:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return v


class AlertLocationItem(BaseModel):
    """Preset for subscriber /setlocation; keywords match job location/title (case-insensitive)."""

    name: str
    keywords: list[str] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_alert_loc_name(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("keywords", mode="before")
    @classmethod
    def _strip_alert_loc_keywords(cls, v: object) -> object:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return v


class PaidPlanConfig(BaseModel):
    """One Telegram payment option (invoice + subscription length)."""

    id: str = Field(description="Short id used in callback_data and invoice payload, e.g. 1d, 15d, 30d.")
    button_label: str = Field(description="Text on the inline payment button.")
    days: float = Field(ge=1.0, le=365.0, description="Subscription days added after payment.")
    price_gbp: float = Field(ge=0.5, le=500.0, description="Price in GBP.")
    invoice_title: str = Field(default="", description="Telegram invoice title (max 32 chars).")
    invoice_description: str = Field(default="", description="Telegram invoice description.")
    stripe_url: str | None = Field(
        default=None,
        description="Stripe Payment Link (https://buy.stripe.com/...). Opens real checkout in browser.",
    )

    @field_validator("id", mode="before")
    @classmethod
    def _strip_plan_id(cls, v: object) -> object:
        if isinstance(v, str):
            s = v.strip().lower()
            if not s or len(s) > 16:
                raise ValueError("paid plan id must be 1–16 characters")
            return s
        return v

    @field_validator("button_label", "invoice_title", "invoice_description", "stripe_url", mode="before")
    @classmethod
    def _strip_plan_strings(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @model_validator(mode="after")
    def _default_invoice_text(self) -> Self:
        title = self.invoice_title or f"Amazon UK Alerts — {int(self.days)} days"
        desc = (
            self.invoice_description
            or f"Paid access — Amazon warehouse job + shift Telegram alerts ({int(self.days)} days)."
        )
        object.__setattr__(self, "invoice_title", title[:32])
        object.__setattr__(self, "invoice_description", desc[:255])
        return self


def _default_paid_plans() -> list[PaidPlanConfig]:
    return [
        PaidPlanConfig(
            id="1d",
            button_label="1 day payment — £2.25",
            days=1,
            price_gbp=2.25,
            invoice_title="Amazon UK Alerts — 1 day",
        ),
        PaidPlanConfig(
            id="15d",
            button_label="15 day payment — £35",
            days=15,
            price_gbp=35.0,
            invoice_title="Amazon UK Alerts — 15 days",
        ),
        PaidPlanConfig(
            id="30d",
            button_label="1 month — £50",
            days=30,
            price_gbp=50.0,
            invoice_title="Amazon UK Alerts — 1 month",
        ),
    ]


class SubscriptionConfig(BaseModel):
    """Telegram subscriber onboarding: free trial, subscribe button, expiry reminders."""

    trial_duration_hours: float = Field(
        24.0,
        ge=1.0,
        le=720.0,
        description="Length of free trial after user taps Subscribe (default 24h).",
    )
    reference_grant_hours: float = Field(
        72.0,
        ge=1.0,
        le=720.0,
        description="Hours of free alerts when admin grants reference access by chat ID.",
    )
    reference_grant_user_message: str = Field(
        "Ahhaha — you're special to the admin! You get temporary free alerts. Enjoy! 🎉",
        description="Message sent to user when admin grants reference access.",
    )
    reminder_hours_before_expiry: float = Field(
        1.0,
        ge=0.25,
        le=48.0,
        description="Send reminder this many hours before subscription_ends_at.",
    )
    final_reminder_minutes_before_expiry: float = Field(
        20.0,
        ge=5.0,
        le=120.0,
        description="Second reminder with paid Subscribe button (minutes before expiry).",
    )
    trial_ending_minutes_before_expiry: float = Field(
        1.0,
        ge=0.5,
        le=30.0,
        description="Final warning before free trial auto-stops (minutes before expiry).",
    )
    reminder_1h_body: str = Field(
        "📦 Amazon UK warehouse roles fill up fast. Stay subscribed so you're the first to know — "
        "work smart and stay two steps ahead of the crowd.",
        description="Body text for the 1-hour-before-expiry reminder.",
    )
    trial_ending_body: str = Field(
        "You have not subscribed to a paid plan — alerts will stop automatically when the trial ends "
        "and you will not receive job notifications after that. Tap Subscribe below to keep getting alerts.",
        description="Body for the 1-minute trial-ending warning.",
    )
    trial_repeat_blocked_body: str = Field(
        "Your 1-day free trial has already been used and has ended. "
        "Please buy a subscription below to continue receiving job and shift alerts.",
        description="Shown when user taps /start or free trial again after their one-time trial was used.",
    )
    payment_mode: PaymentMode = Field(
        default="auto",
        description=(
            "auto = Stripe links if any plan has stripe_url, else Telegram Payments; "
            "stripe_links = always use Stripe Payment Links; telegram = BotFather invoices only."
        ),
    )
    paid_plans: list[PaidPlanConfig] = Field(
        default_factory=_default_paid_plans,
        description="Payment buttons for free-trial users (1d / 15d / 30d).",
    )
    paid_subscription_days: float = Field(
        30.0,
        ge=1.0,
        le=365.0,
        description="Legacy fallback days if invoice payload has no plan id.",
    )
    paid_price_gbp: float = Field(
        50.0,
        ge=0.5,
        le=500.0,
        description="Legacy fallback price (GBP) if paid_plans is empty.",
    )
    paid_invoice_title: str = Field(
        "Amazon UK Job Alerts — Subscription",
        description="Legacy invoice title when paid_plans is empty.",
    )
    paid_invoice_description: str = Field(
        "Continue receiving Amazon warehouse job + shift alerts after your trial ends.",
        description="Legacy invoice description when paid_plans is empty.",
    )
    reminder_check_interval_seconds: int = Field(
        60,
        ge=15,
        le=600,
        description="How often the subscriber bot checks for expiry reminders.",
    )
    welcome_locations_limit: int = Field(
        12,
        ge=1,
        le=40,
        description="Max location lines shown on /start (from jobs DB, else alert_locations names).",
    )

    def plan_by_id(self, plan_id: str) -> PaidPlanConfig | None:
        pid = (plan_id or "").strip().lower()
        for plan in self.paid_plans:
            if plan.id == pid:
                return plan
        return None

    def primary_paid_plan(self) -> PaidPlanConfig | None:
        if self.paid_plans:
            return self.paid_plans[-1]
        return None

    def uses_stripe_links(self) -> bool:
        """True when paid buttons should open Stripe Payment Links (not Telegram Checkout Test)."""
        if self.payment_mode == "telegram":
            return False
        if self.payment_mode == "stripe_links":
            return True
        return any((p.stripe_url or "").strip() for p in self.paid_plans)

    def stripe_url_for_plan(self, plan_id: str) -> str | None:
        plan = self.plan_by_id(plan_id)
        if plan is None:
            return None
        url = (plan.stripe_url or "").strip()
        return url or None


class BehaviorConfig(BaseModel):
    alert_on_updates: bool = True
    max_items_per_source: int = 80
    save_snapshots_on_parse_issues: bool = True
    use_playwright_for_jobsatamazon: bool = True
    alert_only_when_apply_enabled: bool = True
    capture_jobsatamazon_network_json: bool = True
    notify_admin_on_message_reaction: bool = Field(
        True,
        description="Forward emoji reactions on bot messages to TELEGRAM_CHAT_ID (admin).",
    )


def _normalize_jobsatamazon_www_host(url: str) -> str:
    """
    Force ``www.jobsatamazon.co.uk`` when the hostname is the apex ``jobsatamazon.co.uk``.

    Mixing apex vs ``www`` with ``auth.hiring.amazon.com`` redirects often causes a blank / reload loop
    after OTP on the jobsatamazon login SPA.
    """
    if "jobsatamazon.co.uk" not in url:
        return url
    u = url.replace("://jobsatamazon.co.uk", "://www.jobsatamazon.co.uk")
    u = u.replace("://www.www.jobsatamazon.co.uk", "://www.jobsatamazon.co.uk")
    return u


class ShiftAlertTurboWindow(BaseModel):
    """Faster polling during known UK shift drop windows (local project timezone)."""

    weekday: int = Field(..., ge=0, le=6, description="0=Monday … 6=Sunday (Thursday=3).")
    hour_start: int = Field(..., ge=0, le=23)
    hour_end: int = Field(..., ge=1, le=24, description="Exclusive end hour (17 = through 16:59).")
    poll_interval_min_seconds: int = Field(8, ge=1, le=86_400)
    poll_interval_max_seconds: int = Field(20, ge=1, le=86_400)
    dom_watch_timeout_seconds: float = Field(
        18.0,
        ge=0.0,
        le=120.0,
        description="MutationObserver window during this turbo window (0 = use poll max, min 5s).",
    )

    @model_validator(mode="after")
    def _turbo_window_bounds(self) -> Self:
        if self.poll_interval_max_seconds < self.poll_interval_min_seconds:
            raise ValueError("turbo poll_interval_max_seconds must be >= poll_interval_min_seconds")
        if self.hour_start >= self.hour_end:
            raise ValueError("turbo hour_end must be greater than hour_start")
        return self


class ShiftAlertProfile(BaseModel):
    """One applicant pipeline: own session file + schedule URL + dedupe state."""

    id: str = Field(..., description="Stable id for CLI --profile.")
    display_name: str = ""
    schedule_url: str = ""
    storage_state_path: str = "data/amazon_shift_session.json"
    state_path: str | None = Field(
        default=None,
        description="Dedupe JSON path; default = <storage_stem>.shift_state.json next to session file.",
    )
    enabled: bool = True

    @field_validator("id", "storage_state_path", mode="before")
    @classmethod
    def _strip_str(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("schedule_url", mode="before")
    @classmethod
    def _strip_and_www_profile_schedule(cls, v: object) -> object:
        if isinstance(v, str):
            return _normalize_jobsatamazon_www_host(v.strip())
        return v


class ShiftAlertConfig(BaseModel):
    """
    Optional authenticated shift watcher for Amazon Candidate Self Service.
    Runs as a separate process from `app.main` so job polling is unaffected.
    """

    enabled: bool = False
    # When non-empty, each profile is watched in rotation (multi-account). Empty = use flat fields below.
    profiles: list[ShiftAlertProfile] = Field(default_factory=list)
    schedule_url: str = ""
    login_url: str = "https://www.jobsatamazon.co.uk/login"
    # Where ``login`` opens first (empty = ``schedule_url``, then ``login_url``). Prefer app#/myApplications over /login — fewer OTP loops.
    login_start_url: str = ""
    storage_state_path: str = "data/amazon_shift_session.json"
    state_path: str = "data/shift_alert_state.json"
    poll_interval_min_seconds: int = Field(
        120,
        ge=1,
        le=86_400,
        description="Seconds after a full poll cycle (min of random range). Use min=max=1 for ~1s like app.main POLL_INTERVAL_SECONDS.",
    )
    poll_interval_max_seconds: int = Field(
        300,
        ge=1,
        le=86_400,
        description="Seconds after a cycle (max of random range); set equal to min for a fixed interval.",
    )
    dom_watch_enabled: bool = Field(
        True,
        description="Use in-page MutationObserver between navigations instead of page.goto every poll.",
    )
    dom_watch_timeout_seconds: float = Field(
        0.0,
        ge=0.0,
        le=120.0,
        description="Observer window in seconds (0 = auto from poll max, or turbo window value when turbo active).",
    )
    # Extra pause between profiles in one cycle (default 0). Raise slightly if Amazon throttles multi-login.
    profile_stagger_seconds: int = Field(0, ge=0, le=120)
    empty_state_substrings: list[str] = Field(
        default_factory=lambda: [
            "we do not have any schedules matching",
            "no shifts available at this time",
            "no shifts available",
        ],
        description='Phrases meaning "no shift slots yet" (e.g. My jobs card line). Alert only after one '
        "poll matched these, then they disappear (similar spirit to apply-only alerts in app.main).",
    )
    # After My jobs loads, open the schedule screen too (Amazon “Select Shift” CTA).
    poll_select_shift_for_deep_check: bool = Field(
        True,
        description="Click Select Shift and scan the schedule view; alert if slots appear there even when "
        'the card still says “no shifts” (see deep_* strings). Set false for URLs that have no such button.',
    )
    # Candidate self-service / post–Select Shift: “no schedule yet” vs real picker copy.
    deep_no_schedule_substrings: list[str] = Field(
        default_factory=lambda: [
            "you currently do not have a schedule or start date selected",
        ],
        description='When this copy disappears after you saw it once, and deep_shift_available_substrings match, '
        "Telegram fires even if My jobs still shows empty_state_substrings.",
    )
    deep_shift_available_substrings: list[str] = Field(
        default_factory=lambda: [
            "shift pattern",
            "weekly schedule",
            "select a shift",
            "choose your shift",
            "available shift",
            "save and continue",
            "confirm selection",
        ],
        description="Any one match (case-insensitive) on the post–Select Shift page counts as shift options visible.",
    )
    deep_page_ready_substrings: list[str] = Field(
        default_factory=lambda: [
            "current schedule information",
            "you currently do not have a schedule",
            "select shift",
        ],
        description="Wait for one of these after clicking Select Shift so the SPA has rendered before reading HTML.",
    )
    deep_page_ready_timeout_ms: int = Field(
        20_000,
        ge=2_000,
        le=60_000,
        description="Max wait for deep_page_ready_substrings after the click.",
    )
    # After navigation, wait until the page body contains one of these (SPA / hash routes).
    # Empty list = legacy behaviour (short fixed sleep only). Include strings from BOTH
    # self-service schedule pages and jobsatamazon “My jobs” dashboard.
    page_ready_substrings: list[str] = Field(
        default_factory=lambda: [
            "my jobs",
            "withdrawn",
            "select shift",
            "we do not have any schedules matching",
            "no shifts available at this time",
            "no shifts available",
        ]
    )
    page_ready_timeout_ms: int = Field(
        default=45_000,
        ge=5_000,
        le=120_000,
        description="Max time to wait for page_ready_substrings before giving up this poll.",
    )
    alert_on_fingerprint_change: bool = Field(
        False,
        description="Legacy alias for pre_alert_on_fingerprint_change.",
    )
    pre_alert_on_fingerprint_change: bool = Field(
        True,
        description="Send an immediate 'Heads up' Telegram when the dashboard fingerprint changes before shifts open.",
    )
    pre_alert_cooldown_seconds: int = Field(
        180,
        ge=0,
        le=86_400,
        description="Minimum seconds between Heads up pre-alerts for the same profile (0 = no cooldown).",
    )
    turbo_enabled: bool = Field(
        False,
        description="Use faster poll_interval_* from turbo_windows during configured drop windows.",
    )
    turbo_windows: list[ShiftAlertTurboWindow] = Field(
        default_factory=lambda: [
            ShiftAlertTurboWindow(
                weekday=3,
                hour_start=16,
                hour_end=17,
                poll_interval_min_seconds=8,
                poll_interval_max_seconds=20,
            )
        ],
        description="Drop windows in project timezone (default: Thursday 16:00–17:00 UK).",
    )
    aggressive_select_shift: bool = Field(
        False,
        description="Always use persistent multi-attempt Select Shift clicking (also on during turbo if "
        "select_shift_aggressive_in_turbo).",
    )
    select_shift_click_attempts: int = Field(2, ge=1, le=30, description="Select Shift click rounds per poll (normal).")
    select_shift_aggressive_attempts: int = Field(
        8, ge=1, le=30, description="Select Shift click rounds when aggressive / turbo."
    )
    select_shift_aggressive_in_turbo: bool = Field(
        True,
        description="Use aggressive Select Shift clicking during an active turbo window.",
    )
    session_refresh_hours: float = Field(
        24.0,
        ge=0.0,
        le=168.0,
        description="Re-save Playwright session after this many hours (visit schedule_url while signed in). 0=off.",
    )
    # CDN/WAF block pages (no real shift UI) — must not trigger “shifts available” alerts.
    blocked_page_substrings: list[str] = Field(
        default_factory=lambda: [
            "generated by cloudfront",
            "request could not be satisfied",
            "the request could not be satisfied",
        ],
        description="CDN/WAF block page markers. Avoid weak phrases like '403 error' alone — SPAs embed that in JS.",
    )
    active_weekdays_only: bool = False
    active_hour_start: int = Field(0, ge=0, le=23)
    active_hour_end: int = Field(
        24,
        ge=1,
        le=24,
        description="Exclusive end hour in local time (24 = through end of day 23:59).",
    )
    playwright_headless: bool = True
    # Use installed Chrome/Edge instead of bundled Chromium (often fixes hiring.amazon.com OTP / verify errors).
    # Examples: "chrome", "msedge", "chrome-beta" (must be installed). Empty = default Chromium.
    playwright_browser_channel: str | None = None
    # Full path to a Chromium-based .exe (e.g. Perplexity Comet). If set, overrides playwright_browser_channel.
    playwright_executable_path: str | None = None
    # Extra Chromium flags merged after defaults (see shift_alert.py).
    playwright_launch_args: list[str] = Field(default_factory=list)
    navigation_timeout_ms: int = 60_000
    screenshot_timeout_ms: int = Field(
        55_000,
        ge=5_000,
        le=120_000,
        description="Per-attempt timeout for Playwright page.screenshot during shift checks (ms).",
    )
    alert_title: str = "Amazon UK — shifts are open"
    high_confidence_alert_repeat_count: int = Field(
        3,
        ge=1,
        le=5,
        description="When post–Select Shift deep_slots fires, send this many photo alerts in a row (separate vibrations).",
    )
    high_confidence_alert_repeat_delay_ms: int = Field(
        450,
        ge=0,
        le=5_000,
        description="Pause between burst alerts for high-confidence (deep_slots) detections.",
    )
    # Shown in Telegram caption (e.g. site / city). Empty = try profile display_name when using profiles.
    alert_location: str = ""
    # Smart peak timing (admin-only Telegram warnings + /admin Get Peak Times).
    peak_prediction_enabled: bool = True
    peak_prediction_lead_minutes_min: int = Field(5, ge=1, le=30)
    peak_prediction_lead_minutes_max: int = Field(10, ge=1, le=60)
    peak_prediction_min_samples: int = Field(3, ge=1, le=100)
    peak_prediction_min_slot_count: int = Field(2, ge=1, le=20)
    peak_prediction_check_interval_seconds: int = Field(60, ge=15, le=600)
    # Optional full User-Agent string for ``login`` only (empty = built-in Chrome/Windows template).
    login_user_agent: str = ""

    @field_validator("login_url", "schedule_url", "login_start_url", mode="before")
    @classmethod
    def _strip_and_normalize_jobsatamazon_urls(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        s = v.strip()
        if not s:
            return s
        return _normalize_jobsatamazon_www_host(s)

    @field_validator("playwright_browser_channel", mode="before")
    @classmethod
    def _strip_browser_channel(cls, v: object) -> object:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return v

    @field_validator("playwright_executable_path", mode="before")
    @classmethod
    def _strip_executable_path(cls, v: object) -> object:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return v

    @field_validator("alert_location", mode="before")
    @classmethod
    def _strip_alert_location(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @model_validator(mode="after")
    def _shift_alert_intervals_and_hours(self) -> Self:
        if self.poll_interval_max_seconds < self.poll_interval_min_seconds:
            raise ValueError("shift_alert.poll_interval_max_seconds must be >= poll_interval_min_seconds")
        if self.active_hour_start >= self.active_hour_end:
            raise ValueError(
                "shift_alert.active_hour_end must be greater than active_hour_start "
                "(e.g. 0 and 24 for 24/7 within the day)."
            )
        if self.peak_prediction_lead_minutes_max < self.peak_prediction_lead_minutes_min:
            raise ValueError(
                "shift_alert.peak_prediction_lead_minutes_max must be >= peak_prediction_lead_minutes_min"
            )
        return self


class FileConfig(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    subscription: SubscriptionConfig = Field(default_factory=SubscriptionConfig)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
    shift_alert: ShiftAlertConfig = Field(default_factory=ShiftAlertConfig)
    alert_locations: list[AlertLocationItem] = Field(
        default_factory=list,
        description="Optional presets for subscriber /setlocation (inline keyboard).",
    )
    setlocation: SetLocationConfig = Field(default_factory=SetLocationConfig)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")
    telegram_payment_provider_token: str | None = Field(
        default=None,
        alias="TELEGRAM_PAYMENT_PROVIDER_TOKEN",
        description="From @BotFather → Payments (Stripe etc.). Required for paid Subscribe invoices.",
    )

    poll_interval_seconds: int = Field(default=20, alias="POLL_INTERVAL_SECONDS")
    # If > 0, send a periodic Telegram heartbeat to TELEGRAM_CHAT_ID (hours between messages). 0 = off.
    heartbeat_interval_hours: float = Field(0.0, ge=0.0, alias="HEARTBEAT_INTERVAL_HOURS")
    dry_run: bool = Field(default=False, alias="DRY_RUN")
    config_path: str = Field(default="config.yaml", alias="CONFIG_PATH")
    sqlite_path: str = Field(default="data/jobs.db", alias="SQLITE_PATH")

    http_timeout_seconds: float = Field(default=12.0, alias="HTTP_TIMEOUT_SECONDS")
    http_concurrency: int = Field(default=4, alias="HTTP_CONCURRENCY")

    default_expected_pay_gbp_per_hour_min: float | None = Field(
        default=None, alias="DEFAULT_EXPECTED_PAY_GBP_PER_HOUR_MIN"
    )
    default_expected_pay_gbp_per_hour_max: float | None = Field(
        default=None, alias="DEFAULT_EXPECTED_PAY_GBP_PER_HOUR_MAX"
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO", alias="LOG_LEVEL")

    stripe_webhook_secret: str | None = Field(
        default=None,
        alias="STRIPE_WEBHOOK_SECRET",
        description="Stripe Dashboard webhook signing secret (whsec_...). Enables automatic payment confirmation.",
    )
    stripe_secret_key: str | None = Field(
        default=None,
        alias="STRIPE_SECRET_KEY",
        description="Stripe secret API key (sk_live_...). Verifies payment when user returns without /start param.",
    )
    stripe_webhook_host: str = Field(default="127.0.0.1", alias="STRIPE_WEBHOOK_HOST")
    stripe_webhook_port: int = Field(default=8765, ge=1, le=65535, alias="STRIPE_WEBHOOK_PORT")

    # Shift watcher defaults (optional). CLI `--storage-state` / `--state-path` override these.
    shift_alert_storage_state: str | None = Field(default=None, alias="SHIFT_ALERT_STORAGE_STATE")
    shift_alert_state_path: str | None = Field(default=None, alias="SHIFT_ALERT_STATE_PATH")
    # If set, ``python -m app.shift_alert run`` uses this fixed interval (seconds), like POLL_INTERVAL_SECONDS for app.main.
    shift_alert_poll_interval_seconds: int | None = Field(default=None, alias="SHIFT_ALERT_POLL_SECONDS")

    @field_validator("default_expected_pay_gbp_per_hour_min", "default_expected_pay_gbp_per_hour_max", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("shift_alert_storage_state", "shift_alert_state_path", mode="before")
    @classmethod
    def _blank_shift_paths_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("shift_alert_poll_interval_seconds", mode="before")
    @classmethod
    def _blank_shift_poll_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("shift_alert_poll_interval_seconds", mode="after")
    @classmethod
    def _shift_poll_at_least_one(cls, v: int | None) -> int | None:
        if v is None:
            return None
        return max(1, int(v))

    def require_telegram(self) -> tuple[str, str]:
        if not self.telegram_bot_token or not self.telegram_chat_id:
            raise ValueError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in environment")
        return self.telegram_bot_token, self.telegram_chat_id


def load_file_config(path: str | Path) -> FileConfig:
    p = Path(path)
    raw: dict[str, Any]
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p.resolve()}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return FileConfig.model_validate(raw)

