"""Fills your own store with what the bot bought to sell on, and opens it.

Open the store yourself first (find a spot, set it up, so its window shows) with your inventory
window open beside it. Then this:

1. reads the Equip, Use and Etc tabs of your inventory: hovers each item and reads its name off
   its tooltip (icons aren't trusted: some ores look alike). Only the full sell loop's store
   deals (holdings.py) and custom list items set to sell in your store are listed, never Only 1
   or equipment finder ones, and nothing else in the inventory is touched
2. as soon as it knows an item to list, prices it (see price_item, logged with why) and places
   it: the item, then the store's list, then the amount (9999: all of them) and the price per
   item in the box that opens, until the store is full (10 listings). Each new row of the store
   is read back, and has to show that item's name and price: one that doesn't is taken back
   out, and filling stops
3. shows you what's in the store, to check there, and opens it if you say so

Hold Esc (or press Stop in the launcher) to stop. Everything is logged to logs/store_<time>.txt.

    python store.py [--dry-run]    # --dry-run: read and price only, place nothing

For a test: --item NAME (any number of times) lists those instead of the custom list, and
--assume-paid-under 20 takes them as bought 20% under the price's reference (see store_test.bat).
"""
import argparse
import ctypes
import json
import math
import os
import re
import sqlite3
import statistics as st
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

import crawler
import custom_items
import holdings
import market
import price_checker as pc
import pricing
import scan_log
import sync

HERE = Path(__file__).parent
LOG_DIR = HERE / "logs"
LAUNCHER_SETTINGS = HERE / "launcher_settings.json"
MARKET_BUYS = LOG_DIR / "market_buys.txt"
MAX_ROWS = 10            # a store holds this many listings
MAX_MIXUPS = 3           # wrong items in the store (each taken back out) before it stops
VISIBLE_ROWS = 6         # the store's list shows this many before it scrolls
TABS = ("Equip", "Use", "Etc")
PAID_DAYS = 30           # what the bot paid, from purchases this recent
MATCH_THRESHOLD = 0.8    # a window's template match
MARKET_WAIT = 30         # seconds to wait for MeowDB lookups
DIALOG_WAIT = 3.0
INVENTORY_WAIT = 0.6     # seconds for the inventory window to show after I

# where things are, at UI scale 1, from the top left corner of each window's template
# store window: templates/store_buttons_own.png (SOLD ITEMS, OPEN STORE, CLOSE STORE)
OPEN_BUTTON = (57, 57)
CLOSE_BUTTON = (57, 85)
ROWS_BOX = (-191, 132, 92, 132 + int(VISIBLE_ROWS * 56.2))   # the store's list
ROW_PITCH = 56.2
ROW_ICON = (-188, 4, -144, 50)  # a row's icon, from its top
LIST_SPOT = (-28, 272)          # click here to put an item in the store; wheel here to scroll it
# inventory: templates/inventory_title.png ("ITEM INVENTORY")
TAB_SPOTS = {"Equip": (17, 39), "Use": (64, 39), "Etc": (158, 39), "Cash": (202, 39)}
SLOT0 = (21, 84)                # middle of the first slot
SLOT_PITCH = (50.5, 47.8)
GRID = (5, 6)                   # columns, rows that show without scrolling
SLOT_HALF = 21
TAB_ACTIVE_SAT = 40          # the selected tab is pink (58+ here), the others grey (0)
SLOT_EMPTY_SPREAD = 15.0      # an empty slot measures 5-6, one with an item 29+
# the TO SELL box: templates/to_sell.png (its three pink labels)
COUNT_FIELD = (231, 11)
PRICE_FIELD = (231, 39)
SELL_OK = (206, 135)
TIP_AREA = (-120, -320, 640, 320)  # where a hovered item's tooltip can be, around the cursor
TOOLTIP_DARK = 140    # a tooltip's background is darker than this (grey 0-255); a window's is lighter
CHAT_AREA = (0.18, 0.68, 0.64, 0.93)  # the chat log, as shares of the game window: x0, y0, x1, y1
SPOT_STEP = 12        # minimap units between spots tried for the store
SPOT_TRIES = 12       # spots tried before giving up on a room
SPOT_RANGE = 70       # how far from the middle of the map spots are tried
PORTAL_MESSAGE = "too close to a portal"
HOVER_DEBUG = pc.DEBUG_DIR / "store_hover"
KEEP_HOVER_SHOTS = 30


def log_line(path, text):
    print(text)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%H:%M:%S}  {text}\n")


# ---------------------------------------------------------------- prices

def paid_for(db_path, name, days=PAID_DAYS, now=None):
    """(average paid each, how many) of the bot's purchases of an item in the last days."""
    since = (now or time.time()) - days * 86400
    try:
        q = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
        rows = q.execute("SELECT quantity, price FROM own_buys WHERE item = ? AND epoch >= ?", (name, since)).fetchall()
        q.close()
    except sqlite3.Error:
        return None, 0
    count = sum(n for n, _ in rows)
    return (sum(n * p for n, p in rows) / count if count else None), count


