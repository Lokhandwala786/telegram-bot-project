from __future__ import annotations

from app.storage.sqlite import SqliteStore
from datetime import UTC, datetime, timedelta

from app.config import SubscriptionConfig
from app.subscriber_bot import (
    _expiry_reminder_html,
    _final_expiry_reminder_html,
    _trial_ending_reminder_html,
    _format_message_reaction_admin_notice,
    _format_subscriber_line,
    _help_admin_root_keyboard,
    _help_commands_keyboard,
    _help_text_for,
    _help_run_command_from_callback,
    _is_admin_chat,
    _price_pence_from_gbp,
    _stats_text,
    _subscribe_paid_keyboard,
    _subscribe_trial_keyboard,
    _welcome_message_html,
)


def test_format_message_reaction_admin_notice() -> None:
    text = _format_message_reaction_admin_notice(
        {
            "chat": {"id": 111, "type": "private"},
            "message_id": 42,
            "user": {"id": 222, "first_name": "Ali", "username": "ali_u"},
            "new_reaction": [{"type": "emoji", "emoji": "🔥"}],
        }
    )
    assert text is not None
    assert "🔥" in text
    assert "Ali" in text
    assert "ali_u" in text
    assert "111" in text


def test_format_message_reaction_admin_notice_skips_empty() -> None:
    assert _format_message_reaction_admin_notice({"new_reaction": []}) is None


def test_help_text_user_includes_mute_commands() -> None:
    h = _help_text_for("111", "999")
    assert "/mute" in h
    assert "/unmute" in h


def test_format_subscriber_line_private_with_username() -> None:
    line = _format_subscriber_line(
        "123",
        {"id": 123, "first_name": "Ada", "last_name": "Lovelace", "username": "adal", "type": "private"},
    )
    assert "123" in line
    assert "Ada Lovelace" in line
    assert "@adal" in line


def test_format_subscriber_line_none() -> None:
    assert "not available" in _format_subscriber_line("999", None)


def test_stats_text_includes_counts_and_timestamp() -> None:
    t = _stats_text(subscriber_count=3, jobs_count=42, tz_name="Europe/London")
    assert "Subscribers: 3" in t
    assert "42" in t
    assert "Generated:" in t
    assert "UTC" in t


def test_help_text_user_never_sees_admin_block() -> None:
    h = _help_text_for("111", "999")
    assert "/start" in h
    assert "/subscribers" not in h
    assert "Admin only" not in h


def test_help_text_admin_sees_admin_block() -> None:
    h = _help_text_for("999", "999")
    assert "/start" in h
    assert "/subscribers" in h
    assert "Admin only" in h


def test_help_text_no_admin_config_means_no_admin_block() -> None:
    h = _help_text_for("999", None)
    assert "/subscribers" not in h


def test_help_text_whitespace_admin_id_still_matches() -> None:
    h = _help_text_for("999", "  999  ")
    assert "/subscribers" in h


def test_is_admin_chat() -> None:
    assert _is_admin_chat("999", "999")
    assert _is_admin_chat("999", "  999  ")
    assert not _is_admin_chat("111", "999")


def test_help_admin_root_keyboard_has_user_and_admin() -> None:
    kb = _help_admin_root_keyboard()
    row = kb["inline_keyboard"][0]
    labels = {b["text"] for b in row}
    assert "👤 User" in labels
    assert "🔧 Admin" in labels
    flat = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b["callback_data"] == "help:users_testimonials" for b in flat)


def test_users_testimonials_keyboard_has_three_lists() -> None:
    from app.subscriber_bot import _users_testimonials_keyboard

    kb = _users_testimonials_keyboard(
        trial_count=3,
        paid_count=5,
        lapsed_count=12,
        back_callback="help:pick:menu",
    )
    flat = [b for row in kb["inline_keyboard"] for b in row]
    cbs = {b["callback_data"] for b in flat}
    assert "admin:trial" in cbs
    assert "admin:paid" in cbs
    assert "admin:lapsed" in cbs
    assert any(b["callback_data"] == "help:pick:menu" for b in flat)
    assert any("(3)" in b["text"] for b in flat)
    assert any("(12)" in b["text"] for b in flat)


