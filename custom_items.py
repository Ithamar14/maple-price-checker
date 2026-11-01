"""Your custom list: the items to look out for, each with its own rules (custom_items.json).

    name     as the game spells it
    when     "under": at least percent under the 24h median
             "over":  anything up to percent over the 24h median
             "price": at or below price, whatever the market says
    limit    how many to buy in all (None: no limit); bought counts them, across sessions
    confirm  True: wait for you to press OK; False: buy without asking

Whatever is on the list is never sold at the NPC. Edit it in the launcher's Custom list tab.
A list from before this file (custom_search.txt) is read as a starting point, until the first save.
"""
import json
import re
import time
from pathlib import Path

import market

HERE = Path(__file__).parent
FILE = HERE / "custom_items.json"
OLD_FILE = HERE / "custom_search.txt"
SAVE_TRIES = 20
MAX_OVER = 1000  # percent over the median that "over" can be set to


def key(name):
    """How names are told apart (price_checker.norm): only letters, digits and % count."""
    return re.sub(r"[^a-z0-9%]+", "", name.lower())


def new_item(name):
    """What an item starts as: nothing is spent on it without asking, and only one."""
    return {"name": name, "when": "under", "percent": 20.0, "price": 0, "limit": 1, "bought": 0, "confirm": True}


def load():
    """The list, as a list of items."""
    try:
        return [{**new_item(i["name"]), **i} for i in json.loads(FILE.read_text("utf-8"))["items"]]
    except OSError:
        pass
    except (ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"{FILE.name} can't be read ({e}): fix or delete it") from None
    try:
        lines = [line.strip() for line in OLD_FILE.read_text("utf-8").splitlines()]
    except OSError:
        return []
    return [new_item(n) for n in lines if n and not n.startswith("#")]


def save(items):
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"items": items}, indent=1), "utf-8")
    for _ in range(SAVE_TRIES):
        try:
            tmp.replace(FILE)
            return
        except PermissionError:
            time.sleep(0.1)
    raise OSError(f"couldn't save {FILE.name}: something has it open")


def find(items, name):
    return next((i for i in items if key(i["name"]) == key(name)), None)


def add(name):
    """False if it's already on the list."""
    items = load()
    if find(items, name):
        return False
    save(items + [new_item(name)])
    return True


def remove(name):
    save([i for i in load() if key(i["name"]) != key(name)])


def update(name, **changes):
    """Change an item on disk, leaving what else is there (a bot may be counting) as it is."""
    items = load()
    item = find(items, name)
    if item:
        item.update(changes)
        save(items)


def parse_rules(when, percent, price, limit):
    """What's typed in the launcher, as an item's fields; ValueError says what's wrong."""
    changes = {"when": when}
    if when == "price":
        try:
            changes["price"] = market.parse_mesos(price)
        except ValueError:
            changes["price"] = 0
        if changes["price"] <= 0:
            raise ValueError("Type a price, like 5000, 250k or 1.5m.")
    else:
        try:
            value = float(percent)
        except ValueError:
            value = -1
        if when == "under" and not 0 < value < 100:
            raise ValueError("Under the median: the % has to be between 0 and 100.")
        if when == "over" and not 0 <= value <= MAX_OVER:
            raise ValueError(f"Over the median: the % has to be between 0 and {MAX_OVER}.")
        changes["percent"] = value
    limit = limit.strip()
    if not limit:
        changes["limit"] = None
    else:
        try:
            changes["limit"] = int(limit)
        except ValueError:
            changes["limit"] = 0
        if changes["limit"] <= 0:
            raise ValueError("The limit has to be a whole number over 0, or empty for no limit.")
    return changes


def describe(item):
    if item["when"] == "price":
        return f"at or under {item['price']:,}"
    if item["when"] == "over":
        return f"up to {item['percent']:g}% over median"
    return f"{item['percent']:g}%+ under median"


def left(item):
    """How many more may be bought; None: no limit."""
    return None if item["limit"] is None else max(0, item["limit"] - item["bought"])


class Book:
    """The list for one bot session. Counting goes straight to the file."""

    def __init__(self, items=None):
        self.items = {key(i["name"]): i for i in (load() if items is None else items)}

    def get(self, name):
        return self.items.get(key(name))

    def needs_market(self):
        return any(i["when"] != "price" and left(i) != 0 for i in self.items.values())

    def record(self, name, quantity):
        """Count a purchase; what's read back from disk first keeps edits made in the launcher."""
        item = self.get(name)
        if not item:
            return
        item["bought"] += quantity
        items = load()
        saved = find(items, name)
        if saved:
            saved["bought"] = item["bought"]
            save(items)
