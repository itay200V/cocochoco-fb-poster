#!/usr/bin/env python3
"""
Cocochoco — Facebook Groups Poster v4 (Google Sheets + Cloudinary)
===================================================================
Data flow:
  1. Read active campaign (active=TRUE) from Google Sheets
  2. Download 2-3 random images from Cloudinary folder (image_url_1 column)
  3. Generate Thai Facebook post via Claude API from template_text
  4. Post to all Facebook groups in the campaign row
  5. Send Telegram run report

Google Sheets expected columns:
  active          TRUE / FALSE
  campaign_name   name shown in Telegram report
  template_text   base text sent to Claude for rewriting
  image_url_1     Cloudinary folder path  (e.g. "cocochoco/open_house")
  group_ids       pipe-separated FB group IDs  (e.g. "123|456|789")
  wait_seconds    seconds between posts (optional, default 25)

ENV VARS:
  ANTHROPIC_API_KEY            Claude API key
  TELEGRAM_BOT_TOKEN           Telegram bot token
  TELEGRAM_CHAT_ID             Telegram chat ID
  CLOUDINARY_API_KEY           Cloudinary API key
  CLOUDINARY_API_SECRET        Cloudinary API secret
  CLOUDINARY_CLOUD_NAME        Cloudinary cloud name (default: dhmttntds)
  GOOGLE_SERVICE_ACCOUNT_JSON  Service account JSON as a string (Railway)
                               Falls back to service_account.json file
  GOOGLE_SHEET_ID              Google Sheets ID (overrides --sheet-id)

CLI FLAGS:
  --sheet-id ID         Google Sheets ID
  --cookies PATH        Facebook cookies JSON (default: fb_cookies.json)
  --no-headless         show browser window (local debug)
  --skip-validation     skip hair/beauty group-title check
"""
from __future__ import annotations
import asyncio
import json
import os
import random
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import aiohttp
import requests
from playwright.async_api import async_playwright, Page

# ─── Constants ────────────────────────────────────────────────────────────────

SCRIPT_DIR      = Path(__file__).parent
DEFAULT_COOKIES = SCRIPT_DIR / "fb_cookies.json"
DEFAULT_SHEET   = "1rxfG-DZdgmx4sNHtgfyChvyyQ5Ur4ZutTi_doLhxPjQ"

FIXED_CONTACT = (
    "\n\n📍 สถานที่: Sivatel Tower, BTS Phlo Chit"
    "\n📞 โทร: 092-415-0592"
    "\n📲 Line OA: https://lin.ee/mipAAhk"
    "\n📝 ลงทะเบียน: https://forms.gle/b78oEZGFbegABH2Y6"
)

_TMP       = tempfile.gettempdir()
POSTED_LOG = os.path.join(_TMP, "fb_posted.txt")
FAILED_LOG = os.path.join(_TMP, "fb_failed.txt")
RUN_LOG    = os.path.join(_TMP, "fb_run.log")

HAIR_KEYWORDS = [
    "hair", "salon", "barber", "beauty", "cosmet", "keratin", "เสริมสวย",
    "ผม", "ร้านเสริมสวย", "ช่างผม", "สปา", "spa", "stylist", "hairdress",
    "tricholog", "shampoo", "treatment", "coiffure", "coiffeur", "คอสเมติก",
    "สี", "ดัด", "ยืด", "เคราติน", "บิวตี้", "ความงาม", "เล็บ", "nail",
    "lash", "brow", "makeup", "แต่งหน้า", "ทรง", "perma",
]

# ─── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level:5s}] {msg}"
    print(line, flush=True)
    with open(RUN_LOG, "a") as fh:
        fh.write(line + "\n")

# ─── Google Sheets ────────────────────────────────────────────────────────────

