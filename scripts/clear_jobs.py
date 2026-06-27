import sqlite3
import os

db_path = "data/jobs.db"
if not os.path.exists(db_path):
    print(f"Error: Database not found at {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    # Count existing jobs
    cursor.execute("SELECT COUNT(*) FROM jobs")
    count = cursor.fetchone()[0]
    print(f"Current jobs in database: {count}")

    # Delete all jobs
    if count > 0:
        cursor.execute("DELETE FROM jobs")
        conn.commit()
        print("Successfully cleared all jobs from database!")
    else:
        print("Database is already empty, no jobs to clear.")

except Exception as e:
    print(f"An error occurred: {e}")
finally:
    conn.close()
