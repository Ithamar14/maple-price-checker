"""One price per item for buying and selling alike: what the crawler pays at most for a market deal
and what your store lists it at both start from reference().

The reference blends two sources, each weighted by how much data it has:
- the bot's own scans of the last SCAN_DAYS days: the midpoint of the median asked and the
  median sold, as asks run high and sales low (asks over OUTLIER times the market are troll
  prices and left out; grey rows the bot's own purchases left behind aren't sales)
- MeowDB's 24h median, or its 7-day one when the last day had few reports

A source with fewer than MIN_POINTS data points is left out, and none counts for more than
WEIGHT_CAP, so a burst of reports can't drown out the rest.
"""
import sqlite3
import statistics as st
import time
from pathlib import Path

import price_checker as pc

SCAN_DAYS = 7
MIN_POINTS = 3
WEIGHT_CAP = 30
OUTLIER = 2.0
WEEK_WEIGHT = MIN_POINTS  # MeowDB's 7-day median: how many reports are behind it isn't saved
CACHE_SECONDS = 600


def connect(db_path):
    return sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)


def scan_prices(db_path, name, market=None, days=SCAN_DAYS, now=None):
    """(asks, sold): the price of one item in every listing of it the bot saw in the last days.
    Stars are one item whatever the listing says. Grey rows of the bot's own purchases (that
    shop's owner, that item, that price, bought before the visit) are left out: they're our
    buys, not the market's. Asks over OUTLIER times the market (market, or the asks' own median)
    are troll prices and left out; other sold rows are kept, as someone paid that."""
    since = (now or time.time()) - days * 86400
    try:
        q = connect(db_path)
        rows = q.execute(
            "SELECT l.bundle, l.price, l.sold FROM listings l JOIN shops h ON h.id = l.shop_id "
            "WHERE l.item = ? AND h.epoch >= ? AND NOT (l.sold = 1 AND EXISTS (SELECT 1 FROM own_buys o "
            "WHERE o.item = l.item AND o.owner = h.owner AND o.owner != '' AND o.price = l.price AND o.epoch <= h.epoch + 60))",
            (name, since)).fetchall()
        q.close()
    except sqlite3.Error:
        return [], []
    each = [(price if pc.is_star(name) else price / max(bundle or 1, 1), sold) for bundle, price, sold in rows]
    asks = [p for p, sold in each if not sold]
    sold = [p for p, s in each if s]
    ref = market or (st.median(asks) if asks else None)
    if ref:
        asks = [p for p in asks if p <= OUTLIER * ref]
    return asks, sold


def meow_part(entry):
    """(MeowDB's median, its weight), or (None, 0)."""
    if not entry or entry.get("status"):
        return None, 0
    reports = entry.get("reports_24h") or 0
    if entry.get("median_24h") and reports >= MIN_POINTS:
        return entry["median_24h"], min(reports, WEIGHT_CAP)
    if entry.get("median_7d"):
        return entry["median_7d"], WEEK_WEIGHT
    return None, 0


def scan_part(asks, sold):
    """(the scans' estimate, its weight), or (None, 0). With both, the midpoint of the median ask
    and the median sale; with one, that one."""
    sides = [v for v in (asks, sold) if len(v) >= MIN_POINTS]
    if not sides:
        return None, 0
    value = sum(st.median(v) for v in sides) / len(sides)
    return value, min(sum(len(v) for v in sides), WEIGHT_CAP)


def reference(meow_entry, asks, sold):
    """(reference price or None, {"MeowDB": (value, weight), "scans": (value, weight), "asks": median
    or None, "sold": median or None})."""
    meow = meow_part(meow_entry)
    scans = scan_part(asks, sold)
    parts = {"MeowDB": meow, "scans": scans,
             "asks": st.median(asks) if len(asks) >= MIN_POINTS else None,
             "sold": st.median(sold) if len(sold) >= MIN_POINTS else None}
    weight = meow[1] + scans[1]
    if not weight:
        return None, parts
    return (meow[0] or 0) * meow[1] / weight + (scans[0] or 0) * scans[1] / weight, parts


STOCKED = 50  # a listing with this many in stock is a well-stocked seller's


def stocked_ask(db_path, name, min_stock=STOCKED, days=SCAN_DAYS, now=None):
    """The median asked by sellers with min_stock or more in stock, in the last days: what a
    consumable is taken to be worth (the user's choice: a big stack is a convenience). None with
    fewer than MIN_POINTS such listings. Troll asks, over OUTLIER times their median, are left out."""
    since = (now or time.time()) - days * 86400
    try:
        q = connect(db_path)
        asks = [p for (p,) in q.execute("SELECT l.price FROM listings l JOIN shops h ON h.id = l.shop_id WHERE l.item = ? "
                                         "AND l.sold = 0 AND l.bundle = 1 AND l.stock >= ? AND h.epoch >= ?", (name, min_stock, since))]
        q.close()
    except sqlite3.Error:
        return None
    if asks:
        middle = st.median(asks)
        asks = [p for p in asks if p <= OUTLIER * middle]
    return st.median(asks) if len(asks) >= MIN_POINTS else None


def sold_count(db_path, name, days=SCAN_DAYS, now=None):
    """How many grey rows of an item the scans saw in the last days, the bot's own buys left out:
    the evidence that it sells at all (one grey row is one sale, whatever its amount)."""
    return len(scan_prices(db_path, name, None, days, now)[1])


class Prices:
    """reference() for the crawler, by item name, from a market.Live and the scan database.
    Scans are read again after CACHE_SECONDS, so new visits count during a run."""

    def __init__(self, live, db_path, consumables=()):
        """consumables: names priced by what well-stocked sellers ask (stocked_ask) instead."""
        self.live, self.db_path = live, db_path
        self.consumables = set(consumables)
        self.cache = {}  # name -> (time, asks, sold)
        self.stocked = {}  # name -> (time, stocked_ask)

    def scans(self, name):
        hit = self.cache.get(name)
        if not hit or time.monotonic() - hit[0] > CACHE_SECONDS:
            meow, _ = meow_part(self.live.get(name))
            asks, sold = scan_prices(self.db_path, name, meow)
            hit = self.cache[name] = (time.monotonic(), asks, sold)
        return hit[1], hit[2]

    def reference(self, name):
        if name in self.consumables:
            hit = self.stocked.get(name)
            if not hit or time.monotonic() - hit[0] > CACHE_SECONDS:
                hit = self.stocked[name] = (time.monotonic(), stocked_ask(self.db_path, name))
            return hit[1], {"stocked asks": hit[1]}
        asks, sold = self.scans(name)
        return reference(self.live.get(name), asks, sold)

    def sold(self, name):
        return len(self.scans(name)[1])
