from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from collections.abc import Sequence
from typing import Any

from app.utils.setlocation_menu import filter_location_strings, merge_setlocation_excludes


@dataclass(frozen=True, slots=True)
class UpsertResult:
    status: str  # "new" | "updated" | "unchanged"
    first_seen_utc: datetime
    last_seen_utc: datetime


@dataclass(frozen=True, slots=True)
class SubscriberAdminRow:
    chat_id: str
    first_name: str | None
    trial_activated_at: datetime | None
    paid_at: datetime | None
    subscription_ends_at: datetime | None
    alerts_muted: bool
    location_choice: str
    location_match_substr: str | None
    last_payment_currency: str | None
    last_payment_total_amount: int | None
    last_payment_invoice_payload: str | None
    last_payment_telegram_charge_id: str | None


@dataclass(frozen=True, slots=True)
class EngagementStats:
    """Subscriber activity (Telegram bots cannot see message read/seen receipts)."""

    total_subscribers: int
    subscription_active: int
    active_24h: int
    active_7d: int
    reacted_ever: int
    reacted_7d: int
    muted: int
    avg_interactions_active_7d: float
    top_active_7d: list[tuple[str, str | None, int, str | None]]


@dataclass(frozen=True, slots=True)
class LapsedTrialLead:
    """Trial ended without paid subscription — kept for win-back / promo messages."""

    chat_id: str
    first_name: str | None
    trial_started_at: datetime | None
    trial_ended_at: datetime | None
    recorded_at: datetime
    last_promo_sent_at: datetime | None
    promo_send_count: int


