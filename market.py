"""Free Market prices from MeowDB (https://meowdb.com/msclassic), for spotting player-shop
listings far below what an item usually sells for.

MeowDB collects player price reports for MapleStory Classic. Its API isn't documented; this
uses the two calls the MaplePrice overlay (github.com/cpan1995/maplepricechecker) uses:
search?q=<name> to find an item's id, and item-market-summary?itemId=<id>, which gives the
last 24 hours' low / median / number of reports, among other things.

Prices are looked up live: the first time a listing for an item shows up, a background thread
asks MeowDB for its prices (one request a second) and saves them in data/market_prices.json.
At startup you're asked whether to update prices already in that file (suggested once every
24 hours, not more). Say no, and items already in it keep their saved prices; only items
never looked up before are fetched.
"""
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://meowdb.com/msclassic/api"
UA = "maple-price-checker (personal use; each item once a day at most)"
DUMP_FILE = Path(__file__).parent / "data" / "market_prices.json"
SETTINGS_FILE = Path(__file__).parent / "crawler_settings.json"
REFRESH_HOURS = 24
REQUEST_GAP = 1.0  # seconds between requests to MeowDB


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


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def lookup(name, game_id=None, meow_id=None):
    """MeowDB's prices for one item: an entry for market_prices.json. Items MeowDB doesn't know
    or that can't be traded get an entry too (without prices), so they aren't asked about again."""
    entry = {"fetched": now_iso()}
    if meow_id is None:
        found = _get("search", q=name).get("items", [])
        time.sleep(REQUEST_GAP)
        # the icon key is the game's item id, which tells same-named items apart
        same = [i for i in found if game_id is not None and str(i.get("iconKey")) == str(game_id)]
        same = same or [i for i in found if i["name"].lower() == name.lower()]
        if not same:
            return {**entry, "status": "not found"}
        meow_id = same[0]["id"]
    try:
        d = _get("item-market-summary", itemId=meow_id, condition="clean")
    except urllib.error.HTTPError as e:
        if e.code == 400:  # "can't be traded on the Free Market"
            return {**entry, "id": meow_id, "status": "not tradeable"}
        raise
    finally:
        time.sleep(REQUEST_GAP)
    day = d.get("last24h") or {}
    return {**entry, "id": meow_id,
            "low_24h": day.get("low"),
            "median_24h": day.get("median"),
            "reports_24h": day.get("count") or 0,
            "median_7d": (d.get("last7d") or {}).get("median"),
            "usual_14d": d.get("usual")}


