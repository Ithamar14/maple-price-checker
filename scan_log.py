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
import csv
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import cv2

import price_checker as pc

FILE = Path(__file__).parent / "data" / "market_scans.sqlite"
EXPORT_FILE = FILE.with_name("freemarket_scans.json")  # all of it in one file, to share (see export)
LOG_DIR = Path(__file__).parent / "logs"
EXPORT_EVERY = 300    # seconds between rewrites of that file while crawling; a run's end always does
EXPORT_VERSION = 1
EXPORT_NOTES = [
    "A sold row's price is what someone paid; any other row's is only what's being asked.",
    "A sold row says it sold at that price, not how many: count one sale at a price, never a volume. Its bundle and stock say nothing about the amount.",
    "Throwing stars are one item whatever bundle says: never divide their price by bundle, and they have no stock.",
    "A grey row stays in its store, so one sale can be logged again on a later visit.",
    "Rows that look the same on either side of a scroll count as one, so a store can be undercounted. complete = 0: the store was cut short.",
    "skipped = 1: the owner's shop was logged recently by this contributor, so the visit has no listings. Each contributor decides that alone, so pooled files can hold the same shop twice.",
    "own_buys are the contributor's bot's purchases: leave them out of anything about sales.",
    "deals are listings the bot acted on or noted, so they overlap shops. npc_pays and profit_each come from the contributor's own price list.",
    "ids are unique across contributors: use them to merge a newer file from the same contributor.",
    "Prices are kept as read, troll prices too. For ETC and USE, leave out asks over 2 times the item's price, but keep sold rows, as someone paid that; not for equipment, where stats set the price.",
]
EXPORT_SERVER_NOTES = [  # for whatever pools these files
    "Same shop from two contributors: the 12 hour repeat check is per bot, so treat the same owner and listings within a short window as one visit.",
    "OCR errors in owner names: a misread name looks like a different seller, so don't count low owner_score records towards seller tracking.",
    "Profit figures vary by contributor: recalculate npc_pays and profit_each from one price list, or ignore them.",
    "File size: each upload is the contributor's whole history and keeps growing. Fine for now; later, send only new records.",
    "Other players' names: this holds seller names and shop titles, and pooled data can show who consistently sells low. Keep it behind a login, not on a public URL.",
]
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
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
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


def read_deals(log_dir, contributor):
    """Every line of the crawl_<date>.csv logs. A log is named for the day its run began, and its
    times have no date, so a time far earlier than the last one is the next day."""
    deals = []
    for path in sorted(Path(log_dir).glob("crawl_*.csv")):
        try:
            day = datetime.strptime(path.stem[len("crawl_"):], "%Y-%m-%d")
            with open(path, newline="", encoding="utf-8") as f:
                last = None
                for number, line in enumerate(csv.DictReader(f), 2):
                    try:
                        at = day + (datetime.strptime(line["time"], "%H:%M:%S") - datetime(1900, 1, 1))
                        if last and at < last - timedelta(hours=12):
                            day += timedelta(days=1)
                            at += timedelta(days=1)
                        last = at
                        room, channel = parse_place(line["channel"])
                        deals.append({"id": f"{contributor}:d{path.stem[len('crawl_'):]}:{number}", "at": at.astimezone().isoformat(timespec="seconds"),
                                      "place": line["channel"], "room": room, "channel": channel, "item": line["item"], "asking": int(line["asking"]),
                                      "npc_pays": int(line["npc_pays"]), "profit_each": int(line["profit_each"]), "quantity": int(line["quantity"]), "note": line["note"]})
                    except (ValueError, TypeError, KeyError):  # a line cut short by a crash
                        continue
        except (ValueError, OSError):
            continue
    return deals


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
        self.exported = 0.0
        # made here, used from the thread doing the crawling: only ever one at a time. Another program
        # (the launcher's sync) can be writing to the file too: wait for it rather than fail
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.executescript(SCHEMA)
        for table in ("shops", "own_buys"):  # where a record came from, if another bot (sync.py): None for our own
            if "remote" not in [c[1] for c in self.db.execute(f"PRAGMA table_info({table})")]:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN remote TEXT")
            self.db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_remote ON {table}(remote)")
        self.db.commit()

    def close(self):
        self.db.close()

    def contributor(self):
        """A random name for this copy of the bot, made once: nothing about whose it is."""
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('contributor', ?)", (uuid.uuid4().hex[:12],))
        return self.db.execute("SELECT value FROM meta WHERE key = 'contributor'").fetchone()[0]

    def export(self, path=None, log_dir=None):
        """Everything scanned so far as one JSON file, replaced whole so it's never half written."""
        path, who = Path(path or EXPORT_FILE), self.contributor()
        listings = {}
        for shop, *row in self.db.execute("SELECT l.shop_id, l.row, l.name, l.item, l.bundle, l.price, l.stock, l.sold FROM listings l "
                                          "JOIN shops h ON h.id = l.shop_id WHERE h.remote IS NULL ORDER BY l.shop_id, l.row"):
            listings.setdefault(shop, []).append(dict(zip(("row", "name", "item", "bundle", "price", "stock", "sold"), row)))
        shops = [{"id": f"{who}:s{r[0]}", **dict(zip(("at", "epoch", "place", "room", "channel", "owner", "owner_score", "title", "skipped", "complete"), r[1:])),
                  "listings": listings.get(r[0], [])}
                 for r in self.db.execute("SELECT id, at, epoch, place, room, channel, owner, owner_score, title, skipped, complete FROM shops "
                                          "WHERE remote IS NULL ORDER BY id")]
        buys = [{"id": f"{who}:b{r[0]}", **dict(zip(("at", "epoch", "place", "owner", "item", "quantity", "price"), r[1:]))}
                for r in self.db.execute("SELECT rowid, at, epoch, place, owner, item, quantity, price FROM own_buys WHERE remote IS NULL ORDER BY rowid")]
        data = {"version": EXPORT_VERSION, "contributor": who, "updated": datetime.now().astimezone().isoformat(timespec="seconds"), "notes": EXPORT_NOTES, "server_notes": EXPORT_SERVER_NOTES,
                "shops": shops, "own_buys": buys, "deals": read_deals(log_dir or LOG_DIR, who)}
        part = path.with_name(path.name + ".part")
        part.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
        os.replace(part, path)
        self.exported = time.monotonic()
        return data

    def export_due(self):
        return time.monotonic() - self.exported >= EXPORT_EVERY

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
