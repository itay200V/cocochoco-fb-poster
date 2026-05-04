#!/usr/bin/env python3
"""
fb_export_cookies.py
====================
מייצא את ה-cookies של פייסבוק מ-Chrome הפתוח ושומר ל-fb_cookies.json.

דרישות מוקדמות:
  Chrome פתוח עם: --remote-debugging-port=9222
  ומחובר לפייסבוק.

הרצה:
  python3 fb_export_cookies.py
"""
import asyncio
import json
from pathlib import Path

import aiohttp
import websockets

CDP_URL  = "http://localhost:9222"
OUT_FILE = Path(__file__).parent / "fb_cookies.json"


async def main() -> None:
    print("מתחבר ל-Chrome CDP…")
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{CDP_URL}/json") as r:
            tabs = await r.json()

    page = next((t for t in tabs if t.get("type") == "page"), None)
    if not page:
        print("שגיאה: לא נמצאה כרטיסייה פתוחה ב-Chrome.")
        print("פתח Chrome עם: --remote-debugging-port=9222")
        return

    print(f"כרטיסייה: {page.get('title', '?')}")
    ws_url = page["webSocketDebuggerUrl"]

    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
        # Enable Network domain
        await ws.send(json.dumps({"id": 1, "method": "Network.enable", "params": {}}))

        # Request all cookies
        await ws.send(json.dumps({"id": 2, "method": "Network.getAllCookies", "params": {}}))

        # Collect both responses
        results: dict[int, dict] = {}
        while len(results) < 2:
            raw  = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(raw)
            if "id" in data and data["id"] in (1, 2):
                results[data["id"]] = data

    all_cookies = results[2].get("result", {}).get("cookies", [])
    fb_cookies  = [c for c in all_cookies if "facebook.com" in c.get("domain", "")]

    # Keep only fields that Playwright's add_cookies() accepts
    clean = []
    for c in fb_cookies:
        entry: dict = {
            "name":     c["name"],
            "value":    c["value"],
            "domain":   c["domain"],
            "path":     c["path"],
            "httpOnly": c.get("httpOnly", False),
            "secure":   c.get("secure", False),
        }
        if c.get("expires", -1) > 0:
            entry["expires"] = c["expires"]
        if c.get("sameSite") in ("None", "Lax", "Strict"):
            entry["sameSite"] = c["sameSite"]
        clean.append(entry)

    OUT_FILE.write_text(json.dumps(clean, indent=2, ensure_ascii=False))
    print(f"✅ {len(clean)} Facebook cookies נשמרו ל-{OUT_FILE}")
    print()
    print("השלבים הבאים:")
    print("  1. העלה את fb_cookies.json ל-Railway כ-Secret File (ב-Variables → Files)")
    print("     נתיב: /app/fb_cookies.json")
    print("  2. פרס מחדש את השירות")


if __name__ == "__main__":
    asyncio.run(main())
