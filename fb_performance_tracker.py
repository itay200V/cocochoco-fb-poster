#!/usr/bin/env python3
"""
Cocochoco — Performance Tracker
================================
רץ פעם ביום ב-23:00 BKK.
עבור כל פוסט שנרשם ב-post_history היום,
חוזר לקבוצות בפייסבוק ומושך סטטיסטיקות.
שולח דוח שבועי לטלגרם בכל יום ראשון.
"""

from __future__ import annotations
import asyncio, json, os, sys, random
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from playwright.async_api import async_playwright

SCRIPT_DIR = Path(__file__).parent

def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def load_cookies(path: str = "fb_cookies.json"):
    cookies_path = SCRIPT_DIR / path
    if cookies_path.exists():
        return json.load(open(cookies_path))
    env_json = os.environ.get("FB_COOKIES_JSON")
    if env_json:
        return json.loads(env_json)
    return None

def sanitize_cookies(cookies):
    allowed = {"name","value","domain","path","expires","httpOnly","secure","sameSite"}
    return [{k:v for k,v in c.items() if k in allowed} for c in cookies]

async def get_post_stats(page, group_id: str) -> dict:
    """
    מנסה למצוא את הפוסט האחרון של Cocochoco בקבוצה
    ומחזיר likes + comments.
    מחזיר None אם לא מצא.
    """
    try:
        url = f"https://www.facebook.com/groups/{group_id}"
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)

        stats = await page.evaluate("""() => {
            const posts = document.querySelectorAll('[data-pagelet*="FeedUnit"]');
            for (const post of posts) {
                const text = post.innerText || '';
                if (text.includes('Cocochoco') || text.includes('ONYX') || text.includes('เคราติน')) {
                    const spans = post.querySelectorAll('span');
                    let likes = 0, comments = 0;
                    for (const s of spans) {
                        const t = s.innerText.trim();
                        if (/^\\d+$/.test(t)) {
                            const n = parseInt(t);
                            if (n > likes) likes = n;
                        }
                        if (t.includes('comment') || t.includes('ความคิดเห็น')) {
                            const m = t.match(/\\d+/);
                            if (m) comments = parseInt(m[0]);
                        }
                    }
                    return { found: true, likes, comments, snippet: text.slice(0, 80) };
                }
            }
            return { found: false };
        }""")

        return stats
    except Exception as exc:
        return {"found": False, "error": str(exc)}


def save_stats_to_sheets(sheet_id: str, stats_rows: list[dict]) -> None:
    """שומר stats ב-sheet חדש: post_stats"""
    import gspread
    from google.oauth2.service_account import Credentials

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        creds = Credentials.from_service_account_info(json.loads(sa_json), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            str(SCRIPT_DIR / "service_account.json"), scopes=SCOPES)

    try:
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key(sheet_id)

        try:
            ws = spreadsheet.worksheet("post_stats")
        except:
            ws = spreadsheet.add_worksheet("post_stats", rows=1000, cols=8)
            ws.append_row(["timestamp","group_id","likes","comments","found","angle","campaign","checked_at"])

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        for row in stats_rows:
            ws.append_row([
                row.get("run_ts",""),
                row.get("group_id",""),
                row.get("likes", 0),
                row.get("comments", 0),
                row.get("found", False),
                row.get("angle",""),
                row.get("campaign",""),
                now,
            ])
    except Exception as exc:
        print(f"[WARN] Could not save stats: {exc}")


