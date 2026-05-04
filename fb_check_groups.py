#!/usr/bin/env python3
"""
Check Facebook group names via Chrome CDP.
Prints group ID, name, and whether it seems hair/beauty related.
"""

import json
import os
import time
import urllib.request
import websocket

CDP_URL = "http://localhost:9222"

HAIR_KEYWORDS = [
    "hair", "salon", "barber", "beauty", "cosmet", "keratin", "เสริมสวย",
    "ผม", "ร้านเสริมสวย", "ช่างผม", "สปา", "spa", "stylist", "hairdress",
    "tricholog", "shampoo", "treatment", "coiffure", "coiffeur", "คอสเมติก",
    "สี", "ดัด", "ยืด", "เคราติน", "บิวตี้", "ความงาม", "เล็บ", "nail",
    "lash", "brow", "makeup", "แต่งหน้า", "ทรง", "perma",
]

ALL_GROUPS = [
    "1886167198702072",
    "3574516169285931",
    "485525993247707",
    "409171152972571",
    "274769928397681",
    "4550738428369966",
    "1721624335066834",
    "1313882455872777",
    "582427686617116",
    "509340084719628",
    "188911772983703",
    "408049542737491",
    "1754277188670113",
    "443234448656750",
    "1526984827371848",
    "414884647402991",
    "703641074518735",
    "382884423713395",
    "640825374570436",
]


def get_ws_url():
    resp = urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=5)
    tabs = json.loads(resp.read())
    for tab in tabs:
        if tab.get("type") == "page":
            return tab["webSocketDebuggerUrl"]
    raise RuntimeError("No page tab found in Chrome")


def cdp_send(ws, method, params=None):
    import random
    msg_id = random.randint(1, 99999)
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        raw = ws.recv()
        msg = json.loads(raw)
        if msg.get("id") == msg_id:
            return msg.get("result", {})


def get_group_name(ws, group_id):
    url = f"https://www.facebook.com/groups/{group_id}"
    cdp_send(ws, "Page.navigate", {"url": url})
    time.sleep(4)
    result = cdp_send(ws, "Runtime.evaluate", {
        "expression": "document.title",
        "returnByValue": True
    })
    title = result.get("result", {}).get("value", "")
    return title


def is_hair_related(name):
    name_lower = name.lower()
    return any(kw in name_lower for kw in HAIR_KEYWORDS)


def main():
    ws_url = get_ws_url()
    ws = websocket.WebSocket()
    ws.connect(ws_url)
    cdp_send(ws, "Page.enable")

    print(f"{'GROUP ID':<20} {'HAIR?':<6} TITLE")
    print("-" * 90)

    hair_groups = []
    non_hair_groups = []

    for gid in ALL_GROUPS:
        title = get_group_name(ws, gid)
        related = is_hair_related(title)
        flag = "YES" if related else "NO ⚠"
        print(f"{gid:<20} {flag:<6} {title}")
        if related:
            hair_groups.append(gid)
        else:
            non_hair_groups.append(gid)

    ws.close()

    print("\n=== HAIR-RELATED GROUPS ===")
    for g in hair_groups:
        print(g)

    print("\n=== NOT RELATED — REMOVE THESE ===")
    for g in non_hair_groups:
        print(g)

    import tempfile
    out = os.path.join(tempfile.gettempdir(), "fb_verified_hair_groups.txt")
    with open(out, "w") as f:
        for g in hair_groups:
            f.write(g + "\n")

    print(f"\nSaved {len(hair_groups)} verified groups to {out}")


if __name__ == "__main__":
    main()