def load():
    """The saved prices, or None."""
    try:
        return json.loads(DUMP_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def age_hours(stamp):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() / 3600


class Live:
    """Free Market prices by item name, looked up on MeowDB in the background as items are seen.

    get() never waits: an item not looked up yet has no price at first and gets one a few
    seconds later. Items already saved are only looked up again when update is True (you said
    yes at startup), and then once per session."""

    def __init__(self, dump=None, update=False, game_ids=None, enabled=True):
        self.enabled = enabled
        self.dump = dump or {"source": "https://meowdb.com/msclassic", "updated": None, "items": {}}
        self.items = self.dump["items"]
        self.update = update
        self.game_ids = game_ids or {}  # name -> game item id, to pick the right search result
        self.asked = set()  # names queued this session: each is looked up at most once
        self.waiting = set()  # names queued and not looked up yet
        self.lock = threading.Lock()
        self.todo = queue.Queue()
        self.thread = None
        if update:
            self.dump["updated"] = now_iso()

    def __bool__(self):
        return self.enabled

    def get(self, name):
        """The saved prices for name (None if none yet), queueing a lookup if one is due."""
        if not self.enabled:
            return None
        with self.lock:
            entry = self.items.get(name)
            due = name not in self.asked and (entry is None or self.update)
            if due:
                self.asked.add(name)
                self.waiting.add(name)
        if due:
            self.todo.put(name)
            if self.thread is None:
                self.thread = threading.Thread(target=self._work, daemon=True)
                self.thread.start()
        return entry

    def pending(self, names):
        """Whether any of names is still waiting for its lookup."""
        with self.lock:
            return any(n in self.waiting for n in names)

    def lowest(self, names):
        """For a listing that could be any of names (a name the game cut off), the prices of
        the one with the lowest 24h median, so a listing only counts as a deal when it's one
        whichever item it is. None until every one of them has a price."""
        entries = [self.get(n) for n in names]
        if not entries or any(not e or not e.get("median_24h") for e in entries):
            return None
        return min(entries, key=lambda e: e["median_24h"])

    def _work(self):
        failures = 0
        while True:
            name = self.todo.get()
            with self.lock:
                known = self.items.get(name, {}).get("id")
            try:
                entry = lookup(name, self.game_ids.get(name), known)
                failures = 0
            except (OSError, ValueError) as e:
                with self.lock:
                    self.waiting.discard(name)
                failures += 1
                print(f"  MeowDB lookup for {name} failed ({e}); "
                      f"{'pausing lookups for 5 minutes' if failures >= 3 else 'skipped for now'}")
                time.sleep(300 if failures >= 3 else 10)
                continue
            if entry.get("status"):
                print(f"  market price for {name}: {entry['status']} on MeowDB")
            else:
                print(f"  market price for {name}: 24h median {entry['median_24h']} "
                      f"({entry['reports_24h']} reports), low {entry['low_24h']}")
            # the price checker and the crafting calculator can run at the same time and both
            # save here: take in what the other one saved since, so neither erases the other's
            saved = load() or {}
            with self.lock:
                self.items[name] = entry
                self.waiting.discard(name)
                for other, theirs in saved.get("items", {}).items():
                    mine = self.items.get(other)
                    if mine is None or theirs.get("fetched", "") > mine.get("fetched", ""):
                        self.items[other] = theirs
                if (saved.get("updated") or "") > (self.dump.get("updated") or ""):
                    self.dump["updated"] = saved["updated"]
                text = json.dumps(self.dump, indent=1)
            DUMP_FILE.parent.mkdir(exist_ok=True)
            tmp = DUMP_FILE.with_name(f"{DUMP_FILE.stem}.{os.getpid()}.tmp")
            tmp.write_text(text, "utf-8")
            tmp.replace(DUMP_FILE)


def prepare(game_ids=None):
    """Ask whether to use (and update) Free Market prices; returns a Live, which is empty and
    disabled if you said no."""
    dump = load()
    if dump is not None and "updated" not in dump:  # a dump from before live lookups
        dump["updated"] = dump.pop("fetched", None)
        for entry in dump["items"].values():
            entry.setdefault("fetched", dump["updated"])
    if dump is None or not dump["items"]:
        answer = input("Look up Free Market prices on MeowDB for the items you come across "
                       "(one request a second, each item once)? [Y/n] ").strip().lower()
        if answer not in ("", "y", "yes"):
            print("  no market prices: only NPC deals will be checked")
            return Live(enabled=False)
        print("  market prices are looked up as items show up in stores")
        return Live(dump, update=True, game_ids=game_ids)
    count = len(dump["items"])
    hours = age_hours(dump["updated"]) if dump.get("updated") else None
    when = f"{hours:.1f} hours ago" if hours is not None else "a while ago"
    if hours is not None and hours < REFRESH_HOURS:
        prompt = (f"Market prices for {count} items were last updated {when}. Update them again as items "
                  f"show up? (once every {REFRESH_HOURS} hours is plenty) [y/N] ")
        update = input(prompt).strip().lower() in ("y", "yes")
    else:
        prompt = f"Market prices for {count} items were last updated {when}. Update them as items show up? [Y/n] "
        update = input(prompt).strip().lower() in ("", "y", "yes")
    print("  saved prices are updated once each as items show up; new items are looked up" if update
          else "  saved prices are kept as they are; only items never looked up before are fetched")
    return Live(dump, update=update, game_ids=game_ids)
