"""Your custom list: the items to look out for, each with its own rules (custom_items.json).

    name     as the game spells it
    when     "under": at least percent under the 24h median
             "over":  anything up to percent over the 24h median
             "price": at or below price, whatever the market says
             "value": a potion, by what it restores: at or below hp / hp_per_meso + mp / mp_per_meso
                      (store_deals in crawler_settings.json, the store's consumables ratio, without
                      the store's 3%: it's for using). hp, mp: what one restores
    limit    how many to buy in all (None: no limit); bought counts them, across sessions
    confirm  True: wait for you to press OK; False: buy without asking
    only_one True: buy 1 in all and never more, in every mode, like an upgrade to wear
    store    True: store.py lists it in your own store; False: it's only held (never Only 1 ones)
    request  a buy request from Discord (sync.sync_requests): its number; requested_by, who asked. Its
             limit is always kept, even when the budget is split with no limit
    finder   from the equipment finder: {"slots": at least this many scroll slots left or None,
             "stats": {stat: at least this}}, checked on its tooltip in the store. Always only 1

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
EQUIPMENT_FILE = HERE / "data" / "equipment_stats.json"
SETTINGS_FILE = HERE / "crawler_settings.json"
SAVE_TRIES = 20
# the stats the equipment finder can ask about, by MeowDB's name for them, as tooltips word them
STAT_NAMES = {"wdef": "Weapon Def.", "mdef": "Magic Def.", "watk": "Weapon Attack", "matk": "Magic Attack", "str": "STR", "dex": "DEX", "int": "INT", "luk": "LUK",
              "hp": "Max HP", "mp": "Max MP", "acc": "Accuracy", "avoid": "Evasion", "speed": "Speed", "jump": "Jump", "critRate": "Critical Rate", "critDamage": "Critical Damage"}
MAX_SPREAD = 5  # the most a stat is taken to differ from the item's usual value
MAX_OVER = 1000  # percent over the median that "over" can be set to


def key(name):
    """How names are told apart (price_checker.norm): only letters, digits and % count."""
    return re.sub(r"[^a-z0-9%]+", "", name.lower())


def new_item(name):
    """What an item starts as: nothing is spent on it without asking, and only one."""
    return {"name": name, "when": "under", "percent": 20.0, "price": 0, "hp": 0, "mp": 0, "limit": 1, "bought": 0, "confirm": True,
            "only_one": False, "store": False, "finder": None}


def store_settings(path=None):
    try:
        return json.loads(Path(path or SETTINGS_FILE).read_text("utf-8")).get("store_deals", {})
    except (OSError, ValueError):
        return {}


def ratio(settings=None):
    """(HP a meso, MP a meso): what the "value" rule pays by, the store's consumables ratio."""
    cfg = store_settings() if settings is None else settings
    return cfg.get("hp_per_meso", 2), cfg.get("mp_per_meso", 1)


def known_restore(name, settings=None):
    """[HP, MP] one restores, if it's in the store's consumables list: to fill the boxes in with."""
    cfg = store_settings() if settings is None else settings
    found = next((v for n, v in cfg.get("consumables", {}).items() if key(n) == key(name)), None)
    return list(found) if found else None


def value_price(item, per_meso=None):
    """The most a "value" item is bought for: what it restores, at the ratio."""
    hp_rate, mp_rate = per_meso or ratio()
    return int(item.get("hp", 0) / hp_rate + item.get("mp", 0) / mp_rate)


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


def save_finder(name, finder, confirm, rules):
    """An equipment finder item, always for 1. "added", "updated", or "taken" if the item is already on
    the list as an ordinary item: there's only ever one of an item, and it's left as it was."""
    items = load()
    item = find(items, name)
    if item and not item.get("finder"):
        return "taken"
    result = "updated" if item else "added"
    if not item:
        item = new_item(name)
        items.append(item)
    item.update(rules)
    item.update(finder=finder, confirm=confirm, only_one=True, limit=1)
    save(items)
    return result


def parse_finder(values, slots):
    """What's typed in the equipment finder's boxes (stat: text) as an item's finder; ValueError says
    what's wrong. Empty boxes aren't checked, but something has to be."""
    stats = {}

    def number(text, name):
        try:
            value = int(text)
        except ValueError:
            value = -1
        if value < 0:
            raise ValueError(f"{name}: type a whole number, or leave it empty to not check it.")
        return value

    for key, text in values.items():
        if text.strip():
            stats[key] = number(text.strip(), STAT_NAMES[key])
    least = number(slots.strip(), "Scroll slots") if slots.strip() else None
    if not stats and least is None:
        raise ValueError("Fill in at least one stat or the scroll slots: without them use the Custom list tab.")
    return {"slots": least, "stats": stats}


