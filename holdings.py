"""What the bot bought to sell in your own store (market deals in the full sell loop), kept in
data/holdings.json: how many, what they cost in all, and when the first and last were bought.
These are never sold at the NPC (walker.Walker.sell), and store.py lists them.

An item is "full", taking one of the store's 10 slots, once selling what's held would bring at
least min_slot_profit, or once it has been held full_after_hours: a few cheap ores aren't worth a
slot yet, but shouldn't wait for ever either.

No more than one stack of an item is held (stack_size): it's one store slot.
"""
import json
import time
from pathlib import Path

FILE = Path(__file__).parent / "data" / "holdings.json"
STACKS_FILE = Path(__file__).parent / "data" / "stack_sizes.json"  # MeowDB's slotMax, where it isn't the default
DEFAULT_STACK = 100  # the game's, for an item that doesn't say
_stacks = None


def stack_size(name, item_id=None, path=None):
    """How many of an item fit in one inventory slot, and so in one store slot. Throwing stars
    (2070xxx) are counted by the set, as the bot reads them: one."""
    global _stacks
    if str(item_id or "").startswith("207"):
        return 1
    if path is not None or _stacks is None:
        try:
            found = json.loads(Path(path or STACKS_FILE).read_text("utf-8"))
        except (OSError, ValueError):
            found = {}
        if path is not None:
            return found.get(name, DEFAULT_STACK)
        _stacks = found
    return _stacks.get(name, DEFAULT_STACK)


def save_stack_sizes(items, path=None):
    """From MeowDB's item list (custom_items.fetch_equipment gets it): the slotMax that differ from
    the default. Returns how many."""
    found = {}
    for item in items:
        stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        if item.get("type") in ("Use", "Etc", "Setup", "Cash") and stats.get("slotMax") not in (None, DEFAULT_STACK):
            found[item["name"]] = stats["slotMax"]
    Path(path or STACKS_FILE).write_text(json.dumps(dict(sorted(found.items())), indent=0), "utf-8")
    return len(found)


def load(path=None):
    try:
        return json.loads(Path(path or FILE).read_text("utf-8")).get("items", {})
    except (OSError, ValueError):
        return {}


def save(items, path=None):
    path = Path(path or FILE)
    path.parent.mkdir(exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps({"items": items}, indent=1), "utf-8")
    part.replace(path)


def record(name, quantity, paid, path=None, now=None):
    """Adds a purchase: quantity bought for paid mesos in all."""
    items = load(path)
    now = now or time.time()
    held = items.setdefault(name, {"quantity": 0, "paid": 0, "first": now})
    held["quantity"] += quantity
    held["paid"] += paid
    held["last"] = now
    save(items, path)
    return held


def remove(name, path=None):
    items = load(path)
    if items.pop(name, None) is not None:
        save(items, path)


def set_quantity(name, quantity, path=None):
    """Some sold: quantity left, paid lowered to match at what each one cost. None left: removed."""
    items = load(path)
    held = items.get(name)
    if held is None:
        return
    if quantity <= 0:
        items.pop(name)
    else:
        held["paid"] = round(held["paid"] * quantity / held["quantity"]) if held["quantity"] else 0
        held["quantity"] = quantity
    save(items, path)


def paid_each(held):
    return held["paid"] / held["quantity"] if held.get("quantity") else None


def is_full(held, reference, min_slot_profit, full_after_hours, shop_fee=0.03, now=None):
    """Whether a held item takes up a store slot yet (see the top of this file). "full": true on an
    item says it does, whatever it would bring: one you've said is ready."""
    if held.get("full"):
        return True
    if (now or time.time()) - held["first"] >= full_after_hours * 3600:
        return True
    return bool(reference) and held["quantity"] * reference * (1 - shop_fee) - held["paid"] >= min_slot_profit
