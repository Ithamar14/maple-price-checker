"""What the bot bought to sell in your own store (market deals in the full sell loop), kept in
data/holdings.json: how many, what they cost in all, and when the first and last were bought.
These are never sold at the NPC (walker.Walker.sell), and store.py lists them.

An item is "full", taking one of the store's 10 slots, once selling what's held would bring at
least min_slot_profit, or once it has been held full_after_hours: a few cheap ores aren't worth a
slot yet, but shouldn't wait for ever either.
"""
import json
import time
from pathlib import Path

FILE = Path(__file__).parent / "data" / "holdings.json"


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


def paid_each(held):
    return held["paid"] / held["quantity"] if held.get("quantity") else None


def is_full(held, reference, min_slot_profit, full_after_hours, shop_fee=0.03, now=None):
    """Whether a held item takes up a store slot yet (see the top of this file)."""
    if (now or time.time()) - held["first"] >= full_after_hours * 3600:
        return True
    return bool(reference) and held["quantity"] * reference * (1 - shop_fee) - held["paid"] >= min_slot_profit