def stat_range(usual):
    """(lowest, highest) a stat should be found at when new: an estimate, as MeowDB only has the
    usual value. About 10% either way, at least 1 and at most MAX_SPREAD."""
    spread = max(1, min(MAX_SPREAD, (usual + 9) // 10))
    return max(0, usual - spread), usual + spread


def load_equipment():
    """{name: {"subtype", "level", "job", "slots", "stats": {stat: usual value}}}, empty if not fetched yet."""
    try:
        return json.loads(EQUIPMENT_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def equipment_entries(items):
    """MeowDB's item list, as what load_equipment reads."""
    found = {}
    for item in items:
        stats = item.get("stats")
        if item.get("type") != "Equip" or not isinstance(stats, dict) or stats.get("untradeable"):
            continue
        found[item["name"]] = {"subtype": item.get("subtype"), "level": item.get("levelReq") or 0, "job": stats.get("reqJob"), "slots": stats.get("slots") or 0,
                               "stats": {k: stats[k] for k in STAT_NAMES if stats.get(k)}}
    return dict(sorted(found.items()))


def fetch_equipment():
    """From MeowDB, about 11 requests. Returns how many items."""
    items, number = [], 1
    while True:
        page = market._get("items", limit=500, page=number)
        time.sleep(market.REQUEST_GAP)
        for item in page["items"]:
            if isinstance(item.get("stats"), str):
                try:
                    item["stats"] = json.loads(item["stats"])
                except ValueError:
                    pass
        items += page["items"]
        if not page["items"] or not page.get("hasMore"):
            break
        number += 1
    found = equipment_entries(items)
    EQUIPMENT_FILE.parent.mkdir(exist_ok=True)
    import holdings  # the same list has every item's stack size
    holdings.save_stack_sizes(items)
    EQUIPMENT_FILE.write_text(json.dumps(found), "utf-8")
    return len(found)


def parse_rules(when, percent, price, limit, hp="", mp=""):
    """What's typed in the launcher, as an item's fields; ValueError says what's wrong."""
    changes = {"when": when}
    if when == "value":
        for name, text in (("hp", hp), ("mp", mp)):
            try:
                changes[name] = int(str(text).strip() or 0)
            except ValueError:
                changes[name] = -1
            if changes[name] < 0:
                raise ValueError(f"{name.upper()} restored: type a whole number, or leave it empty for none.")
        if not changes["hp"] and not changes["mp"]:
            raise ValueError("By HP/MP value: type how much HP or MP one restores.")
    elif when == "price":
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


def describe_finder(finder):
    parts = [f"{STAT_NAMES[k]} {v}+" for k, v in finder["stats"].items()]
    if finder.get("slots") is not None:
        parts.append(f"{finder['slots']}+ slots")
    return ", ".join(parts)


def describe_price(item):
    if item["when"] == "value":
        restores = " + ".join(f"{item[k]:,} {k.upper()}" for k in ("hp", "mp") if item.get(k))
        return f"by value: {restores}, at or under {value_price(item):,}"
    if item["when"] == "price":
        return f"at or under {item['price']:,}"
    if item["when"] == "over":
        return f"up to {item['percent']:g}% over median"
    return f"{item['percent']:g}%+ under median"


def describe(item):
    text = describe_price(item)
    return f"{text}; {describe_finder(item['finder'])}" if item.get("finder") else text


def left(item, ignore_limits=False):
    """How many more may be bought; None: no limit. ignore_limits doesn't get past only_one, or a buy request's amount."""
    if item.get("request"):
        ignore_limits = False
    if item["only_one"] or item.get("finder"):
        return max(0, 1 - item["bought"])
    return None if ignore_limits or item["limit"] is None else max(0, item["limit"] - item["bought"])


class Book:
    """The list for one bot session. Counting goes straight to the file."""

    def __init__(self, items=None):
        self.items = {key(i["name"]): i for i in (load() if items is None else items)}

    def get(self, name):
        return self.items.get(key(name))

    def needs_market(self, ignore_limits=False):
        return any(i["when"] not in ("price", "value") and left(i, ignore_limits) != 0 for i in self.items.values())

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
