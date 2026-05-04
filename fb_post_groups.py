#!/usr/bin/env python3
"""
Cocochoco — Facebook Groups Poster v3 (Playwright / Railway edition)
=====================================================================
  • Playwright Chromium — headless by default, no external Chrome needed
  • Facebook auth via saved cookies (fb_cookies.json) — no login prompt
  • Claude API — generates unique Thai post each run
  • Telegram report — sent after every run
  • Railway-ready — works inside Docker with no display

ENV VARS:
  ANTHROPIC_API_KEY   required for AI text generation (falls back to config text)
  TELEGRAM_BOT_TOKEN  Telegram bot token for run reports
  TELEGRAM_CHAT_ID    Telegram chat/channel ID to send reports to

CLI FLAGS:
  --config PATH        campaign config JSON  (default: fb_campaign_config.json)
  --templates PATH     templates JSON        (default: posts_templates.json)
  --cookies PATH       Facebook cookies JSON (default: fb_cookies.json)
  --no-headless        show browser window (local debug)
  --skip-validation    skip hair/beauty group-title check
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
from playwright.async_api import async_playwright, Page

# ─── Paths / constants ────────────────────────────────────────────────────────

SCRIPT_DIR        = Path(__file__).parent
DEFAULT_CONFIG    = SCRIPT_DIR / "fb_campaign_config.json"
DEFAULT_TEMPLATES = SCRIPT_DIR / "posts_templates.json"
DEFAULT_COOKIES   = SCRIPT_DIR / "fb_cookies.json"

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

# ─── Telegram report ──────────────────────────────────────────────────────────

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
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID לא מוגדרים — דיווח טלגרם מדולג", "WARN")
        return

    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    images_line = ", ".join(image_names) if image_names else "—"
    status_line = f"✅ פורסם: {ok}  |  ❌ נכשל: {fail}  |  ⏭ דולג: {skipped}"

    text = (
        f"📊 *דוח פרסום — Cocochoco*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 קמפיין: `{campaign_name}`\n"
        f"🕐 תאריך: {now}\n"
        f"{status_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🖼 תמונות: {images_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 *טקסט שפורסם:*\n{post_text}"
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}

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

# ─── Config ───────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as fh:
        cfg = json.load(fh)
    images = []
    for p in cfg.get("images", []):
        resolved = Path(p).expanduser()
        if not resolved.is_absolute():
            resolved = SCRIPT_DIR / resolved
        images.append(str(resolved))
    for img in images:
        if not os.path.exists(img):
            log(f"Image not found: {img}", "ERROR")
            sys.exit(1)
    return {
        "post_text": cfg["post_text"],
        "images":    images,
        "group_ids": cfg["group_ids"],
        "wait":      cfg.get("wait_between_posts_seconds", 25),
    }

def load_templates(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)

def load_cookies(path: str) -> list | None:
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)

# ─── Claude API ───────────────────────────────────────────────────────────────

async def generate_post_text(templates_cfg: dict, fallback_text: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log("ANTHROPIC_API_KEY not set — using config post_text as-is", "WARN")
        return fallback_text

    templates = templates_cfg.get("templates", [])
    if not templates:
        log("posts_templates.json has no templates — using config post_text", "WARN")
        return fallback_text

    template = random.choice(templates)
    log(f"Claude template: {template.get('id', '?')} (tone: {template.get('tone', '?')})")

    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=api_key)

        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=(
                "You are a Thai social media copywriter for COCOCHOCO Academy Bangkok, "
                "a professional keratin treatment and hair styling school. "
                "Write natural, warm Facebook group posts in Thai. "
                "Output ONLY the post text — no labels, no explanations, no English."
            ),
            messages=[{
                "role": "user",
                "content": (
                    "Rewrite this Facebook post in Thai with fresh, natural phrasing.\n"
                    "Keep ALL facts exactly: dates, phone number, Line OA link, "
                    "academy name, course name, bullet points, and emojis.\n"
                    "Only vary sentence structure and word choice.\n\n"
                    f"Original post:\n{template['text']}"
                ),
            }],
        )

        text = msg.content[0].text.strip()
        log(f"Claude output: {len(text)} chars")
        return text

    except Exception as exc:
        log(f"Claude API error: {exc} — falling back to config text", "WARN")
        return fallback_text

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
            log(f"  ✗ SKIP {gid} — not hair/beauty: {title[:70]}", "WARN")
    log(f"Validation done: {len(approved)} approved, {len(rejected)} skipped")
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
        await page.wait_for_timeout(3000)

        # Find composer dialog and focus input
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
            await page.wait_for_timeout(3000)

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
    config_path:     str,
    templates_path:  str,
    cookies_path:    str,
    headless:        bool,
    skip_validation: bool,
) -> None:
    cfg           = load_config(config_path)
    templates_cfg = load_templates(templates_path)
    cookies       = load_cookies(cookies_path)

    log("=" * 60)
    log("Cocochoco — Facebook Groups Poster v3 (Playwright)")
    log(f"Config:          {config_path}")
    log(f"Templates:       {templates_path} ({'loaded' if templates_cfg else 'not found — using raw text'})")
    log(f"Cookies:         {cookies_path} ({'loaded' if cookies else 'NOT FOUND'})")
    log(f"Groups:          {len(cfg['group_ids'])}")
    log(f"Images:          {len(cfg['images'])}")
    log(f"Headless mode:   {headless}")
    log(f"Skip validation: {skip_validation}")
    log("=" * 60)

    if not cookies:
        log("fb_cookies.json לא נמצא!", "ERROR")
        log("הרץ: python3 fb_export_cookies.py — ואז העלה מחדש ל-Railway", "ERROR")
        sys.exit(1)

    if templates_cfg:
        post_text = await generate_post_text(templates_cfg, cfg["post_text"])
    else:
        post_text = cfg["post_text"]
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
        await context.add_cookies(cookies)
        page = await context.new_page()

        try:
            log("Checking Facebook login…")
            await page.goto("https://www.facebook.com", wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            cur_url = page.url
            if "login" in cur_url or "checkpoint" in cur_url:
                log("NOT LOGGED IN — cookies פגו תוקף!", "ERROR")
                log("הרץ fb_export_cookies.py מקומית ועדכן את fb_cookies.json ב-Railway", "ERROR")
                await browser.close()
                return

            log("Logged in ✓")
            await page.wait_for_timeout(2000)

            group_ids = cfg["group_ids"]
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

                success = await post_to_group(page, gid, cfg["images"], post_text)
                if success:
                    ok += 1
                    posted.add(gid)
                else:
                    fail += 1

                if i < total:
                    log(f"  Waiting {cfg['wait']}s…")
                    await page.wait_for_timeout(cfg["wait"] * 1000)

            log("=" * 60)
            log(f"Run complete — ✅ posted: {ok} | ❌ failed: {fail} | ⏭ skipped: {skipped}")
            if fail and os.path.exists(FAILED_LOG):
                log("Failed groups:")
                with open(FAILED_LOG) as fh:
                    for line in fh:
                        log(f"  {line.strip()}", "ERROR")

            campaign_name = Path(config_path).stem
            image_names   = [Path(p).name for p in cfg["images"]]
            await send_telegram_report(
                campaign_name=campaign_name,
                ok=ok,
                fail=fail,
                skipped=skipped,
                post_text=post_text,
                image_names=image_names,
            )

        finally:
            await browser.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Cocochoco Facebook Groups Poster v3")
    parser.add_argument("--config",          default=str(DEFAULT_CONFIG))
    parser.add_argument("--templates",       default=str(DEFAULT_TEMPLATES))
    parser.add_argument("--cookies",         default=str(DEFAULT_COOKIES))
    parser.add_argument("--no-headless",     action="store_true")
    parser.add_argument("--skip-validation", action="store_true")
    args = parser.parse_args()

    asyncio.run(main(
        config_path     = args.config,
        templates_path  = args.templates,
        cookies_path    = args.cookies,
        headless        = not args.no_headless,
        skip_validation = args.skip_validation,
    ))
