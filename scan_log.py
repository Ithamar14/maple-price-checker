"""Everything the crawler reads in player stores, kept in data/market_scans.sqlite: grey (sold out)
rows, stacks and items not in the item list too, with the shop's owner and title.

    shops      one row per visit: when, where, owner, title. skipped = 1: the owner's shop was
               logged less than hours ago, so nothing from this visit was added
    listings   one row per listing in a logged visit. sold = 1: grey when first seen. bundle is
               the number in "500 for 3,333 mesos", stock the number on its icon when one was read
               (throwing stars: none),
               item the item list's name for it when the name fits exactly one item
    own_buys   what the bot bought, so its own purchases going grey aren't taken for other
               people's sales

READ THIS BEFORE ANY ANALYSIS OF THIS DATA:
- The price of a sold row is what someone paid, the price of the others is only what's being asked.
- A sold (grey) row says that it sold at that price, NOT how many sold. Its bundle and stock say
  nothing about the amount: count it as one sale at a price, and never as a sales volume.
- Throwing stars (price_checker.THROWING_STARS) are one item whatever bundle says. "1 for 500,000"
  and "800 for 500,000" are both one item at 500,000: never divide their price by bundle.
- A grey row stays in its store, so one sale can be logged again on a later visit to that shop.
- Rows that look the same on either side of a scroll count as one, so a store can be undercounted.
  Rows in shops.complete = 0 were cut short. Excluding own_buys matters for anything about sales.
"""
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import cv2

import price_checker as pc

FILE = Path(__file__).parent / "data" / "market_scans.sqlite"
HOURS_BETWEEN = 12    # a shop's owner is logged once in this many hours
OWNER_BOX = (-241, 79, -78, 109)      # from the store buttons at UI scale 1: (x0, y0, x1, y1)
TITLE_BOX = (-236, -161, 694, -126)
READ_ZOOM = 2
MIN_OWNER_SCORE = 0.8  # read less surely than this, the name isn't trusted to recognise a shop
# like pc.LISTING_RE, but the end of "mesos" may be covered: the price before it is whole
ROW_RE = re.compile(rf"^\s*{pc.NUM}\s*for\s*{pc.NUM}\s*m", re.I)
PLACE_RE = re.compile(r"^(?:room (\d+))?\s*(?:channel )?(\d+)?$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (id INTEGER PRIMARY KEY, at TEXT, epoch REAL, place TEXT, room INTEGER, channel INTEGER,
    owner TEXT, owner_score REAL, title TEXT, skipped INTEGER, complete INTEGER, rows INTEGER);
CREATE TABLE IF NOT EXISTS listings (shop_id INTEGER REFERENCES shops(id), row INTEGER, name TEXT, item TEXT,
    bundle INTEGER, price INTEGER, stock INTEGER, sold INTEGER);
