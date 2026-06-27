import os
import sqlite3
import httpx
import asyncio
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN")
db_path = os.getenv("SQLITE_PATH", "data/jobs.db")

if not token:
    print("Error: TELEGRAM_BOT_TOKEN not found in environment or .env file.")
    exit(1)

# Connect to database to get subscribers
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT chat_id FROM subscribers")
subscribers = [str(row[0]) for row in cursor.fetchall()]
conn.close()

print(f"Loaded {len(subscribers)} subscribers from database.")

async def delete_latest_message_for_user(client: httpx.AsyncClient, chat_id: str):
    url_send = f"https://api.telegram.org/bot{token}/sendMessage"
    url_delete = f"https://api.telegram.org/bot{token}/deleteMessage"
    
    try:
        # 1. Send dummy message to get the latest message_id
        r = await client.post(url_send, json={"chat_id": chat_id, "text": "."}, timeout=10.0)
        if r.status_code != 200:
            # User has blocked the bot
            return
        
        res = r.json()
        if not res.get("ok"):
            return
        
        dummy_msg_id = res["result"]["message_id"]
        
        # 2. Immediately delete the dummy message
        await client.post(url_delete, json={"chat_id": chat_id, "message_id": dummy_msg_id})
        
        # 3. Search BACKWARDS from dummy_msg_id - 1 down to dummy_msg_id - 250
        # This guarantees we will reach the broadcast message (even if the gap is around 170).
        # The first message we delete successfully is the broadcast message.
        deleted = False
        for msg_id in range(dummy_msg_id - 1, dummy_msg_id - 250, -1):
            r_del = await client.post(url_delete, json={"chat_id": chat_id, "message_id": msg_id})
            if r_del.status_code == 200:
                res_del = r_del.json()
                if res_del.get("ok"):
                    print(f"[{chat_id}] Successfully deleted latest message (ID: {msg_id})")
                    deleted = True
                    break
            elif r_del.status_code == 403:
                break
        
        if not deleted:
            print(f"[{chat_id}] No message could be deleted.")
            
    except Exception as e:
        print(f"[{chat_id}] Error: {e}")

async def main():
    async with httpx.AsyncClient() as client:
        # Process in batches of 5 to avoid rate limits
        for i in range(0, len(subscribers), 5):
            batch = subscribers[i:i+5]
            tasks = [delete_latest_message_for_user(client, cid) for cid in batch]
            await asyncio.gather(*tasks)
            await asyncio.sleep(0.25)

if __name__ == "__main__":
    asyncio.run(main())