async def send_weekly_report(sheet_id: str) -> None:
    """שולח דוח שבועי לטלגרם (רק בימי ראשון)"""
    if datetime.now().weekday() != 6:  # 6 = Sunday
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return

    import gspread
    from google.oauth2.service_account import Credentials

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        creds = Credentials.from_service_account_info(json.loads(sa_json), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            str(SCRIPT_DIR / "service_account.json"), scopes=SCOPES)

    try:
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key(sheet_id)

        try:
            ph = spreadsheet.worksheet("post_history").get_all_records()
        except:
            ph = []

        try:
            ps = spreadsheet.worksheet("post_stats").get_all_records()
        except:
            ps = []

        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        weekly_ph = [r for r in ph if r.get("run_timestamp","") >= week_ago]
        weekly_ps = [r for r in ps if r.get("checked_at","") >= week_ago]

        total_posts = len(weekly_ph)
        total_likes = sum(r.get("likes",0) for r in weekly_ps)
        total_comments = sum(r.get("comments",0) for r in weekly_ps)
        found_rate = len([r for r in weekly_ps if r.get("found")]) / max(len(weekly_ps),1) * 100

        angle_stats = {}
        for r in weekly_ps:
            a = r.get("angle","?")
            if a not in angle_stats:
                angle_stats[a] = {"likes":0,"comments":0,"count":0}
            angle_stats[a]["likes"] += r.get("likes",0)
            angle_stats[a]["comments"] += r.get("comments",0)
            angle_stats[a]["count"] += 1

        best_angle = max(angle_stats, key=lambda a: angle_stats[a]["likes"] + angle_stats[a]["comments"]*2, default="?")

        report = (
            f"📊 <b>דוח שבועי — Cocochoco Bot</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🗓 שבוע: {week_ago} עד היום\n\n"
            f"📝 ריצות בוצעו: {total_posts}\n"
            f"👍 סה\"כ לייקים: {total_likes}\n"
            f"💬 סה\"כ תגובות: {total_comments}\n"
            f"🔍 שיעור איתור פוסטים: {found_rate:.0f}%\n\n"
            f"🏆 <b>Angle הכי אפקטיבי:</b> {_esc(best_angle)}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>ביצועים לפי angle:</b>\n"
        )

        for angle, data in sorted(angle_stats.items(), key=lambda x: x[1]["likes"]+x[1]["comments"]*2, reverse=True):
            report += f"  • {_esc(angle)}: {data['likes']} לייקים, {data['comments']} תגובות\n"

        async with aiohttp.ClientSession() as session:
            await session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": report, "parse_mode": "HTML"}
            )
        print("Weekly report sent")
    except Exception as exc:
        print(f"[WARN] Weekly report error: {exc}")


async def main():
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "1rxfG-DZdgmx4sNHtgfyChvyyQ5Ur4ZutTi_doLhxPjQ")
    cookies = load_cookies()

    if not cookies:
        print("[ERROR] No cookies found")
        sys.exit(1)

    import gspread
    from google.oauth2.service_account import Credentials
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        creds = Credentials.from_service_account_info(json.loads(sa_json), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            str(SCRIPT_DIR / "service_account.json"), scopes=SCOPES)

    try:
        gc = gspread.authorize(creds)
        ph_records = gc.open_by_key(sheet_id).worksheet("post_history").get_all_records()
        today = datetime.now().strftime("%Y-%m-%d")
        todays_runs = [r for r in ph_records if r.get("run_timestamp","").startswith(today)]
    except Exception as exc:
        print(f"[WARN] Could not load post_history: {exc}")
        todays_runs = []

    try:
        import re
        campaign_records = gc.open_by_key(sheet_id).sheet1.get_all_records()
        active = [r for r in campaign_records if str(r.get("active","")).upper() == "TRUE"]
        raw_ids = str(active[0].get("group_ids","")) if active else ""
        group_ids = [g.strip() for g in re.split(r"[|,\n\r\s]+", raw_ids) if g.strip()]
    except:
        group_ids = []

    if not group_ids:
        print("[WARN] No group IDs found — skipping performance check")
        await send_weekly_report(sheet_id)
        return

    sample_groups = random.sample(group_ids, min(6, len(group_ids)))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu"]
        )
        context = await browser.new_context(viewport={"width":1280,"height":900})
        await context.add_cookies(sanitize_cookies(cookies))
        page = await context.new_page()

        await page.goto("https://www.facebook.com", wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        if "login" in page.url:
            print("[ERROR] Cookies expired")
            await browser.close()
            return

        stats_rows = []
        latest_run = todays_runs[-1] if todays_runs else {}

        for gid in sample_groups:
            print(f"Checking group: {gid}")
            result = await get_post_stats(page, gid)
            stats_rows.append({
                "run_ts": latest_run.get("run_timestamp",""),
                "group_id": gid,
                "likes": result.get("likes", 0),
                "comments": result.get("comments", 0),
                "found": result.get("found", False),
                "angle": latest_run.get("angle_used",""),
                "campaign": latest_run.get("campaign_name",""),
            })
            await page.wait_for_timeout(3000 + random.randint(0,2000))

        await browser.close()

    if stats_rows:
        save_stats_to_sheets(sheet_id, stats_rows)

    await send_weekly_report(sheet_id)
    print("Performance tracker done")

if __name__ == "__main__":
    asyncio.run(main())