def load_campaign_from_sheets(sheet_id: str) -> dict:
    import gspread
    from google.oauth2.service_account import Credentials

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        info  = json.loads(sa_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        sa_file = SCRIPT_DIR / "service_account.json"
        if not sa_file.exists():
            log("service_account.json לא נמצא ו-GOOGLE_SERVICE_ACCOUNT_JSON לא מוגדר", "ERROR")
            sys.exit(1)
        creds = Credentials.from_service_account_file(str(sa_file), scopes=SCOPES)

    log(f"Google Sheets: connecting to {sheet_id}…")
    gc        = gspread.authorize(creds)
    worksheet = gc.open_by_key(sheet_id).sheet1
    records   = worksheet.get_all_records()

    active = [r for r in records if str(r.get("active", "")).strip().upper() == "TRUE"]
    if not active:
        log("אין קמפיין פעיל ב-Google Sheets (אין שורה עם active=TRUE)", "ERROR")
        sys.exit(1)

    row = active[0]
    log(f"קמפיין פעיל: {row.get('campaign_name', '?')}")

    raw_ids = str(row.get("group_ids", ""))
    log(f"group_ids raw value: {repr(raw_ids)}")
    # support |, comma, newline, and whitespace as separators
    import re
    group_ids = [g.strip() for g in re.split(r"[|,\n\r\s]+", raw_ids) if g.strip()]

    if not group_ids:
        log("לא נמצאו group_ids בשורת הקמפיין", "ERROR")
        sys.exit(1)

    return {
        "campaign_name": str(row.get("campaign_name", "campaign")),
        "template_text": str(row.get("template_text", "")),
        "image_folder":  str(row.get("image_url_1", "")),
        "group_ids":     group_ids,
        "wait":          int(row.get("wait_seconds", 25) or 25),
    }

# ─── Cloudinary ───────────────────────────────────────────────────────────────

def download_cloudinary_images(folder: str, count: int = 3) -> list[str]:
    import cloudinary
    import cloudinary.api

    cloudinary.config(
        cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME", "dhmttntds"),
        api_key    = os.environ.get("CLOUDINARY_API_KEY"),
        api_secret = os.environ.get("CLOUDINARY_API_SECRET"),
    )

    log(f"Cloudinary: listing folder '{folder}'…")
    result = cloudinary.api.resources(type="upload", folder=folder, max_results=100)
    resources = result.get("resources", [])

    if not resources:
        log(f"Cloudinary: לא נמצאו תמונות בתיקייה '{folder}'", "ERROR")
        sys.exit(1)

    selected = random.sample(resources, min(count, len(resources)))
    tmp_dir  = tempfile.mkdtemp(prefix="fb_images_")
    paths: list[str] = []

    for res in selected:
        url  = res["secure_url"]
        ext  = res.get("format", "jpg")
        name = res["public_id"].replace("/", "_") + f".{ext}"
        dest = os.path.join(tmp_dir, name)
        log(f"  Downloading: {url}")
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            fh.write(resp.content)
        paths.append(dest)

    log(f"Cloudinary: {len(paths)} image(s) ready")
    return paths

# ─── System Guidelines ────────────────────────────────────────────────────────

def load_system_guidelines() -> str:
    """Load brand_guidelines.md then campaign_active.md as a combined system prompt."""
    parts = []
    for filename in ("brand_guidelines.md", "campaign_active.md"):
        path = SCRIPT_DIR / filename
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
            log(f"Loaded {filename} ({len(parts[-1])} chars)")
        else:
            log(f"{filename} not found — skipping", "WARN")
    return "\n\n---\n\n".join(parts)

# ─── Claude API ───────────────────────────────────────────────────────────────

async def generate_post_text(template_text: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log("ANTHROPIC_API_KEY לא מוגדר — משתמש בטקסט המקורי", "WARN")
        return template_text + FIXED_CONTACT

    guidelines = load_system_guidelines()

    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=api_key)

        system_prompt = guidelines if guidelines else (
            "You are a Thai social media copywriter for COCOCHOCO Academy Bangkok. "
            "Write natural, warm Facebook group posts in Thai for professional hairstylists. "
            "Output ONLY the post text — no labels, no explanations."
        )

        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": (
                    "Write a Facebook group post based on the campaign details above.\n"
                    "Follow ALL brand and campaign rules strictly.\n"
                    "Output ONLY the post text — no labels, no explanations.\n\n"
                    f"Campaign template / additional notes:\n{template_text}"
                ),
            }],
        )

        text = msg.content[0].text.strip()
        log(f"Claude output: {len(text)} chars")
        return text

    except Exception as exc:
        log(f"Claude API error: {exc} — using original template", "WARN")
        return template_text + FIXED_CONTACT

# ─── Cookies ──────────────────────────────────────────────────────────────────

