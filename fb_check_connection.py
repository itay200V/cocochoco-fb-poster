#!/usr/bin/env python3
"""בדיקת חיבור CDP בלבד — לא מנווט, לא מפרסם."""
import asyncio
import json
import aiohttp
import websockets

CDP_URL = "http://localhost:9222"

async def main():
    print("מתחבר ל-CDP...")
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{CDP_URL}/json") as r:
            tabs = await r.json()

    page = next((t for t in tabs if t.get("type") == "page"), None)
    if not page:
        print("לא נמצאה כרטיסיית דפדפן פתוחה ב-Chrome.")
        return

    ws_url = page["webSocketDebuggerUrl"]
    print(f"CDP מחובר. כרטיסייה: {page.get('title', '?')}")
    print(f"URL: {page.get('url', '?')}")

    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
        msg_id = 1
        await ws.send(json.dumps({"id": msg_id, "method": "Runtime.evaluate", "params": {
            "expression": "window.location.href",
            "returnByValue": True,
        }}))
        resp = json.loads(await ws.recv())
        current_url = resp.get("result", {}).get("result", {}).get("value", "?")
        print(f"URL נוכחי (JS): {current_url}")

        if "facebook.com" in current_url and "login" not in current_url:
            print("מחובר לפייסבוק!")
        elif "login" in current_url:
            print("לא מחובר לפייסבוק — נדרש לוגין.")
        else:
            print(f"הדף הנוכחי אינו פייסבוק: {current_url}")

if __name__ == "__main__":
    asyncio.run(main())
