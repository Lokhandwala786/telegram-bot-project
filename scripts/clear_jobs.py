import sqlite3
import os

db_path = "data/jobs.db"
if not os.path.exists(db_path):
    print(f"Error: Database not found at {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    # Count existing jobs and sent alerts
    cursor.execute("SELECT COUNT(*) FROM jobs")
    jobs_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM sent_alerts")
    alerts_count = cursor.fetchone()[0]
    
    print(f"Current jobs in database: {jobs_count}")
    print(f"Current sent alerts in database: {alerts_count}")

    # Delete all jobs and sent_alerts
    cursor.execute("DELETE FROM jobs")
    cursor.execute("DELETE FROM sent_alerts")
    conn.commit()
    print("Successfully cleared all jobs AND sent_alerts from database!")

except Exception as e:
    print(f"An error occurred: {e}")
finally:
    conn.close()