class SqliteStore:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            path, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def close(self) -> None:
        self._conn.close()

    def _ensure_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              key TEXT PRIMARY KEY,
              job_id TEXT,
              url TEXT NOT NULL,
              source TEXT NOT NULL,
              source_url TEXT NOT NULL,
              title TEXT,
              location TEXT,
              pay_gbp_per_hour REAL,
              pay_text TEXT,
              expected_pay_text TEXT,
              shift TEXT,
              posted_date_text TEXT,
              content_hash TEXT NOT NULL,
              first_seen_utc TEXT NOT NULL,
              last_seen_utc TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen_utc)")
        cur.execute("PRAGMA table_info(jobs)")
        cols = {str(row[1]) for row in cur.fetchall()}
        if "raw_metadata_json" not in cols:
            cur.execute("ALTER TABLE jobs ADD COLUMN raw_metadata_json TEXT")
            self._conn.commit()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
              chat_id TEXT PRIMARY KEY
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriber_filters (
              chat_id TEXT PRIMARY KEY,
              location_choice TEXT NOT NULL DEFAULT 'all',
              min_pay REAL,
              location_match_substr TEXT,
              alerts_muted INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS setlocation_keyboard (
              chat_id TEXT NOT NULL,
              message_id INTEGER NOT NULL,
              choices_json TEXT NOT NULL,
              PRIMARY KEY (chat_id, message_id)
            )
            """
        )
        self._conn.commit()
        self._migrate_subscriber_filters_columns()
        self._migrate_subscribers_subscription_columns()

    def _migrate_subscribers_subscription_columns(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA table_info(subscribers)")
        cols = {str(row[1]) for row in cur.fetchall()}
        if "first_name" not in cols:
            cur.execute("ALTER TABLE subscribers ADD COLUMN first_name TEXT")
        if "subscription_ends_at" not in cols:
            cur.execute("ALTER TABLE subscribers ADD COLUMN subscription_ends_at TEXT")
        if "expiry_reminder_sent" not in cols:
            cur.execute(
                "ALTER TABLE subscribers ADD COLUMN expiry_reminder_sent INTEGER NOT NULL DEFAULT 0"
            )
        if "final_expiry_reminder_sent" not in cols:
            cur.execute(
                "ALTER TABLE subscribers ADD COLUMN final_expiry_reminder_sent INTEGER NOT NULL DEFAULT 0"
            )
        if "trial_ending_reminder_sent" not in cols:
            cur.execute(
                "ALTER TABLE subscribers ADD COLUMN trial_ending_reminder_sent INTEGER NOT NULL DEFAULT 0"
            )
        if "trial_activated_at" not in cols:
            cur.execute("ALTER TABLE subscribers ADD COLUMN trial_activated_at TEXT")
        if "paid_at" not in cols:
            cur.execute("ALTER TABLE subscribers ADD COLUMN paid_at TEXT")
        if "last_payment_currency" not in cols:
            cur.execute("ALTER TABLE subscribers ADD COLUMN last_payment_currency TEXT")
        if "last_payment_total_amount" not in cols:
            cur.execute("ALTER TABLE subscribers ADD COLUMN last_payment_total_amount INTEGER")
        if "last_payment_invoice_payload" not in cols:
            cur.execute("ALTER TABLE subscribers ADD COLUMN last_payment_invoice_payload TEXT")
        if "last_payment_telegram_charge_id" not in cols:
            cur.execute("ALTER TABLE subscribers ADD COLUMN last_payment_telegram_charge_id TEXT")
        if "is_owner" not in cols:
            cur.execute(
                "ALTER TABLE subscribers ADD COLUMN is_owner INTEGER NOT NULL DEFAULT 0"
            )
        if "admin_reference_granted_at" not in cols:
            cur.execute("ALTER TABLE subscribers ADD COLUMN admin_reference_granted_at TEXT")
        if "last_interaction_at" not in cols:
            cur.execute("ALTER TABLE subscribers ADD COLUMN last_interaction_at TEXT")
        if "interaction_count" not in cols:
            cur.execute(
                "ALTER TABLE subscribers ADD COLUMN interaction_count INTEGER NOT NULL DEFAULT 0"
            )
        if "last_reaction_at" not in cols:
            cur.execute("ALTER TABLE subscribers ADD COLUMN last_reaction_at TEXT")
        if "reaction_count" not in cols:
            cur.execute(
                "ALTER TABLE subscribers ADD COLUMN reaction_count INTEGER NOT NULL DEFAULT 0"
            )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS banned_subscribers (
              chat_id TEXT PRIMARY KEY,
              banned_at TEXT NOT NULL,
              reason TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS lapsed_trial_leads (
              chat_id TEXT PRIMARY KEY,
              first_name TEXT,
              trial_started_at TEXT,
              trial_ended_at TEXT NOT NULL,
              recorded_at TEXT NOT NULL,
              last_promo_sent_at TEXT,
              promo_send_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS stripe_webhook_events (
              event_id TEXT PRIMARY KEY,
              received_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS stripe_checkout_sessions (
              session_id TEXT PRIMARY KEY,
              chat_id TEXT NOT NULL,
              plan_id TEXT NOT NULL,
              processed_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def _migrate_subscriber_filters_columns(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA table_info(subscriber_filters)")
        cols = {str(row[1]) for row in cur.fetchall()}
        if "location_match_substr" not in cols:
            cur.execute("ALTER TABLE subscriber_filters ADD COLUMN location_match_substr TEXT")
            self._conn.commit()
        cur.execute("PRAGMA table_info(subscriber_filters)")
        cols2 = {str(row[1]) for row in cur.fetchall()}
        if "alerts_muted" not in cols2:
            cur.execute(
                "ALTER TABLE subscriber_filters ADD COLUMN alerts_muted INTEGER NOT NULL DEFAULT 0"
            )
            self._conn.commit()

    def add_subscriber(self, chat_id: str, *, first_name: str | None = None) -> None:
        fn = (first_name or "").strip() or None
        self._conn.execute(
            """
            INSERT INTO subscribers (chat_id, first_name)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
              first_name = COALESCE(excluded.first_name, subscribers.first_name)
            """,
            (chat_id, fn),
        )
        self._conn.commit()

    def get_subscriber_first_name(self, chat_id: str) -> str | None:
        cur = self._conn.execute(
            "SELECT first_name FROM subscribers WHERE chat_id = ?",
            (chat_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        raw = row["first_name"]
        s = str(raw).strip() if raw is not None else ""
        return s or None

    def get_subscription_ends_at(self, chat_id: str) -> datetime | None:
        cur = self._conn.execute(
            "SELECT subscription_ends_at FROM subscribers WHERE chat_id = ?",
            (chat_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        raw = row["subscription_ends_at"]
        if raw is None:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    def subscriber_free_trial_already_claimed(self, chat_id: str) -> bool:
        """True if this chat already used the one-time free trial (trial_activated_at set)."""
        cur = self._conn.execute(
            """
            SELECT trial_activated_at, COALESCE(is_owner, 0) AS is_owner
            FROM subscribers WHERE chat_id = ?
            """,
            (chat_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        if int(row["is_owner"] or 0):
            return False
        return row["trial_activated_at"] is not None

    def subscriber_is_trial_unpaid(self, chat_id: str) -> bool:
        """Started free trial but never completed a paid subscription."""
        cur = self._conn.execute(
            """
            SELECT trial_activated_at, paid_at, COALESCE(is_owner, 0) AS is_owner
            FROM subscribers WHERE chat_id = ?
            """,
            (chat_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        if int(row["is_owner"] or 0):
            return False
        if row["paid_at"] is not None:
            return False
        return row["trial_activated_at"] is not None

    def subscriber_has_paid(self, chat_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT paid_at FROM subscribers WHERE chat_id = ?",
            (chat_id,),
        )
        row = cur.fetchone()
        return row is not None and row["paid_at"] is not None

    def subscriber_is_owner(self, chat_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT COALESCE(is_owner, 0) AS o FROM subscribers WHERE chat_id = ?",
            (chat_id,),
        )
        row = cur.fetchone()
        return row is not None and int(row["o"] or 0) != 0

    def _parse_dt_optional(self, raw: object) -> datetime | None:
        if raw is None:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    def remove_lapsed_trial_lead(self, chat_id: str) -> None:
        """User paid or was re-activated — no longer a win-back lead."""
        self._conn.execute("DELETE FROM lapsed_trial_leads WHERE chat_id = ?", (chat_id,))
        self._conn.commit()

    def sync_lapsed_trial_leads(self, *, now_utc: datetime | None = None) -> int:
        """
        Save users whose trial ended without payment. Returns count of newly recorded leads.
        """
        now = now_utc or datetime.now(UTC)
        cur = self._conn.execute(
            """
            SELECT chat_id, first_name, trial_activated_at, subscription_ends_at
            FROM subscribers
            WHERE trial_activated_at IS NOT NULL
              AND paid_at IS NULL
              AND subscription_ends_at IS NOT NULL
              AND subscription_ends_at < ?
              AND COALESCE(is_owner, 0) = 0
              AND chat_id NOT IN (SELECT chat_id FROM banned_subscribers)
            """,
            (now.isoformat(),),
        )
        new_count = 0
        for row in cur.fetchall():
            cid = str(row["chat_id"])
            exists = self._conn.execute(
                "SELECT 1 FROM lapsed_trial_leads WHERE chat_id = ?",
                (cid,),
            ).fetchone()
            fn = row["first_name"]
            name = str(fn).strip() if fn is not None else None
            if name == "":
                name = None
            trial_end = self._parse_dt_optional(row["subscription_ends_at"])
            if trial_end is None:
                continue
            trial_start = self._parse_dt_optional(row["trial_activated_at"])
            if exists:
                self._conn.execute(
                    """
                    UPDATE lapsed_trial_leads SET
                      first_name = COALESCE(?, first_name),
                      trial_started_at = COALESCE(?, trial_started_at),
                      trial_ended_at = ?
                    WHERE chat_id = ?
                    """,
                    (name, trial_start.isoformat() if trial_start else None, trial_end.isoformat(), cid),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO lapsed_trial_leads (
                      chat_id, first_name, trial_started_at, trial_ended_at, recorded_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        name,
                        trial_start.isoformat() if trial_start else None,
                        trial_end.isoformat(),
                        now.isoformat(),
                    ),
                )
                new_count += 1
        self._conn.commit()
        return new_count

    def list_lapsed_trial_leads(self) -> list[LapsedTrialLead]:
        cur = self._conn.execute(
            """
            SELECT chat_id, first_name, trial_started_at, trial_ended_at,
                   recorded_at, last_promo_sent_at, promo_send_count
            FROM lapsed_trial_leads
            ORDER BY trial_ended_at DESC
            """
        )
        out: list[LapsedTrialLead] = []
        for row in cur.fetchall():
            recorded = self._parse_dt_optional(row["recorded_at"])
            if recorded is None:
                continue
            out.append(
                LapsedTrialLead(
                    chat_id=str(row["chat_id"]),
                    first_name=(
                        str(row["first_name"]).strip() if row["first_name"] is not None else None
                    )
                    or None,
                    trial_started_at=self._parse_dt_optional(row["trial_started_at"]),
                    trial_ended_at=self._parse_dt_optional(row["trial_ended_at"]),
                    recorded_at=recorded,
                    last_promo_sent_at=self._parse_dt_optional(row["last_promo_sent_at"]),
                    promo_send_count=int(row["promo_send_count"] or 0),
                )
            )
        return out

    def get_lapsed_trial_chat_ids(self) -> list[str]:
        cur = self._conn.execute(
            """
            SELECT chat_id FROM lapsed_trial_leads
            WHERE chat_id NOT IN (SELECT chat_id FROM banned_subscribers)
            ORDER BY chat_id
            """
        )
        return [str(row[0]) for row in cur.fetchall()]

    def count_lapsed_trial_leads(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM lapsed_trial_leads")
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def mark_lapsed_trial_promo_sent(self, chat_ids: list[str], *, now_utc: datetime | None = None) -> None:
        if not chat_ids:
            return
        now = (now_utc or datetime.now(UTC)).isoformat()
        for cid in chat_ids:
            self._conn.execute(
                """
                UPDATE lapsed_trial_leads SET
                  last_promo_sent_at = ?,
                  promo_send_count = COALESCE(promo_send_count, 0) + 1
                WHERE chat_id = ?
                """,
                (now, cid),
            )
        self._conn.commit()

    def grant_admin_reference_access(
        self,
        chat_id: str,
        *,
        grant_hours: float,
        first_name: str | None = None,
        now_utc: datetime | None = None,
    ) -> datetime | None:
        """Admin-granted temporary free alerts (not owner, not paid)."""
        if self.subscriber_is_owner(chat_id):
            return None
        now = now_utc or datetime.now(UTC)
        self._conn.execute("DELETE FROM banned_subscribers WHERE chat_id = ?", (chat_id,))
        current = self.get_subscription_ends_at(chat_id)
        base = now
        if current is not None and current > now:
            base = current
        ends = base + timedelta(hours=float(grant_hours))
        fn = (first_name or "").strip() or None
        self._conn.execute(
            """
            INSERT INTO subscribers (
              chat_id, first_name, subscription_ends_at, admin_reference_granted_at,
              expiry_reminder_sent, final_expiry_reminder_sent, trial_ending_reminder_sent
            )
            VALUES (?, ?, ?, ?, 0, 0, 0)
            ON CONFLICT(chat_id) DO UPDATE SET
              first_name = COALESCE(excluded.first_name, subscribers.first_name),
              subscription_ends_at = excluded.subscription_ends_at,
              admin_reference_granted_at = excluded.admin_reference_granted_at,
              expiry_reminder_sent = 0,
              final_expiry_reminder_sent = 0,
              trial_ending_reminder_sent = 0
            """,
            (chat_id, fn, ends.isoformat(), now.isoformat()),
        )
        self._conn.commit()
        self.remove_lapsed_trial_lead(chat_id)
        return ends

    def ensure_owner_vip(self, chat_id: str, *, first_name: str | None = None) -> None:
        """Bot owner: lifetime access, no payment or expiry reminders."""
        ends = datetime(2099, 12, 31, 23, 59, 59, tzinfo=UTC)
        fn = (first_name or "").strip() or None
        self._conn.execute(
            """
            INSERT INTO subscribers (
              chat_id, first_name, is_owner, subscription_ends_at,
              expiry_reminder_sent, final_expiry_reminder_sent, trial_ending_reminder_sent
            )
            VALUES (?, ?, 1, ?, 1, 1, 1)
            ON CONFLICT(chat_id) DO UPDATE SET
              first_name = COALESCE(excluded.first_name, subscribers.first_name),
              is_owner = 1,
              subscription_ends_at = excluded.subscription_ends_at,
              expiry_reminder_sent = 1,
              final_expiry_reminder_sent = 1,
              trial_ending_reminder_sent = 1
            """,
            (chat_id, fn, ends.isoformat()),
        )
        self._conn.commit()

    def subscriber_has_active_subscription(self, chat_id: str, *, now_utc: datetime | None = None) -> bool:
        if self.subscriber_is_owner(chat_id):
            return True
        ends = self.get_subscription_ends_at(chat_id)
        if ends is None:
            return False
        now = now_utc or datetime.now(UTC)
        return ends > now

    def activate_trial_subscription(self, chat_id: str, *, trial_hours: float, now_utc: datetime | None = None) -> datetime:
        """Start or extend trial from now; clears expiry reminder flags."""
        now = now_utc or datetime.now(UTC)
        ends = now + timedelta(hours=float(trial_hours))
        self._conn.execute(
            """
            INSERT INTO subscribers (
              chat_id, subscription_ends_at, trial_activated_at,
              expiry_reminder_sent, final_expiry_reminder_sent, trial_ending_reminder_sent
            )
            VALUES (?, ?, ?, 0, 0, 0)
            ON CONFLICT(chat_id) DO UPDATE SET
              subscription_ends_at = excluded.subscription_ends_at,
              trial_activated_at = COALESCE(subscribers.trial_activated_at, excluded.trial_activated_at),
              expiry_reminder_sent = 0,
              final_expiry_reminder_sent = 0,
              trial_ending_reminder_sent = 0
            """,
            (chat_id, ends.isoformat(), now.isoformat()),
        )
        self._conn.commit()
        return ends

    def grant_free_trial_to_all(
        self, *, trial_hours: float, now_utc: datetime | None = None
    ) -> tuple[int, int]:
        """
        Admin promo: give all non-owner, non-banned subscribers free access until (now + trial_hours),
        without reducing anyone's existing later subscription end time.

        Returns (total_updated_rows, trial_started_rows).
        """
        now = now_utc or datetime.now(UTC)
        target_end = now + timedelta(hours=float(trial_hours))
        target_end_iso = target_end.isoformat()
        now_iso = now.isoformat()

        # Extend access window for everyone (never shorten).
        cur1 = self._conn.execute(
            """
            UPDATE subscribers
            SET
              subscription_ends_at = CASE
                WHEN subscription_ends_at IS NULL THEN ?
                WHEN subscription_ends_at < ? THEN ?
                ELSE subscription_ends_at
              END,
              expiry_reminder_sent = 0,
              final_expiry_reminder_sent = 0,
              trial_ending_reminder_sent = 0
            WHERE COALESCE(is_owner, 0) = 0
              AND chat_id NOT IN (SELECT chat_id FROM banned_subscribers)
            """,
            (target_end_iso, target_end_iso, target_end_iso),
        )

        # Mark trial start only for unpaid users who never had a trial before.
        cur2 = self._conn.execute(
            """
            UPDATE subscribers
            SET trial_activated_at = ?
            WHERE COALESCE(is_owner, 0) = 0
              AND chat_id NOT IN (SELECT chat_id FROM banned_subscribers)
              AND paid_at IS NULL
              AND (trial_activated_at IS NULL OR trial_activated_at = '')
              AND (subscription_ends_at IS NOT NULL AND subscription_ends_at >= ?)
            """,
            (now_iso, now_iso),
        )
        self._conn.commit()
        return int(cur1.rowcount or 0), int(cur2.rowcount or 0)

    def activate_paid_subscription(
        self,
        chat_id: str,
        *,
        paid_days: float,
        now_utc: datetime | None = None,
        payment: dict[str, Any] | None = None,
    ) -> datetime:
        """Extend subscription after payment; resets reminder flags for the new period."""
        now = now_utc or datetime.now(UTC)
        current = self.get_subscription_ends_at(chat_id)
        base = now
        if current is not None and current > now:
            base = current
        ends = base + timedelta(days=float(paid_days))
        pay_currency = None
        pay_amount = None
        pay_payload = None
        pay_charge = None
        if payment:
            pay_currency = str(payment.get("currency") or "") or None
            raw_amt = payment.get("total_amount")
            try:
                pay_amount = int(raw_amt) if raw_amt is not None else None
            except (TypeError, ValueError):
                pay_amount = None
            pay_payload = str(payment.get("invoice_payload") or "") or None
            pay_charge = str(payment.get("telegram_payment_charge_id") or "") or None
        if payment:
            self._conn.execute(
                """
                INSERT INTO subscribers (
                  chat_id, subscription_ends_at, paid_at,
                  last_payment_currency, last_payment_total_amount,
                  last_payment_invoice_payload, last_payment_telegram_charge_id,
                  expiry_reminder_sent, final_expiry_reminder_sent, trial_ending_reminder_sent
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0)
                ON CONFLICT(chat_id) DO UPDATE SET
                  subscription_ends_at = excluded.subscription_ends_at,
                  paid_at = excluded.paid_at,
                  last_payment_currency = excluded.last_payment_currency,
                  last_payment_total_amount = excluded.last_payment_total_amount,
                  last_payment_invoice_payload = excluded.last_payment_invoice_payload,
                  last_payment_telegram_charge_id = excluded.last_payment_telegram_charge_id,
                  expiry_reminder_sent = 0,
                  final_expiry_reminder_sent = 0,
                  trial_ending_reminder_sent = 0
                """,
                (
                    chat_id,
                    ends.isoformat(),
                    now.isoformat(),
                    pay_currency,
                    pay_amount,
                    pay_payload,
                    pay_charge,
                ),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO subscribers (chat_id, subscription_ends_at, paid_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  subscription_ends_at = excluded.subscription_ends_at,
                  paid_at = excluded.paid_at,
                  expiry_reminder_sent = 0,
                  final_expiry_reminder_sent = 0,
                  trial_ending_reminder_sent = 0
                """,
                (chat_id, ends.isoformat(), now.isoformat()),
            )
        self._conn.commit()
        self.remove_lapsed_trial_lead(chat_id)
        return ends

    def is_banned(self, chat_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM banned_subscribers WHERE chat_id = ? LIMIT 1",
            (chat_id,),
        )
        return cur.fetchone() is not None

    def kick_subscriber(self, chat_id: str, *, reason: str | None = None, now_utc: datetime | None = None) -> bool:
        """Ban chat_id and remove from subscribers/filters."""
        if self.subscriber_is_owner(chat_id):
            return False
        now = now_utc or datetime.now(UTC)
        self._conn.execute(
            """
            INSERT INTO banned_subscribers (chat_id, banned_at, reason)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET banned_at = excluded.banned_at, reason = excluded.reason
            """,
            (chat_id, now.isoformat(), reason),
        )
        self.remove_subscriber(chat_id)
        return True

    def _row_to_subscriber_admin(self, row: sqlite3.Row) -> SubscriberAdminRow:
        def _parse_dt(raw: object) -> datetime | None:
            if raw is None:
                return None
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)

        fn = row["first_name"]
        name = str(fn).strip() if fn is not None else None
        if name == "":
            name = None
        raw_amt = row["last_payment_total_amount"]
        try:
            amt = int(raw_amt) if raw_amt is not None else None
        except (TypeError, ValueError):
            amt = None
        return SubscriberAdminRow(
            chat_id=str(row["chat_id"]),
            first_name=name,
            trial_activated_at=_parse_dt(row["trial_activated_at"]),
            paid_at=_parse_dt(row["paid_at"]),
            subscription_ends_at=_parse_dt(row["subscription_ends_at"]),
            alerts_muted=int(row["alerts_muted"] or 0) != 0,
            location_choice=str(row["location_choice"] or "all"),
            location_match_substr=(
                (str(row["location_match_substr"]).strip() or None)
                if row["location_match_substr"] is not None
                else None
            ),
            last_payment_currency=(
                str(row["last_payment_currency"]) if row["last_payment_currency"] else None
            ),
            last_payment_total_amount=amt,
            last_payment_invoice_payload=(
                str(row["last_payment_invoice_payload"])
                if row["last_payment_invoice_payload"]
                else None
            ),
            last_payment_telegram_charge_id=(
                str(row["last_payment_telegram_charge_id"])
                if row["last_payment_telegram_charge_id"]
                else None
            ),
        )

    def _subscriber_admin_select_sql(self) -> str:
        return """
            SELECT
              s.chat_id,
              s.first_name,
              s.trial_activated_at,
              s.paid_at,
              s.subscription_ends_at,
              COALESCE(f.alerts_muted, 0) AS alerts_muted,
              COALESCE(f.location_choice, 'all') AS location_choice,
              f.location_match_substr,
              s.last_payment_currency,
              s.last_payment_total_amount,
              s.last_payment_invoice_payload,
              s.last_payment_telegram_charge_id
            FROM subscribers s
            LEFT JOIN subscriber_filters f ON f.chat_id = s.chat_id
        """

    def list_trial_only_subscribers(self) -> list[SubscriberAdminRow]:
        """Users who activated free trial and have not paid."""
        cur = self._conn.execute(
            self._subscriber_admin_select_sql()
            + """
            WHERE s.trial_activated_at IS NOT NULL AND s.paid_at IS NULL
              AND COALESCE(s.is_owner, 0) = 0
            ORDER BY s.trial_activated_at DESC
            """
        )
        return [self._row_to_subscriber_admin(row) for row in cur.fetchall()]

    def list_paid_subscribers(self) -> list[SubscriberAdminRow]:
        """Users with at least one recorded payment."""
        cur = self._conn.execute(
            self._subscriber_admin_select_sql()
            + """
            WHERE s.paid_at IS NOT NULL AND COALESCE(s.is_owner, 0) = 0
            ORDER BY s.paid_at DESC
            """
        )
        return [self._row_to_subscriber_admin(row) for row in cur.fetchall()]

    def mark_expiry_reminder_sent(self, chat_id: str) -> None:
        self._conn.execute(
            "UPDATE subscribers SET expiry_reminder_sent = 1 WHERE chat_id = ?",
            (chat_id,),
        )
        self._conn.commit()

    def mark_final_expiry_reminder_sent(self, chat_id: str) -> None:
        self._conn.execute(
            "UPDATE subscribers SET final_expiry_reminder_sent = 1 WHERE chat_id = ?",
            (chat_id,),
        )
        self._conn.commit()

    def mark_trial_ending_reminder_sent(self, chat_id: str) -> None:
        self._conn.execute(
            "UPDATE subscribers SET trial_ending_reminder_sent = 1 WHERE chat_id = ?",
            (chat_id,),
        )
        self._conn.commit()

    def get_subscribers_needing_expiry_reminder(
        self,
        *,
        reminder_hours_before: float,
        now_utc: datetime | None = None,
    ) -> list[tuple[str, str | None, datetime]]:
        """
        Subscribers whose subscription ends within ``reminder_hours_before`` hours
        (and not yet past), with reminder not yet sent.
        Returns (chat_id, first_name, subscription_ends_at).
        """
        now = now_utc or datetime.now(UTC)
        window_end = now + timedelta(hours=float(reminder_hours_before))
        cur = self._conn.execute(
            """
            SELECT chat_id, first_name, subscription_ends_at
            FROM subscribers
            WHERE subscription_ends_at IS NOT NULL
              AND COALESCE(is_owner, 0) = 0
              AND COALESCE(expiry_reminder_sent, 0) = 0
              AND subscription_ends_at > ?
              AND subscription_ends_at <= ?
            """,
            (now.isoformat(), window_end.isoformat()),
        )
        out: list[tuple[str, str | None, datetime]] = []
        for row in cur.fetchall():
            raw_end = row["subscription_ends_at"]
            if raw_end is None:
                continue
            try:
                ends = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
            except ValueError:
                continue
            if ends.tzinfo is None:
                ends = ends.replace(tzinfo=UTC)
            else:
                ends = ends.astimezone(UTC)
            fn = row["first_name"]
            name = str(fn).strip() if fn is not None else None
            if name == "":
                name = None
            out.append((str(row["chat_id"]), name, ends))
        return out

    def get_subscribers_needing_final_expiry_reminder(
        self,
        *,
        reminder_minutes_before: float,
        now_utc: datetime | None = None,
    ) -> list[tuple[str, str | None, datetime]]:
        """Subscribers ending within ``reminder_minutes_before`` minutes (20-min pay reminder)."""
        now = now_utc or datetime.now(UTC)
        window_end = now + timedelta(minutes=float(reminder_minutes_before))
        cur = self._conn.execute(
            """
            SELECT chat_id, first_name, subscription_ends_at
            FROM subscribers
            WHERE subscription_ends_at IS NOT NULL
              AND COALESCE(is_owner, 0) = 0
              AND COALESCE(final_expiry_reminder_sent, 0) = 0
              AND subscription_ends_at > ?
              AND subscription_ends_at <= ?
            """,
            (now.isoformat(), window_end.isoformat()),
        )
        out: list[tuple[str, str | None, datetime]] = []
        for row in cur.fetchall():
            raw_end = row["subscription_ends_at"]
            if raw_end is None:
                continue
            try:
                ends = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
            except ValueError:
                continue
            if ends.tzinfo is None:
                ends = ends.replace(tzinfo=UTC)
            else:
                ends = ends.astimezone(UTC)
            fn = row["first_name"]
            name = str(fn).strip() if fn is not None else None
            if name == "":
                name = None
            out.append((str(row["chat_id"]), name, ends))
        return out

    def get_subscribers_needing_trial_ending_reminder(
        self,
        *,
        reminder_minutes_before: float,
        now_utc: datetime | None = None,
    ) -> list[tuple[str, str | None, datetime]]:
        """Final trial warning (e.g. 1 minute before auto-stop)."""
        now = now_utc or datetime.now(UTC)
        window_end = now + timedelta(minutes=float(reminder_minutes_before))
        cur = self._conn.execute(
            """
            SELECT chat_id, first_name, subscription_ends_at
            FROM subscribers
            WHERE subscription_ends_at IS NOT NULL
              AND COALESCE(is_owner, 0) = 0
              AND COALESCE(trial_ending_reminder_sent, 0) = 0
              AND subscription_ends_at > ?
              AND subscription_ends_at <= ?
            """,
            (now.isoformat(), window_end.isoformat()),
        )
        out: list[tuple[str, str | None, datetime]] = []
        for row in cur.fetchall():
            raw_end = row["subscription_ends_at"]
            if raw_end is None:
                continue
            try:
                ends = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
            except ValueError:
                continue
            if ends.tzinfo is None:
                ends = ends.replace(tzinfo=UTC)
            else:
                ends = ends.astimezone(UTC)
            fn = row["first_name"]
            name = str(fn).strip() if fn is not None else None
            if name == "":
                name = None
            out.append((str(row["chat_id"]), name, ends))
        return out

    def remove_subscriber(self, chat_id: str) -> None:
        if self.subscriber_is_owner(chat_id):
            return
        self._conn.execute("DELETE FROM setlocation_keyboard WHERE chat_id = ?", (chat_id,))
        self._conn.execute("DELETE FROM subscriber_filters WHERE chat_id = ?", (chat_id,))
        self._conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        self._conn.commit()

    def get_all_subscribers(self) -> list[str]:
        cur = self._conn.execute("SELECT chat_id FROM subscribers")
        return [str(row[0]) for row in cur.fetchall()]

    def get_all_non_banned_subscribers(self) -> list[str]:
        """All chats excluding banned and owner VIP."""
        cur = self._conn.execute(
            """
            SELECT chat_id FROM subscribers
            WHERE chat_id NOT IN (SELECT chat_id FROM banned_subscribers)
              AND COALESCE(is_owner, 0) = 0
            ORDER BY chat_id
            """
        )
        return [str(row[0]) for row in cur.fetchall()]

    def get_subscribers_for_alerts(self, *, now_utc: datetime | None = None) -> list[str]:
        """Active trial/subscription chats that have not muted job / shift Telegram alerts."""
        now = (now_utc or datetime.now(UTC)).isoformat()
        cur = self._conn.execute(
            """
            SELECT s.chat_id FROM subscribers s
            LEFT JOIN subscriber_filters f ON f.chat_id = s.chat_id
            WHERE COALESCE(f.alerts_muted, 0) = 0
              AND s.chat_id NOT IN (SELECT chat_id FROM banned_subscribers)
              AND (
                COALESCE(s.is_owner, 0) = 1
                OR (
                  s.subscription_ends_at IS NOT NULL
                  AND s.subscription_ends_at > ?
                )
              )
            ORDER BY s.chat_id
            """,
            (now,),
        )
        return [str(row[0]) for row in cur.fetchall()]

    def subscriber_alerts_muted(self, chat_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT COALESCE(alerts_muted, 0) AS m FROM subscriber_filters WHERE chat_id = ?",
            (chat_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        return int(row["m"] or 0) != 0

    def set_subscriber_alerts_muted(self, chat_id: str, *, muted: bool) -> None:
        v = 1 if muted else 0
        self._conn.execute(
            """
            INSERT INTO subscriber_filters (chat_id, location_choice, location_match_substr, alerts_muted)
            VALUES (?, 'all', NULL, ?)
            ON CONFLICT(chat_id) DO UPDATE SET alerts_muted = excluded.alerts_muted
            """,
            (chat_id, v),
        )
        self._conn.commit()

    def get_subscriber_count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM subscribers")
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def record_subscriber_interaction(
        self,
        chat_id: str,
        *,
        reaction: bool = False,
        now_utc: datetime | None = None,
    ) -> None:
        """Track bot activity (messages, buttons, emoji on alerts). Not the same as Telegram read receipts."""
        now = now_utc or datetime.now(UTC)
        now_iso = now.isoformat()
        if reaction:
            self._conn.execute(
                """
                INSERT INTO subscribers (chat_id, last_interaction_at, interaction_count, last_reaction_at, reaction_count)
                VALUES (?, ?, 1, ?, 1)
                ON CONFLICT(chat_id) DO UPDATE SET
                  last_interaction_at = excluded.last_interaction_at,
                  interaction_count = COALESCE(subscribers.interaction_count, 0) + 1,
                  last_reaction_at = excluded.last_reaction_at,
                  reaction_count = COALESCE(subscribers.reaction_count, 0) + 1
                """,
                (chat_id, now_iso, now_iso),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO subscribers (chat_id, last_interaction_at, interaction_count)
                VALUES (?, ?, 1)
                ON CONFLICT(chat_id) DO UPDATE SET
                  last_interaction_at = excluded.last_interaction_at,
                  interaction_count = COALESCE(subscribers.interaction_count, 0) + 1
                """,
                (chat_id, now_iso),
            )
        self._conn.commit()

    def get_engagement_stats(self, *, now_utc: datetime | None = None) -> EngagementStats:
        now = now_utc or datetime.now(UTC)
        now_iso = now.isoformat()
        day_ago = (now - timedelta(hours=24)).isoformat()
        week_ago = (now - timedelta(days=7)).isoformat()

        total = int(self._conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0])
        sub_active = int(
            self._conn.execute(
                """
                SELECT COUNT(*) FROM subscribers
                WHERE COALESCE(is_owner, 0) = 1
                   OR (subscription_ends_at IS NOT NULL AND subscription_ends_at > ?)
                """,
                (now_iso,),
            ).fetchone()[0]
        )
        active_24h = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM subscribers WHERE last_interaction_at IS NOT NULL AND last_interaction_at > ?",
                (day_ago,),
            ).fetchone()[0]
        )
        active_7d = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM subscribers WHERE last_interaction_at IS NOT NULL AND last_interaction_at > ?",
                (week_ago,),
            ).fetchone()[0]
        )
        reacted_ever = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM subscribers WHERE COALESCE(reaction_count, 0) > 0"
            ).fetchone()[0]
        )
        reacted_7d = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM subscribers WHERE last_reaction_at IS NOT NULL AND last_reaction_at > ?",
                (week_ago,),
            ).fetchone()[0]
        )
        muted = int(
            self._conn.execute(
                """
                SELECT COUNT(*) FROM subscribers s
                LEFT JOIN subscriber_filters f ON f.chat_id = s.chat_id
                WHERE COALESCE(f.alerts_muted, 0) = 1
                """
            ).fetchone()[0]
        )
        avg_row = self._conn.execute(
            """
            SELECT AVG(COALESCE(interaction_count, 0)) FROM subscribers
            WHERE last_interaction_at IS NOT NULL AND last_interaction_at > ?
            """,
            (week_ago,),
        ).fetchone()
        avg_i = float(avg_row[0]) if avg_row and avg_row[0] is not None else 0.0

        cur = self._conn.execute(
            """
            SELECT chat_id, first_name, COALESCE(interaction_count, 0), last_interaction_at
            FROM subscribers
            WHERE last_interaction_at IS NOT NULL AND last_interaction_at > ?
            ORDER BY interaction_count DESC, last_interaction_at DESC
            LIMIT 8
            """,
            (week_ago,),
        )
        top = [
            (str(r[0]), r[1], int(r[2]), str(r[3]))
            for r in cur.fetchall()
        ]
        return EngagementStats(
            total_subscribers=total,
            subscription_active=sub_active,
            active_24h=active_24h,
            active_7d=active_7d,
            reacted_ever=reacted_ever,
            reacted_7d=reacted_7d,
            muted=muted,
            avg_interactions_active_7d=round(avg_i, 1),
            top_active_7d=top,
        )

    def count_jobs(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM jobs")
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def get_subscriber_location_choices_for(self, chat_ids: list[str]) -> dict[str, str]:
        """chat_id -> location_choice ('all' if no row)."""
        prefs = self.get_subscriber_alert_prefs(chat_ids)
        return {cid: prefs[cid][0] for cid in prefs}

    def get_subscriber_alert_prefs(
        self, chat_ids: list[str]
    ) -> dict[str, tuple[str, str | None]]:
        """
        chat_id -> (location_choice, location_match_substr).
        ``location_match_substr`` set when user picked a real job location from the DB list.
        """
        if not chat_ids:
            return {}
        out: dict[str, tuple[str, str | None]] = {
            str(cid): ("all", None) for cid in chat_ids
        }
        placeholders = ",".join("?" * len(chat_ids))
        cur = self._conn.execute(
            f"""
            SELECT chat_id, location_choice, location_match_substr
            FROM subscriber_filters WHERE chat_id IN ({placeholders})
            """,
            chat_ids,
        )
        for row in cur.fetchall():
            cid = str(row["chat_id"])
            choice = str(row["location_choice"] or "all").strip() or "all"
            raw_ms = row["location_match_substr"]
            ms = str(raw_ms).strip() if raw_ms is not None else None
            if ms == "":
                ms = None
            out[cid] = (choice, ms)
        return out

    def set_subscriber_location_choice(self, chat_id: str, location_choice: str) -> None:
        """Legacy preset name (``config.alert_locations``) or ``all`` — clears DB substring filter."""
        choice = location_choice.strip() or "all"
        self._conn.execute(
            """
            INSERT INTO subscriber_filters (chat_id, location_choice, location_match_substr)
            VALUES (?, ?, NULL)
            ON CONFLICT(chat_id) DO UPDATE SET
              location_choice = excluded.location_choice,
              location_match_substr = NULL
            """,
            (chat_id, choice),
        )
        self._conn.commit()

    def set_subscriber_location_match_substr(self, chat_id: str, match_substr: str | None) -> None:
        """
        Filter alerts to jobs whose location/title contains this substring (case-insensitive).
        Pass None or empty to clear (same as all locations for per-subscriber filter).
        """
        ms = match_substr.strip() if match_substr else ""
        if not ms:
            self._conn.execute(
                """
                INSERT INTO subscriber_filters (chat_id, location_choice, location_match_substr)
                VALUES (?, 'all', NULL)
                ON CONFLICT(chat_id) DO UPDATE SET
                  location_choice = 'all',
                  location_match_substr = NULL
                """,
                (chat_id,),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO subscriber_filters (chat_id, location_choice, location_match_substr)
                VALUES (?, 'all', ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  location_choice = 'all',
                  location_match_substr = excluded.location_match_substr
                """,
                (chat_id, ms),
            )
        self._conn.commit()

    def get_distinct_job_locations(
        self,
        limit: int = 24,
        *,
        exclude_substrings: Sequence[str] | None = None,
    ) -> list[str]:
        """Distinct non-empty ``jobs.location`` values, most common first (real data for /setlocation)."""
        excludes = merge_setlocation_excludes(exclude_substrings)
        lim = max(1, min(40, int(limit)))
        fetch_cap = lim * 5 if excludes else lim
        cur = self._conn.execute(
            f"""
            SELECT TRIM(location) AS loc, COUNT(*) AS c
            FROM jobs
            WHERE location IS NOT NULL AND TRIM(location) != ''
            GROUP BY TRIM(location)
            ORDER BY c DESC, loc ASC
            LIMIT {fetch_cap}
            """
        )
        raw = [str(row["loc"]) for row in cur.fetchall() if row["loc"]]
        return filter_location_strings(raw, excludes)[:lim]

    def save_setlocation_keyboard(self, chat_id: str, message_id: int, choices: list[str]) -> None:
        self._conn.execute("DELETE FROM setlocation_keyboard WHERE chat_id = ?", (chat_id,))
        self._conn.execute(
            """
            INSERT INTO setlocation_keyboard (chat_id, message_id, choices_json)
            VALUES (?, ?, ?)
            """,
            (chat_id, int(message_id), json.dumps(choices, ensure_ascii=False)),
        )
        self._conn.commit()

    def get_setlocation_keyboard(self, chat_id: str, message_id: int) -> list[str] | None:
        cur = self._conn.execute(
            "SELECT choices_json FROM setlocation_keyboard WHERE chat_id = ? AND message_id = ?",
            (chat_id, int(message_id)),
        )
        row = cur.fetchone()
        if row is None:
            return None
        try:
            data = json.loads(str(row["choices_json"]))
            if isinstance(data, list):
                return [str(x) for x in data]
        except (json.JSONDecodeError, TypeError):
            return None
        return None

    def get_subscriber_filter_row(self, chat_id: str) -> tuple[str, float | None, str | None]:
        """Returns (location_choice, min_pay or None, location_match_substr or None)."""
        cur = self._conn.execute(
            "SELECT location_choice, min_pay, location_match_substr FROM subscriber_filters WHERE chat_id = ?",
            (chat_id,),
        )
        row = cur.fetchone()
        if row is None:
            return "all", None, None
        loc = str(row["location_choice"] or "all").strip() or "all"
        mp = row["min_pay"]
        try:
            pay = float(mp) if mp is not None else None
        except (TypeError, ValueError):
            pay = None
        raw_ms = row["location_match_substr"]
        ms = str(raw_ms).strip() if raw_ms is not None else None
        if ms == "":
            ms = None
        return loc, pay, ms

    def upsert_job(
        self,
        *,
        key: str,
        job_id: str | None,
        url: str,
        source: str,
        source_url: str,
        title: str | None,
        location: str | None,
        pay_gbp_per_hour: float | None,
        pay_text: str | None,
        expected_pay_text: str | None,
        shift: str | None,
        posted_date_text: str | None,
        content_hash: str,
        now_utc: datetime,
        raw_metadata_json: str | None = None,
    ) -> UpsertResult:
        cur = self._conn.cursor()
        row = cur.execute("SELECT content_hash, first_seen_utc FROM jobs WHERE key = ?", (key,)).fetchone()
        if row is None and job_id:
            stripped = str(job_id).strip()
            if stripped:
                legacy = cur.execute(
                    "SELECT key, content_hash, first_seen_utc FROM jobs WHERE job_id = ? LIMIT 1",
                    (stripped,),
                ).fetchone()
                if legacy:
                    old_key = str(legacy["key"])
                    cur.execute("UPDATE jobs SET key = ? WHERE key = ?", (key, old_key))
                    self._conn.commit()
                    row = cur.execute(
                        "SELECT content_hash, first_seen_utc FROM jobs WHERE key = ?", (key,)
                    ).fetchone()
        if row is None:
            cur.execute(
                """
                INSERT INTO jobs(
                  key, job_id, url, source, source_url, title, location, pay_gbp_per_hour,
                  pay_text, expected_pay_text, shift, posted_date_text,
                  content_hash, first_seen_utc, last_seen_utc, raw_metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    key,
                    job_id,
                    url,
                    source,
                    source_url,
                    title,
                    location,
                    pay_gbp_per_hour,
                    pay_text,
                    expected_pay_text,
                    shift,
                    posted_date_text,
                    content_hash,
                    now_utc.isoformat(),
                    now_utc.isoformat(),
                    raw_metadata_json,
                ),
            )
            self._conn.commit()
            return UpsertResult(status="new", first_seen_utc=now_utc, last_seen_utc=now_utc)

        prev_hash = str(row["content_hash"])
        first_seen = datetime.fromisoformat(str(row["first_seen_utc"]).replace("Z", "+00:00"))
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=UTC)
        else:
            first_seen = first_seen.astimezone(UTC)
        if prev_hash != content_hash:
            cur.execute(
                """
                UPDATE jobs SET
                  job_id=?,
                  url=?,
                  source=?,
                  source_url=?,
                  title=?,
                  location=?,
                  pay_gbp_per_hour=?,
                  pay_text=?,
                  expected_pay_text=?,
                  shift=?,
                  posted_date_text=?,
                  content_hash=?,
                  last_seen_utc=?,
                  raw_metadata_json=?
                WHERE key=?
                """,
                (
                    job_id,
                    url,
                    source,
                    source_url,
                    title,
                    location,
                    pay_gbp_per_hour,
                    pay_text,
                    expected_pay_text,
                    shift,
                    posted_date_text,
                    content_hash,
                    now_utc.isoformat(),
                    raw_metadata_json,
                    key,
                ),
            )
            self._conn.commit()
            return UpsertResult(status="updated", first_seen_utc=first_seen, last_seen_utc=now_utc)

        cur.execute("UPDATE jobs SET last_seen_utc=? WHERE key=?", (now_utc.isoformat(), key))
        self._conn.commit()
        return UpsertResult(status="unchanged", first_seen_utc=first_seen, last_seen_utc=now_utc)

    def _ensure_shift_analytics_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS shift_analytics (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              location_key TEXT NOT NULL,
              profile_id TEXT NOT NULL DEFAULT 'default',
              dropped_at_utc TEXT NOT NULL,
              weekday INTEGER NOT NULL,
              hour INTEGER NOT NULL,
              minute INTEGER NOT NULL,
              confidence TEXT NOT NULL DEFAULT 'normal',
              timezone TEXT NOT NULL DEFAULT 'Europe/London'
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_shift_analytics_loc ON shift_analytics(location_key)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_shift_analytics_dropped ON shift_analytics(dropped_at_utc)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS shift_peak_warnings_sent (
              location_key TEXT NOT NULL,
              slot_key TEXT NOT NULL,
              warned_at_utc TEXT NOT NULL,
              PRIMARY KEY (location_key, slot_key)
            )
            """
        )
        self._conn.commit()

    def record_shift_drop(
        self,
        *,
        location_key: str,
        profile_id: str,
        dropped_at_utc: datetime | None = None,
        weekday: int,
        hour: int,
        minute: int,
        confidence: str = "normal",
        timezone: str = "Europe/London",
    ) -> None:
        self._ensure_shift_analytics_schema()
        now = dropped_at_utc or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)
        self._conn.execute(
            """
            INSERT INTO shift_analytics (
              location_key, profile_id, dropped_at_utc, weekday, hour, minute, confidence, timezone
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                location_key.strip() or "default",
                (profile_id or "default").strip(),
                now.isoformat(),
                int(weekday),
                int(hour),
                int(minute),
                (confidence or "normal").strip(),
                (timezone or "Europe/London").strip(),
            ),
        )
        self._conn.commit()

    def list_shift_drop_events(self, *, limit: int = 500) -> list[dict[str, Any]]:
        self._ensure_shift_analytics_schema()
        cur = self._conn.execute(
            """
            SELECT location_key, profile_id, dropped_at_utc, weekday, hour, minute, confidence
            FROM shift_analytics
            ORDER BY dropped_at_utc DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        )
        return [dict(row) for row in cur.fetchall()]

    def count_shift_drops(self) -> int:
        self._ensure_shift_analytics_schema()
        cur = self._conn.execute("SELECT COUNT(*) FROM shift_analytics")
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def peak_warning_already_sent(self, location_key: str, slot_key: str) -> bool:
        self._ensure_shift_analytics_schema()
        cur = self._conn.execute(
            """
            SELECT 1 FROM shift_peak_warnings_sent
            WHERE location_key = ? AND slot_key = ?
            LIMIT 1
            """,
            (location_key, slot_key),
        )
        return cur.fetchone() is not None

    def mark_peak_warning_sent(
        self,
        location_key: str,
        slot_key: str,
        *,
        warned_at_utc: datetime | None = None,
    ) -> None:
        self._ensure_shift_analytics_schema()
        now = warned_at_utc or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO shift_peak_warnings_sent (location_key, slot_key, warned_at_utc)
            VALUES (?, ?, ?)
            """,
            (location_key, slot_key, now.isoformat()),
        )
        self._conn.commit()

    def record_stripe_webhook_event(self, event_id: str) -> bool:
        """Record Stripe webhook event id; returns False if already processed (idempotent)."""
        eid = (event_id or "").strip()
        if not eid:
            return True
        now = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO stripe_webhook_events (event_id, received_at) VALUES (?, ?)",
            (eid, now),
        )
        self._conn.commit()
        return int(cur.rowcount or 0) > 0

    def record_stripe_checkout_session(self, session_id: str, chat_id: str, plan_id: str) -> bool:
        """Returns False if this checkout session was already applied (duplicate webhook/redirect)."""
        sid = (session_id or "").strip()
        if not sid:
            return True
        now = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO stripe_checkout_sessions (session_id, chat_id, plan_id, processed_at)
            VALUES (?, ?, ?, ?)
            """,
            (sid, chat_id, plan_id.lower(), now),
        )
        self._conn.commit()
        return int(cur.rowcount or 0) > 0

    def paid_plan_recently_activated(
        self, chat_id: str, plan_id: str, *, within_minutes: float = 45.0
    ) -> bool:
        """True when this plan was already applied for chat_id very recently (Stripe webhook path)."""
        cur = self._conn.execute(
            """
            SELECT paid_at, last_payment_invoice_payload
            FROM subscribers WHERE chat_id = ?
            """,
            (chat_id,),
        )
        row = cur.fetchone()
        if row is None or row["paid_at"] is None:
            return False
        payload = str(row["last_payment_invoice_payload"] or "")
        expected = f"stripe_sub_{plan_id.lower()}_{chat_id}"
        if payload != expected:
            return False
        paid = self._parse_dt_optional(row["paid_at"])
        if paid is None:
            return False
        return (datetime.now(UTC) - paid.astimezone(UTC)) <= timedelta(minutes=float(within_minutes))

    def purge_old_jobs(self, *, days: int = 30, now_utc: datetime | None = None) -> int:
        """Remove jobs that have not been seen for more than `days` days."""
        now = now_utc or datetime.now(UTC)
        threshold = (now - timedelta(days=days)).isoformat()
        cur = self._conn.execute("DELETE FROM jobs WHERE last_seen_utc < ?", (threshold,))
        self._conn.commit()
        return int(cur.rowcount or 0)

    def get_job_by_key(self, key: str) -> Any | None:
        from app.models.job import JobListing
        cur = self._conn.execute("SELECT * FROM jobs WHERE key = ?", (key,))
        row = cur.fetchone()
        if row is None:
            return None
        raw_meta = {}
        # Safely handle raw_metadata_json if column exists in the row
        if "raw_metadata_json" in row.keys() and row["raw_metadata_json"]:
            try:
                raw_meta = json.loads(str(row["raw_metadata_json"]))
            except Exception:
                pass
        return JobListing(
            source=str(row["source"]),
            source_url=str(row["source_url"]),
            job_id=str(row["job_id"]) if row["job_id"] else None,
            url=str(row["url"]),
            title=str(row["title"]) if row["title"] else None,
            location=str(row["location"]) if row["location"] else None,
            pay_gbp_per_hour=float(row["pay_gbp_per_hour"]) if row["pay_gbp_per_hour"] is not None else None,
            pay_text=str(row["pay_text"]) if row["pay_text"] else None,
            expected_pay_text=str(row["expected_pay_text"]) if row["expected_pay_text"] else None,
            shift=str(row["shift"]) if row["shift"] else None,
            posted_date_text=str(row["posted_date_text"]) if row["posted_date_text"] else None,
            raw_metadata=raw_meta,
        )

    def _ensure_sent_alerts_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_alerts (
              job_key TEXT,
              chat_id TEXT,
              message_id INTEGER NOT NULL,
              sent_at_utc TEXT NOT NULL,
              PRIMARY KEY (job_key, chat_id)
            )
            """
        )
        self._conn.commit()

    def record_sent_alert(self, job_key: str, chat_id: str, message_id: int, now_utc: datetime) -> None:
        self._ensure_sent_alerts_schema()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO sent_alerts (job_key, chat_id, message_id, sent_at_utc)
            VALUES (?, ?, ?, ?)
            """,
            (job_key, chat_id, int(message_id), now_utc.isoformat()),
        )
        self._conn.commit()

    def get_active_alerts(self) -> list[dict[str, Any]]:
        self._ensure_sent_alerts_schema()
        cur = self._conn.execute(
            "SELECT job_key, chat_id, message_id, sent_at_utc FROM sent_alerts"
        )
        out = []
        for r in cur.fetchall():
            out.append({
                "job_key": str(r["job_key"]),
                "chat_id": str(r["chat_id"]),
                "message_id": int(r["message_id"]),
                "sent_at_utc": str(r["sent_at_utc"]),
            })
        return out

    def delete_sent_alert(self, job_key: str, chat_id: str) -> None:
        self._ensure_sent_alerts_schema()
        self._conn.execute(
            "DELETE FROM sent_alerts WHERE job_key = ? AND chat_id = ?",
            (job_key, chat_id),
        )
        self._conn.commit()


