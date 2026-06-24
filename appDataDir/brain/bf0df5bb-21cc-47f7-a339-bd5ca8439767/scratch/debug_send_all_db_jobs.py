import asyncio
import httpx
from datetime import UTC, datetime
from app.config import Settings, load_file_config
from app.storage.sqlite import SqliteStore
from app.models.job import JobListing
from app.main import _fmt, _expected_pay_text
from app.notifiers import TelegramNotifier
from app.notifiers.telegram import TelegramMessage

async def main():
    settings = Settings()
    file_cfg = load_file_config(settings.config_path)
    store = SqliteStore(settings.sqlite_path)
    
    # Get active subscribers for alerts
    chat_ids = store.get_subscribers_for_alerts()
    print("Active subscribers:", chat_ids)
    if not chat_ids and settings.telegram_chat_id:
        chat_ids = [settings.telegram_chat_id]
        print("Fallback to TELEGRAM_CHAT_ID:", chat_ids)
        
    if not chat_ids:
        print("No active subscribers and no TELEGRAM_CHAT_ID set.")
        return

    # Load all jobs from database
    cur = store._conn.execute("SELECT * FROM jobs")
    rows = cur.fetchall()
    print(f"Loaded {len(rows)} jobs from database.")
    
    now = datetime.now(UTC)
    
    # We want to test sending all these jobs to the subscribers, but wait,
    # to avoid spamming the user if it succeeds, let's print the formatting first,
    # and if we want to debug, we can try sending to settings.telegram_chat_id (which is the developer's chat ID).
    dev_chat_id = settings.telegram_chat_id
    if not dev_chat_id:
        print("TELEGRAM_CHAT_ID not set in env.")
        return
        
    print(f"Testing sending to developer chat ID: {dev_chat_id}")
    
    for row in rows:
        # Reconstruct JobListing object
        # Schema: key, job_id, url, source, source_url, title, location, pay_gbp_per_hour, pay_text, expected_pay_text, shift, posted_date_text, content_hash, first_seen_utc, last_seen_utc
        # Let's inspect raw metadata if there is any (wait, raw_metadata isn't in jobs table direct columns except via some fields?
        # Oh, in `SqliteStore.upsert_job` how are they saved?
        # Let's look at `app/storage/sqlite.py` to see how `upsert_job` works and where metadata is stored.
        pass

if __name__ == "__main__":
    asyncio.run(main())