def test_help_commands_keyboard_user_has_start_and_no_back_by_default() -> None:
    kb = _help_commands_keyboard("user", show_back=False)
    flat = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b["callback_data"] == "help:run:user:start" for b in flat)
    assert not any(b["callback_data"] == "help:pick:menu" for b in flat)


def test_help_commands_keyboard_admin_section_has_back() -> None:
    kb = _help_commands_keyboard("admin", show_back=True)
    flat = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b["callback_data"] == "help:pick:menu" for b in flat)
    assert any(b["callback_data"] == "help:run:admin:stats" for b in flat)


def test_help_run_command_from_callback() -> None:
    parsed = _help_run_command_from_callback("help:run:user:mute")
    assert parsed == ("user", "/mute")


def test_welcome_message_includes_name_and_trial() -> None:
    html_msg = _welcome_message_html(
        first_name="Ali",
        locations=["Coventry", "Northampton"],
        trial_hours=24,
    )
    assert "Ali" in html_msg
    assert "Coventry" in html_msg
    assert "free trial" in html_msg.lower()


def test_subscribe_keyboard_callback() -> None:
    kb = _subscribe_trial_keyboard()
    btn = kb["inline_keyboard"][0][0]
    assert btn["callback_data"] == "sub:activate"
    assert "Subscribe" in btn["text"]


def test_expiry_reminder_includes_hour_and_body() -> None:
    body = "work smart and stay two steps ahead"
    t = _expiry_reminder_html(first_name="Sara", body=body)
    assert "Sara" in t
    assert "1 hour" in t
    assert "two steps ahead" in t


def test_final_expiry_reminder_mentions_20_minutes_and_pay() -> None:
    t = _final_expiry_reminder_html(first_name="Ali")
    assert "20 minutes" in t
    assert "145" in t
    assert "paid plan" in t


def test_trial_ending_reminder_mentions_auto_stop() -> None:
    t = _trial_ending_reminder_html(
        first_name="Bob",
        body="alerts will stop automatically",
    )
    assert "1 minute" in t
    assert "Bob" in t
    assert "stop automatically" in t


def test_welcome_without_locations_omits_location_section() -> None:
    msg = _welcome_message_html(first_name="X", locations=[], trial_hours=24)
    assert "available locations" not in msg
    assert "free trial" in msg


def test_subscribe_paid_keyboard_three_plans() -> None:
    from app.config import SubscriptionConfig

    kb = _subscribe_paid_keyboard(SubscriptionConfig())
    rows = kb["inline_keyboard"]
    assert len(rows) == 3
    assert rows[0][0]["callback_data"] == "sub:pay:1d"
    assert "2.25" in rows[0][0]["text"]
    assert rows[1][0]["callback_data"] == "sub:pay:15d"
    assert "35" in rows[1][0]["text"]
    assert rows[2][0]["callback_data"] == "sub:pay:30d"
    assert "50" in rows[2][0]["text"]


def test_payment_plans_menu_text_lists_plans() -> None:
    from app.subscriber_bot import _payment_plans_menu_text
    from app.config import SubscriptionConfig

    text = _payment_plans_menu_text(SubscriptionConfig())
    assert "Help me" in text
    assert "£2.25" in text
    assert "£35" in text
    assert "£50" in text


def test_format_trial_time_left() -> None:
    from datetime import UTC, datetime, timedelta

    from app.subscriber_bot import _format_trial_time_left

    now = datetime(2026, 5, 25, 10, 0, 0, tzinfo=UTC)
    assert "5h" in _format_trial_time_left(now + timedelta(hours=5, minutes=20), now=now)
    assert "left" in _format_trial_time_left(now + timedelta(hours=1), now=now)
    assert "ago" in _format_trial_time_left(now - timedelta(hours=2), now=now)


def test_subscriber_free_trial_already_claimed(tmp_path) -> None:
    from app.storage.sqlite import SqliteStore

    s = SqliteStore(str(tmp_path / "trial.db"))
    assert not s.subscriber_free_trial_already_claimed("1")
    s.activate_trial_subscription("1", trial_hours=24)
    assert s.subscriber_free_trial_already_claimed("1")
    s.close()


