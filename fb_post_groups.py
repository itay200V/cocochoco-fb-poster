#!/usr/bin/env python3
"""
Cocochoco — Facebook Groups Poster v4 (Google Sheets + Cloudinary)
===================================================================
Data flow:
  1. Read active campaign (active=TRUE) from Google Sheets
  2. Download image pool from Cloudinary folder (image_url_1 column)
  3. Generate Thai Facebook post via Claude API from template_text
  4. Post to all Facebook groups — each group gets its own random image set
  5. Send Telegram run report + actual images

Google Sheets expected columns:
  active          TRUE / FALSE
  campaign_name   name shown in Telegram report
  template_text   base text sent to Claude for rewriting
  image_url_1     Cloudinary image folder path  (e.g. "cocochoco/open_house")
  group_ids       pipe-separated FB group IDs  (e.g. "123|456|789")
  wait_seconds    seconds between posts (optional, default 15)
  media_type      images | video | mixed  (default: images)
  video_folder    Cloudinary video folder  (default: <image_url_1>_video)

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
import re
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

CONTENT_ANGLES = [
    "skill",       # Skill Development
    "business",    # Business Growth
    "community",   # Professional Community
    "product",     # Product Experience
    "partnership", # Partnership Opportunity
    "brand",       # International Brand Story
]

# Failure reasons that must NOT trigger a retry
PERMANENT_REASONS = (
    "posting disabled",
    "not a member",
    "Write-something button not found",
    "Redirected to login",
    "cookies expired",
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

    raw_ids   = str(row.get("group_ids", ""))
    group_ids = [g.strip() for g in re.split(r"[|,\n\r\s]+", raw_ids) if g.strip()]

    if not group_ids:
        log("לא נמצאו group_ids בשורת הקמפיין", "ERROR")
        sys.exit(1)

    image_folder = str(row.get("image_url_1", ""))
    media_type   = str(row.get("media_type", "images")).strip().lower() or "images"
    video_folder = str(row.get("video_folder", "")).strip() or f"{image_folder}_video"

    return {
        "campaign_name": str(row.get("campaign_name", "campaign")),
        "template_text": str(row.get("template_text", "")),
        "image_folder":  image_folder,
        "video_folder":  video_folder,
        "media_type":    media_type,
        "group_ids":     group_ids,
        "wait":          int(row.get("wait_seconds", 15) or 15),
    }

# ─── Cloudinary ───────────────────────────────────────────────────────────────

def download_cloudinary_images(folder: str, count: int = 8) -> list[str]:
    """Download a pool of images; each group will sample its own subset."""
    import cloudinary
    import cloudinary.api

    cloudinary.config(
        cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME", "dhmttntds"),
        api_key    = os.environ.get("CLOUDINARY_API_KEY"),
        api_secret = os.environ.get("CLOUDINARY_API_SECRET"),
    )

    log(f"Cloudinary: listing folder '{folder}'…")
    result    = cloudinary.api.resources(type="upload", folder=folder, max_results=100)
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

    log(f"Cloudinary: {len(paths)} image(s) in pool")
    return paths


def download_cloudinary_video(folder: str) -> str | None:
    """Download one random video from a Cloudinary folder. Returns local path or None."""
    import cloudinary
    import cloudinary.api

    cloudinary.config(
        cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME", "dhmttntds"),
        api_key    = os.environ.get("CLOUDINARY_API_KEY"),
        api_secret = os.environ.get("CLOUDINARY_API_SECRET"),
    )

    log(f"Cloudinary: listing video folder '{folder}'…")
    try:
        result    = cloudinary.api.resources(type="upload", resource_type="video",
                                             folder=folder, max_results=50)
        resources = result.get("resources", [])
    except Exception as exc:
        log(f"Cloudinary video listing error: {exc}", "WARN")
        return None

    if not resources:
        log(f"Cloudinary: no videos found in '{folder}'", "WARN")
        return None

    res  = random.choice(resources)
    url  = res["secure_url"]
    ext  = res.get("format", "mp4")
    name = res["public_id"].replace("/", "_") + f".{ext}"
    dest = os.path.join(tempfile.mkdtemp(prefix="fb_video_"), name)

    log(f"  Downloading video: {url}")
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
    log(f"Cloudinary: video ready ({len(resp.content) // 1024} KB)")
    return dest

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

# ─── Post History ─────────────────────────────────────────────────────────────

def load_post_history(sheet_id: str) -> list[dict]:
    """טוען 10 פוסטים אחרונים מ-post_history sheet"""
    import gspread
    from google.oauth2.service_account import Credentials

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        info = json.loads(sa_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        sa_file = SCRIPT_DIR / "service_account.json"
        creds = Credentials.from_service_account_file(str(sa_file), scopes=SCOPES)

    try:
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(sheet_id).worksheet("post_history")
        records = ws.get_all_records()
        return records[-10:] if len(records) > 10 else records
    except Exception as exc:
        log(f"Could not load post_history: {exc}", "WARN")
        return []


def pick_next_angle(history: list[dict]) -> str:
    """בוחר angle שלא שומש לאחרונה"""
    used_recently = [r.get("angle_used", "") for r in history[-6:]]
    available = [a for a in CONTENT_ANGLES if a not in used_recently]
    if not available:
        available = CONTENT_ANGLES  # fallback — כל ה-6 שומשו, מתחיל מחדש
    chosen = random.choice(available)
    log(f"Angle chosen: {chosen} (recently used: {used_recently})")
    return chosen


def save_post_to_history(
    sheet_id: str,
    campaign_name: str,
    angle: str,
    post_text: str,
    group_count: int,
    media_type: str,
) -> None:
    """שומר את הריצה ב-post_history sheet"""
    import gspread
    from google.oauth2.service_account import Credentials

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        info = json.loads(sa_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        sa_file = SCRIPT_DIR / "service_account.json"
        creds = Credentials.from_service_account_file(str(sa_file), scopes=SCOPES)

    try:
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(sheet_id).worksheet("post_history")

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        snippet = post_text[:100].replace("\n", " ")

        ws.append_row([
            now,
            campaign_name,
            angle,
            snippet,
            post_text[:500],
            group_count,
            media_type,
        ])
        log(f"Saved run to post_history: angle={angle}")
    except Exception as exc:
        log(f"Could not save to post_history: {exc}", "WARN")


# ─── Claude API ───────────────────────────────────────────────────────────────

async def generate_post_variants(
    template_text: str,
    sheet_id: str,
    num_variants: int = 3
) -> tuple[list[str], str]:
    """
    מחזיר (list של 3 גרסאות שונות, angle_used)
    כל גרסה מתאימה לקבוצות שונות
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log("ANTHROPIC_API_KEY לא מוגדר", "WARN")
        fallback = template_text + FIXED_CONTACT
        return [fallback] * num_variants, "fallback"

    guidelines = load_system_guidelines()
    history = load_post_history(sheet_id)
    angle = pick_next_angle(history)

    history_context = ""
    if history:
        history_context = "\n\nPOSTS FROM LAST RUNS (DO NOT REPEAT THESE):\n"
        for h in history[-5:]:
            history_context += f"- [{h.get('angle_used','')}] {h.get('post_snippet','')}\n"

    angle_instructions = {
        "skill":       "Focus on SKILL DEVELOPMENT. Opening: a question about staying competitive.",
        "business":    "Focus on BUSINESS GROWTH. Opening: ROI numbers or revenue potential.",
        "community":   "Focus on PROFESSIONAL COMMUNITY. Opening: invitation to meet peers.",
        "product":     "Focus on PRODUCT EXPERIENCE. Opening: sensory/visual result description.",
        "partnership": "Focus on PARTNERSHIP OPPORTUNITY. Opening: exclusive partner benefits.",
        "brand":       "Focus on INTERNATIONAL BRAND STORY. Opening: global credibility.",
    }

    angle_instruction = angle_instructions.get(angle, "")

    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=api_key)

        system_prompt = guidelines if guidelines else (
            "You are a Thai social media copywriter for COCOCHOCO Academy Bangkok."
        )

        user_prompt = (
            f"Write EXACTLY {num_variants} DIFFERENT Facebook group posts for Thai hairstylists.\n\n"
            f"ANGLE FOR THIS RUN: {angle.upper()}\n"
            f"{angle_instruction}\n\n"
            f"RULES:\n"
            f"- Each post must have a DIFFERENT opening sentence\n"
            f"- Each post must use a DIFFERENT structure (one more narrative, one more list-based, one more question-led)\n"
            f"- Each post must use DIFFERENT emoji selection\n"
            f"- All posts must follow brand_guidelines.md strictly\n"
            f"- Output ONLY the posts, separated by this exact delimiter: ===VARIANT===\n"
            f"- No labels, no numbering, no explanations\n\n"
            f"{history_context}\n\n"
            f"Campaign template:\n{template_text}"
        )

        msg = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1800,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw = msg.content[0].text.strip()
        variants = [v.strip() for v in raw.split("===VARIANT===") if v.strip()]

        if len(variants) < num_variants:
            log(f"Got {len(variants)} variants instead of {num_variants} — duplicating last", "WARN")
            while len(variants) < num_variants:
                variants.append(variants[-1])

        log(f"Generated {len(variants)} variants, angle={angle}")
        for i, v in enumerate(variants):
            log(f"  Variant {i+1}: {v[:60]}…")

        return variants[:num_variants], angle

    except Exception as exc:
        log(f"Claude API error: {exc}", "WARN")
        fallback = template_text + FIXED_CONTACT
        return [fallback] * num_variants, "fallback"

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
    allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    return [{k: v for k, v in c.items() if k in allowed} for c in cookies]


