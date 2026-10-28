"""Download NPC sell-back prices for MapleStory Classic World items.

Source: https://maplestory.quest (datamined from the Classic World client).
Only the per-item pages are fetched, which the site's robots.txt allows.
Progress is cached so the script can be stopped and resumed.

Usage:
    python fetch_prices.py                       # everything, equipment up to level 30
    python fetch_prices.py --max-equip-level 50
    python fetch_prices.py --max-equip-level 0   # no equipment
"""
import argparse
import html
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

BASE = "https://maplestory.quest"
UA = "Mozilla/5.0 (maple-price-checker; personal use)"
DATA_DIR = Path(__file__).parent / "data"
CACHE_FILE = DATA_DIR / "fetch_cache.json"
OUT_FILE = DATA_DIR / "prices.json"

ITEM_URL_RE = re.compile(r"/items/(\d+)/")
TITLE_RE = re.compile(r"<title>([^<]+)</title>")
DESC_PAYS_RE = re.compile(r"An NPC pays ([\d,]+) mesos? for one")
BODY_PAYS_RE = re.compile(r">NPC pays<(?:[^>]*>){1,12}?\s*([\d,]+|-)\s*<")
LEVEL_RE = re.compile(r">Level<(?:[^>]*>){1,12}?\s*(\d+)\s*<")


def get(url, retries=5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            wait = 2 ** attempt * 2
            print(f"  retry {attempt + 1} for {url} in {wait}s ({e})")
            time.sleep(wait)
    raise RuntimeError(f"giving up on {url}")


def item_urls():
    xml = get(f"{BASE}/sitemap.xml")
    urls = re.findall(r"<loc>([^<]+/items/\d+/[^<]+)</loc>", xml)
    return {int(ITEM_URL_RE.search(u).group(1)): u for u in urls}


# the site adds these to tell same-named items apart; the game shows the bare name
SITE_TAG_RE = re.compile(r" \((?:Weapon|Etc item|Use item|Setup item|Cash item|Equipment|Hat|Top|Bottom|"
                         r"Overall|Shoes|Gloves|Shield|Cape|Earrings|Accessory)\)$")


def clean_name(name):
    return SITE_TAG_RE.sub("", name)


def parse_item(page):
    title = html.unescape(TITLE_RE.search(page).group(1))
    name = clean_name(re.split(r" \(item \d+\)| - MapleStory", title)[0].strip())
    m = DESC_PAYS_RE.search(page)
    if not m:
        m = BODY_PAYS_RE.search(page)
    pays = None
    if m and m.group(1) != "-":
        pays = int(m.group(1).replace(",", ""))
    lv = LEVEL_RE.search(page)
    return name, pays, int(lv.group(1)) if lv else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-equip-level", type=int, default=30,
                    help="keep equipment up to this required level (0 = skip equipment)")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--delay", type=float, default=0.75, help="seconds between requests per worker")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    cache = json.loads(CACHE_FILE.read_text("utf-8")) if CACHE_FILE.exists() else {}

    def is_equip(item_id):
        return int(item_id) < 2000000

    urls = item_urls()
    # equipment needs fetching before its level is known, so every equip page is read once
    todo = {i: u for i, u in urls.items()
            if str(i) not in cache and (args.max_equip_level > 0 or not is_equip(i))}
    print(f"{len(urls)} items in sitemap, {len(cache)} cached, {len(todo)} to fetch")

    def work(item):
        item_id, url = item
        name, pays, level = parse_item(get(url))
        time.sleep(args.delay)
        return item_id, name, pays, level

    done = 0
    with ThreadPoolExecutor(args.workers) as pool:
        for fut in as_completed(pool.submit(work, it) for it in todo.items()):
            try:
                item_id, name, pays, level = fut.result()
            except Exception as e:
                print(f"  failed: {e}")
                continue
            cache[str(item_id)] = {"name": name, "npc_pays": pays, "level": level}
            done += 1
            if done % 25 == 0:
                CACHE_FILE.write_text(json.dumps(cache), "utf-8")
                print(f"  {done}/{len(todo)}")
    CACHE_FILE.write_text(json.dumps(cache), "utf-8")

    items = [{"id": int(i), **v, "name": clean_name(v["name"])} for i, v in cache.items()
             if v["npc_pays"] and (not is_equip(i) or (v.get("level") or 0) <= args.max_equip_level)]
    items.sort(key=lambda x: x["name"].lower())
    OUT_FILE.write_text(json.dumps({
        "source": BASE,
        "game": "MapleStory Classic World",
        "fetched": date.today().isoformat(),
        "items": items,
    }, indent=1), "utf-8")
    print(f"wrote {len(items)} priced items to {OUT_FILE}")


if __name__ == "__main__":
    main()