def load_cookies(path: str) -> list | None:
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)

    env_json = os.environ.get("FB_COOKIES_JSON")
    if env_json:
        cookies = json.loads(env_json)
        tmp_path = os.path.join(tempfile.gettempdir(), "fb_cookies.json")
        with open(tmp_path, "w") as fh:
            json.dump(cookies, fh)
        log(f"FB_COOKIES_JSON loaded from env → saved to {tmp_path}")
        return cookies

    return None


def sanitize_cookies(cookies: list) -> list:
    """Keep only fields Playwright's add_cookies accepts."""
    allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    return [{k: v for k, v in c.items() if k in allowed} for c in cookies]

# ─── Post / fail logs ─────────────────────────────────────────────────────────

def load_posted() -> set:
    if os.path.exists(POSTED_LOG):
        with open(POSTED_LOG) as fh:
            return {line.split("\t")[0] for line in fh.read().splitlines() if line}
    return set()

def mark_posted(gid: str) -> None:
    ts = datetime.now().isoformat()
    with open(POSTED_LOG, "a") as fh:
        fh.write(f"{gid}\t{ts}\n")
    log(f"✅ Marked posted: {gid}")

def mark_failed(gid: str, reason: str) -> None:
    ts = datetime.now().isoformat()
    with open(FAILED_LOG, "a") as fh:
        fh.write(f"{ts}\t{gid}\t{reason}\n")
    log(f"FAILED {gid}: {reason}", "ERROR")

# ─── Telegram report ──────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    """Escape special HTML characters for Telegram HTML parse mode."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def send_telegram_report(
    campaign_name: str,
    ok: int,
    fail: int,
    skipped: int,
    post_text: str,
    image_names: list[str],
) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID לא מוגדרים — דיווח מדולג", "WARN")
        return

    now         = datetime.now().strftime("%d/%m/%Y %H:%M")
    images_line = _esc(", ".join(image_names) if image_names else "—")
    status_line = f"✅ פורסם: {ok}  |  ❌ נכשל: {fail}  |  ⏭ דולג: {skipped}"

    text = (
        f"📊 <b>דוח פרסום — Cocochoco</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 קמפיין: <code>{_esc(campaign_name)}</code>\n"
        f"🕐 תאריך: {now}\n"
        f"{status_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🖼 תמונות: {images_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>טקסט שפורסם:</b>\n{_esc(post_text)}"
    )

    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    log("דוח טלגרם נשלח בהצלחה")
                else:
                    body = await r.text()
                    log(f"שגיאה בשליחת טלגרם ({r.status}): {body}", "WARN")
    except Exception as exc:
        log(f"שגיאת טלגרם: {exc}", "WARN")