CREATE TABLE IF NOT EXISTS own_buys (at TEXT, epoch REAL, place TEXT, owner TEXT, item TEXT, quantity INTEGER, price INTEGER);
CREATE INDEX IF NOT EXISTS shops_owner ON shops(owner, epoch);
CREATE INDEX IF NOT EXISTS listings_item ON listings(item, sold);
CREATE INDEX IF NOT EXISTS listings_shop ON listings(shop_id);
"""


def read_rows(lines, frame):
    """[{name, bundle, price, stock, sold}] for every row on a page, top first. lines are the OCR
    lines of the store's list, in the frame's pixels."""
    rows = []
    for l in sorted(lines, key=lambda l: l.y0):
        m = ROW_RE.match(l.text)
        if not m:
            continue
        name = pc.name_above(l, lines)
        if name is None:
            continue
        try:
            bundle, price = pc.to_int(m.group(1)), pc.to_int(m.group(2))
        except ValueError:
            continue
        # the stock is a number on the item's icon, left of the text
        stock = next((int(o.text) for o in lines if o.text.strip().isdigit() and o.x1 <= l.x0 + 5
                      and abs(o.cy - l.cy) < l.h), None)
        if pc.is_star(name.text):  # on stars that's the stars in the stack; a listing's one stack
            stock = None
        rows.append({"name": name.text.strip(), "bundle": bundle, "price": price, "stock": stock,
                     "sold": bool(pc.is_faded(frame, name))})
    return rows


def merge_page(rows, page):
    """Adds a page to the rows so far. The last scroll of a list moves it less than a whole page, so
    a page can start with the rows the last one ended on. Compared without sold, as a row the bot
    just bought from goes grey: the first look at a row is the one that counts. Rows that really
    are the same item, amount and price across a page break count as one: fewer, never more."""
    same = lambda r: (r["name"], r["bundle"], r["price"])
    for k in range(min(len(rows), len(page)), 0, -1):
        if [same(r) for r in rows[-k:]] == [same(r) for r in page[:k]]:
            return rows + page[k:]
    return rows + page


def read_box(frame, reader, where, loc, scale):
    """(text, lowest score) in a box placed from the store buttons at loc."""
    x0, y0, x1, y1 = where
    crop = frame[max(0, int(loc[1] + y0 * scale)):int(loc[1] + y1 * scale),
                 max(0, int(loc[0] + x0 * scale)):int(loc[0] + x1 * scale)]
    if not crop.size:
        return "", 0.0
    zoom = READ_ZOOM / scale
    found = sorted(reader.detect(cv2.resize(crop, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_CUBIC)),
                   key=lambda d: d[0][0][0])
    if not found:
        return "", 0.0
    return " ".join(t.strip() for _, t, _ in found), float(min(s for _, _, s in found))


def parse_place(place):
    """(room, channel) out of "room 14 channel 38", "room 6" or "38". None for what isn't there."""
    m = PLACE_RE.match(str(place).strip())
    if not m:
        return None, None
    return int(m[1]) if m[1] else None, int(m[2]) if m[2] else None


class ShopVisit:
    """What's been read of the store that's open."""

    def __init__(self, place):
        self.place = place
        self.owner, self.score, self.title = "", 0.0, ""
        self.begun = False
        self.rows = []
        self.pages = set()
        self.equip_checked = {}  # the equipment finder's verdicts in this store
        self.complete = False


class ScanLog:
    def __init__(self, path=None, hours=HOURS_BETWEEN):
        self.hours = hours
        self.path = Path(path or FILE)
        self.path.parent.mkdir(exist_ok=True)
        # made here, used from the thread doing the crawling: only ever one at a time
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def recent(self, owner, now=None):
        """Was this owner's shop logged less than hours ago?"""
        if not owner or self.hours <= 0:
            return False
        row = self.db.execute("SELECT MAX(epoch) FROM shops WHERE owner = ? AND skipped = 0", (owner,)).fetchone()
        return bool(row[0]) and (now or time.time()) - row[0] < self.hours * 3600

    def save(self, visit, match=None, now=None):
        """match(name) -> the item list's name, or None. Returns "logged", "skipped" or None for a
        visit where nothing was read. A row with an owner it couldn't read is still logged."""
        if not visit.begun:
            return None
        now = now or time.time()
        trusted = visit.owner if visit.score >= MIN_OWNER_SCORE else ""
        skipped = self.recent(trusted, now)
        at = datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")
        room, channel = parse_place(visit.place)
        rows = [] if skipped else visit.rows
        with self.db:
            shop = self.db.execute(
                "INSERT INTO shops (at, epoch, place, room, channel, owner, owner_score, title, skipped, complete, rows) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (at, now, str(visit.place), room, channel, visit.owner, visit.score, visit.title, int(skipped),
                 int(visit.complete), len(rows))).lastrowid
            self.db.executemany(
                "INSERT INTO listings (shop_id, row, name, item, bundle, price, stock, sold) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(shop, i, r["name"], match(r["name"]) if match else None, r["bundle"], r["price"], r["stock"], int(r["sold"]))
                 for i, r in enumerate(rows)])
        return "skipped" if skipped else "logged"

    def bought(self, place, owner, item, quantity, price, now=None):
        now = now or time.time()
        at = datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")
        with self.db:
            self.db.execute("INSERT INTO own_buys (at, epoch, place, owner, item, quantity, price) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (at, now, str(place), owner, item, quantity, price))