def expiring_cookies(cookies: list, days: int = 3) -> list[str]:
    """Return names of cookies expiring within `days` days."""
    now       = datetime.now().timestamp()
    threshold = days * 24 * 3600
    return [
        c.get("name", "?")
        for c in cookies
        if 0 < c.get("expires", -1) - now < threshold
    ]

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

# ─── Telegram ─────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _tg_post(session: aiohttp.ClientSession, token: str, method: str, **kwargs) -> bool:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        async with session.post(url, timeout=aiohttp.ClientTimeout(total=30), **kwargs) as r:
            if r.status == 200:
                return True
            body = await r.text()
            log(f"Telegram {method} error ({r.status}): {body}", "WARN")
            return False
    except Exception as exc:
        log(f"Telegram {method} exception: {exc}", "WARN")
        return False


async def send_telegram_alert(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    async with aiohttp.ClientSession() as s:
        await _tg_post(s, token, "sendMessage",
                       json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"})


async def send_telegram_report(
    campaign_name: str,
    ok: int,
    fail: int,
    skipped: int,
    post_text: str,
    media_type: str,
    media_names: list[str],
) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID לא מוגדרים — דיווח מדולג", "WARN")
        return

    now         = datetime.now().strftime("%d/%m/%Y %H:%M")
    media_line  = _esc(", ".join(media_names) if media_names else "—")
    status_line = f"✅ פורסם: {ok}  |  ❌ נכשל: {fail}  |  ⏭ דולג: {skipped}"
    media_icon  = {"images": "🖼", "video": "🎬", "mixed": "🎬🖼"}.get(media_type, "🖼")

    text = (
        f"📊 <b>דוח פרסום — Cocochoco</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 קמפיין: <code>{_esc(campaign_name)}</code>\n"
        f"🕐 תאריך: {now}\n"
        f"{status_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{media_icon} מדיה ({media_type}): {media_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>טקסט שפורסם:</b>\n{_esc(post_text)}"
    )

    async with aiohttp.ClientSession() as s:
        await _tg_post(s, token, "sendMessage",
                       json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
        log("דוח טלגרם נשלח")


async def send_telegram_media(
    image_paths: list[str],
    video_path: str | None,
    media_type: str,
) -> None:
    """Send media preview to Telegram based on media_type (images/video/mixed)."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return

    async with aiohttp.ClientSession() as session:
        if media_type == "video" and video_path:
            with open(video_path, "rb") as fh:
                data = aiohttp.FormData()
                data.add_field("chat_id", chat_id)
                data.add_field("video", fh, filename=Path(video_path).name,
                               content_type="video/mp4")
                await _tg_post(session, token, "sendVideo", data=data)
            log(f"סרטון נשלח לטלגרם: {Path(video_path).name}")

        elif media_type == "mixed" and video_path and image_paths:
            # sendMediaGroup: one photo + one video
            img_path = image_paths[0]
            media    = [
                {"type": "photo", "media": "attach://photo0"},
                {"type": "video", "media": "attach://video0"},
            ]
            data = aiohttp.FormData()
            data.add_field("chat_id", chat_id)
            data.add_field("media", json.dumps(media))
            with open(img_path, "rb") as fh:
                data.add_field("photo0", fh.read(), filename=Path(img_path).name,
                               content_type="image/jpeg")
            with open(video_path, "rb") as fh:
                data.add_field("video0", fh.read(), filename=Path(video_path).name,
                               content_type="video/mp4")
            await _tg_post(session, token, "sendMediaGroup", data=data)
            log("תמונה + סרטון נשלחו לטלגרם")

        else:
            # images mode (or fallback)
            if not image_paths:
                return
            if len(image_paths) == 1:
                path = image_paths[0]
                with open(path, "rb") as fh:
                    data = aiohttp.FormData()
                    data.add_field("chat_id", chat_id)
                    data.add_field("photo", fh, filename=Path(path).name,
                                   content_type="image/jpeg")
                    await _tg_post(session, token, "sendPhoto", data=data)
                log(f"תמונה נשלחה לטלגרם: {Path(path).name}")
            else:
                media = [{"type": "photo", "media": f"attach://photo{i}"}
                         for i in range(len(image_paths))]
                data  = aiohttp.FormData()
                data.add_field("chat_id", chat_id)
                data.add_field("media", json.dumps(media))
                for i, path in enumerate(image_paths):
                    with open(path, "rb") as fh:
                        data.add_field(f"photo{i}", fh.read(),
                                       filename=Path(path).name, content_type="image/jpeg")
                await _tg_post(session, token, "sendMediaGroup", data=data)
                log(f"{len(image_paths)} תמונות נשלחו לטלגרם")

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

async def post_to_group(
    page: Page, group_id: str, images: list[str], post_text: str
) -> tuple[bool, bool]:
    """
    Returns (success, retryable).
    success=True means posted OK.
    retryable=False means a permanent failure — do not retry.
    """
    url = f"https://www.facebook.com/groups/{group_id}"
    log(f"  → {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(7000)

        title   = await page.title()
        cur_url = page.url
        log(f"  Page: {title}")

        if "login" in cur_url or "checkpoint" in cur_url:
            return False, False  # permanent — cookies expired

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
            return False, False  # permanent — not a member or posting disabled

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
            return False, True  # transient — dialog didn't open in time

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
        if "post-disabled" in str(post_result):
            return False, False  # permanent — posting disabled
        if "no-composer" in str(post_result):
            return False, True   # transient — composer disappeared

        await page.wait_for_timeout(8000)
        mark_posted(group_id)
        return True, False

    except Exception as exc:
        log(f"  Exception: {exc}", "WARN")
        return False, True  # transient — playwright / network error

# ─── Main ─────────────────────────────────────────────────────────────────────

async def main(
    sheet_id:        str,
    cookies_path:    str,
    headless:        bool,
    skip_validation: bool,
) -> None:
    campaign   = load_campaign_from_sheets(sheet_id)
    media_type = campaign["media_type"]

    # Download media assets based on media_type
    image_pool: list[str] = []
    video_path: str | None = None

    if media_type in ("images", "mixed"):
        image_pool = download_cloudinary_images(campaign["image_folder"], count=8)

    if media_type in ("video", "mixed"):
        video_path = download_cloudinary_video(campaign["video_folder"])
        if video_path is None and media_type == "video":
            log("No video available and media_type=video — aborting", "ERROR")
            sys.exit(1)

    cookies = load_cookies(cookies_path)

    log("=" * 60)
    log("Cocochoco — Facebook Groups Poster v4 (Sheets + Cloudinary)")
    log(f"Campaign:        {campaign['campaign_name']}")
    log(f"Media type:      {media_type}")
    log(f"Groups:          {len(campaign['group_ids'])}")
    log(f"Image pool:      {len(image_pool)}")
    log(f"Video:           {Path(video_path).name if video_path else '—'}")
    log(f"Cookies:         {cookies_path} ({'loaded' if cookies else 'NOT FOUND'})")
    log(f"Headless mode:   {headless}")
    log(f"Skip validation: {skip_validation}")
    log("=" * 60)

    if not cookies:
        log("fb_cookies.json לא נמצא!", "ERROR")
        log("הרץ: python3 fb_export_cookies.py", "ERROR")
        sys.exit(1)

    # Proactive cookie expiry warning
    expiring = expiring_cookies(cookies, days=3)
    if expiring:
        msg = (
            f"⚠️ <b>אזהרה — Cocochoco Bot</b>\n"
            f"הcookies הבאים פגים תוך 3 ימים:\n"
            f"<code>{', '.join(expiring)}</code>\n"
            f"יש לחדש את fb_cookies.json בהקדם."
        )
        await send_telegram_alert(msg)
        log(f"Cookie expiry warning sent: {expiring}", "WARN")

    post_variants, angle_used = await generate_post_variants(
        campaign["template_text"],
        sheet_id,
        num_variants=3
    )
    post_text = post_variants[0]  # לדוח הטלגרם
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
                await send_telegram_alert(
                    "🚨 <b>Cocochoco Bot — כישלון קריטי</b>\n"
                    "הבוט לא הצליח להתחבר לפייסבוק — ה-Cookies פגו תוקף.\n"
                    "יש לחדש את fb_cookies.json ולעדכן ב-Railway."
                )
                await browser.close()
                return

            log("Logged in ✓")
            await page.wait_for_timeout(2000)

            group_ids = campaign["group_ids"]
            if not skip_validation:
                group_ids, _ = await validate_groups(page, group_ids)

            posted  = load_posted()
            ok = fail = skipped = 0
            total       = len(group_ids)
            sample_size = min(3, len(image_pool))

            def pick_group_files() -> list[str]:
                """Return a fresh random media selection for one group."""
                if media_type == "images":
                    return random.sample(image_pool, sample_size)
                elif media_type == "video":
                    return [video_path] if video_path else []
                else:  # mixed
                    if image_pool and video_path:
                        return [random.choice(image_pool), video_path]
                    return image_pool[:1] or ([video_path] if video_path else [])

            # Build Telegram preview once (same logic — 3 files, like any single group)
            telegram_preview = pick_group_files()

            for i, gid in enumerate(group_ids, 1):
                log(f"[{i}/{total}] {gid}")
                if gid in posted:
                    log("  Already posted this run — skipping")
                    skipped += 1
                    continue

                # Each group gets its own independent random selection
                group_files = pick_group_files()
                log(f"  Media: {[Path(p).name for p in group_files]}")

                variant_index = i % len(post_variants)  # rotation: 0,1,2,0,1,2,...
                group_post_text = post_variants[variant_index]
                log(f"  Using variant {variant_index + 1}/{len(post_variants)}")
                success, retryable = await post_to_group(page, gid, group_files, group_post_text)

                if not success and retryable:
                    log(f"  Transient failure — retrying in 15s…", "WARN")
                    await page.wait_for_timeout(15000)
                    success, _ = await post_to_group(page, gid, group_files, group_post_text)

                if success:
                    ok += 1
                    posted.add(gid)
                else:
                    mark_failed(gid, "permanent failure" if not retryable else "retry also failed")
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

            media_names = [Path(p).name for p in telegram_preview]
            await send_telegram_report(
                campaign_name=campaign["campaign_name"],
                ok=ok,
                fail=fail,
                skipped=skipped,
                post_text=post_text,
                media_type=media_type,
                media_names=media_names,
            )
            save_post_to_history(
                sheet_id=sheet_id,
                campaign_name=campaign["campaign_name"],
                angle=angle_used,
                post_text=post_text,
                group_count=ok,
                media_type=media_type,
            )
            tg_images = [p for p in telegram_preview if not p.endswith((".mp4", ".mov", ".avi"))]
            tg_video  = next((p for p in telegram_preview if p.endswith((".mp4", ".mov", ".avi"))), video_path if media_type in ("video", "mixed") else None)
            await send_telegram_media(tg_images, tg_video, media_type)

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
