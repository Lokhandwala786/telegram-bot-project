import sqlite3
from datetime import UTC, datetime
from pathlib import Path

db = Path(__file__).resolve().parents[1] / "data" / "jobs.db"
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
now = datetime.now(UTC)
merged = [
    "7989573059", "8765492240", "6200747193", "8885267888", "7767057501",
    "7848974033", "8012552558", "8282089150", "8635533628", "7468470757",
]
print("Now UTC:", now.isoformat())
print()
print("=== 10 merged users ===")
for cid in merged:
    r = conn.execute(
        "SELECT chat_id, first_name, subscription_ends_at, trial_activated_at, paid_at FROM subscribers WHERE chat_id=?",
        (cid,),
    ).fetchone()
    if not r:
        print(cid, "NOT FOUND")
        continue
    ends = r["subscription_ends_at"]
    active = "active" if ends and datetime.fromisoformat(ends.replace("Z", "+00:00")) > now else "expired/none"
    print(f"{r['first_name'] or '?'} ({cid}) — {active}")
    print(f"  trial_activated_at: {r['trial_activated_at']}")
    print(f"  subscription_ends_at: {ends}")
    print(f"  paid_at: {r['paid_at']}")
print()
no_trial = conn.execute(
    "SELECT COUNT(*) FROM subscribers WHERE trial_activated_at IS NULL"
).fetchone()[0]
active_trial = conn.execute(
    """SELECT COUNT(*) FROM subscribers 
       WHERE trial_activated_at IS NOT NULL AND paid_at IS NULL 
       AND subscription_ends_at > ?""",
    (now.isoformat(),),
).fetchone()[0]
print("Subscribers without trial_activated_at:", no_trial)
print("Active free trial (unpaid):", active_trial)
conn.close()
