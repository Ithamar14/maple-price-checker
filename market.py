"""Free Market prices from MeowDB (https://meowdb.com/msclassic), for spotting player-shop
listings far below what an item usually sells for.

MeowDB collects player price reports for MapleStory Classic. Its API isn't documented; this
uses the two calls the MaplePrice overlay (github.com/cpan1995/maplepricechecker) uses:
search?q=<name> to find an item's id, and item-market-summary?itemId=<id>, which gives the
last 24 hours' low / median / number of reports, among other things.

To go easy on the site, prices are downloaded once into data/market_prices.json, one request
per second, and reused: at startup the crawler says how old that dump is and asks whether to
download a new one (suggested once every 24 hours, not more).
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://meowdb.com/msclassic/api"
UA = "maple-price-checker (personal use; one download a day)"
DUMP_FILE = Path(__file__).parent / "data" / "market_prices.json"
SETTINGS_FILE = Path(__file__).parent / "crawler_settings.json"
REFRESH_HOURS = 24


def load_settings():
    """The "market" part of crawler_settings.json (shared by the crawler and the price checker)."""
    try:
        return json.loads(SETTINGS_FILE.read_text("utf-8")).get("market", {})
    except (OSError, ValueError):
        return {}


def deal_limit(prices, percent, min_reports):
    """The highest asking price that's a market deal: percent under the median of the last 24
    hours. None when there's no trustworthy price (no median, or fewer than min_reports reports
    behind it), and then nothing is ever a market deal."""
    if not prices or not prices.get("median_24h") or (prices.get("reports_24h") or 0) < min_reports:
        return None
    return int(prices["median_24h"] * (100 - percent) / 100)


def parse_percent(text):
    """'30', '30%', '12.5' -> 30.0 / 12.5; between 0 and 100."""
    value = float(text.strip().rstrip("%"))
    if not 0 < value < 100:
        raise ValueError(text)
    return value


def ask_percent(default):
    while True:
        text = input(f"Market deals: how far under the 24h median must a listing be? [{default:g}%] ")
        if not text.strip():
            return default
        try:
            return parse_percent(text)
        except ValueError:
            print("  please type a number between 0 and 100, e.g. 30")


def _get(path, **params):
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def download(names, known_ids=None):
    """{name: prices} for every name MeowDB knows. known_ids (from the last dump) saves
    looking the same items up again."""
    known_ids = known_ids or {}
    items = {}
    for n, name in enumerate(names, 1):
        try:
            item_id = known_ids.get(name)
            if item_id is None:
                found = [i for i in _get("search", q=name).get("items", []) if i["name"].lower() == name.lower()]
                time.sleep(1)
                if not found:
                    print(f"  {n}/{len(names)} {name}: not found on MeowDB")
                    continue
                item_id = found[0]["id"]
            d = _get("item-market-summary", itemId=item_id, condition="clean")
            time.sleep(1)
        except urllib.error.HTTPError as e:
            if e.code == 400:  # "can't be traded on the Free Market"
                print(f"  {n}/{len(names)} {name}: not tradeable on the Free Market")
                continue
            raise
        day = d.get("last24h") or {}
        items[name] = {
            "id": item_id,
            "low_24h": day.get("low"),
            "median_24h": day.get("median"),
            "reports_24h": day.get("count") or 0,
            "median_7d": (d.get("last7d") or {}).get("median"),
            "usual_14d": d.get("usual"),
        }
        e = items[name]
        print(f"  {n}/{len(names)} {name}: 24h low {e['low_24h']}, median {e['median_24h']} "
              f"({e['reports_24h']} reports)")
    return items


def load():
    """The last dump, or None."""
    try:
        return json.loads(DUMP_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def age_hours(dump):
    fetched = datetime.fromisoformat(dump["fetched"])
    return (datetime.now(timezone.utc) - fetched).total_seconds() / 3600


def prepare(names):
    """Load the dump, after asking whether to download a new one. {name: prices}, or {} if
    there's no market data to use."""
    dump = load()
    if dump is None:
        answer = input(f"No Free Market prices downloaded yet. Download them for {len(names)} items "
                       f"from MeowDB now (about {2 * len(names)} seconds)? [Y/n] ").strip().lower()
        if answer not in ("", "y", "yes"):
            print("  no market prices: only NPC deals will be checked")
            return {}
    else:
        hours = age_hours(dump)
        missing = [n for n in names if n not in dump["items"]]
        note = f"; {len(missing)} items in the settings aren't in it" if missing else ""
        if hours < REFRESH_HOURS:
            prompt = (f"Free Market prices were downloaded {hours:.1f} hours ago{note}. Download again anyway? "
                      f"(once every {REFRESH_HOURS} hours is plenty) [y/N] ")
            fresh = input(prompt).strip().lower() in ("y", "yes")
        else:
            prompt = f"Free Market prices were downloaded {hours:.0f} hours ago{note}. Download new ones? [Y/n] "
            fresh = input(prompt).strip().lower() in ("", "y", "yes")
        if not fresh and missing:
            # items added to the settings since the last download: fetch just those
            answer = input(f"  Download prices for just the {len(missing)} new items ({', '.join(missing)})? [Y/n] ")
            if answer.strip().lower() in ("", "y", "yes"):
                print(f"Downloading Free Market prices from MeowDB ({len(missing)} items, one request a second)...")
                try:
                    dump["items"].update(download(missing))
                    DUMP_FILE.write_text(json.dumps(dump, indent=1), "utf-8")
                except (OSError, ValueError) as e:
                    print(f"  download failed ({e}); going on without them")
        if not fresh:
            return {n: dump["items"][n] for n in names if n in dump["items"]}
    print(f"Downloading Free Market prices from MeowDB ({len(names)} items, one request a second)...")
    known = {n: v["id"] for n, v in (dump or {}).get("items", {}).items()}
    try:
        items = download(names, known)
    except (OSError, ValueError) as e:
        print(f"  download failed ({e}); {'using the old prices' if dump else 'no market prices'}")
        return {n: dump["items"][n] for n in names if n in dump["items"]} if dump else {}
    DUMP_FILE.parent.mkdir(exist_ok=True)
    DUMP_FILE.write_text(json.dumps({"source": "https://meowdb.com/msclassic",
                                     "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                     "items": items}, indent=1), "utf-8")
    print(f"  saved {len(items)} items to {DUMP_FILE.name}")
    return items
