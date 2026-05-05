#!/usr/bin/env python3
"""
fb_export_cookies.py
====================
קורא cookies של פייסבוק ישירות מקובץ הפרופיל של Chrome בדיסק.
לא דורש שChrome יהיה פתוח.

דרישות:
  pip3 install cryptography

הרצה:
  python3 fb_export_cookies.py
"""
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

OUT_FILE     = Path(__file__).parent / "fb_cookies.json"
COOKIES_DB   = Path.home() / "Library/Application Support/Google/Chrome/Profile 2/Cookies"
SAMESITE_MAP = {-1: None, 0: "None", 1: "Lax", 2: "Strict"}

# Chrome epoch starts at Jan 1, 1601 — convert to Unix timestamp
CHROME_EPOCH_OFFSET = 11644473600


def get_encryption_key() -> bytes:
    """Fetch Chrome's AES key from macOS Keychain and derive it via PBKDF2."""
    result = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage", "-a", "Chrome"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"שגיאה בקריאת Keychain: {result.stderr.strip()}")
        sys.exit(1)
    password = result.stdout.strip().encode()
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)


def decrypt_value(encrypted: bytes, key: bytes) -> str:
    """Decrypt a Chrome cookie value (v10 AES-CBC)."""
    if not encrypted:
        return ""
    if not encrypted.startswith(b"v10"):
        # unencrypted (rare)
        return encrypted.decode("utf-8", errors="replace")

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    iv   = b" " * 16
    data = encrypted[3:]  # strip 'v10' prefix
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    dec    = cipher.decryptor()
    raw    = dec.update(data) + dec.finalize()
    # remove PKCS7 padding
    pad = raw[-1]
    return raw[:-pad].decode("utf-8", errors="replace")


def main() -> None:
    if not COOKIES_DB.exists():
        print(f"שגיאה: קובץ Cookies לא נמצא ב-{COOKIES_DB}")
        print("בדוק שהנתיב נכון — אולי הפרופיל שלך הוא 'Default' ולא 'Profile 2'")
        sys.exit(1)

    print(f"קורא cookies מ-{COOKIES_DB}")
    key = get_encryption_key()

    # Copy DB to temp (Chrome may lock it)
    tmp_db = tempfile.mktemp(suffix=".db")
    shutil.copy2(COOKIES_DB, tmp_db)

    con = sqlite3.connect(tmp_db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT host_key, name, encrypted_value, value, path, "
        "expires_utc, is_secure, is_httponly, samesite "
        "FROM cookies WHERE host_key LIKE '%facebook.com'"
    ).fetchall()
    con.close()

    print(f"נמצאו {len(rows)} cookies של פייסבוק")

    clean = []
    for row in rows:
        raw_value = row["value"] or ""
        if row["encrypted_value"]:
            raw_value = decrypt_value(bytes(row["encrypted_value"]), key)

        expires_utc = row["expires_utc"]
        expires_unix = (expires_utc / 1_000_000 - CHROME_EPOCH_OFFSET) if expires_utc else -1

        samesite_int = row["samesite"] if row["samesite"] is not None else -1
        samesite_str = SAMESITE_MAP.get(samesite_int)

        entry: dict = {
            "name":     row["name"],
            "value":    raw_value,
            "domain":   row["host_key"],
            "path":     row["path"],
            "httpOnly": bool(row["is_httponly"]),
            "secure":   bool(row["is_secure"]),
        }
        if expires_unix > 0:
            entry["expires"] = expires_unix
        if samesite_str in ("None", "Lax", "Strict"):
            entry["sameSite"] = samesite_str
        clean.append(entry)

    OUT_FILE.write_text(json.dumps(clean, indent=2, ensure_ascii=False))
    print(f"✅ {len(clean)} cookies נשמרו ל-{OUT_FILE}")


if __name__ == "__main__":
    main()
