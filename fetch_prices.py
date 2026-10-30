"""Download NPC sell-back prices for MapleStory Classic World items.

Source: https://maplestory.quest (datamined from the Classic World client).
Only the per-item pages are fetched, which the site's robots.txt allows.
Progress is cached so the script can be stopped and resumed.

Every item is kept, also ones an NPC won't buy (npc_pays null, e.g. scrolls): their names are
needed to recognise listings for Free Market prices.

Usage:
    python fetch_prices.py                       # everything
    python fetch_prices.py --max-equip-level 50  # equipment only up to level 50
    python fetch_prices.py --max-equip-level 0   # no equipment
"""
import argparse
import html
import json
import re
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

BASE = "https://maplestory.quest"
UA = "Mozilla/5.0 (maple-price-checker; personal use)"
DATA_DIR = Path(__file__).parent / "data"
CACHE_FILE = DATA_DIR / "fetch_cache.json"
OUT_FILE = DATA_DIR / "prices.json"
RECIPES_FILE = DATA_DIR / "recipes.json"

ITEM_URL_RE = re.compile(r"/items/(\d+)/")
TITLE_RE = re.compile(r"<title>([^<]+)</title>")
DESC_PAYS_RE = re.compile(r"An NPC pays ([\d,]+) mesos? for one")
BODY_PAYS_RE = re.compile(r">NPC pays<(?:[^>]*>){1,12}?\s*([\d,]+|-)\s*<")
LEVEL_RE = re.compile(r">Level<(?:[^>]*>){1,12}?\s*(\d+)\s*<")
SLOTS_RE = re.compile(r'"name":"Upgrade slots","value":(\d+)')  # equipment: scroll slots when new
# the "Crafted" part of an item's page: one <li> per recipe that makes it
CRAFTED_RE = re.compile(r">Crafted</h3>(.*?)</section>", re.S)
RECIPE_RE = re.compile(r"<li\b(.*?)</li>", re.S)
DISCIPLINE_RE = re.compile(r"</i>([A-Za-z ]+)<span[^>]*>craft level <!-- -->(\d+)</span>")
FEE_RE = re.compile(r'mesos\.png"/>([\d,]+)</span>')
INGREDIENT_RE = re.compile(r'aria-label="([^"]+?) ×(\d+)"[^>]*href="/items/(\d+)/')
MAKES_RE = re.compile(r'title="Makes (\d+)"')
# the "Used to craft" part of a material's page: one card per recipe, with every ingredient.
# Some products' own pages (scrolls) don't show their recipe, so recipes are also put
# together from these cards
USED_RE = re.compile(r">Used to craft \(\d+\)</h3>(.*?)</section>", re.S)
CARD_RE = re.compile(r'<li class="result">(.*?)</article></li>', re.S)
PRODUCT_RE = re.compile(r'class="result-stretch" href="/items/(\d+)/[^"]*">([^<]+)<')
META_RE = re.compile(r'class="result-meta">(.*?)</p>', re.S)
META_TEXT_RE = re.compile(r"([A-Za-z ]+?)\s*·\s*Craft Lv\s*(\d+)\s*·\s*([\d,]+)")
SELF_RE = re.compile(r'title="[^"]+? ×(\d+): this item"')


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


def parse_recipes(page):
    """Every recipe that makes this item: discipline, craft level, meso fee, ingredients
    ({id, name, qty}) and how many one craft makes."""
    section = CRAFTED_RE.search(page)
    recipes = []
    for li in RECIPE_RE.findall(section.group(1)) if section else []:
        kind, fee, makes = DISCIPLINE_RE.search(li), FEE_RE.search(li), MAKES_RE.search(li)
        ingredients = [{"id": int(i), "name": html.unescape(n), "qty": int(q)}
                       for n, q, i in INGREDIENT_RE.findall(li)]
        if not (kind and ingredients):
            continue
        recipes.append({"discipline": kind.group(1).strip(), "craft_level": int(kind.group(2)),
                        "fee": int(fee.group(1).replace(",", "")) if fee else 0,
                        "makes": int(makes.group(1)) if makes else 1, "ingredients": ingredients})
    return recipes


def parse_used_in(page, item_id, name):
    """Every recipe this item goes into, as shown on its page: product, discipline, craft
    level, fee and all ingredients (this item included). Recipes the site shows elsewhere
    (it lists a dozen per page) come from the other ingredients' pages."""
    section = USED_RE.search(page)
    recipes = []
    for card in CARD_RE.findall(section.group(1)) if section else []:
        product, meta, own = PRODUCT_RE.search(card), META_RE.search(card), SELF_RE.search(card)
        meta = META_TEXT_RE.search(html.unescape(re.sub(r"<[^>]+>|<!-- -->", "", meta.group(1)))) if meta else None
        if not (product and meta and own):
            continue
        ingredients = [{"id": item_id, "name": name, "qty": int(own.group(1))}]
        ingredients += [{"id": int(i), "name": html.unescape(n), "qty": int(q)} for n, q, i in INGREDIENT_RE.findall(card)]
        recipes.append({"id": int(product.group(1)), "name": clean_name(html.unescape(product.group(2))),
                        "discipline": meta.group(1).strip(), "craft_level": int(meta.group(2)),
                        "fee": int(meta.group(3).replace(",", "")), "ingredients": ingredients})
    return recipes