def median_when_bought(name, path=MARKET_BUYS):
    """MeowDB's median when the bot last bought this as a market deal (logs/market_buys.txt), or None."""
    try:
        lines = Path(path).read_text("utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if f" x {name} at " in line:
            m = re.search(r"median ([\d,]+)\)", line)
            return int(m.group(1).replace(",", "")) if m else None
    return None


def price_item(name, npc, reference, parts, paid=None, median_then=None, equipment=False):
    """{"price": mesos each or None (not listed), "reference", "parts", "floor", "flag"}.

    The price is 1 under the reference (pricing.reference: the same one market deals are bought
    by). Never under the floor: what was paid plus the 3% the store keeps, and what the NPC pays."""
    out = {"price": None, "reference": reference, "parts": parts, "floor": None, "flag": ""}
    if equipment:
        out["flag"] = "equipment: its stats set its price, so price it yourself"
        return out
    if not reference:
        out["flag"] = "no market data: not listed"
        return out
    want = math.ceil(reference) - 1
    floor_paid = math.ceil(paid / (1 - crawler.SHOP_FEE)) if paid else 0
    out["floor"] = max(floor_paid, npc or 0) or None
    if npc and want < npc:
        out["flag"] = f"the market ({reference:,.0f}) is under what the NPC pays ({npc:,}): sell it to the NPC instead"
        return out
    if paid and want < floor_paid:
        out["price"] = floor_paid
        if median_then and median_then > reference:
            out["flag"] = f"price dropped: the median was {median_then:,} when bought at {paid:,.0f}, it's {reference:,.0f} now"
        elif median_then:
            out["flag"] = f"bought at {paid:,.0f}, too close to the median then ({median_then:,}): check the buy rule"
        else:
            out["flag"] = f"bought at {paid:,.0f}, over today's market ({reference:,.0f}): check whether the bot should have bought it"
        out["flag"] += f"; listed at the floor, {floor_paid:,}"
        return out
    out["price"] = want
    return out


# ---------------------------------------------------------------- the screen

def find_template(frame, tpl, guess=None):
    """(x, y, scale) of the template's best match, or None under MATCH_THRESHOLD."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    guess = guess or frame.shape[0] / 1080
    best = (0, None, None)
    for s in sorted({round(guess * (1 + k / 100), 2) for k in range(-8, 9)} | {1.0}):
        t = crawler.scaled(tpl, s)
        if t.shape[0] > gray.shape[0] or t.shape[1] > gray.shape[1]:
            continue
        _, v, _, loc = cv2.minMaxLoc(cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED))
        if v > best[0]:
            best = (v, loc, s)
    return (best[1][0], best[1][1], best[2]) if best[0] >= MATCH_THRESHOLD else None


def at(anchor, dx, dy):
    x, y, s = anchor
    return x + dx * s, y + dy * s


def crop(frame, anchor, box):
    x, y, s = anchor
    x0, y0, x1, y1 = box
    h, w = frame.shape[:2]
    return frame[max(0, int(y + y0 * s)):min(h, int(y + y1 * s)), max(0, int(x + x0 * s)):min(w, int(x + x1 * s))]


def slot_center(inv, col, row):
    return at(inv, SLOT0[0] + col * SLOT_PITCH[0], SLOT0[1] + row * SLOT_PITCH[1])


def slot_icon(frame, inv, col, row):
    cx, cy = slot_center(inv, col, row)
    s, half = inv[2], SLOT_HALF * inv[2]
    return frame[int(cy - half):int(cy + half), int(cx - half):int(cx + half)].copy()


def slot_used(icon):
    return float(icon.std()) > SLOT_EMPTY_SPREAD


def active_tab(frame, inv):
    for name, (dx, dy) in TAB_SPOTS.items():
        patch = crop(frame, inv, (dx - 6, dy - 5, dx + 6, dy + 5))
        if patch.size and cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)[..., 1].mean() >= TAB_ACTIVE_SAT:
            return name
    return None


def tooltip_box(before, after, s, cursor=None):
    """(x0, y0, x1, y1) of the tooltip of the item hovered: what showed up between two frames
    next to the cursor, as the tooltip is see-through and its colour shifts with what's behind it.
    Only near the cursor, as players, chat and clouds move elsewhere. None if nothing
    tooltip-sized changed there."""
    h, w = after.shape[:2]
    ox, oy = 0, 0
    if cursor:
        ox, oy = max(0, int(cursor[0] + TIP_AREA[0] * s)), max(0, int(cursor[1] + TIP_AREA[1] * s))
        x1, y1 = min(w, int(cursor[0] + TIP_AREA[2] * s)), min(h, int(cursor[1] + TIP_AREA[3] * s))
        before, after = before[oy:y1, ox:x1], after[oy:y1, ox:x1]
    changed = (cv2.absdiff(before, after).max(axis=2) > 25).astype(np.uint8)
    changed = cv2.morphologyEx(changed, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(changed)
    boxes = [st_ for st_ in stats[1:] if st_[2] >= 120 * s and st_[3] >= 40 * s]
    if not boxes:
        return None
    x, y, bw, bh, _ = max(boxes, key=lambda b: b[4])
    return ox + x, oy + y, ox + x + bw, oy + y + bh


def tooltip_name(frame, box, reader, db):
    """The item name a tooltip shows: its first line of near-white text that's an item. None if none."""
    x0, y0, x1, y1 = box
    tip = frame[y0:y1, x0:x1]
    bright = np.where((tip.min(axis=2) >= pc.TooltipReader.TEXT_MIN)[..., None], 0, 255).astype(np.uint8).repeat(3, axis=2)
    lines = [l for l in sorted(reader.read(bright), key=lambda l: l.y0) if on_dark(tip, l)]
    for line in lines[:3]:
        found = db.candidates(line.text)
        if len(found) == 1:
            return found[0][0]
    return None


def on_dark(image, line):
    """Whether a line of text sits on a dark background (a tooltip's), not a light one (a window's):
    the box found for a tooltip can take in some of the store's window next to it."""
    patch = image[max(0, int(line.y0) - 3):int(line.y1) + 3, max(0, int(line.x0)):int(line.x1)]
    if not patch.size:
        return False
    gray = cv2.cvtColor(np.ascontiguousarray(patch), cv2.COLOR_BGR2GRAY)
    return float(np.percentile(gray, 40)) < TOOLTIP_DARK


def chat_lines(frame, reader):
    """The chat log's lines, top first."""
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = CHAT_AREA
    area = np.ascontiguousarray(frame[int(h * y0):int(h * y1), int(w * x0):int(w * x1)])
    return [l.text for l in sorted(reader.read(area), key=lambda l: l.y0)]


def chat_count(frame, reader, phrase):
    """How many lines of the chat log hold this phrase: one more than before is a new message."""
    return sum(phrase.lower() in line.lower() for line in chat_lines(frame, reader))


def store_spots(at, step=SPOT_STEP, tries=SPOT_TRIES, reach=SPOT_RANGE):
    """Minimap spots to try for the store, nearest first, either side of where we're standing."""
    spots = [at]
    for n in range(1, tries):
        for x in (at + n * step, at - n * step):
            if abs(x) <= reach and len(spots) < tries:
                spots.append(x)
    return spots


def is_own_store(frame, store, reader):
    """Whether the window found is your own store: a store you visit has the same layout, with
    SELLER INFO, BUY ITEM and LEAVE STORE on its buttons."""
    patch = crop(frame, store, (OPEN_BUTTON[0] - 54, OPEN_BUTTON[1] - 10, OPEN_BUTTON[0] + 54, OPEN_BUTTON[1] + 10))
    patch = cv2.resize(patch, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return "OPEN" in " ".join(t for t, _ in reader.recognize(patch)).upper().replace("0", "O")


def store_rows(frame, store, reader):
    """[{name, bundle, price, ...}] of the store's rows showing, top first."""
    box = crop(frame, store, ROWS_BOX)
    return scan_log.read_rows(reader.read(box), box)


def message_box(frame, reader):
    """(text, [OK / CANCEL button middles, left first]) of a blue message box in the middle of the
    screen ("Not available in the current state.", "Are you sure you want to open your store?"),
    or None."""
    h, w = frame.shape[:2]
    x0, y0 = int(w * 0.3), int(h * 0.3)
    area = frame[y0:int(h * 0.75), x0:int(w * 0.7)].astype(np.int16)
    blue = ((area[..., 0] > 160) & (area[..., 2] < 120) & (area[..., 1] > 100) & (area[..., 1] < 170)).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(blue)
    boxes = [s for s in stats[1:] if s[2] > w * 0.12 and s[3] > h * 0.04 and s[4] > 0.6 * s[2] * s[3]]
    if not boxes:
        return None
    bx, by, bw, bh, _ = max(boxes, key=lambda s: s[4])
    text = " ".join(t for _, t, _ in reader.detect(np.ascontiguousarray(frame[y0 + by:y0 + by + bh, x0 + bx:x0 + bx + bw])))
    below = frame[y0 + by + bh:min(h, y0 + by + bh + int(h * 0.07)), x0 + bx:x0 + bx + bw].astype(np.int16)
    orange = ((below[..., 2] > 200) & (below[..., 1] > 80) & (below[..., 1] < 160) & (below[..., 0] < 90)).astype(np.uint8)
    n, _, stats, cent = cv2.connectedComponentsWithStats(orange)
    buttons = sorted((x0 + bx + c[0], y0 + by + bh + c[1]) for s, c in zip(stats[1:], cent[1:]) if s[4] > 300)
    return text, buttons


# ---------------------------------------------------------------- the bot

class Stopped(Exception):
    pass


class MixUp(Stopped):
    """The store row showed another item than the one put in: it was taken back out."""

    def __init__(self, text, seen):
        super().__init__(text)
        self.seen = seen  # the item the row showed, if its name was one item's


class StoreBot:
    def __init__(self, args):
        found = pc.find_window(args.window)
        if not found:
            raise SystemExit(f"{args.window} isn't open (or is minimised)")
        self.hwnd = found[0]
        self.screen = crawler.Screen(self.hwnd)
        self.controls = crawler.Controls(self.hwnd, float("inf"))
        self.db = pc.PriceDB(pc.PRICES_FILE, args.match)
        self.reader = pc.Reader(1.0)
        self.assume_under = args.assume_paid_under  # a test: what was paid, as % under the reference
        self.store_cfg = json.loads(crawler.SETTINGS_FILE.read_text("utf-8")).get("store_deals", {})
        self.consumables = self.store_cfg.get("consumables", {})
        self.live = market.Live(market.load(), game_ids=self.db.ids)
        self.store_tpl = crawler.load_template("store_buttons_own.png")
        self.inv_tpl = crawler.load_template("inventory_title.png")
        self.sell_tpl = crawler.load_template("to_sell.png")
        self.equipment = set(custom_items.load_equipment()) | set(self.db.slots)
        LOG_DIR.mkdir(exist_ok=True)
        self.log_file = LOG_DIR / f"store_{datetime.now():%Y-%m-%d_%H%M%S}.txt"
        threading.Thread(target=self._watch_stop, daemon=True).start()

    def _watch_stop(self):
        while not self.controls.stop.is_set():
            if crawler.stop_requested():
                self.controls.stop_reason = "the launcher's Stop button"
                self.controls.stop.set()
            time.sleep(0.2)

    def log(self, text):
        log_line(self.log_file, text)

    # ---- looking

    def sync_first(self):
        """One round with the bots' server before anything is priced, when the launcher's Server tab
        is on: the other bots' latest scans count in the prices. Not reachable: prices from what's here."""
        cfg = sync.read_json(LAUNCHER_SETTINGS) or {}
        if not (cfg.get("sv_on") and cfg.get("sv_url") and cfg.get("sv_key")):
            return
        try:
            summary = sync.run_once(sync.Client(cfg["sv_url"], cfg["sv_key"]), running={"store"}, files=False)
            self.log(f"synced with the server before pricing: {summary}")
        except (sync.SyncError, OSError, ValueError) as e:
            self.log(f"couldn't sync before pricing ({e}): pricing from the scans already here")

    def find_inventory(self, guess=None):
        """The inventory window, opened with I if it isn't showing. Stopped if it still isn't."""
        inv = find_template(self.screen.grab(), self.inv_tpl, guess)
        if not inv:
            self.log("  the inventory window isn't showing: pressing I")
            self.controls.type_text("i")
            self.controls.wait(INVENTORY_WAIT)
            self.park()
            inv = find_template(self.screen.grab(), self.inv_tpl, guess)
        if not inv:
            raise Stopped("the inventory window isn't showing, even after pressing I: open it, and check it isn't covered")
        return inv

    def windows(self):
        frame = self.screen.grab()
        store = find_template(frame, self.store_tpl)
        if not store or not is_own_store(frame, store, self.reader):
            raise Stopped("your store's window isn't showing: set up the store first, so its OPEN STORE button shows")
        self.store, self.inv = store, self.find_inventory(store[2])
        return self.screen.grab()

    def click(self, x, y):
        self.controls.click(*self.screen.to_screen(x, y))

    def park(self):
        r = self.screen.region
        self.controls.move(*self.screen.to_screen(r["width"] * crawler.PARK_AT[0], r["height"] * crawler.PARK_AT[1]))
        self.controls.wait(0.25)

    def open_tab(self, tab):
        for _ in range(2):
            self.click(*at(self.inv, *TAB_SPOTS[tab]))
            self.park()
            frame = self.screen.grab()
            if active_tab(frame, self.inv) == tab:
                return frame
        raise Stopped(f"couldn't open the {tab} tab of the inventory")

    def hover_name(self, col, row, before):
        """The hovered item's name, once two reads agree, or None. before: the screen with the
        cursor parked, to find the tooltip by."""
        cursor = slot_center(self.inv, col, row)
        self.controls.move(*self.screen.to_screen(*cursor))
        names = []
        for _ in range(3):
            self.controls.wait(0.35)
            frame = self.screen.grab()
            box = tooltip_box(before, frame, self.inv[2], cursor)
            names.append(tooltip_name(frame, box, self.reader, self.db) if box else None)
            if len(names) >= 2 and names[-1] and names[-1] == names[-2]:
                return names[-1]
        self.save_hover(before, frame, cursor, box, f"slot {row + 1}.{col + 1}")
        return None

    def save_hover(self, before, frame, cursor, box, what):
        """A picture of a hover whose name wasn't read: the screen, the area searched (blue), the
        box taken for the tooltip (red), and the text read in it, in debug/store_hover."""
        HOVER_DEBUG.mkdir(parents=True, exist_ok=True)
        s, image = self.inv[2], frame.copy()
        cx, cy = int(cursor[0]), int(cursor[1])
        cv2.rectangle(image, (int(cx + TIP_AREA[0] * s), int(cy + TIP_AREA[1] * s)), (int(cx + TIP_AREA[2] * s), int(cy + TIP_AREA[3] * s)), (255, 0, 0), 2)
        cv2.drawMarker(image, (cx, cy), (0, 255, 255), cv2.MARKER_CROSS, 20, 2)
        text = "no tooltip found"
        if box:
            cv2.rectangle(image, (int(box[0]), int(box[1])), (int(box[2]), int(box[3])), (0, 0, 255), 2)
            tip = frame[box[1]:box[3], box[0]:box[2]]
            bright = np.where((tip.min(axis=2) >= pc.TooltipReader.TEXT_MIN)[..., None], 0, 255).astype(np.uint8).repeat(3, axis=2)
            text = " | ".join(l.text for l in sorted(self.reader.read(bright), key=lambda l: l.y0)) or "no text read"
        name = f"{datetime.now():%Y%m%d_%H%M%S}_{what.replace(' ', '_')}"
        cv2.imwrite(str(HOVER_DEBUG / f"{name}.png"), image)
        cv2.imwrite(str(HOVER_DEBUG / f"{name}_before.png"), before)
        for old in sorted(HOVER_DEBUG.glob("*.png"))[:-2 * KEEP_HOVER_SHOTS]:
            old.unlink(missing_ok=True)
        self.log(f"    {what}: name not read ({text}); picture in debug/store_hover/{name}.png")

    def fill(self, wanted, rows, dry_run=False):
        """Goes through the tabs, slot by slot, and puts each item whose name is in wanted in the
        store as soon as it knows it, at its price. Returns (what was priced, the store's rows)."""
        priced, listed, mixups = [], set(), 0
        self.seen, self.overflow, self.unread = set(), False, 0
        for tab in TABS:
            if len(rows) >= MAX_ROWS:
                break
            done = set()  # slots looked at on this tab: a mix-up starts the tab again, past them
            while True:
                try:
                    priced, rows = self.fill_tab(tab, wanted, rows, dry_run, priced, listed, done)
                    break
                except MixUp as e:
                    mixups += 1
                    if mixups > MAX_MIXUPS:
                        raise Stopped(f"{e}, and {mixups} mix-ups in all: check the tooltips (debug/store_hover)")
                    self.log(f"    {e}: taken back out" + (f" (it was {e.seen})" if e.seen else ""))
        return priced, rows

    def fill_tab(self, tab, wanted, rows, dry_run, priced, listed, done):
        """One tab of fill(): the slots not looked at yet, each listed once it's known."""
        frame = self.open_tab(tab)
        used = [(c, r) for r in range(GRID[1]) for c in range(GRID[0]) if slot_used(slot_icon(frame, self.inv, c, r))]
        self.log(f"{tab} tab: {len(used)} items showing" + (f", {len(done)} looked at already" if done else ""))
        if len(used) >= GRID[0] * GRID[1]:  # every slot showing is full: more can be below, out of sight
            self.overflow = True
        for c, r in [s for s in used if s not in done]:
            done.add((c, r))
            name = self.hover_name(c, r, frame)
            self.log(f"  slot {r + 1}.{c + 1}: {name or '(name not read)'}" + (" - to list" if name in wanted else ""))
            if name:
                self.seen.add(name)
            else:
                self.unread += 1
            if name not in wanted or name in listed:
                continue
            item = self.price_one(tab, c, r, name)
            priced.append(item)
            self.log(f"    {name}: {money(item['price']) if item['price'] else 'not listed'} "
                     f"(MeowDB {part(item['parts']['MeowDB'])}, scans {part(item['parts']['scans'])}: asked {money(item['parts']['asks'])}, sold {money(item['parts']['sold'])}; "
                     f"paid {money(item['paid'])}, floor {money(item['floor'])}){' - ' + item['flag'] if item['flag'] else ''}")
            if not item["price"] or dry_run:
                continue
            if len(rows) >= MAX_ROWS:
                self.log(f"    the store is full ({MAX_ROWS} listings): not placed")
                return priced, rows
            rows = self.place(item, rows)
            listed.add(name)
            self.log(f"    listed: {rows[-1]['name']}, {rows[-1]['bundle']} for {rows[-1]['price']:,} mesos")
            self.park()
            frame = self.screen.grab()  # the screen to find the next tooltips by, with this item gone
        self.park()
        return priced, rows

    def forget_gone(self, held, rows):
        """Store deals held that are neither in the inventory nor in the store any more were sold or
        moved by hand: forgotten, so they don't count towards what the crawler holds. Not when a tab
        was too full to see all of it, or a name couldn't be read: they may be there unseen."""
        in_store = {n for r in rows for n, _ in self.db.candidates(r["name"])}
        gone = sorted(n for n in held if n not in self.seen and n not in in_store)
        if not gone:
            return
        if self.overflow or self.unread:
            self.log(f"not in sight: {', '.join(gone)} (kept as held: a tab was full, or a name wasn't read)")
            return
        for name in gone:
            holdings.remove(name)
        self.log(f"no longer held, sold or moved by hand: {', '.join(gone)}")

    def read_store(self):
        """Every row in the store, scrolling through the list when it's longer than it shows."""
        spot = self.screen.to_screen(*at(self.store, *LIST_SPOT))
        self.controls.wheel(*spot, notches=5)  # to the top
        self.park()
        rows = store_rows(self.screen.grab(), self.store, self.reader)
        if len(rows) >= VISIBLE_ROWS:
            self.controls.wheel(*spot, notches=-5)
            self.park()
            rows = scan_log.merge_page(rows, store_rows(self.screen.grab(), self.store, self.reader))
        return rows

    # ---- prices

    def price_one(self, tab, c, r, name):
        """The item in this slot, priced (see price_item). Its MeowDB price is looked up first if
        it's over a day old or was never looked up."""
        self.live.get(name)
        self.live.refresh([name])
        end = time.monotonic() + MARKET_WAIT
        while self.live.pending([name]) and time.monotonic() < end:
            self.controls.wait(0.3)
        npc = self.db.lookup(name)
        npc = npc[1] if npc else None
        entry = self.live.items.get(name)
        meow, _ = pricing.meow_part(entry)
        if name in self.consumables:  # what well-stocked sellers ask: the same as it was bought by
            reference = pricing.stocked_ask(scan_log.FILE, name)
            parts = {"MeowDB": (None, 0), "scans": (reference, 0), "asks": reference, "sold": None}
        else:
            reference, parts = pricing.reference(entry, *pricing.scan_prices(scan_log.FILE, name, meow))
        held = holdings.load().get(name)
        if self.assume_under is None and held:
            paid, count = holdings.paid_each(held), held["quantity"]
        elif self.assume_under is None:
            paid, count = paid_for(scan_log.FILE, name)
        else:
            paid, count = (reference * (1 - self.assume_under / 100) if reference else None), 0
        result = price_item(name, npc, reference, parts, paid, median_when_bought(name), name in self.equipment)
        return {"tab": tab, "col": c, "row": r, "name": name, "paid": paid, "bought": count, **result}

    # ---- placing

    def wait_for_dialog(self):
        """("sell", anchor) once the TO SELL box shows, ("message", (text, buttons)) for a message box."""
        end = time.monotonic() + DIALOG_WAIT
        while time.monotonic() < end:
            self.controls.wait(0.2)
            frame = self.screen.grab()
            dialog = find_template(frame, self.sell_tpl, self.store[2])
            if dialog:
                return "sell", dialog
            box = message_box(frame, self.reader)
            if box and box[0]:
                return "message", box
        return None, None

    def type_into(self, dialog, spot, number):
        self.click(*at(dialog, *spot))
        self.controls.key("backspace", 8)
        self.controls.type_number(number)

    def place(self, item, before):
        """Puts one item in the store and checks the new row. Returns the rows after it."""
        name, price = item["name"], item["price"]
        self.open_tab(item["tab"])
        self.click(*slot_center(self.inv, item["col"], item["row"]))
        self.controls.wait(0.2)
        self.click(*at(self.store, *LIST_SPOT))
        kind, found = self.wait_for_dialog()
        if kind == "message":
            text, buttons = found
            if buttons:
                self.click(*buttons[0])
            raise Stopped(f"the game said \"{text}\" when {name} was put in the store" +
                          (": the store is full" if "not available" in text.lower() else ""))
        if kind != "sell":
            raise Stopped(f"no TO SELL box opened for {name}: check whether it's still on your cursor")
        dialog = found
        self.type_into(dialog, COUNT_FIELD, 9999)
        self.type_into(dialog, PRICE_FIELD, price)
        self.click(*at(dialog, *SELL_OK))  # checked on the store's new row, not in the box
        self.controls.wait(0.6)
        rows = self.read_store()
        new = rows[len(before):]
        ok = len(new) == 1 and name in [n for n, _ in self.db.candidates(new[0]["name"])] \
            and new[0]["price"] == price and new[0]["bundle"] == 1
        if not ok:
            seen = ", ".join(f"{r['name']} {r['bundle']} for {r['price']:,}" for r in new) or "no new row"
            self.log(f"  the store shows {seen} after putting in {name} at {price:,}: taking it back out")
            if new:
                self.take_back(len(rows) - 1, len(rows))
                if len(self.read_store()) != len(before):
                    raise Stopped(f"{name} didn't show in the store as expected ({seen}), and couldn't be taken back out")
            found = self.db.candidates(new[0]["name"]) if new else []
            raise MixUp(f"{name} didn't show in the store as expected ({seen})", found[0][0] if len(found) == 1 else None)
        return rows

    def take_back(self, index, total):
        """Moves row index (of total) of the store back into an empty inventory slot."""
        if index >= VISIBLE_ROWS:  # scrolled to the bottom, the list shows its last rows
            self.controls.wheel(*self.screen.to_screen(*at(self.store, *LIST_SPOT)), notches=-5)
            index -= total - VISIBLE_ROWS
        self.click(*at(self.store, ROW_ICON[0] + 22, ROWS_BOX[1] + index * ROW_PITCH + 27))
        self.controls.wait(0.2)
        frame = self.screen.grab()
        empty = next(((c, r) for r in range(GRID[1]) for c in range(GRID[0]) if not slot_used(slot_icon(frame, self.inv, c, r))), None)
        if empty:
            self.click(*slot_center(self.inv, *empty))
            self.park()

    # ---- setting the store up

    def find_permit(self):
        """(col, row) of the store permit on the Cash tab, by its tooltip, or None."""
        frame = self.open_tab("Cash")
        for c, r in [(c, r) for r in range(GRID[1]) for c in range(GRID[0]) if slot_used(slot_icon(frame, self.inv, c, r))]:
            cursor = slot_center(self.inv, c, r)
            self.controls.move(*self.screen.to_screen(*cursor))
            for _ in range(3):
                self.controls.wait(0.35)
                shot = self.screen.grab()
                box = tooltip_box(frame, shot, self.inv[2], cursor)
                if not box:
                    continue
                x0, y0, x1, y1 = box
                tip = shot[y0:y1, x0:x1]
                bright = np.where((tip.min(axis=2) >= pc.TooltipReader.TEXT_MIN)[..., None], 0, 255).astype(np.uint8).repeat(3, axis=2)
                text = " ".join(l.text for l in self.reader.read(bright)).lower()
                if "store permit" in text:
                    self.park()
                    return c, r
                break
        self.park()
        return None

    def try_here(self, permit, name):
        """Uses the permit where we're standing: "open" once the store's window shows, "taken" (a
        store is in the way), "portal" (too close to one), or "nothing" if nothing happened."""
        before = chat_count(self.screen.grab(), self.reader, PORTAL_MESSAGE)
        self.open_tab("Cash")
        self.controls.click(*self.screen.to_screen(*slot_center(self.inv, *permit)), double=True)
        self.park()
        end = time.monotonic() + DIALOG_WAIT
        while time.monotonic() < end:
            self.controls.wait(0.3)
            frame = self.screen.grab()
            box = message_box(frame, self.reader)
            if box and "name of your store" in box[0].lower():
                self.log(f"  the name box opened: naming the store {name!r}")
                self.controls.type_text(name)
                self.controls.wait(0.3)
                self.click(*box[1][0])  # OK
                self.park()
                return "open" if self.wait_for_store() else "nothing"
            if box and "open a store here" in box[0].lower():
                if box[1]:
                    self.click(*box[1][0])
                self.park()
                return "taken"
            if chat_count(frame, self.reader, PORTAL_MESSAGE) > before:
                return "portal"
        return "nothing"

    def wait_for_store(self):
        end = time.monotonic() + DIALOG_WAIT
        while time.monotonic() < end:
            frame = self.screen.grab()
            store = find_template(frame, self.store_tpl)
            if store and is_own_store(frame, store, self.reader):
                return True
            self.controls.wait(0.3)
        return False

    def place_store(self, name):
        """Walks along the floor trying spots until the store's set up, nearest first."""
        import walker
        frame = self.screen.grab()
        self.inv = self.find_inventory()
        permit = self.find_permit()
        if not permit:
            raise Stopped("no store permit found on the Cash tab")
        self.log(f"store permit found on the Cash tab, slot {permit[1] + 1}.{permit[0] + 1}")
        walk = walker.Walker(self.hwnd, self.controls, self.screen, self.reader)
        at = walk.dot()
        if at is None:
            raise Stopped("can't see your dot on the minimap: is it showing?")
        for spot in store_spots(at):
            if abs(spot - at) > 1:
                try:
                    walk.walk_to_point(spot)
                except walker.Lost as e:
                    self.log(f"  couldn't walk to {spot:+.0f} ({e}): trying the next spot")
                    continue
                at = walk.dot() or spot
            result = self.try_here(permit, name)
            if result == "nothing":  # the permit's double-click missed, or a chat message was: once more
                result = self.try_here(permit, name)
                if result == "nothing":
                    result = "nothing twice: taken as too close to a portal"
            self.log(f"  spot {at:+.0f}: {result}")
            if result == "open":
                return at
        raise Stopped(f"no spot in this room let the store open after {SPOT_TRIES} tries")

    def close_store(self):
        """CLOSE STORE and its OK. The unsold items go back to the inventory."""
        self.click(*at(self.store, *CLOSE_BUTTON))
        kind, found = self.wait_for_dialog()
        if kind != "message" or "close your store" not in found[0].lower() or not found[1]:
            raise Stopped(f"pressing CLOSE STORE didn't ask to close it ({found[0] if kind == 'message' else 'nothing showed'})")
        self.click(*found[1][0])
        self.park()
        self.controls.wait(1.0)
        if find_template(self.screen.grab(), self.store_tpl):
            raise Stopped("the store's window is still showing after closing it")

    def open_store(self):
        self.click(*at(self.store, *OPEN_BUTTON))
        kind, found = self.wait_for_dialog()
        if kind != "message" or "open" not in found[0].lower() or not found[1]:
            raise Stopped(f"pressing OPEN STORE didn't ask to open it ({found[0] if kind == 'message' else 'nothing showed'})")
        self.click(*found[1][0])
        self.park()
        self.controls.wait(0.8)
        if message_box(self.screen.grab(), self.reader):
            raise Stopped("a message is still showing after OK: check the game")

    def to_front(self):
        ctypes.windll.user32.SetForegroundWindow(self.hwnd)
        self.controls.ensure_front()


# ---------------------------------------------------------------- asking you

def ask(title, header, rows, columns, yes, no):
    """A window with a table: True if you press yes."""
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    answer = [False]
    tk.Label(root, text=header, justify="left", anchor="w", wraplength=900).pack(fill="x", padx=10, pady=(10, 6))
    table = ttk.Treeview(root, columns=[c for c, _ in columns], show="headings", height=min(max(len(rows), 3), 16))
    for c, width in columns:
        table.heading(c, text=c)
        table.column(c, width=width, stretch=c == columns[-1][0])
    for r in rows:
        table.insert("", "end", values=r)
    table.pack(fill="both", expand=True, padx=10)
    bar = ttk.Frame(root)
    bar.pack(fill="x", padx=10, pady=10)

    def done(value):
        answer[0] = value
        root.destroy()
    ttk.Button(bar, text=yes, command=lambda: done(True)).pack(side="left")
    ttk.Button(bar, text=no, command=lambda: done(False)).pack(side="left", padx=8)
    root.protocol("WM_DELETE_WINDOW", lambda: done(False))
    root.mainloop()
    return answer[0]


def money(v):
    return f"{v:,.0f}" if v else "-"


def part(value_weight):
    """A pricing.reference() part: "320 (x30)", its value and weight."""
    value, weight = value_weight or (None, 0)
    return f"{value:,.0f} (x{weight})" if value else "-"


def plan_rows(plan):
    return [(p["name"], money(p["price"]) if p["price"] else "not listed", part(p["parts"]["MeowDB"]), money(p["parts"]["asks"]),
             money(p["parts"]["sold"]), money(p["paid"]), money(p["floor"]),
             money(p["price"] * (1 - crawler.SHOP_FEE) - p["paid"]) if p["price"] and p["paid"] else "-", p["flag"]) for p in plan]


PLAN_COLUMNS = [("Item", 170), ("Price each", 80), ("MeowDB", 70), ("Asked", 70), ("Sold", 70), ("Paid", 60),
                ("Floor", 60), ("Profit each", 75), ("Note", 380)]


# ---------------------------------------------------------------- running it

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="read the inventory and price it, but place nothing")
    ap.add_argument("--window", default="maplestory.exe")
    ap.add_argument("--match", type=int, default=88, help="item name match strictness 0-100")
    ap.add_argument("--place", metavar="NAME",
                    help="find a spot on this floor and set the store up with this name first (store permit on the Cash tab)")
    ap.add_argument("--item", action="append", help="a test: list this item instead of the custom list (any number of times)")
    ap.add_argument("--assume-paid-under", type=float, metavar="PERCENT",
                    help="a test: take every item as bought this many %% under its price's reference")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True, errors="replace")
    bot = StoreBot(args)
    held = holdings.load()
    custom = {i["name"] for i in custom_items.load() if i["store"] and not i["only_one"] and not i.get("finder")}
    wanted = set(args.item) if args.item else set(held) | custom
    try:
        if not wanted:
            raise Stopped("nothing to sell: no store deals held, and no custom list item set to sell in your store")
        bot.to_front()
        if args.place:
            spot = bot.place_store(args.place)
            bot.log(f"store {args.place!r} set up at {spot:+.0f} on the minimap")
        bot.windows()
        before = bot.read_store()
        bot.log(f"store window found; {len(before)} listings in it already")
        bot.sync_first()
        if not args.item:
            bot.log(f"to list, as found: {', '.join(sorted(wanted)) or 'nothing'} (store deals held, and custom items set to sell)")
        if args.item:
            bot.log(f"test: listing {', '.join(sorted(wanted))} instead of the custom list")
        if args.assume_paid_under is not None:
            bot.log(f"test: taking everything as bought {args.assume_paid_under:g}% under its reference price")
        priced, rows = bot.fill(wanted, before, args.dry_run)
        if not args.item and not args.dry_run:
            bot.forget_gone(held, rows)
        if args.dry_run:
            ask("Store: dry run", "Dry run: this is what would be listed. Nothing was placed.", plan_rows(priced), PLAN_COLUMNS, "OK", "Close")
            return
        if len(rows) == len(before):
            raise Stopped("nothing was placed: " + ("none of the items to list are in the inventory tabs showing" if not priced
                                                    else "see the notes above"))
        shown = [(r["name"], f"{r['bundle']} for {r['price']:,}") for r in bot.read_store()]
        if ask("Store: open it?", "What the store holds now. Open it?", shown, [("Item", 220), ("Listing", 200)], "Open the store", "Leave it closed"):
            bot.to_front()
            bot.windows()
            bot.open_store()
            bot.log("store opened")
        else:
            bot.log("left closed: open it yourself, or close it to take everything back")
    except (Stopped, crawler.Stop) as e:
        bot.log(f"stopped: {e}")
    finally:
        bot.controls.stop.set()


if __name__ == "__main__":
    main()
