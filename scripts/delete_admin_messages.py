import os
import httpx
import asyncio
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN")
admin_chat_id = os.getenv("TELEGRAM_CHAT_ID")

if not token or not admin_chat_id:
    print("Error: Missing token or admin_chat_id in environment.")
    exit(1)

async def scan_and_delete_admin():
    url_delete = f"https://api.telegram.org/bot{token}/deleteMessage"
    async with httpx.AsyncClient() as client:
        # We will try to delete message IDs from 5000 to 6300 in the admin's chat
        print(f"Scanning and deleting messages in admin chat {admin_chat_id} from ID 5500 to 6300...")
        
        # Process in batches of 50 to avoid rate limits
        for base in range(5500, 6300, 50):
            tasks = []
            for msg_id in range(base, base + 50):
                tasks.append(client.post(url_delete, json={"chat_id": admin_chat_id, "message_id": msg_id}))
            
            results = await asyncio.gather(*tasks)
            for msg_id, r in zip(range(base, base + 50), results):
                if r.status_code == 200 and r.json().get("ok"):
                    print(f"Successfully deleted message ID: {msg_id}")
            
            await asyncio.sleep(0.1)

if __name__ == "__main__":
    asyncio.run(scan_and_delete_admin())
