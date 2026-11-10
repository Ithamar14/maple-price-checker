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
never looked up before are fetched. --market-update yes/no answers that without asking.

    python market.py --update-stale    # looks up every saved price over 24 hours old, then exits
"""
import argparse
import json
import os
import queue
import re
import sys
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
# MeowDB blocked this tool on 2026-11-10 for sending requests faster than one every 10 seconds, against its tool
# rules (https://meowdb.com/reuse#tool-rules), and took back any earlier permission. Nothing is asked while this is
# False: saved prices are still used. Set it True only once MeowDB has given permission again (contact@meowdb.com).
ALLOWED = False
BLOCKED_NOTE = "MeowDB blocked this tool on 2026-11-10: nothing is asked until it gives permission again (market.ALLOWED)"
REQUEST_GAP = 11.0  # seconds between requests to MeowDB: its rules allow one every 10 seconds at most


class Blocked(OSError):
    """MeowDB isn't asked (ALLOWED is False). An OSError, so it's handled like MeowDB being unreachable."""
SAVE_TRIES = 20    # Windows refuses to replace a file while something is reading it: try again
update_saved = None  # "yes" / "no" from --market-update: prepare() doesn't ask, None: it does


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


def parse_mesos(text):
    """'250000', '250,000', '250k', '1.5m' -> int."""
    m = re.fullmatch(r"\s*([\d.,]+)\s*([km]?)\s*", text.lower())
    if not m:
        raise ValueError(text)
    number = float(m.group(1).replace(",", ""))
    return int(number * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)])


def parse_numbers(text, low, high):
    """'40-1', '1,3,20,39', '6-1,16-13', '5' -> a list, in the order given, each number once.
    ValueError if it isn't that, or a number isn't from low to high."""
    numbers = []
    for part in text.replace(" ", "").split(","):
        first, dash, last = part.partition("-")
        if not first.isdigit() or (dash and not last.isdigit()):
            raise ValueError(text)
        a, b = int(first), int(last or first)
        if not (low <= a <= high and low <= b <= high):
            raise ValueError(text)
        for n in (range(a, b - 1, -1) if a >= b else range(a, b + 1)):
            if n not in numbers:
                numbers.append(n)
    return numbers


def add_update_flag(ap):
    ap.add_argument("--market-update", choices=["yes", "no"],
                    help="update saved Free Market prices as items show up (asked if not given)")


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
    if not ALLOWED:
        raise Blocked(BLOCKED_NOTE)
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


def priced(dump):
    """The saved items that have prices to keep up to date: an item MeowDB doesn't know or that
    can't be traded has none, and looking those up every day wouldn't change that."""
    return {n: e for n, e in ((dump or {}).get("items") or {}).items() if not e.get("status")}


def is_stale(entry):
    return not entry.get("fetched") or age_hours(entry["fetched"]) >= REFRESH_HOURS


def stale_names(dump):
    """The saved prices due for an update: over REFRESH_HOURS old."""
    return [n for n, e in priced(dump).items() if is_stale(e)]


def last_fetched(dump):
    """How many hours ago the newest saved price was looked up, or None."""
    stamps = [e["fetched"] for e in priced(dump).values() if e.get("fetched")]
    return age_hours(max(stamps, key=datetime.fromisoformat)) if stamps else None


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
            self._queue(name)
        return entry

    def _queue(self, name):
        if not ALLOWED:  # never looked up: the saved price, if any, is all there is
            with self.lock:
                self.waiting.discard(name)
            return
        self.todo.put(name)
        if self.thread is None:
            self.thread = threading.Thread(target=self._work, daemon=True)
            self.thread.start()

    def refresh(self, names):
        """Look up again whichever of names has a price over REFRESH_HOURS old, whatever was
        said about updating: this is for prices about to be spent on."""
        if not self.enabled:
            return
        with self.lock:
            due = [n for n in names if n not in self.waiting and n in self.items
                   and not self.items[n].get("status") and is_stale(self.items[n])]
            self.asked.update(due)
            self.waiting.update(due)
        for name in due:
            self._queue(name)

    def update_now(self, names):
        """Look up every one of names, however recently it last was."""
        if not self.enabled:
            return
        with self.lock:
            due = [n for n in names if n not in self.waiting]
            self.asked.update(due)
            self.waiting.update(due)
        for name in due:
            self._queue(name)

    def fresh(self, names):
        """Whether every one of names has a price under REFRESH_HOURS old."""
        with self.lock:
            entries = [self.items.get(n) for n in names]
        return bool(entries) and all(e and not is_stale(e) for e in entries)

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
            for attempt in range(SAVE_TRIES):
                try:
                    tmp.replace(DUMP_FILE)
                    break
                except PermissionError:
                    time.sleep(0.1)
            else:
                print("  couldn't save the market prices just now; the next lookup tries again")
            with self.lock:  # only now: whoever waits for this may exit straight after
                self.waiting.discard(name)


def prepare(game_ids=None):
    """Ask whether to use (and update) Free Market prices; returns a Live, which is empty and
    disabled if you said no."""
    dump = load()
    if dump is not None and "updated" not in dump:  # a dump from before live lookups
        dump["updated"] = dump.pop("fetched", None)
        for entry in dump["items"].values():
            entry.setdefault("fetched", dump["updated"])
    if update_saved is not None:
        print("  saved prices are updated once each as items show up; new items are looked up" if update_saved == "yes"
              else "  saved prices are kept as they are; only items never looked up before are fetched")
        return Live(dump, update=update_saved == "yes", game_ids=game_ids)
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


def update_stale():
    """Look up every saved price that's due, printing 'progress <done> <total>' along the way."""
    if not ALLOWED:
        print(BLOCKED_NOTE)
        return
    dump = load()
    names = stale_names(dump)
    if not names:
        print("all saved prices are under 24 hours old: nothing to update")
        return
    print(f"updating {len(names)} prices on MeowDB, one every {REQUEST_GAP:g} seconds")
    live = Live(dump, update=True)
    for name in names:
        live.get(name)
    shown = None
    while True:
        waiting = sum(1 for n in names if live.pending([n]))
        if waiting != shown:
            print(f"progress {len(names) - waiting} {len(names)}")
            shown = waiting
        if not waiting:
            break
        time.sleep(1)
    with live.lock:
        left = len(set(names) & set(stale_names(live.dump)))
    print(f"updated {len(names) - left} prices" + (f"; {left} failed and are still due" if left else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--update-stale", action="store_true", help="look up every saved price over 24 hours old")
    if ap.parse_args().update_stale:
        sys.stdout.reconfigure(line_buffering=True, errors="replace")
        update_stale()
    else:
        ap.print_help()
