"""MeowDB's numbers for the market report, pulled by the server once a day so no day is missed, whether a
PC is on or not. MeowDB only shows the last 24 hours and 7 days at the moment you ask, so a missed day
can't be pulled later.

Each day's snapshot is what .claude/skills/market-report/pull_meowdb.py saves on a PC: {"items": every
MeowDB item, "summary": {item id: its market summary}, "fetched": {item id: when}}, for the items the
report uses. It's kept gzipped in the server's database (snapshots table), and the PCs download the ones
they don't have when they sync (sync.sync_snapshots), into data/meowdb_snapshots/<day>.json.

About one request a second, each item once a day, from PULL_HOUR (UTC). A pull that's cut off (a restart,
MeowDB down) goes on from where it stopped at the next check.
"""
import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://meowdb.com/msclassic/api"
UA = "maple-price-checker (personal use; each item once a day at most)"  # as market.py
GAP = 1.1           # seconds between requests
CHECK_EVERY = 600   # seconds between looks at whether today's is done
PULL_HOUR = int(os.environ.get("PULL_HOUR", 6))  # UTC: 08:00 in Israel in winter, 09:00 in summer; the same date there
EQUIP_LEVELS = {35}
SAVE_EVERY = 50     # items between saves, so a restart loses little
MAX_FAILURES = 5    # in a row: MeowDB is down, try again at the next check


def get(path, **params):
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
        return json.load(r)


def pack(state):
    return gzip.compress(json.dumps(state).encode("utf-8"))


def unpack(blob):
    return json.loads(gzip.decompress(blob).decode("utf-8"))


def list_items(fetch=get, sleep=time.sleep):
    items, number, seen = [], 1, set()
    while True:
        page = fetch("items", limit=500, page=number)
        sleep(GAP)
        new = [i for i in page["items"] if i["id"] not in seen]
        seen.update(i["id"] for i in new)
        items += new
        if not new or not page.get("hasMore"):
            break
        number += 1
    for i in items:
        if isinstance(i.get("stats"), str):
            try:
                i["stats"] = json.loads(i["stats"])
            except ValueError:
                pass
    return items


def craft_inputs(recipes):
    """What Arcforge recipes use, and every catalyst, so that craft costs can be worked out."""
    recipes = recipes.get("recipes", []) if recipes else []
    names = {g["name"] for r in recipes if r.get("discipline") == "Arcforge" for g in r.get("ingredients", [])}
    return names | {r["catalyst"]["name"] for r in recipes if r.get("catalyst")}


def group(item, levels, inputs):
    """The report's group for an item, or None when it isn't pulled (as pull_meowdb.group)."""
    kind, sub, name = item.get("type"), item.get("subtype") or "", item["name"]
    if kind == "Equip":
        return "equipment" if item.get("levelReq") in levels else None
    if kind == "Use":
        if sub == "Scroll":
            return "scroll"
        if sub == "Throwing Star":
            return "star"
        if "Elixir" in name:
            return "elixir"
    if kind == "Etc":
        if sub in ("Ore & Mineral", "Gem") or name.endswith(" Ore"):
            return "ore"
        if sub == "Crafting Material":
            return "material"
    return "input" if name in inputs else None


def today():
    return datetime.now(timezone.utc).date().isoformat()


class Puller:
    """The daily pull, on its own thread. store is server.Store."""

    def __init__(self, store, fetch=get, sleep=time.sleep, log=print):
        self.store, self.fetch, self.sleep, self.log = store, fetch, sleep, log
        self.stop = threading.Event()

    def recipes(self):
        rows = self.store.run("SELECT body FROM files WHERE name = 'recipes_meowdb.json'")
        try:
            return json.loads(rows[0][0]) if rows else None
        except ValueError:
            return None

    def load(self, day):
        rows = self.store.run("SELECT body, done FROM snapshots WHERE day = ?", (day,))
        return (unpack(rows[0][0]), bool(rows[0][1])) if rows else (None, False)

    def save(self, day, state, done):
        self.store.run("INSERT INTO snapshots (day, body, done, updated) VALUES (?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET "
                       "body = excluded.body, done = excluded.done, updated = excluded.updated", (day, pack(state), int(done), time.time()))

    def pull(self, day):
        """Today's, or what's left of it. True once it's whole."""
        state, done = self.load(day)
        if done:
            return True
        state = state or {"items": None, "summary": {}, "fetched": {}}
        if state["items"] is None:
            state["items"] = list_items(self.fetch, self.sleep)
            self.save(day, state, False)
        inputs = craft_inputs(self.recipes())
        todo = [i for i in state["items"] if group(i, EQUIP_LEVELS, inputs) and str(i["id"]) not in state["summary"]]
        self.log(f"snapshot {day}: {len(todo)} items to look up")
        failures = 0
        for n, item in enumerate(todo, 1):
            if self.stop.is_set():
                break
            key = str(item["id"])
            try:
                state["summary"][key] = self.fetch("item-market-summary", itemId=item["id"], condition="clean")
                state["fetched"][key] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                failures = 0
            except urllib.error.HTTPError as e:
                state["summary"][key] = {"status": e.code}
            except (OSError, ValueError) as e:
                failures += 1
                self.log(f"snapshot {day}: {item['name']} failed: {e}")
                if failures >= MAX_FAILURES:
                    self.save(day, state, False)
                    self.log(f"snapshot {day}: {failures} failures in a row, trying again later")
                    return False
            if n % SAVE_EVERY == 0:
                self.save(day, state, False)
            self.sleep(GAP)
        whole = not self.stop.is_set() and all(str(i["id"]) in state["summary"] for i in todo)
        self.save(day, state, whole)
        if whole:
            self.log(f"snapshot {day}: done, {len(state['summary'])} items")
        return whole

    def due(self, now=None):
        """Today's day, if it's time for it and it isn't done yet; else None."""
        now = now or datetime.now(timezone.utc)
        day = now.date().isoformat()
        return day if now.hour >= PULL_HOUR and not self.load(day)[1] else None

    def run(self):
        while not self.stop.is_set():
            try:
                day = self.due()
                if day:
                    self.pull(day)
            except Exception as e:  # never takes the server down with it
                self.log(f"snapshot: {e!r}")
            self.stop.wait(CHECK_EVERY)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()
        return self