def test_engagement_stats_counts(tmp_path) -> None:
    from datetime import UTC, datetime, timedelta

    from app.storage.sqlite import SqliteStore

    db = tmp_path / "eng.db"
    store = SqliteStore(str(db))
    store.add_subscriber("111", first_name="A")
    store.add_subscriber("222", first_name="B")
    store.record_subscriber_interaction("111")
    store.record_subscriber_interaction("111", reaction=True)
    store.activate_trial_subscription("111", trial_hours=24)
    stats = store.get_engagement_stats()
    assert stats.total_subscribers == 2
    assert stats.active_24h >= 1
    assert stats.reacted_ever >= 1
    assert stats.subscription_active >= 1
    store.close()


def test_help_user_keyboard_has_payment_entry() -> None:
    from app.subscriber_bot import _help_commands_keyboard

    kb = _help_commands_keyboard("user", show_back=False)
    last = kb["inline_keyboard"][-1][0]
    assert last["callback_data"] == "sub:pay_menu"
    assert "payment" in last["text"].lower()


def test_subscribe_paid_keyboard_stripe_urls() -> None:
    from app.config import PaidPlanConfig, SubscriptionConfig

    sub = SubscriptionConfig(
        payment_mode="stripe_links",
        paid_plans=[
            PaidPlanConfig(
                id="15d",
                button_label="15 days — £35",
                days=15,
                price_gbp=35,
                stripe_url="https://buy.stripe.com/test_15d",
            ),
        ],
    )
    kb = _subscribe_paid_keyboard(sub, chat_id="999")
    url = kb["inline_keyboard"][0][0]["url"]
    assert url.startswith("https://buy.stripe.com/test_15d")
    assert "client_reference_id=tg_999_15d" in url
    assert "callback_data" not in kb["inline_keyboard"][0][0]


def test_chat_open_url_username_and_user_id() -> None:
    from app.subscriber_bot import _chat_open_url

    assert _chat_open_url("123", {"username": "ali_test"}) == "https://t.me/ali_test"
    assert _chat_open_url("8674859284", None) == "tg://user?id=8674859284"
    assert _chat_open_url("-100123", None) is None


def test_admin_contact_line_html_has_link() -> None:
    from app.subscriber_bot import SubscriberChatMeta, _admin_contact_line_html

    line = _admin_contact_line_html(
        "123",
        SubscriberChatMeta("Ali", "ali", "https://t.me/ali"),
    )
    assert 'href="https://t.me/ali"' in line
    assert "tap to chat" in line


def test_price_pence_from_gbp() -> None:
    assert _price_pence_from_gbp(2.25) == 225
    assert _price_pence_from_gbp(50) == 5000


def test_sqlite_list_trial_and_paid(tmp_path) -> None:
    db = tmp_path / "lists.db"
    s = SqliteStore(str(db))
    try:
        now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
        s.add_subscriber("t1", first_name="TrialUser")
        s.activate_trial_subscription("t1", trial_hours=24, now_utc=now)
        s.add_subscriber("p1", first_name="PaidUser")
        s.activate_trial_subscription("p1", trial_hours=1, now_utc=now)
        s.activate_paid_subscription(
            "p1",
            paid_days=30,
            now_utc=now,
            payment={
                "currency": "GBP",
                "total_amount": 499,
                "invoice_payload": "paid_sub_p1_1",
                "telegram_payment_charge_id": "chg_test",
            },
        )
        trials = s.list_trial_only_subscribers()
        paid = s.list_paid_subscribers()
        assert len(trials) == 1
        assert trials[0].chat_id == "t1"
        assert len(paid) == 1
        assert paid[0].chat_id == "p1"
        assert paid[0].last_payment_total_amount == 499
    finally:
        s.close()


def test_grant_admin_reference_access(tmp_path) -> None:
    db = tmp_path / "ref.db"
    s = SqliteStore(str(db))
    try:
        now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
        ends = s.grant_admin_reference_access("777", grant_hours=48, now_utc=now)
        assert ends is not None
        assert ends > now + timedelta(hours=47)
        assert s.subscriber_has_active_subscription("777", now_utc=now)
        assert "777" in s.get_subscribers_for_alerts(now_utc=now)
        s.kick_subscriber("777")
        assert s.is_banned("777")
        ends2 = s.grant_admin_reference_access("777", grant_hours=24, now_utc=now)
        assert ends2 is not None
        assert not s.is_banned("777")
    finally:
        s.close()