async def send_telegram_photos(image_paths: list[str]) -> None:
    """Send actual image files to Telegram via sendPhoto / sendMediaGroup."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    if not image_paths:
        return

    try:
        async with aiohttp.ClientSession() as session:
            if len(image_paths) == 1:
                # Single photo — use sendPhoto
                path = image_paths[0]
                url  = f"https://api.telegram.org/bot{token}/sendPhoto"
                with open(path, "rb") as fh:
                    data = aiohttp.FormData()
                    data.add_field("chat_id", chat_id)
                    data.add_field("photo", fh, filename=Path(path).name, content_type="image/jpeg")
                    async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=30)) as r:
                        if r.status == 200:
                            log(f"תמונה נשלחה לטלגרם: {Path(path).name}")
                        else:
                            body = await r.text()
                            log(f"שגיאה בשליחת תמונה ({r.status}): {body}", "WARN")
            else:
                # Multiple photos — use sendMediaGroup
                url   = f"https://api.telegram.org/bot{token}/sendMediaGroup"
                media = [{"type": "photo", "media": f"attach://photo{i}"} for i in range(len(image_paths))]
                data  = aiohttp.FormData()
                data.add_field("chat_id", chat_id)
                data.add_field("media", json.dumps(media))
                for i, path in enumerate(image_paths):
                    with open(path, "rb") as fh:
                        data.add_field(
                            f"photo{i}", fh.read(),
                            filename=Path(path).name,
                            content_type="image/jpeg",
                        )
                async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 200:
                        log(f"{len(image_paths)} תמונות נשלחו לטלגרם")
                    else:
                        body = await r.text()
                        log(f"שגיאה בשליחת תמונות ({r.status}): {body}", "WARN")
    except Exception as exc:
        log(f"שגיאת שליחת תמונות לטלגרם: {exc}", "WARN")

# ─── Group validation ─────────────────────────────────────────────────────────

async def validate_groups(page: Page, group_ids: list[str]) -> tuple[list, list]:
    approved, rejected = [], []
    log(f"Validating {len(group_ids)} groups (hair/beauty filter)…")
    for gid in group_ids:
        await page.goto(f"https://www.facebook.com/groups/{gid}", wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        title = (await page.title() or "").lower()
        if any(kw in title for kw in HAIR_KEYWORDS):
            approved.append(gid)
            log(f"  ✓ {gid} — {title[:70]}")
        else:
            rejected.append(gid)
            log(f"  ✗ SKIP {gid} — {title[:70]}", "WARN")
    log(f"Validation: {len(approved)} approved, {len(rejected)} skipped")
    return approved, rejected

# ─── Post to one group ────────────────────────────────────────────────────────

async def post_to_group(page: Page, group_id: str, images: list[str], post_text: str) -> bool:
    url = f"https://www.facebook.com/groups/{group_id}"
    log(f"  → {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(7000)

        title   = await page.title()
        cur_url = page.url
        log(f"  Page: {title}")

        if "login" in cur_url or "checkpoint" in cur_url:
            mark_failed(group_id, "Redirected to login — cookies expired")
            return False

        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)

        trigger = await page.evaluate("""() => {
            for (const b of document.querySelectorAll('div[role="button"]')) {
                const t = (b.innerText||'').trim();
                if (['Write something...','เขียนบางอย่าง...'].includes(t) && b.offsetParent) {
                    b.click(); return 'clicked:' + t;
                }
            }
            return 'not-found';
        }""")

        if not trigger or "not-found" in str(trigger):
            mark_failed(group_id, "Write-something button not found (not a member or posting disabled)")
            return False
        log(f"  Composer trigger: {trigger}")
        await page.wait_for_timeout(8000)

        for _ in range(2):
            focus = await page.evaluate("""() => {
                for (const d of document.querySelectorAll('[role="dialog"]')) {
                    const el = d.querySelector('[contenteditable="true"]');
                    const ok = [...d.querySelectorAll('[role="button"]')].some(b =>
                        ['Post','โพสต์'].includes((b.getAttribute('aria-label')||b.innerText||'').trim()));
                    if (el && ok) { el.focus(); el.click(); return 'focused'; }
                }
                return 'not-found';
            }""")
            if focus == "focused":
                break
            await page.wait_for_timeout(8000)

        if focus != "focused":
            mark_failed(group_id, f"Focus failed: {focus}")
            return False

        await page.wait_for_timeout(300)
        await page.keyboard.type(post_text, delay=10)
        log(f"  Text typed ({len(post_text)} chars)")
        await page.wait_for_timeout(1000)

        if images:
            log(f"  Uploading {len(images)} image(s)…")
            mark_result = await page.evaluate("""() => {
                for (const d of document.querySelectorAll('[role="dialog"]')) {
                    const el = d.querySelector('[contenteditable="true"]');
                    const ok = [...d.querySelectorAll('[role="button"]')].some(b =>
                        ['Post','โพสต์'].includes((b.getAttribute('aria-label')||b.innerText||'').trim()));
                    if (el && ok) {
                        const fi = d.querySelector('input[type="file"]');
                        if (fi) { fi.setAttribute('data-fb-target','yes'); return 'marked'; }
                        return 'no-file-input';
                    }
                }
                return 'no-composer';
            }""")

            if mark_result == "marked":
                file_input = page.locator('input[data-fb-target="yes"]')
                await file_input.set_input_files(images)
                log(f"  {len(images)} file(s) set — waiting for upload…")
                await page.wait_for_timeout(8000)
            else:
                log(f"  Image upload skipped: {mark_result}", "WARN")

        post_result = await page.evaluate("""() => {
            for (const d of document.querySelectorAll('[role="dialog"]')) {
                if (!d.querySelector('[contenteditable="true"]')) continue;
                for (const btn of d.querySelectorAll('[role="button"]')) {
                    const t = (btn.getAttribute('aria-label')||btn.innerText||'').trim();
                    if (['Post','โพสต์'].includes(t) && btn.getAttribute('aria-disabled') !== 'true') {
                        btn.click(); return 'clicked:' + t;
                    }
                }
                return 'post-disabled';
            }
            return 'no-composer';
        }""")

        log(f"  Post button: {post_result}")
        if any(x in str(post_result) for x in ["no-composer", "disabled"]):
            mark_failed(group_id, f"Post button issue: {post_result}")
            return False

        await page.wait_for_timeout(8000)
        mark_posted(group_id)
        return True

    except Exception as exc:
        mark_failed(group_id, str(exc)[:150])
        return False

# ─── Main ─────────────────────────────────────────────────────────────────────

async def main(
    sheet_id:        str,
    cookies_path:    str,
    headless:        bool,
    skip_validation: bool,
) -> None:
    # 1. Load campaign from Google Sheets
    campaign = load_campaign_from_sheets(sheet_id)

    # 2. Download images from Cloudinary
    images = download_cloudinary_images(campaign["image_folder"], count=3)

    # 3. Load Facebook cookies
    cookies = load_cookies(cookies_path)

    log("=" * 60)
    log("Cocochoco — Facebook Groups Poster v4 (Sheets + Cloudinary)")
    log(f"Campaign:        {campaign['campaign_name']}")
    log(f"Groups:          {len(campaign['group_ids'])}")
    log(f"Images:          {len(images)}")
    log(f"Cookies:         {cookies_path} ({'loaded' if cookies else 'NOT FOUND'})")
    log(f"Headless mode:   {headless}")
    log(f"Skip validation: {skip_validation}")
    log("=" * 60)

    if not cookies:
        log("fb_cookies.json לא נמצא!", "ERROR")
        log("הרץ: python3 fb_export_cookies.py", "ERROR")
        sys.exit(1)

    # 4. Generate post text via Claude
    post_text = await generate_post_text(campaign["template_text"])
    log(f"Post preview: {post_text[:100].strip()}…")

    for f in [POSTED_LOG, FAILED_LOG]:
        if os.path.exists(f):
            os.remove(f)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
                "--window-size=1280,900",
            ],
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        await context.add_cookies(sanitize_cookies(cookies))
        page = await context.new_page()

        try:
            log("Checking Facebook login…")
            await page.goto("https://www.facebook.com", wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            if "login" in page.url or "checkpoint" in page.url:
                log("NOT LOGGED IN — cookies פגו תוקף!", "ERROR")
                log("הרץ fb_export_cookies.py ועדכן את fb_cookies.json", "ERROR")
                await browser.close()
                return

            log("Logged in ✓")
            await page.wait_for_timeout(2000)

            group_ids = campaign["group_ids"]
            if not skip_validation:
                group_ids, _ = await validate_groups(page, group_ids)

            posted  = load_posted()
            ok = fail = skipped = 0
            total   = len(group_ids)

            for i, gid in enumerate(group_ids, 1):
                log(f"[{i}/{total}] {gid}")
                if gid in posted:
                    log("  Already posted this run — skipping")
                    skipped += 1
                    continue

                success = await post_to_group(page, gid, images, post_text)
                if success:
                    ok += 1
                    posted.add(gid)
                else:
                    fail += 1

                if i < total:
                    log(f"  Waiting {campaign['wait']}s…")
                    await page.wait_for_timeout(campaign["wait"] * 1000)

            log("=" * 60)
            log(f"Run complete — ✅ posted: {ok} | ❌ failed: {fail} | ⏭ skipped: {skipped}")
            if fail and os.path.exists(FAILED_LOG):
                log("Failed groups:")
                with open(FAILED_LOG) as fh:
                    for line in fh:
                        log(f"  {line.strip()}", "ERROR")

            image_names = [Path(p).name for p in images]
            await send_telegram_report(
                campaign_name=campaign["campaign_name"],
                ok=ok,
                fail=fail,
                skipped=skipped,
                post_text=post_text,
                image_names=image_names,
            )
            await send_telegram_photos(images)

        finally:
            await browser.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Cocochoco Facebook Groups Poster v4")
    parser.add_argument("--sheet-id",        default=os.environ.get("GOOGLE_SHEET_ID", DEFAULT_SHEET))
    parser.add_argument("--cookies",         default=str(DEFAULT_COOKIES))
    parser.add_argument("--no-headless",     action="store_true")
    parser.add_argument("--skip-validation", action="store_true")
    args = parser.parse_args()

    asyncio.run(main(
        sheet_id        = args.sheet_id,
        cookies_path    = args.cookies,
        headless        = not args.no_headless,
        skip_validation = args.skip_validation,
    ))