def parse_item(page, item_id=None):
    title = html.unescape(TITLE_RE.search(page).group(1))
    name = clean_name(re.split(r" \(item \d+\)| - MapleStory", title)[0].strip())
    m = DESC_PAYS_RE.search(page)
    if not m:
        m = BODY_PAYS_RE.search(page)
    pays = None
    if m and m.group(1) != "-":
        pays = int(m.group(1).replace(",", ""))
    lv = LEVEL_RE.search(page)
    slots = SLOTS_RE.search(page)
    return {"name": name, "npc_pays": pays, "level": int(lv.group(1)) if lv else None,
            "slots": int(slots.group(1)) if slots else None, "recipes": parse_recipes(page),
            "used_in": parse_used_in(page, item_id, name)}


def merge_recipes(cache):
    """Recipes from products' own pages, plus the ones only the materials' pages show. The
    same recipe seen on several pages counts once; a product page's version wins (it also
    says how many one craft makes)."""
    def key(r):
        return (r["id"], r["discipline"], r["craft_level"],
                frozenset((i["id"], i["qty"]) for i in r["ingredients"]))

    found = {}
    for i, v in cache.items():
        for r in v.get("recipes") or []:
            r = {"id": int(i), "name": clean_name(v["name"]), **r}
            found[key(r)] = r
    for v in cache.values():
        for r in v.get("used_in") or []:
            found.setdefault(key(r), {**r, "makes": 1})
    return list(found.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-equip-level", type=int, default=999,
                    help="keep equipment up to this required level (0 = skip equipment)")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--delay", type=float, default=0.75, help="seconds between requests per worker")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    cache = json.loads(CACHE_FILE.read_text("utf-8")) if CACHE_FILE.exists() else {}

    def is_equip(item_id):
        return int(item_id) < 2000000

    urls = item_urls()
    # equipment needs fetching before its level is known, so every equip page is read once;
    # items cached before recipes (or upgrade slots) were read get fetched again once, and
    # materials (not equipment) before the recipes they go into were read
    todo = {i: u for i, u in urls.items()
            if (str(i) not in cache or "recipes" not in cache[str(i)]
                or (not is_equip(i) and "used_in" not in cache[str(i)]))
            and (args.max_equip_level > 0 or not is_equip(i))}
    print(f"{len(urls)} items in sitemap, {len(cache)} cached, {len(todo)} to fetch")

    def work(item):
        item_id, url = item
        item = parse_item(get(url), item_id)
        time.sleep(args.delay)
        return item_id, item

    done = 0
    with ThreadPoolExecutor(args.workers) as pool:
        for fut in as_completed(pool.submit(work, it) for it in todo.items()):
            try:
                item_id, item = fut.result()
            except Exception as e:
                print(f"  failed: {e}")
                continue
            if not is_equip(item_id):
                del item["slots"]
            cache[str(item_id)] = item
            done += 1
            if done % 25 == 0:
                CACHE_FILE.write_text(json.dumps(cache), "utf-8")
                print(f"  {done}/{len(todo)}")
    CACHE_FILE.write_text(json.dumps(cache), "utf-8")

    items = [{"id": int(i), **{k: x for k, x in v.items() if k not in ("recipes", "used_in")},
              "name": clean_name(v["name"])}
             for i, v in cache.items() if not is_equip(i) or (v.get("level") or 0) <= args.max_equip_level]
    items.sort(key=lambda x: x["name"].lower())
    OUT_FILE.write_text(json.dumps({
        "source": BASE,
        "game": "MapleStory Classic World",
        "fetched": date.today().isoformat(),
        "items": items,
    }, indent=1), "utf-8")
    print(f"wrote {len(items)} items ({sum(1 for i in items if i['npc_pays'])} with an NPC price) to {OUT_FILE}")
    recipes = merge_recipes(cache)
    recipes.sort(key=lambda r: (r["discipline"], r["craft_level"], r["name"].lower()))
    RECIPES_FILE.write_text(json.dumps({"source": f"{BASE} (item pages)", "game": "MapleStory Classic World",
                                        "fetched": date.today().isoformat(), "recipes": recipes}, indent=1), "utf-8")
    counts = Counter(r["discipline"] for r in recipes)
    print(f"wrote {len(recipes)} recipes ({', '.join(f'{k} {n}' for k, n in sorted(counts.items()))}) to {RECIPES_FILE}")


if __name__ == "__main__":
    main()