def test_owner_vip_lifetime_access(tmp_path) -> None:
    db = tmp_path / "vip.db"
    s = SqliteStore(str(db))
    try:
        s.ensure_owner_vip("999", first_name="Admin")
        assert s.subscriber_is_owner("999")
        assert s.subscriber_has_active_subscription("999")
        now = datetime(2090, 1, 1, tzinfo=UTC)
        assert "999" in s.get_subscribers_for_alerts(now_utc=now)
        s.remove_subscriber("999")
        assert s.subscriber_is_owner("999")
        assert s.get_all_subscribers() == ["999"]
        assert not s.kick_subscriber("999")
        assert s.get_all_subscribers() == ["999"]
    finally:
        s.close()


def test_sqlite_kick_bans_and_removes(tmp_path) -> None:
    db = tmp_path / "kick.db"
    s = SqliteStore(str(db))
    try:
        now = datetime.now(UTC)
        s.activate_trial_subscription("999", trial_hours=24, now_utc=now)
        assert s.get_subscribers_for_alerts(now_utc=now) == ["999"]
        s.kick_subscriber("999")
        assert s.is_banned("999")
        assert s.get_all_subscribers() == []
        assert s.get_subscribers_for_alerts(now_utc=now) == []
    finally:
        s.close()


def test_sqlite_trial_subscription_and_alerts(tmp_path) -> None:
    db = tmp_path / "sub.db"
    s = SqliteStore(str(db))
    try:
        now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
        s.add_subscriber("111", first_name="Test")
        assert s.get_subscribers_for_alerts(now_utc=now) == []
        s.activate_trial_subscription("111", trial_hours=24, now_utc=now)
        assert s.subscriber_has_active_subscription("111", now_utc=now)
        assert s.get_subscribers_for_alerts(now_utc=now) == ["111"]
        after = now + timedelta(hours=25)
        assert not s.subscriber_has_active_subscription("111", now_utc=after)
        assert s.get_subscribers_for_alerts(now_utc=after) == []
    finally:
        s.close()


def test_sqlite_final_expiry_reminder_window(tmp_path) -> None:
    db = tmp_path / "fin.db"
    s = SqliteStore(str(db))
    try:
        now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
        s.add_subscriber("333", first_name="Fin")
        ends = s.activate_trial_subscription("333", trial_hours=24, now_utc=now)
        check_early = ends - timedelta(hours=1, minutes=30)
        assert s.get_subscribers_needing_final_expiry_reminder(
            reminder_minutes_before=20, now_utc=check_early
        ) == []
        check_late = ends - timedelta(minutes=15)
        due = s.get_subscribers_needing_final_expiry_reminder(
            reminder_minutes_before=20, now_utc=check_late
        )
        assert len(due) == 1
        assert due[0][0] == "333"
        s.mark_final_expiry_reminder_sent("333")
        assert s.get_subscribers_needing_final_expiry_reminder(
            reminder_minutes_before=20, now_utc=check_late
        ) == []
    finally:
        s.close()


def test_sqlite_paid_subscription_extends(tmp_path) -> None:
    db = tmp_path / "paid.db"
    s = SqliteStore(str(db))
    try:
        now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
        s.activate_trial_subscription("444", trial_hours=1, now_utc=now)
        ends = s.activate_paid_subscription("444", paid_days=30, now_utc=now)
        assert ends > now + timedelta(days=29)
    finally:
        s.close()


def test_sqlite_expiry_reminder_window(tmp_path) -> None:
    db = tmp_path / "rem.db"
    s = SqliteStore(str(db))
    try:
        now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
        s.add_subscriber("222", first_name="Rem")
        ends = s.activate_trial_subscription("222", trial_hours=24, now_utc=now)
        # 90 minutes before end — outside 1h window
        check_early = ends - timedelta(hours=1, minutes=30)
        assert s.get_subscribers_needing_expiry_reminder(
            reminder_hours_before=1, now_utc=check_early
        ) == []
        # 50 minutes before end — inside 1h window
        check_late = ends - timedelta(minutes=50)
        due = s.get_subscribers_needing_expiry_reminder(
            reminder_hours_before=1, now_utc=check_late
        )
        assert len(due) == 1
        assert due[0][0] == "222"
        s.mark_expiry_reminder_sent("222")
        assert s.get_subscribers_needing_expiry_reminder(
            reminder_hours_before=1, now_utc=check_late
        ) == []
    finally:
        s.close()


