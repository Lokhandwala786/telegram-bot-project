import sqlite3

conn = sqlite3.connect("data/jobs.db")
cursor = conn.cursor()

# Get all columns and search
cursor.execute("SELECT chat_id, first_name FROM subscribers")
all_subs = cursor.fetchall()

print("All subscribers matching 'naznin' or similar:")
found = False
for chat_id, first_name in all_subs:
    # Check if first_name contains naznin or similar (case-insensitive)
    name_str = str(first_name or "").lower()
    if "naz" in name_str or "nin" in name_str or "naaz" in name_str:
        print(f"Chat ID: {chat_id} | Name: {first_name}")
        found = True

if not found:
    print("No matching subscriber found. Let's list the first 5 subscribers just in case:")
    for chat_id, first_name in all_subs[:5]:
        print(f"Chat ID: {chat_id} | Name: {first_name}")

conn.close()
