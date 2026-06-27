import os
import httpx
import asyncio
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN")

# We will target the two active/recently expired users:
# 8241123186 (lubna) and 7710647827 (Samiha)
target_chats = ["8241123186", "7710647827"]

async def scan_and_delete(client: httpx.AsyncClient, chat_id: str):
    url_delete = f"https://api.telegram.org/bot{token}/deleteMessage"
    print(f"--- Scanning and deleting in chat: {chat_id} ---")
    
    # Scan message IDs from 5800 to 6300
    for base in range(5800, 6300, 50):
        tasks = []
        for msg_id in range(base, base + 50):
            tasks.append(client.post(url_delete, json={"chat_id": chat_id, "message_id": msg_id}))
        
        results = await asyncio.gather(*tasks)
        for msg_id, r in zip(range(base, base + 50), results):
            if r.status_code == 200 and r.json().get("ok"):
                print(f"[{chat_id}] Successfully deleted message ID: {msg_id}")
        
        await asyncio.sleep(0.05)

async def main():
    async with httpx.AsyncClient() as client:
        for chat_id in target_chats:
            await scan_and_delete(client, chat_id)

if __name__ == "__main__":
    asyncio.run(main())