def test_sqlite_trial_ending_reminder_window(tmp_path) -> None:
    db = tmp_path / "t1m.db"
    s = SqliteStore(str(db))
    try:
        now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
        ends = s.activate_trial_subscription("555", trial_hours=24, now_utc=now)
        check = ends - timedelta(seconds=45)
        due = s.get_subscribers_needing_trial_ending_reminder(
            reminder_minutes_before=1, now_utc=check
        )
        assert len(due) == 1
        assert due[0][0] == "555"
    finally:
        s.close()


def test_subscription_config_defaults() -> None:
    cfg = SubscriptionConfig()
    assert cfg.trial_duration_hours == 24
    assert cfg.reminder_hours_before_expiry == 1
    assert cfg.final_reminder_minutes_before_expiry == 20
    assert cfg.trial_ending_minutes_before_expiry == 1
    assert "two steps ahead" in cfg.reminder_1h_body
    assert len(cfg.paid_plans) == 3
    assert cfg.plan_by_id("1d") is not None
    assert cfg.plan_by_id("1d").price_gbp == 2.25
    assert cfg.plan_by_id("30d").price_gbp == 50.0


def test_parse_payment_plan_from_payload() -> None:
    from app.subscriber_bot import _paid_plan_from_payload
    from app.config import SubscriptionConfig

    cfg = SubscriptionConfig()
    plan = _paid_plan_from_payload("paid_sub_15d_12345_999", cfg)
    assert plan is not None
    assert plan.id == "15d"
    assert plan.days == 15


def test_sqlite_subscriber_mute_excludes_from_alert_list(tmp_path) -> None:
    db = tmp_path / "m.db"
    s = SqliteStore(str(db))
    try:
        now = datetime.now(UTC)
        s.activate_trial_subscription("111", trial_hours=24, now_utc=now)
        s.activate_trial_subscription("222", trial_hours=24, now_utc=now)
        assert s.get_subscribers_for_alerts(now_utc=now) == ["111", "222"]
        s.set_subscriber_alerts_muted("111", muted=True)
        assert s.subscriber_alerts_muted("111") is True
        assert s.get_subscribers_for_alerts() == ["222"]
        s.set_subscriber_alerts_muted("111", muted=False)
        assert s.get_subscribers_for_alerts() == ["111", "222"]
    finally:
        s.close()


def test_sqlite_count_jobs(tmp_path) -> None:
    db = tmp_path / "t.db"
    s = SqliteStore(str(db))
    assert s.count_jobs() == 0
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    s.upsert_job(
        key="jid:x",
        job_id="x",
        url="https://u",
        source="amazon.jobs",
        source_url="https://src",
        title="t",
        location="L",
        pay_gbp_per_hour=None,
        pay_text=None,
        expected_pay_text=None,
        shift=None,
        posted_date_text=None,
        content_hash="h",
        now_utc=now,
    )
    assert s.count_jobs() == 1
    s.close()


def test_sqlite_lapsed_trial_leads_sync_and_paid_removal(tmp_path) -> None:
    db = tmp_path / "lapsed.db"
    s = SqliteStore(str(db))
    try:
        now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
        s.activate_trial_subscription("9001", trial_hours=1, now_utc=now)
        after_trial = now + timedelta(hours=2)
        assert s.sync_lapsed_trial_leads(now_utc=after_trial) == 1
        assert s.count_lapsed_trial_leads() == 1
        leads = s.list_lapsed_trial_leads()
        assert len(leads) == 1
        assert leads[0].chat_id == "9001"
        assert leads[0].promo_send_count == 0
        # Re-sync updates, does not duplicate
        assert s.sync_lapsed_trial_leads(now_utc=after_trial) == 0
        assert s.count_lapsed_trial_leads() == 1
        s.mark_lapsed_trial_promo_sent(["9001"], now_utc=after_trial)
        leads = s.list_lapsed_trial_leads()
        assert leads[0].promo_send_count == 1
        assert leads[0].last_promo_sent_at is not None
        # Paid user removed from lapsed list
        s.activate_paid_subscription("9001", paid_days=30, now_utc=after_trial)
        assert s.count_lapsed_trial_leads() == 0
        # Active trial not lapsed yet
        s.activate_trial_subscription("9002", trial_hours=24, now_utc=now)
        assert s.sync_lapsed_trial_leads(now_utc=now) == 0
    finally:
        s.close()
