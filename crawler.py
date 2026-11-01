"""Free Market crawler for MapleStory Classic.

Opens every player store visible on screen and BUYS single items priced below what an NPC
pays, within a session budget and the profit rules in crawler_settings.json. Before each
purchase it reads the row a second time; after it, it checks your mesos went down by
exactly what was expected, and stops if not. After the room it changes channel with the
keyboard (Esc -> Enter -> arrow keys) and repeats, from --channels.

    Hold ESC for 1 second at any time to stop it.

It moves the real mouse and presses real keys, so the game must stay in front. It pauses
by itself if another window comes to the front.

Usage:
    python crawler.py --dry-run          # look only: show which shops it would open
    python crawler.py                    # asks auto/confirm, NPC only or not, and the budget, crawls this room, then channels 40..1
    python crawler.py --confirm          # you press OK on every purchase window
    python crawler.py --npc-only         # only NPC deals: no market prices, no market deals
    python crawler.py --budget 250k --channels 12-1 --minutes 20
    python crawler.py --no-channel-change
"""
import argparse
import csv
import ctypes
import json
import math
import ctypes.wintypes as wt
import os
import re
import signal
import sys
import threading
import time
import winsound
from collections import Counter
from datetime import datetime
from pathlib import Path

import cv2
import mss
import numpy as np

import custom_items
import market
import price_checker as pc

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
SETTINGS_FILE = HERE / "crawler_settings.json"
LOG_DIR = HERE / "logs"
user32 = ctypes.windll.user32

# All positions below are in pixels of a 2560x1440 game screen and get multiplied by the
# UI scale (window height / 1440, or what the store-button match found).
SIGN_CLICK = (60, -40)              # from a shop sign's coin icon to the sign's text
SIGN_THRESHOLD = 0.7
BUY_ITEM = (75, 16)                 # from the store buttons' top-left (StoreFinder)
LEAVE_STORE = (75, 55)
MESOS_BOX = (-20, 82, 150, 108)     # your mesos, under the seller (x0, y0, x1, y1 from the buttons)
QUANTITY_OK = (320, 149)            # OK button, from the quantity dialog's matched title
TOTAL_LINE = (-20, -34, 290, -4)    # "The total price is X mesos." above "Will you make the purchase?"
TOTAL_OK = (228, 100)
SINGLE_OK = (265, 112)              # OK in "Are you sure you want to buy it?", from its matched text
# the game's notice when the quantity is more than it allows: "You may only enter a number
# equal to or lower than 199." (OCR may run words together, so spaces are optional)
QUANTITY_REFUSED_RE = re.compile(r"only\s*enter\s*a\s*number.*?lower\s*than\s*([\d,.]+)", re.I)
TOTAL_RE = re.compile(r"total\s*price\s*is\s*([\d,.]+)\s*meso", re.I)
# The game draws its own cursor, so it shows up in screenshots: left on the store buttons, the
# store stops being recognised. After every click it goes here (fractions of the game window):
# the chat bar at the bottom, where no shop sign, store or dialog is ever looked for.
PARK_AT = (0.35, 0.93)
STOCK_VOTES = 3                     # of the 12 reads of a stock number, how many must agree
LIST_ROWS = 6                      # rows showing in a store's list
SAME_PAGES_AT_END = 2              # scrolls that change nothing before it counts as the end of the list
MAX_BUYS_PER_LISTING = 3            # a stack whose stock was read low gets topped up, at most this often
FROZEN_AFTER = 3.0                  # seconds of identical screen captures before warning that capture is stuck
SHOP_FEE = 0.03                     # a player shop keeps 3% of each sale ("will be deducted from sales")
MARKET_CONFIRM_WAIT = 120.0        # seconds the purchase window waits for you on a market deal
CUSTOM_START_WAIT = 5.0            # seconds per custom list item at startup, for its price to arrive
CUSTOM_PRICE_WAIT = 10.0           # seconds a store stays open for a custom search item's price to arrive
REVIEW_PAUSE = 5.0                 # seconds a store stays open when it lists a review / custom search item
SHOP_OPEN_WAIT = 6.0               # seconds to wait for a store to open after double-clicking its sign
NO_SHOPS_WAIT = 10.0              # seconds to wait for shops after a channel change before moving on
CH1_CELL = (64, 128)               # from the "CHANGE CHANNEL" title to CH.1's centre
CH_STEP = (131, 41)                 # cell spacing; 8 channels per row
CH_HIGHLIGHT_PATCH = (-45, -30, 6)  # x range and +-y around a cell centre to read its colour

VK = {"esc": 0x1B, "enter": 0x0D, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
      "backspace": 0x08, "end": 0x23, "alt": 0x12, "ctrl": 0x11, "shift": 0x10, "space": 0x20,
      **{str(d): 0x30 + d for d in range(10)}}
EXTENDED = {"left", "up", "right", "down", "end"}


class Stop(Exception):
    pass


# ---------------------------------------------------------------- input (SendInput)

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def _send(inp):
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        raise Stop("Windows blocked the input")


def runs_with_more_rights(hwnd):
    """True if the window's program is elevated (admin) and we aren't. Windows then drops
    our input to it without any error, so it has to be checked up front."""
    if ctypes.windll.shell32.IsUserAnAdmin():
        return False
    kernel32, advapi32 = ctypes.windll.kernel32, ctypes.windll.advapi32
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not proc:
        return True
    try:
        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(proc, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return True  # not even allowed to look: it has more rights than us
        try:
            elevated, size = wt.DWORD(), wt.DWORD()
            advapi32.GetTokenInformation(token, 20, ctypes.byref(elevated), 4, ctypes.byref(size))  # TokenElevation
            return bool(elevated.value)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(proc)


class Controls:
    """Mouse and keyboard, with the safety checks in front of every action:
    ESC-hold stop, session time limit, and 'only while the game is in front'."""

    def __init__(self, hwnd, deadline):
        self.hwnd = hwnd
        self.deadline = deadline
        self.stop = threading.Event()
        self.stop_reason = ""
        threading.Thread(target=self._watch_esc, daemon=True).start()

    def _watch_esc(self):
        held_since = None
        while not self.stop.is_set():
            if user32.GetAsyncKeyState(VK["esc"]) & 0x8000:
                held_since = held_since or time.monotonic()
                if time.monotonic() - held_since >= 1.0:  # our own Esc taps last ~0.05s
                    self.stop_reason = "ESC held"
                    self.stop.set()
            else:
                held_since = None
            time.sleep(0.05)

    def check(self):
        if self.stop.is_set():
            raise Stop(self.stop_reason)
        if time.monotonic() > self.deadline:
            raise Stop("session time limit reached")

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check()
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))

    def ensure_front(self):
        told = False
        while user32.GetForegroundWindow() != self.hwnd:
            if not told:
                print("  paused: bring MapleStory to the front to continue")
                told = True
            self.wait(0.2)
        if told:
            print("  resumed")
            self.wait(0.5)

    def move(self, x, y):
        """Glide the cursor to screen pixel (x, y) instead of jumping there."""
        self.ensure_front()
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        dist = ((x - pt.x) ** 2 + (y - pt.y) ** 2) ** 0.5
        if dist < 3:
            return
        steps = max(8, int(dist / 25))
        duration = 0.12 + dist / 4000
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)  # ease in and out
            cx, cy = pt.x + (x - pt.x) * t, pt.y + (y - pt.y) * t
            mi = MOUSEINPUT(int((cx - vx) * 65535 / (vw - 1)), int((cy - vy) * 65535 / (vh - 1)),
                            0, 0x0001 | 0x8000 | 0x4000, 0, 0)  # move | absolute | virtual desktop
            _send(INPUT(0, _INPUTUNION(mi=mi)))
            self.wait(duration / steps)

    def click(self, x, y, double=False):
        self.move(x, y)
        self.wait(0.08)
        for i in range(2 if double else 1):
            self.check()
            _send(INPUT(0, _INPUTUNION(mi=MOUSEINPUT(0, 0, 0, 0x0002, 0, 0))))  # left down
            time.sleep(0.05)
            _send(INPUT(0, _INPUTUNION(mi=MOUSEINPUT(0, 0, 0, 0x0004, 0, 0))))  # left up
            if double and i == 0:
                time.sleep(0.08)

    def wheel(self, x, y, notches=-1):
        """Scroll the mouse wheel over screen pixel (x, y); negative = down."""
        self.move(x, y)
        for _ in range(abs(notches)):
            self.check()
            delta = (120 if notches > 0 else -120) & 0xFFFFFFFF  # WHEEL_DELTA, as a DWORD
            _send(INPUT(0, _INPUTUNION(mi=MOUSEINPUT(0, 0, delta, 0x0800, 0, 0))))  # MOUSEEVENTF_WHEEL
            self.wait(0.1)

    def key(self, name, times=1):
        vk = VK[name]
        scan = user32.MapVirtualKeyW(vk, 0)
        ext = 0x0001 if name in EXTENDED else 0
        for _ in range(times):
            self.ensure_front()
            _send(INPUT(1, _INPUTUNION(ki=KEYBDINPUT(vk, scan, 0x0008 | ext, 0, 0))))  # scancode down
            time.sleep(0.05)
            _send(INPUT(1, _INPUTUNION(ki=KEYBDINPUT(vk, scan, 0x0008 | ext | 0x0002, 0, 0))))
            self.wait(0.12)

    def key_down(self, name):
        self.ensure_front()
        ext = 0x0001 if name in EXTENDED else 0
        _send(INPUT(1, _INPUTUNION(ki=KEYBDINPUT(VK[name], user32.MapVirtualKeyW(VK[name], 0), 0x0008 | ext, 0, 0))))

    def key_up(self, name):
        ext = 0x0001 if name in EXTENDED else 0
        _send(INPUT(1, _INPUTUNION(ki=KEYBDINPUT(VK[name], user32.MapVirtualKeyW(VK[name], 0), 0x0008 | ext | 0x0002, 0, 0))))

    def hold(self, name, seconds):
        """Hold a key down for this long (let go again even if the crawler is stopped meanwhile)."""
        self.key_down(name)
        try:
            self.wait(seconds)
        finally:
            self.key_up(name)

    def type_number(self, n):
        for d in str(n):
            self.key(d)


# ---------------------------------------------------------------- screen

class Screen:
    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.sct = getattr(mss, "MSS", mss.mss)()
        self.region = pc.client_rect(hwnd)  # the game picture on screen, without black bars
        self.bars = None
        self.last_small = None
        self.unchanged_since = time.monotonic()
        self.warned_frozen = False

    def grab(self):
        window = pc.client_rect(self.hwnd)
        self.raw = np.ascontiguousarray(np.array(self.sct.grab(window))[:, :, :3])  # with any black bars
        frame, self.region, self.bars = pc.trim_bars(self.raw, window, self.bars)
        self.check_frozen(frame)
        self.frame = np.ascontiguousarray(frame)  # the latest picture, for marked screenshots
        return self.frame

    def check_frozen(self, frame):
        """The game picture always moves a little (characters, effects, chat). If captures stay
        exactly the same for FROZEN_AFTER seconds, Windows is handing out an old picture, which
        happens with borderless full screen and "fullscreen optimizations": warn once."""
        small = frame[::16, ::16].copy()
        if self.last_small is None or small.shape != self.last_small.shape or (small != self.last_small).any():
            self.last_small = small
            self.unchanged_since = time.monotonic()
            return
        if not self.warned_frozen and time.monotonic() - self.unchanged_since > FROZEN_AFTER:
            self.warned_frozen = True
            print("\n  WARNING: the screen capture of MapleStory isn't updating, so the crawler is working from\n"
                  "  an old picture. This happens in full screen mode. Fix: right-click MapleStory.exe >\n"
                  "  Properties > Compatibility > tick 'Disable fullscreen optimizations', restart the game.\n"
                  "  Or play in a (large) window instead of full screen.\n")

    def to_screen(self, x, y):
        return int(self.region["left"] + x), int(self.region["top"] + y)


# ---------------------------------------------------------------- what to show on screen

SHOP = "#5070ff"        # shop signs found in the room (blue; cyan is your custom search)
TARGET = "#ff40ff"      # the shop / channel being worked on
FOUND = pc.STATUS_COLOR  # store window, dialogs, channel window, status lines
SEEN = "#a0a0a0"        # any other text OCR read
SKIPPED = "#ff8c00"     # a shop skipped because of its name
LISTED = pc.LISTED_COLOR  # a recognised single-item listing
MARKET = pc.MARKET_COLOR  # a listing far below its Free Market price
DEAL = pc.NPC_DEAL_COLOR  # a listing that passes the buy rules (NPC deal)
REVIEW = pc.REVIEW_COLOR  # an item on the review_items list: worth checking its stats yourself
CUSTOM = pc.CUSTOM_COLOR  # an item on your custom list
WARN = pc.WARN_COLOR    # something went wrong or looks off
READING = "#4da6ff"     # a spot read for a number (stock, mesos, total)
CLICK = "#ffffff"


class Marks:
    """What the crawler sees and does, for the overlay to draw. The crawler runs on a worker
    thread and the overlay on the main one, so everything goes through a lock.

    Each layer is a list of ("box", (x0, y0, x1, y1), color, label) or
    ("cross", (x, y), color, label), in game-window pixels."""

    def __init__(self):
        self.lock = threading.Lock()
        self.layers = {}  # name -> (items, expires at or None)
        self.action = "starting"
        self.info = ""

    def set(self, layer, items, ttl=None):
        items = tuple((kind, tuple(int(v) for v in where), color, label) for kind, where, color, label in items)
        with self.lock:
            self.layers[layer] = (items, time.monotonic() + ttl if ttl else None)

    def clear(self, *layers):
        with self.lock:
            for layer in layers:
                self.layers.pop(layer, None)

    def doing(self, action):
        with self.lock:
            self.action = action

    def tell(self, info):
        with self.lock:
            self.info = info

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            items = tuple(i for items, expires in self.layers.values() if expires is None or expires > now
                          for i in items)
            return self.action, self.info, items


def box(x0, y0, x1, y1, color, label="", below=False):
    """below: put the label under the box instead of to its right (where another label may be)."""
    return ("box_below" if below else "box", (x0, y0, x1, y1), color, label)


class CrawlOverlay(pc.Overlay):
    def draw_marks(self, region, marks):
        action, info, items = marks.snapshot()
        if not self.begin(region, (action, info, items)):
            return
        c = self.canvas
        self.text(8, 8, f"crawler: {action}", FOUND)
        if info:
            self.text(8, 28, info, FOUND)
        x0, y0, x1, y1 = c.bbox("all")  # dark strip behind the status, so it reads over the minimap
        c.tag_lower(c.create_rectangle(x0 - 6, y0 - 4, x1 + 6, y1 + 4, fill="#101010", outline=""))
        for kind, where, color, label in items:
            if kind == "cross":
                x, y = where
                c.create_line(x - 12, y, x + 13, y, fill=color, width=2)
                c.create_line(x, y - 12, x, y + 13, fill=color, width=2)
                c.create_oval(x - 6, y - 6, x + 6, y + 6, outline=color, width=2)
                lx, ly = x + 14, y - 14
            else:
                x0, y0, x1, y1 = where
                c.create_rectangle(x0, y0, x1, y1, outline=color, width=1 if color == SEEN else 2)
                lx, ly = (x0, y1 + 10) if kind == "box_below" else (x1 + 6, (y0 + y1) / 2)
            if label:
                self.text(lx, ly, label, color, anchor="w")


def load_template(name, color=False):
    img = cv2.imread(str(TEMPLATES / name), cv2.IMREAD_COLOR if color else cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise SystemExit(f"missing template: templates/{name}")
    return img


def scaled(tpl, s):
    return tpl if abs(s - 1) < 1e-3 else cv2.resize(tpl, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)


def best_match(img, tpl, scales):
    best = (0.0, None, None)
    for s in scales:
        t = scaled(tpl, s)
        if t.shape[0] > img.shape[0] or t.shape[1] > img.shape[1]:
            continue
        _, v, _, loc = cv2.minMaxLoc(cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED))
        if v > best[0]:
            best = (v, loc, s)
    return best


def fine_match(gray, tpl, center, spread=0.08):
    """best_match for scales within +-spread (relative) of center. Text-only templates (dialog
    titles) only match within about 0.5% of their true size, which at a small UI scale is far
    finer than 0.01, so this tries 1% steps and then refines the best one in 0.2% steps."""
    n = round(spread * 100)
    best = best_match(gray, tpl, [center * (1 + k / 100) for k in range(-n, n + 1)])
    if best[2] is None:
        return best
    return max(best, best_match(gray, tpl, [best[2] * (1 + k / 500) for k in range(-5, 6)]), key=lambda b: b[0])


def eights_as_zeros(text):
    """The stock digits 0 and 8 look alike and get mixed up (100 read as 180), and reading a
    stack too high makes the game refuse the purchase. So a read counts for itself and for every
    version of it with some of its 8s as 0s: '180' also votes for 100. With the lowest agreed
    value winning, a mix-up can only ever make it buy too few."""
    options = {""}
    for d in text:
        options = {o + c for o in options for c in (("8", "0") if d == "8" else (d,))}
    return {int(o) for o in options if not o.startswith("0")}  # a stock number never starts with 0


def near(s, spread=0.03, step=0.01):
    return sorted({round(s + d, 2) for d in np.arange(-spread, spread + 1e-9, step)}, key=lambda v: abs(v - s))


# ---------------------------------------------------------------- the crawler

class Crawler:
    def __init__(self, args):
        self.args = args
        found = pc.find_window(args.window)
        if not found:
            raise SystemExit(f"{args.window} isn't open (or is minimised)")
        self.hwnd = found[0]
        self.screen = Screen(self.hwnd)
        self.db = pc.PriceDB(pc.PRICES_FILE, args.match)
        self.reader = pc.Reader(1.0)
        self.finder = pc.StoreFinder()
        self.coins = [load_template(n, color=True)
                      for n in ("shop_coin.png", "shop_coin_stall.png", "shop_coin_wood.png")]
        self.channel_title = load_template("channel_title.png")
        self.quantity_dialog = load_template("buy_quantity.png")
        self.total_dialog = load_template("buy_total.png")
        self.single_dialog = load_template("buy_single.png")
        self.dialog_scale = None  # UI scale the dialog templates matched at, once one has
        self.dialog_scores = {}   # template name -> (best score, scale) of its last search
        self.ui_scale = None
        self.controls = None
        LOG_DIR.mkdir(exist_ok=True)
        self.log_path = LOG_DIR / f"crawl_{datetime.now():%Y-%m-%d}.csv"
        settings = json.loads(SETTINGS_FILE.read_text("utf-8"))
        self.buy_rules = sorted((r["price_from"], r["min_profit"]) for r in settings["buy_rules"])
        self.never_buy = [w.lower() for w in settings.get("never_buy", [])]
        self.min_total_profit = settings.get("min_total_profit", 50)
        big_buy = settings.get("big_buy", {})
        self.big_buy_percent = big_buy.get("over_budget_percent", 25)
        self.big_buy_flip = big_buy.get("min_flip_percent", 5)
        self.skip_shops_with = [pc.norm(w) for w in settings.get("skip_shops_with", []) if pc.norm(w)]
        market_cfg = market.load_settings()
        self.market_below = market_cfg.get("below_24h_median_percent", 30)
        self.market_min_reports = market_cfg.get("min_reports_24h", 5)
        self.market_auto = set(market_cfg.get("auto_buy", []))  # market deals bought without asking you
        self.market = market.Live(enabled=False)  # Free Market prices, after load_market()
        self.channel_refused = False  # the last channel change didn't take: the channel may be full
        self.sell_below = None  # stop opening shops once under this percent of the budget is left
        self.confirm_all = False  # every purchase waits on its last window for you to press OK
        self.market_deals = True  # False: NPC deals only (prices may still be loaded for custom_percent)
        self.buy_custom = False  # buy custom list items, each by its own rules
        self.book = custom_items.Book()
        self.watch = pc.Watchlists(pc.load_custom_items(self.db), pc.load_review_items())
        self.budget = 0
        self.spent = 0
        self.bought = []  # (name, quantity, paid, value, "npc" or "market")
        self.custom_log = None  # the full sell loop's list of custom items bought
        self.custom_started = None
        self.custom_bought = []  # (time, name, quantity, paid, where, how)
        self.marks = Marks()

    def required_profit(self, asking):
        """Minimum profit per item the buy rules ask for at this price."""
        need = None
        for price_from, min_profit in self.buy_rules:
            if asking >= price_from:
                need = min_profit
        return need  # None: below every rule, never buy

    def profit_each(self, hit, market=False):
        """Profit per item: what an NPC pays minus the price, or for a market deal, what selling
        it at today's 24h median brings after the shop fee, minus the price (an estimate)."""
        if market:
            return int(self.market.lowest(hit.names)["median_24h"] * (1 - SHOP_FEE)) - hit.asking
        return hit.profit

    def worth_buying(self, hit):
        if self.watch.kind(hit) == "custom":  # bought by its own rules, and never sold to make the profit
            return False
        if any(w in n.lower() for n in hit.names for w in self.never_buy):
            return False
        need = self.required_profit(hit.asking)
        return need is not None and hit.profit >= need

    def money_low(self):
        return self.sell_below is not None and self.budget - self.spent < self.budget * self.sell_below / 100

    def custom_entries(self, hit):
        """The custom list items this listing is; None if it could also be something not on the
        list (a cut-off name): what's bought has to be safe from being sold at the NPC."""
        entries = [self.book.get(n) for n in hit.names]
        return entries if entries and all(entries) else None

    def custom_top_price(self, entries, hit):
        """The most to pay, by the strictest of the items it could be; None if that can't be said:
        a median needs enough reports behind it, and to be under a day old."""
        tops = []
        for item in entries:
            if item["when"] == "price":
                tops.append(item["price"])
                continue
            if not self.market.fresh(hit.names):
                return None
            percent = item["percent"] if item["when"] == "under" else -item["percent"]
            top = market.deal_limit(self.market.lowest(hit.names), percent, self.market_min_reports)
            if top is None:
                return None
            tops.append(top)
        return min(tops)

    def custom_left(self, hit):
        """How many more of this listing's item may be bought; None: no limit."""
        counts = [n for n in map(custom_items.left, self.custom_entries(hit) or ()) if n is not None]
        return min(counts) if counts else None

    def custom_buy(self, hit):
        """Whether to buy this listing, by its own item's rules: its limit, then its price."""
        entries = self.buy_custom and self.custom_entries(hit)
        if not entries or self.custom_left(hit) == 0:
            return False
        top = self.custom_top_price(entries, hit)
        return top is not None and hit.asking <= top

    def custom_needs_price(self, hit):
        """Whether this listing's item goes by the market, with buying left to do."""
        entries = self.buy_custom and self.custom_entries(hit)
        return bool(entries) and self.custom_left(hit) != 0 and any(i["when"] != "price" for i in entries)

    def too_thin_for_budget(self, deal, quantity):
        """Whether an NPC deal ties up too much of the session budget for its profit: it costs
        more than over_budget_percent of the budget, and earns under min_flip_percent of its price."""
        return (quantity * deal.asking * 100 > self.budget * self.big_buy_percent
                and deal.profit * 100 < deal.asking * self.big_buy_flip)

    def load_market(self):
        """Ask about and load the Free Market price dump (see market.py)."""
        self.market = market.prepare(self.db.ids)

    def update_custom_prices(self):
        """The latest prices of everything on the custom list still to be bought, before the first
        shop opens: one that turns up first thing shouldn't be missed for a price still on its way."""
        names = []
        for item in self.book.items.values():
            known = self.db.items.get(pc.norm(item["name"]))
            if known and custom_items.left(item) != 0:
                names.append(known[0])  # as MeowDB's lookups and the listings spell it
        if not names or not self.market:
            return
        print(f"  custom list: getting the latest prices of {len(names)} items from MeowDB...")
        self.market.update_now(names)
        end = time.monotonic() + CUSTOM_START_WAIT * len(names)
        while self.market.pending(names) and time.monotonic() < end and not stop_requested():
            time.sleep(0.25)
        late = [n for n in names if self.market.pending([n])]
        print(f"  custom list: still waiting on {', '.join(late)}; they may be late" if late else "  custom list: prices are up to date")

    def market_limit(self, hit):
        """The highest asking price that's a market deal for this item, or None when there's no
        trustworthy price: a deal is below_24h_median_percent under the median price of the last
        24 hours, and that median has to come from at least min_reports_24h reports."""
        return market.deal_limit(self.market.lowest(hit.names), self.market_below, self.market_min_reports)

    def market_deal(self, hit):
        limit = self.market_limit(hit)
        return (self.market_deals and limit is not None and hit.asking <= limit and self.watch.kind(hit) != "custom"
                and not any(w in n.lower() for n in hit.names for w in self.never_buy))

    # ---- helpers

    def scale_guess(self, frame):
        return self.ui_scale or self.finder.last_scale or round(frame.shape[0] / 1440, 2)

    def at(self, origin, offset, s):
        return origin[0] + offset[0] * s, origin[1] + offset[1] * s

    def click_frame(self, x, y, double=False):
        self.marks.set("click", [("cross", (x, y), CLICK, "double-click" if double else "click")], ttl=1.5)
        self.controls.click(*self.screen.to_screen(x, y), double=double)
        if self.args.debug:
            self.save_click(x, y)
        self.park_mouse()

    def save_click(self, x, y):
        """debug/clicks/NN.png: the screen right after a click, with where it aimed (red) and
        where Windows says the cursor actually is (green). If they differ, input is misplaced."""
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        cx, cy = pt.x - self.screen.region["left"], pt.y - self.screen.region["top"]
        img = self.screen.grab().copy()
        cv2.drawMarker(img, (int(x), int(y)), (0, 0, 255), cv2.MARKER_CROSS, 40, 2)
        cv2.drawMarker(img, (int(cx), int(cy)), (0, 255, 0), cv2.MARKER_TILTED_CROSS, 30, 2)
        cv2.putText(img, f"{self.marks.action} | aimed ({x:.0f},{y:.0f}) cursor ({cx},{cy}) | "
                    f"window {self.screen.region}", (10, img.shape[0] - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        folder = pc.DEBUG_DIR / "clicks"
        folder.mkdir(parents=True, exist_ok=True)
        self.clicks_saved = getattr(self, "clicks_saved", 0) + 1
        cv2.imwrite(str(folder / f"{self.clicks_saved % 40:02d}.png"), img)  # keeps the last 40
        print(f"    [debug] click {self.clicks_saved}: aimed ({x:.0f},{y:.0f}), cursor at ({cx},{cy})")

    def park_mouse(self):
        """Move the cursor out of the way (see PARK_AT) and let the game redraw."""
        r = self.screen.region
        self.controls.move(*self.screen.to_screen(r["width"] * PARK_AT[0], r["height"] * PARK_AT[1]))
        self.controls.wait(0.15)

    def doing(self, action):
        self.marks.doing(action)

    def update_info(self):
        npc = [b for b in self.bought if b[4] == "npc"]
        mkt = [b for b in self.bought if b[4] == "market"]
        value, paid = sum(b[3] for b in npc), sum(b[2] for b in npc)
        info = (f"budget {self.budget:,} | spent {self.spent:,} | bought {sum(b[1] for b in self.bought)} items\n"
                f"profit {value - paid:+,} once sold to an NPC (worth {value:,})")
        if mkt:
            info += (f"\nmarket buys: {sum(b[1] for b in mkt)} items for {sum(b[2] for b in mkt):,}, "
                     f"~{sum(b[3] for b in mkt):,} at today's median")
        self.marks.tell(info)

    def mark_dialog(self, layer, found, tpl, label):
        """Box a matched dialog (found = ((x, y), scale)), or clear its mark when it's gone."""
        if not found:
            self.marks.clear(layer)
            return
        (x, y), s = found
        th, tw = tpl.shape[:2]
        self.marks.set(layer, [box(x, y, x + tw * s, y + th * s, FOUND, label)])

    def find_store(self, frame):
        """finder.find, also boxing the store window (or clearing what was marked in it once it's gone)."""
        found = self.finder.find(frame)
        if not found:
            self.marks.clear("store", "listings", "stock", "mesos", "recheck")
            return None
        (x, y), s = self.finder.last_loc, self.finder.last_scale
        th, tw = self.finder.tpl.shape[:2]
        self.marks.set("store", [box(x, y, x + tw * s, y + th * s, FOUND, "store buttons"),
                                 box(*found[0], FOUND)])
        return found

    def wait_for(self, test, timeout, interval=0.15):
        end = time.monotonic() + timeout
        while True:
            frame = self.screen.grab()
            result = test(frame)
            if result:
                return frame, result
            if time.monotonic() > end:
                return frame, None
            self.controls.wait(interval)

    def log(self, channel, hit, quantity, note):
        new = not self.log_path.exists()
        with open(self.log_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "channel", "item", "asking", "npc_pays", "profit_each", "quantity", "note"])
            w.writerow([f"{datetime.now():%H:%M:%S}", channel, hit.name, hit.asking, hit.npc_pays,
                        hit.profit, quantity, note])

    # ---- shops in the room

    def find_shops(self, frame):
        """(click point, coin icon box) of every shop sign on screen, top row first."""
        s = self.scale_guess(frame)
        if self.ui_scale is None:  # settle the UI scale once, on a clearly matching coin icon
            v, _, best = max((best_match(frame, c, near(s)) for c in self.coins), key=lambda b: b[0])
            if v >= 0.85:
                self.ui_scale = s = best
        h, w = frame.shape[:2]
        points = []
        for coin in self.coins:
            tpl = scaled(coin, s)
            res = cv2.matchTemplate(frame, tpl, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= SIGN_THRESHOLD)
            for x, y in sorted(zip(xs, ys), key=lambda p: -res[p[1], p[0]]):
                if all(abs(x - a) > 30 * s or abs(y - b) > 30 * s for a, b, _ in points):
                    points.append((int(x), int(y), tpl.shape[:2]))
        shops = []
        for x, y, (th, tw) in points:
            cx, cy = self.at((x, y), SIGN_CLICK, s)
            cy = max(cy, min(y, 12))  # a sign cut off at the top: click the strip of it still showing
            if 10 < cx < w - 10 and 10 < cy < h * 0.9:  # keep clear of the edges and the bottom bar
                shops.append(((cx, cy), (x, y, x + tw, y + th)))
        shops.sort(key=lambda p: (round(p[0][1] / (60 * s)), p[0][0]))
        self.marks.set("shops", [box(*coin, SHOP, str(i)) for i, (_, coin) in enumerate(shops, 1)])
        return shops

    def crawl_room(self, channel, max_shops=None):
        """Open the shops in this room, or only the first max_shops that open."""
        self.controls.ensure_front()  # a capture with another window on top would show no shops
        frame = self.screen.grab()
        if self.find_store(frame):
            print("  a store is already open, leaving it first")
            self.leave_store()
        self.doing("looking for shops")
        end = time.monotonic() + NO_SHOPS_WAIT  # the game may still be redrawing after you switched to it
        while True:
            self.controls.ensure_front()
            frame = self.screen.grab()
            shops = self.find_shops(frame)
            if shops or time.monotonic() > end:
                break
            self.controls.wait(0.5)
        print(f"[channel {channel}] {len(shops)} shops on screen")
        names = self.read_shop_names(frame, shops)
        opened_shops = 0
        for i, ((x, y), coin) in enumerate(shops, 1):
            if self.money_low():
                print("  low on budget: leaving the rest of this room")
                break
            if max_shops is not None and opened_shops >= max_shops:
                print(f"  checked {opened_shops} shops: that's enough for this room")
                break
            self.controls.check()
            self.handle_late_store(channel)
            skip = next((w for w in self.skip_shops_with if w in pc.norm(names[i - 1])), None)
            if skip:
                print(f"  shop {i}/{len(shops)} '{names[i - 1]}': name has '{skip}'; skipped")
                continue
            print(f"  shop {i}/{len(shops)} '{names[i - 1]}'")
            self.marks.set("target", [box(*coin, TARGET, f"shop {i}/{len(shops)}")])
            self.doing(f"opening shop {i}/{len(shops)}")
            self.click_frame(x, y, double=True)
            self.doing(f"waiting for shop {i}/{len(shops)} to open")
            started = time.monotonic()
            _, opened = self.wait_for(self.find_store, SHOP_OPEN_WAIT)
            if not opened:
                print(f"    didn't open within {SHOP_OPEN_WAIT:.0f}s (full, closed, or missed); skipping")
                continue
            print(f"    opened after {time.monotonic() - started:.1f}s")
            opened_shops += 1
            self.controls.wait(0.4)  # let the list draw
            self.read_store(channel)
            self.leave_store()
        self.handle_late_store(channel)
        self.marks.clear("target")

    def read_shop_names(self, frame, shops):
        """The text on each shop's sign (sitting above its coin icon), for skipping shops by
        name before opening them. Shops to skip are boxed orange on the overlay, with a red box
        where the skip word was read; --debug also saves all of it to debug/shop_names.png."""
        if not self.skip_shops_with:
            return [""] * len(shops)
        self.doing(f"reading the names of {len(shops)} shops")
        s = self.scale_guess(frame)
        zoom = 1.5 / s
        # a skip word matches with anything but letters/digits between its letters ("N X", "N.X")
        patterns = [(w, re.compile(r"[^a-z0-9]*".join(map(re.escape, w)), re.I)) for w in self.skip_shops_with]
        names, marks, areas = [], [], []
        for i, (_, (x0, y0, x1, y1)) in enumerate(shops, 1):
            nx0, ny0, nx1, ny1 = max(0, x0 - 12 * s), max(0, y0 - 78 * s), x0 + 210 * s, y0 - 4 * s
            crop = frame[int(ny0):int(ny1), int(nx0):int(nx1)]
            lines = []
            if crop.size:
                big = cv2.resize(crop, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_CUBIC)
                lines = sorted(self.reader.detect(big), key=lambda d: d[0][0][1])  # top line first
            name = " ".join(t for _, t, _ in lines)
            names.append(name)
            areas.append(((nx0, ny0, nx1, ny1), name))
            hits = []
            for word, pattern in patterns:
                for quad, text, _ in lines:
                    for m in pattern.finditer(text):
                        # OCR boxes whole lines, so place the letters by their position in the line
                        lx0, ly0 = min(p[0] for p in quad), min(p[1] for p in quad)
                        lx1, ly1 = max(p[0] for p in quad), max(p[1] for p in quad)
                        per_char = (lx1 - lx0) / max(1, len(text))
                        hits.append(box(nx0 + (lx0 + m.start() * per_char) / zoom - 2, ny0 + ly0 / zoom - 2,
                                        nx0 + (lx0 + m.end() * per_char) / zoom + 2, ny0 + ly1 / zoom + 2,
                                        WARN, word))
            if hits:
                marks.append(box(nx0, ny0, nx1, y1, SKIPPED, f"{i} skip", below=True))
                marks.extend(hits)
            else:
                marks.append(box(x0, y0, x1, y1, SHOP, str(i)))
        self.marks.set("shops", marks)
        if self.args.debug:
            self.save_shop_names(frame, areas, marks)
        return names

    def save_shop_names(self, frame, areas, marks):
        """debug/shop_names.png: every sign area read (grey) with the text read there, skipped
        shops (orange) and where the skip word was seen (red)."""
        img = frame.copy()
        bgr = lambda h: (int(h[5:7], 16), int(h[3:5], 16), int(h[1:3], 16))
        for (x0, y0, x1, y1), name in areas:
            cv2.rectangle(img, (int(x0), int(y0)), (int(x1), int(y1)), (160, 160, 160), 1)
            cv2.putText(img, name.encode("ascii", "replace").decode() or "(nothing read)",
                        (int(x0), int(y0) - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 0), 1)
        for _, where, color, label in marks:
            if color in (SKIPPED, WARN):
                x0, y0, x1, y1 = (int(v) for v in where)
                cv2.rectangle(img, (x0, y0), (x1, y1), bgr(color), 2)
                cv2.putText(img, label, (x0, y1 + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, bgr(color), 2)
        pc.DEBUG_DIR.mkdir(exist_ok=True)
        cv2.imwrite(str(pc.DEBUG_DIR / "shop_names.png"), img)
        print(f"    [debug] shop names read: {[a[1] for a in areas]} (debug\\shop_names.png)")

    def handle_late_store(self, channel):
        """A store can open after the bot stopped waiting for it. Clicking on to the next shop
        through it would go wrong, so read it and leave it first."""
        if self.find_store(self.screen.grab()):
            print("    a store opened late; reading it and leaving before going on")
            self.controls.wait(0.4)
            self.read_store(channel)
            self.leave_store()

    # ---- inside a store

    def read_store(self, channel):
        # rows finished with: skipped, failed, or bought MAX_BUYS_PER_LISTING times. By where they are
        # too, as a store can have several rows of one item at one price, and with stacks of any size
        done = set()
        page_number = 0
        same_pages = 0
        reviewed = set()  # review / custom search listings already pointed out in this store
        buys = Counter()
        last_page = None
        for _ in range(200):  # pages and purchases; a store needs far fewer
            frame = self.screen.grab()
            found = self.find_store(frame)
            if not found:
                return
            (lx0, ly0, lx1, ly1), _ = found  # the item list
            row_height = (ly1 - ly0) / LIST_ROWS

            def row_key(hit):
                return hit.name, hit.asking, page_number, round((hit.box[1] - ly0) / row_height)

            self.doing("reading the store's listings")
            lines, _, singles, _ = pc.scan(frame, self.reader, self.finder, self.db, 1, False)
            self.mark_listings(lines, singles)
            self.log_page(channel, page_number, lines, singles, done, row_key)
            # a custom list item isn't missed for its price being old or still on its way
            priced = [h for h in singles if self.custom_needs_price(h)]
            if priced:
                for h in priced:
                    self.market.refresh(h.names)
                    self.market.lowest(h.names)
                pending = [h for h in priced if self.market.pending(h.names)]
                if pending:
                    self.doing(f"custom search: getting the market price of {', '.join(h.name for h in pending)}")
                    end = time.monotonic() + CUSTOM_PRICE_WAIT
                    while time.monotonic() < end and any(self.market.pending(h.names) for h in pending):
                        self.controls.wait(0.25)
                    self.mark_listings(lines, singles)
            customs = [d for d in singles if self.custom_buy(d) and row_key(d) not in done]
            new = [h for h in singles if self.watch.kind(h) and (h.name, h.asking) not in reviewed]
            if new:
                reviewed.update((h.name, h.asking) for h in new)
                for h in new:
                    print(f"  {self.watch.label(h)} {h.name}: asking {h.asking:,} (channel {channel})")
                    self.log(channel, h, 0, self.watch.label(h).lower())
                self.doing(f"{' / '.join(sorted({self.watch.label(h) for h in new}))}: "
                           f"{', '.join(h.name for h in new)} (hold ESC to stop here)")
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
                if not customs and not (self.buy_custom and all(self.watch.kind(h) == "custom" for h in new)):
                    self.controls.wait(REVIEW_PAUSE)  # your rules already say what to do with custom items
            if customs:  # before any other deal: it's what you're looking for
                deal = customs[0]
                key = row_key(deal)
                buys[key] += 1
                ask = self.confirm_all or any(i["confirm"] for i in self.custom_entries(deal))
                if not self.buy(deal, frame, channel, market=True, ask_you=ask, custom=True) \
                        or buys[key] >= MAX_BUYS_PER_LISTING:
                    done.add(key)
                continue
            # NPC deals (guaranteed profit) are bought on their own; market deals wait for you
            npc = [d for d in singles if self.worth_buying(d) and row_key(d) not in done]
            mkt = [d for d in singles if not self.worth_buying(d) and self.market_deal(d) and row_key(d) not in done]
            if npc or mkt:
                deal = (npc or mkt)[0]
                key = row_key(deal)
                # a listing that's still there (not grey) after a purchase has stock left, because its
                # stock number was read low: it gets bought again (a stack it can't read is skipped)
                buys[key] += 1
                ask = self.confirm_all or (not npc and deal.name not in self.market_auto)
                if not self.buy(deal, frame, channel, market=not npc, ask_you=ask) or buys[key] >= MAX_BUYS_PER_LISTING:
                    done.add(key)
                continue  # the list changes after a purchase: read this page again
            page = tuple(l.text for l in lines)
            # the same again may just be rows that look alike: only twice in a row is the end of the list
            same_pages = same_pages + 1 if page == last_page else 0
            if same_pages >= SAME_PAGES_AT_END:
                return
            if not any(l.y1 > ly1 - row_height * 1.5 for l in lines):
                return  # nothing in the bottom row: it all fits and there's nothing to scroll. Not by
                # counting rows, as pale sold-out ones can go unread
            last_page = page
            page_number += 1
            self.doing("scrolling the store list")
            self.controls.wheel(*self.screen.to_screen((lx0 + lx1) / 2, (ly0 + ly1) / 2))
            self.park_mouse()
            self.controls.wait(0.2)

    def log_page(self, channel, page, lines, singles, done, row_key):
        """logs/store_reads_<date>.txt: what was read on each page of each store, and what it made of
        each listing, to look into a deal that was missed."""
        rows = []
        for h in singles:
            verdict = ("done with" if row_key(h) in done else "custom buy" if self.custom_buy(h) else "NPC deal" if self.worth_buying(h)
                       else "market deal" if self.market_deal(h) else "no")
            rows.append(f"{h.name} at {h.asking:,}: {verdict}")
        text = " | ".join(l.text for l in lines)
        with open(LOG_DIR / f"store_reads_{datetime.now():%Y-%m-%d}.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%H:%M:%S} {channel} page {page}: {'; '.join(rows) or 'nothing it could buy'}\n    read: {text}\n")

    def mark_listings(self, lines, singles):
        """Grey box on every OCR line; yellow on recognised single items, light green on ones it
        would buy, cyan / purple on your custom search / review items, green on market deals."""
        items = [box(l.x0, l.y0, l.x1, l.y1, SEEN) for l in lines]
        for h in singles:
            x0, y0, x1, y1 = h.box
            buy = self.worth_buying(h)
            parts = [h.npc_text + (" BUY" if buy else "")] if h.npc_pays else []
            if h.options:
                parts.append(f"could be {len(h.options)} items")
            watched = self.watch.color(h)
            color = DEAL if buy else watched or LISTED
            if watched:
                parts.append(self.watch.label(h))
            if self.custom_buy(h):
                entries = self.custom_entries(h)
                parts.append("CUSTOM: " + ("ASKS YOU" if self.confirm_all or any(i["confirm"] for i in entries) else "AUTO-BUY"))
            elif self.watch.kind(h) == "custom" and self.custom_left(h) == 0:
                parts.append("CUSTOM: limit reached")
            m = self.market.lowest(h.names)
            if m and m.get("median_24h"):
                below = 100 * (h.asking - m["median_24h"]) / m["median_24h"]
                market_part = f"FM 24h median {m['median_24h']:,} ({below:+.0f}%)"
                if not buy and not watched and self.market_deal(h):
                    color = MARKET
                    market_part += " MARKET DEAL: " + ("AUTO-BUY" if h.name in self.market_auto else "ASKS YOU")
                parts.append(market_part)
            elif self.market.pending(h.names):
                parts.append("getting data...")
            items.append(box(x0 - 4, y0 - 4, x1 + 4, y1 + 4, color, " | ".join(parts)))
        self.marks.set("listings", items)

    def read_stock(self, frame, hit):
        """The small number in the bottom-left of the item's icon: how many are for sale.
        None when it can't be read.

        In a small game window these digits are only a few pixels tall, and a single read is
        often unsure or even confidently wrong (48 read as 43), so it's read 12 ways (crop, zoom,
        padding) and a value needs STOCK_VOTES of them to agree. When two values both do, the
        lower one wins: buying too few just means buying again, too many could misfire."""
        s = self.finder.last_scale or 1
        x0, _, _, y1 = hit.box
        cx0, cy0, cx1, cy1 = max(0, x0 - 80 * s), max(0, y1 - 24 * s), max(0, x0 - 35 * s), y1 + 2 * s
        votes = Counter()
        for px, py in ((0, 0), (4 * s / 0.53, 3 * s / 0.53)):
            crop = frame[max(0, int(cy0 - py)):int(cy1 + py), max(0, int(cx0 - px)):int(cx1 + px)]
            if crop.size == 0:
                continue
            for zoom in (3, 4, 6):
                big = cv2.resize(crop, None, fx=zoom / s, fy=zoom / s, interpolation=cv2.INTER_CUBIC)
                for img in (big, cv2.copyMakeBorder(big, 12, 12, 12, 12, cv2.BORDER_REPLICATE)):
                    # the spot is known, so skip text detection (it misses these outlined digits)
                    for text, score in self.reader.recognize(img):
                        if score >= 0.6 and re.fullmatch(r"\d+", text):
                            for n in eights_as_zeros(text):
                                votes[n] += 1
        agreed = [n for n, count in votes.items() if count >= STOCK_VOTES]
        stock = min(agreed) if agreed else None
        self.marks.set("stock", [box(cx0, cy0, cx1, cy1, READING, f"stock {stock if stock else '?'}", below=True)])
        return stock

    def match_dialog(self, frame, name, tpl, threshold=0.7):
        """((x, y), scale) of a dialog's title template on screen, or None. All dialogs share
        the UI scale, so once one has matched, the others are only searched close to its size;
        before that, around the store buttons' size. The best score is kept for debugging."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        center = self.dialog_scale or self.finder.last_scale or self.scale_guess(frame)
        v, loc, s = fine_match(gray, tpl, center, 0.01 if self.dialog_scale else 0.08)
        self.dialog_scores[name] = (round(float(v), 3), s and round(float(s), 4))
        if v < threshold:
            return None
        self.dialog_scale = s
        return loc, s

    def find_quantity_dialog(self, frame):
        found = self.match_dialog(frame, "quantity", self.quantity_dialog)
        self.mark_dialog("quantity", found, self.quantity_dialog, "quantity window")
        return found

    def find_single_dialog(self, frame):
        """'Are you sure you want to buy it?': what an unstackable item asks instead of a quantity."""
        found = self.match_dialog(frame, "single", self.single_dialog)
        self.mark_dialog("single", found, self.single_dialog, "'are you sure?' window")
        return found

    def find_total_dialog(self, frame):
        """The second confirmation ("The total price is X mesos. Will you make the purchase?")."""
        found = self.match_dialog(frame, "total", self.total_dialog)
        self.mark_dialog("total", found, self.total_dialog, "'total price' window")
        return found

    def read_quantity_refused(self, frame):
        """N from the game's "You may only enter a number equal to or lower than N." notice (it
        appears instead of the total-price window when the quantity is more than it allows), or
        None when that notice isn't on screen. Read with OCR in the middle of the screen, where
        game notices open, so it doesn't depend on the window size."""
        h, w = frame.shape[:2]
        x0, y0, x1, y1 = int(w * 0.25), int(h * 0.2), int(w * 0.75), int(h * 0.75)
        s = self.finder.last_scale or self.scale_guess(frame)
        crop = frame[y0:y1, x0:x1]
        if s < 0.95:
            crop = cv2.resize(crop, None, fx=1 / s, fy=1 / s, interpolation=cv2.INTER_CUBIC)
        text = " ".join(t for _, t, _ in self.reader.detect(crop))
        m = QUANTITY_REFUSED_RE.search(text)
        if not m:
            return None
        limit = int(re.sub(r"[,.]", "", m.group(1)))
        self.marks.set("refused", [box(x0, y0, x1, y1, WARN, f"game: at most {limit}", below=True)], ttl=3)
        return limit

    def read_total(self, frame, dialog):
        (x, y), s = dialog
        x0, y0, x1, y1 = TOTAL_LINE
        rx0, ry0, rx1, ry1 = max(0, x + x0 * s), max(0, y + y0 * s), x + x1 * s, y + y1 * s
        crop = frame[int(ry0):int(ry1), int(rx0):int(rx1)]
        if crop.size == 0:
            return None
        big = cv2.resize(crop, None, fx=2 / s, fy=2 / s, interpolation=cv2.INTER_CUBIC)
        text = " ".join(t for _, t, score in self.reader.detect(big) if score >= 0.8)
        m = TOTAL_RE.search(text)
        total = int(re.sub(r"[,.]", "", m.group(1))) if m else None
        self.marks.set("total_read", [box(rx0, ry0, rx1, ry1, READING,
                                          f"total {total:,}" if total is not None else "total ?")])
        return total

    def read_mesos(self, frame):
        """Your mesos, shown in the store window under the seller. None if not read confidently."""
        if not self.find_store(frame):
            return None
        (x, y), s = self.finder.last_loc, self.finder.last_scale
        rx0, ry0 = max(0, x + MESOS_BOX[0] * s), max(0, y + MESOS_BOX[1] * s)
        rx1, ry1 = x + MESOS_BOX[2] * s, y + MESOS_BOX[3] * s
        crop = frame[int(ry0):int(ry1), int(rx0):int(rx1)]
        if crop.size == 0:
            return None
        big = cv2.resize(crop, None, fx=2 / s, fy=2 / s, interpolation=cv2.INTER_CUBIC)
        texts = [t for _, t, score in self.reader.detect(big) if score >= 0.9]
        mesos = None
        if len(texts) == 1 and re.fullmatch(r"[\d,.]+", texts[0]):
            mesos = int(re.sub(r"[,.]", "", texts[0]))
        self.marks.set("mesos", [box(rx0, ry0, rx1, ry1, READING,
                                     f"your mesos {mesos:,}" if mesos is not None else "your mesos ?")])
        return mesos

    def confirm_listing(self, frame, deal):
        """Read the deal's row a second time, enlarged, and check it says the same thing:
        a misread price is the most expensive mistake an auto-buyer can make."""
        s = self.finder.last_scale or 1
        x0, y0, x1, y1 = deal.box
        pad = 8 * s
        rx0, ry0, rx1, ry1 = max(0, x0 - pad), max(0, y0 - pad), x1 + 60 * s, y1 + pad
        crop = frame[int(ry0):int(ry1), int(rx0):int(rx1)]
        big = cv2.resize(crop, None, fx=1.6 / s, fy=1.6 / s, interpolation=cv2.INTER_CUBIC)
        lines = self.reader.read(big)
        agrees = False
        for l in lines:
            m = pc.LISTING_RE.match(l.text)
            name = pc.name_above(l, lines) if m else None
            if name and pc.to_int(m.group(1)) == 1 and pc.to_int(m.group(2)) == deal.asking:
                # a cut-off name agrees when it fits every item the first read narrowed it to
                # (the icon may have narrowed that further than the text alone can)
                found = {n for n, _ in self.db.candidates(name.text)}
                agrees = bool(found) and set(deal.names) <= found
                break
        self.marks.set("recheck", [box(rx0, ry0, rx1, ry1, READING if agrees else WARN,
                                       "second read agrees" if agrees else "second read DISAGREES", below=True)])
        return agrees

    def buy(self, deal, frame, channel, market=False, ask_you=False, custom=False):
        """True if it bought (some of) the listing. market: a market deal rather than an NPC
        one; ask_you: leave its final OK to you (market deals not on the auto_buy list);
        custom: from the custom search list, where the price is all that matters, and if it's
        left to you it waits for your answer however long it takes."""
        try:
            return self._buy(deal, frame, channel, market, ask_you, custom)
        finally:
            self.marks.clear("quantity", "single", "total", "total_read", "stock", "recheck", "refused")
            self.update_info()

    def buy_quantity(self, deal, stock, remaining, custom):
        """How many of a listing to go for: its stock, what the budget allows, and a custom list
        item's limit."""
        quantity = min(stock, remaining // deal.asking)
        wanted = self.custom_left(deal) if custom else None
        return quantity if wanted is None else min(quantity, wanted)

    def _buy(self, deal, frame, channel, market=False, ask_you=False, custom=False):
        remaining = self.budget - self.spent
        self.doing(f"{'market deal' if market else 'deal'} on {deal.name}: reading the stock")
        # None when unreadable, which is normal for equipment (its icon shows no number). Which buy
        # window opens settles it: "Are you sure?" means a single item; "How many?" means a stack
        # whose size isn't known, and that gets cancelled rather than guessed at.
        stock_read = self.read_stock(frame, deal)
        stock = stock_read or 1
        quantity = self.buy_quantity(deal, stock, remaining, custom)
        if market:
            median = (self.market.lowest(deal.names) or {}).get("median_24h")  # a fixed price needs none
            against = f"24h median {median:,} ({100 * (deal.asking - median) / median:+.0f}%)" if median else "no market price"
            print(f"  {'CUSTOM SEARCH' if custom else 'MARKET DEAL'} {deal.name}: asking {deal.asking:,}, {against}, "
                  f"stock {stock_read or 'not shown'}{'; asks you' if ask_you else '; auto-buy'}")
        else:
            print(f"  DEAL {deal.name}: asking {deal.asking:,}, {deal.npc_label}{deal.could_be} "
                  f"(+{deal.profit:,} each), stock {stock_read or 'not shown'}")
        if quantity < 1:
            print(f"    over budget ({remaining:,} left); skipped")
            self.log(channel, deal, 0, "over budget")
            return
        self.doing("reading your mesos")
        before = self.read_mesos(frame)
        if before is None:
            print("    couldn't read your mesos, so the purchase couldn't be checked; skipped")
            self.log(channel, deal, 0, "mesos unreadable")
            return
        quantity = min(quantity, before // deal.asking)
        if quantity < 1:
            print(f"    not enough mesos ({before:,}); skipped")
            self.log(channel, deal, 0, "not enough mesos")
            return
        if not custom:  # a custom list item is wanted, not flipped
            total = quantity * self.profit_each(deal, market)
            if total < self.min_total_profit:
                print(f"    {quantity} x {self.profit_each(deal, market):+,} = {total:+,} profit, under the "
                      f"{self.min_total_profit:,} minimum per purchase; skipped")
                self.log(channel, deal, 0, f"total profit {total} < {self.min_total_profit}")
                return
        if not market and self.too_thin_for_budget(deal, quantity):
            cost = quantity * deal.asking
            flip = 100 * deal.profit / deal.asking
            print(f"    {cost:,} is {100 * cost / self.budget:.0f}% of the budget for a {flip:.1f}% flip "
                  f"(over {self.big_buy_percent:g}% needs at least {self.big_buy_flip:g}%); skipped")
            self.log(channel, deal, 0, f"{flip:.1f}% flip too thin for {cost} of the budget")
            return
        self.doing("double-checking the listing")
        if not self.confirm_listing(frame, deal):
            print("    a second read of the row disagreed; skipped to be safe")
            self.log(channel, deal, 0, "second read disagreed")
            return

        x0, y0, x1, y1 = deal.box
        self.doing(f"selecting {deal.name}")
        self.click_frame((x0 + x1) / 2, (y0 + y1) / 2)  # select the row
        self.controls.wait(0.3)
        origin, s = self.finder.last_loc, self.finder.last_scale
        selected = self.screen.grab()
        self.dialog_scores = {}
        self.doing("clicking BUY ITEM")
        self.click_frame(*self.at(origin, BUY_ITEM, s))
        self.doing("waiting for the buy window")
        # stackable items ask "How many?"; single items go straight to "Are you sure?"
        frame, dialog = self.wait_for(lambda f: (q := self.find_quantity_dialog(f)) and ("quantity", q)
                                      or (o := self.find_single_dialog(f)) and ("single", o), 3.0)
        if not dialog:
            pc.DEBUG_DIR.mkdir(exist_ok=True)
            cv2.imwrite(str(pc.DEBUG_DIR / "buy_window_missing.png"), frame)
            scores = ", ".join(f"{k} {v} at scale {sc}" for k, (v, sc) in self.dialog_scores.items())
            print(f"    no buy window recognised; skipped (best matches: {scores}; "
                  f"screenshot: debug\\buy_window_missing.png)")
            self.log(channel, deal, 0, "no buy window")
            # if something did open, cancel it: nothing is spent before its last OK
            if selected.shape == frame.shape and cv2.absdiff(selected, frame).mean() > 1.0:
                self.doing("a window opened but wasn't recognised: pressing Esc to cancel it")
                self.controls.key("esc")
                self.controls.wait(0.5)
            return
        kind, where = dialog
        if kind == "quantity" and stock_read is None:
            self.doing("it's a stack, but its stock couldn't be read: cancelling")
            self.controls.key("esc")  # Cancel: nothing is spent
            _, closed = self.wait_for(lambda f: not self.find_quantity_dialog(f), 2.0)
            if not closed:
                raise Stop("the quantity window won't close")
            print("    it's a stack, but its stock couldn't be read confidently; skipped")
            self.log(channel, deal, 0, "stack size unreadable")
            return

        if kind == "single":
            quantity = 1
            expected = deal.asking
            if ask_you:
                if not self.wait_for_you(self.find_single_dialog, market, f"1 x {deal.name} for {expected:,}", custom):
                    self.log(channel, deal, 0, "no answer from you, cancelled")
                    return
                closed = True
            else:
                self.doing(f"confirming the purchase of 1 for {expected:,}")
                self.controls.key("enter")  # OK
                _, closed = self.wait_for(lambda f: not self.find_single_dialog(f), 2.0)
            if not closed:
                self.click_frame(*self.at(where[0], SINGLE_OK, where[1]))
                _, closed = self.wait_for(lambda f: not self.find_single_dialog(f), 2.0)
                if not closed:
                    raise Stop("the purchase confirmation won't close")
        else:
            self.doing(f"entering quantity {quantity}")
            self.controls.key("end")
            self.controls.key("backspace", 6)
            self.controls.type_number(quantity)
            self.doing("closing the quantity window")
            self.controls.key("enter")  # OK
            _, closed = self.wait_for(lambda f: not self.find_quantity_dialog(f), 2.0)
            if not closed:  # Enter didn't confirm: press the OK button
                self.click_frame(*self.at(where[0], QUANTITY_OK, where[1]))
                _, closed = self.wait_for(lambda f: not self.find_quantity_dialog(f), 2.0)
                if not closed:
                    raise Stop("the quantity window won't close")

            # second confirmation: it states the total, so check it before saying yes
            expected = quantity * deal.asking
            self.doing(f"checking the total price (expecting {expected:,})")
            frame, total_dialog = self.wait_for(self.find_total_dialog, 3.0)
            if not total_dialog:
                limit = self.read_quantity_refused(frame)
                if limit is None:
                    raise Stop("the 'total price' confirmation didn't appear")
                # the game refused the quantity ("You may only enter a number equal to or lower
                # than N"): nothing was bought; close the notice and skip the listing
                self.doing(f"the game only allows up to {limit}: closing its notice and skipping")
                print(f"    the game refused {quantity} (it allows at most {limit}); skipped")
                self.controls.key("enter")  # the notice's OK
                self.controls.wait(0.5)
                if self.find_quantity_dialog(self.screen.grab()):
                    self.controls.key("esc")  # Cancel the quantity window if it came back
                    self.wait_for(lambda f: not self.find_quantity_dialog(f), 2.0)
                if self.read_quantity_refused(self.screen.grab()) is not None:
                    raise Stop("the game's 'you may only enter a number' notice won't close")
                self.log(channel, deal, 0, f"game refused quantity {quantity} (max {limit})")
                return
            total = self.read_total(frame, total_dialog)
            if total != expected:
                self.doing(f"total read as {total}, expected {expected:,}: cancelling")
                self.controls.key("esc")  # Cancel: nothing is spent
                self.wait_for(lambda f: not self.find_total_dialog(f), 2.0)
                print(f"    the game's total read as {total}, expected {expected:,}; cancelled")
                self.log(channel, deal, 0, f"total {total} != expected {expected}, cancelled")
                return
            if ask_you:
                if not self.wait_for_you(self.find_total_dialog, market,
                                         f"{quantity} x {deal.name} for {expected:,}", custom):
                    self.log(channel, deal, 0, "no answer from you, cancelled")
                    return
                closed = True
            else:
                self.doing(f"confirming the purchase of {quantity} for {expected:,}")
                self.controls.key("enter")  # OK
                _, closed = self.wait_for(lambda f: not self.find_total_dialog(f), 2.0)
            if not closed:
                self.click_frame(*self.at(total_dialog[0], TOTAL_OK, total_dialog[1]))
                _, closed = self.wait_for(lambda f: not self.find_total_dialog(f), 2.0)
                if not closed:
                    raise Stop("the purchase confirmation won't close")

        # check what was actually paid against what was expected. The mesos line is small and
        # sometimes can't be read (or updates late), so the listing counts as proof too: it goes
        # grey once its whole stock is sold, and otherwise its stock goes down by what was bought.
        self.doing(f"checking the purchase went through (mesos should drop by {expected:,})")
        was_grey = pc.is_faded(selected, pc.Line("", *deal.box))
        sold = [False]

        def mesos_changed(f):
            if not sold[0] and not was_grey:
                sold[0] = self.listing_sold(f, deal, stock_read, quantity)
            m = self.read_mesos(f)
            return m is not None and m != before and (m,)

        frame, changed = self.wait_for(mesos_changed, 4.0, 0.3)
        paid = before - changed[0] if changed else None
        if paid == expected or (paid is None and sold[0]):
            how = "" if paid is not None else " (your mesos couldn't be read; the store shows it sold)"
            self.spent += expected
            if custom:
                self.book.record(deal.name, quantity)
                self.log_custom_buy(channel, deal, quantity, expected, "you confirmed" if ask_you else "auto-bought")
                wanted = self.custom_left(deal)
                print(f"    custom list: {'no limit' if wanted is None else f'{wanted} more to buy'} of {deal.name}")
            if market:
                value = quantity * (self.market.lowest(deal.names) or {}).get("median_24h", 0)
                self.bought.append((deal.name, quantity, expected, value, "market"))
                worth = f"about {value:,} at today's market median"
                how += " (market deal, you confirmed)" if ask_you else " (market deal, auto-bought)"
                self.log_market_buy(channel, deal, quantity, "you confirmed" if ask_you else "auto-bought")
            else:
                value = quantity * deal.npc_pays
                self.bought.append((deal.name, quantity, expected, value, "npc"))
                worth = f"NPC value {value:,}"
                if ask_you:
                    how += " (you confirmed)"
                if self.market.lowest(deal.names):  # a market item too: log it with the others
                    self.log_market_buy(channel, deal, quantity, f"NPC deal, NPC pays {deal.npc_pays:,}")
            print(f"    BOUGHT {quantity} for {expected:,}{how} ({worth}). Budget left {self.budget - self.spent:,}")
            self.log(channel, deal, quantity, f"bought for {expected}{how}")
            self.doing(f"bought {quantity} x {deal.name} for {expected:,}")
            winsound.MessageBeep(winsound.MB_OK)
            self.controls.wait(0.6)
            return True
        if ask_you and paid is None:
            print("    nothing was paid: you cancelled (or it didn't go through)")
            self.log(channel, deal, 0, "cancelled by you")
            self.doing("purchase cancelled")
            return
        pc.DEBUG_DIR.mkdir(exist_ok=True)
        cv2.imwrite(str(pc.DEBUG_DIR / "purchase_unconfirmed.png"), frame)
        self.log(channel, deal, quantity, f"paid {paid}, expected {expected}")
        if paid is None:
            raise Stop("the purchase didn't show up in your mesos or the store (inventory full? store "
                       "changed?); screenshot: debug\\purchase_unconfirmed.png")
        self.spent += max(paid, 0)
        if custom and paid > 0:  # whatever happened, what was paid for counts towards the limit
            self.book.record(deal.name, max(1, paid // deal.asking))
            self.log_custom_buy(channel, deal, max(1, paid // deal.asking), paid, f"{expected:,} was expected")
        raise Stop(f"mesos went down by {paid:,} but {expected:,} was expected; stopping to be safe")

    def start_custom_log(self):
        """A new logs/custom_buys_<date>_<time>.txt for this run of the full sell loop."""
        self.custom_started = datetime.now()
        self.custom_bought = []
        self.custom_log = LOG_DIR / f"custom_buys_{self.custom_started:%Y-%m-%d_%H%M%S}.txt"
        self.write_custom_log()

    def log_custom_buy(self, channel, deal, quantity, paid, how):
        self.custom_bought.append((datetime.now(), deal.name, quantity, paid, channel, how))
        self.write_custom_log()

    def write_custom_log(self, ended=None):
        """Written again after every purchase, so it's there even if the run is killed. ended: how
        long the run took, once it's over."""
        if not self.custom_log:
            return
        total = sum(b[3] for b in self.custom_bought)
        over = f"ended {datetime.now():%H:%M}, took {ended}" if ended else "no end recorded: still running, or it was killed"
        text = [f"Full sell loop started {self.custom_started:%Y-%m-%d %H:%M}, {over}",
                f"Custom items bought: {sum(b[2] for b in self.custom_bought):,}, for {total:,} mesos", ""]
        items = {}
        for _, name, quantity, paid, _, _ in self.custom_bought:
            count, spent = items.get(name, (0, 0))
            items[name] = count + quantity, spent + paid
        text += [f"{name}: {count:,} for {spent:,} (average {spent / count:,.1f} each)" for name, (count, spent) in items.items()]
        if items:
            text.append("")
        text += [f"{when:%H:%M:%S}  {quantity:,} x {name} at {paid / quantity:,.0f} each = {paid:,} ({where}, {how})"
                 for when, name, quantity, paid, where, how in self.custom_bought]
        self.custom_log.write_text("\n".join(text) + "\n", "utf-8")

    def log_market_buy(self, channel, deal, quantity, how):
        """logs/market_buys.txt: one line per market deal bought (auto-bought, or confirmed by
        you), with the price to sell it back at without losing money (shops take SHOP_FEE of
        every sale from the seller)."""
        m = self.market.lowest(deal.names) or {}
        today = f"low {m['low_24h']:,}, median {m['median_24h']:,}" if m.get("median_24h") else "no market price"
        break_even = math.ceil(deal.asking / (1 - SHOP_FEE))
        line = (f"{datetime.now():%Y-%m-%d %H:%M}  bought {quantity} x {deal.name} at {deal.asking:,} each "
                f"({quantity * deal.asking:,} total, channel {channel}, {how}) - sell for at least {break_even:,} each "
                f"to break even after the {SHOP_FEE:.0%} shop fee (today's market: {today})\n")
        with open(LOG_DIR / "market_buys.txt", "a", encoding="utf-8") as f:
            f.write(line)
        print(f"    break even: sell for at least {break_even:,} each (logs\\market_buys.txt)")

    def wait_for_you(self, find_dialog, market, what, custom=False):
        """For market deals (and every deal in confirm mode) it leaves the purchase window open
        for you to press OK (buy) or Cancel, and waits until the window has been gone for a
        moment (a single frame without it may just be your cursor covering it). False if nobody
        answered within MARKET_CONFIRM_WAIT; the purchase is then cancelled. A custom search item
        has no time limit (holding ESC still stops the crawler)."""
        title = "CUSTOM SEARCH" if custom else "MARKET DEAL" if market else "DEAL"
        self.doing(f"{title} {what}: press OK to buy or Cancel (waiting for you)")
        limit = "no time limit" if custom else f"up to {MARKET_CONFIRM_WAIT:.0f}s"
        print(f"    waiting for you: press OK to buy, or Cancel ({limit}), then let go of the mouse")
        for _ in range(3):
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            self.controls.wait(0.3)
        end, gone_since = float("inf") if custom else time.monotonic() + MARKET_CONFIRM_WAIT, None
        while time.monotonic() < end:
            if find_dialog(self.screen.grab()):
                gone_since = None
            elif gone_since is None:
                gone_since = time.monotonic()
            elif time.monotonic() - gone_since > 1.0:
                self.park_mouse()  # your cursor may be on the store buttons, which hides the store
                return True
            self.controls.wait(0.25)
        self.doing("no answer: cancelling the purchase")
        print("    no answer; cancelled")
        self.controls.key("esc")
        self.wait_for(lambda f: not find_dialog(f), 2.0)
        return False

    def listing_sold(self, frame, deal, stock, quantity):
        """Whether the store shows the purchase: the row went grey (its whole stock sold) or,
        after buying part of a stack, its stock went down by exactly quantity."""
        if pc.is_faded(frame, pc.Line("", *deal.box)):
            self.marks.set("sold", [box(*deal.box, READING, "row went grey: sold", below=True)], ttl=3)
            return True
        if stock and quantity < stock and self.read_stock(frame, deal) == stock - quantity:
            self.marks.set("sold", [box(*deal.box, READING, f"stock {stock} -> {stock - quantity}: sold",
                                        below=True)], ttl=3)
            return True
        return False

    def leave_store(self):
        for _ in range(2):
            if not self.find_store(self.screen.grab()):
                return
            self.doing("closing the store window")
            origin, s = self.finder.last_loc, self.finder.last_scale
            self.click_frame(*self.at(origin, LEAVE_STORE, s))
            _, gone = self.wait_for(lambda f: not self.find_store(f), 3.0)
            if gone:
                self.controls.wait(0.4)
                return
        raise Stop("couldn't leave the store")

    # ---- channels

    def find_channel_window(self, frame):
        found = self.match_dialog(frame, "channel", self.channel_title, threshold=0.8)
        self.mark_dialog("channel", found, self.channel_title, "channel window")
        if not found:
            self.marks.clear("channel_cell")
        return found

    def highlighted_channel(self, frame, window):
        (tx, ty), s = window
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        px0, px1, py = CH_HIGHLIGHT_PATCH
        sat = {}
        for ch in range(1, 41):
            r, c = divmod(ch - 1, 8)
            x = tx + (CH1_CELL[0] + c * CH_STEP[0]) * s
            y = ty + (CH1_CELL[1] + r * CH_STEP[1]) * s
            patch = hsv[int(y - py * s):int(y + py * s), int(x + px0 * s):int(x + px1 * s)]
            sat[ch] = patch[..., 1].mean() if patch.size else 0
        ranked = sorted(sat, key=sat.get, reverse=True)
        top, second = sat[ranked[0]], sat[ranked[1]]
        # blue (selected) cells are saturated, the others grey; only trust one clear winner
        if top < 60 or second > top * 0.6:
            self.marks.clear("channel_cell")
            return None
        r, c = divmod(ranked[0] - 1, 8)
        x = tx + (CH1_CELL[0] + c * CH_STEP[0]) * s
        y = ty + (CH1_CELL[1] + r * CH_STEP[1]) * s
        hw, hh = CH_STEP[0] * s / 2, CH_STEP[1] * s / 2
        self.marks.set("channel_cell", [box(x - hw, y - hh, x + hw, y + hh, TARGET, f"selected: CH {ranked[0]}")])
        return ranked[0]

    def change_channel(self, target):
        print(f"changing to channel {target}")
        self.channel_refused = False
        self.marks.clear("shops", "target")
        self.doing(f"channel {target}: opening the game menu")
        self.controls.key("esc")      # game menu
        self.controls.wait(0.4)
        self.doing(f"channel {target}: opening the channel window")
        self.controls.key("enter")    # its first entry: Change Channel
        frame, window = self.wait_for(self.find_channel_window, 2.5)
        if not window:
            self.doing("closing the game menu")
            self.controls.key("esc")
            print("  the channel window didn't open with Esc -> Enter; stopping channel changes")
            return False
        # park the mouse away from the window: hovering a cell may highlight it too
        self.doing(f"channel {target}: reading which channel is selected")
        self.park_mouse()
        self.controls.wait(0.15)
        frame = self.screen.grab()

        start = self.highlighted_channel(frame, window)
        print(f"  selected when the window opened: {start}")
        if start is None:
            return self.give_up_channel(frame, "couldn't tell which channel is selected")
        if start == target:
            # that's the channel we're on (crawled already); never press Enter without moving
            self.doing(f"already on channel {target}: closing the channel window")
            self.controls.key("esc")
            self.wait_for(lambda f: not self.find_channel_window(f), 2.0)
            print(f"  already on channel {target}; skipping it")
            return None
        current = start
        while current != target:
            diff = target - current
            self.doing(f"channel {target}: moving the selection (now on {current})")
            self.controls.key("down" if diff >= 8 else "up" if diff <= -8 else "right" if diff > 0 else "left")
            for _ in range(8):  # the game or the capture can lag: give it 2 seconds, without pressing again
                self.controls.wait(0.25)
                frame = self.screen.grab()
                now = self.highlighted_channel(frame, window)
                if now not in (None, current):
                    break
            print(f"    {current} -> {now}")
            if now is None or now == current:
                return self.give_up_channel(frame, f"the arrow key didn't move the selection from {current}")
            current = now
        self.doing(f"channel {target}: pressing Enter")
        self.controls.key("enter")
        self.marks.clear("channel", "channel_cell")

        # it really changed only if the room goes away (loading) and then comes back
        self.doing(f"channel {target}: waiting for the room to unload")
        _, left = self.wait_for(lambda f: f.mean() < 40 or not self.find_shops(f), 10.0, 0.3)
        if not left:
            # a fast change can be over between two looks: the channel window says where we are
            now = self.channel_now()
            if now is None:
                return self.give_up_channel(self.screen.grab(), "pressed Enter but the channel didn't change")
            if now != target:
                print(f"  still on channel {now}: the game didn't change to {target}; moving on")
                self.channel_refused = True
                return None
        self.doing(f"channel {target}: waiting for the room to load")
        _, back = self.wait_for(lambda f: f.mean() >= 40, 25.0, 0.5)
        if not back:
            return self.give_up_channel(self.screen.grab(), "the room didn't come back after the channel change")
        self.doing(f"channel {target}: waiting for the shops to appear")
        _, shops = self.wait_for(lambda f: len(self.find_shops(f)) > 0, NO_SHOPS_WAIT, 0.5)
        if not shops:
            print(f"  no shops on channel {target} after {NO_SHOPS_WAIT:.0f}s; moving on")
            return None
        self.controls.wait(1.5)  # let other players' stores finish appearing
        return True

    def start_channel(self, channels):
        """Whether to crawl the channel we're on, and the channels to go to after that. Only those
        asked for are crawled, so this one only if it's among them; with no channels asked for, we
        stay here. If it can't tell, it doesn't crawl: changing channel settles where we are."""
        if not channels:
            return True, []
        now = self.channel_now()
        if now in channels:
            return True, [c for c in channels if c != now]
        print(f"  on channel {now if now is not None else 'unknown'}, which isn't on the list: not crawling it")
        return False, list(channels)

    def channel_now(self):
        """The channel we're on, read from the channel window (opened and closed again), or None."""
        self.doing("checking which channel we're on")
        self.controls.key("esc")
        self.controls.wait(0.4)
        self.controls.key("enter")
        _, window = self.wait_for(self.find_channel_window, 2.5)
        if not window:
            self.controls.key("esc")
            return None
        self.park_mouse()
        self.controls.wait(0.15)
        now = self.highlighted_channel(self.screen.grab(), window)
        self.controls.key("esc")
        self.wait_for(lambda f: not self.find_channel_window(f), 2.0)
        self.marks.clear("channel", "channel_cell")
        return now

    def give_up_channel(self, frame, why):
        pc.DEBUG_DIR.mkdir(exist_ok=True)
        cv2.imwrite(str(pc.DEBUG_DIR / "channel_change_failed.png"), frame)
        if self.find_channel_window(frame):
            self.doing("closing the channel window")
            self.controls.key("esc")
        print(f"  {why}; stopping (screenshot: debug\\channel_change_failed.png)")
        return False

    # ---- entry points

    def dry_run(self):
        print("Switch to MapleStory now. Looking in 3 seconds... (nothing will be clicked)")
        self.doing("dry run: switch to MapleStory, looking in 3 seconds")
        time.sleep(3)
        if user32.GetForegroundWindow() != self.hwnd:
            print("warning: MapleStory isn't the front window, so the screenshot shows whatever covers it")
        frame = self.screen.grab()
        self.doing("dry run: looking for shops")
        shops = self.find_shops(frame)
        out = frame.copy()
        for i, ((x, y), _) in enumerate(shops, 1):
            cv2.circle(out, (int(x), int(y)), 14, (0, 0, 255), 3)
            cv2.putText(out, str(i), (int(x) + 16, int(y) + 8), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 3)
        self.find_store(frame)
        lines, store, singles, _ = pc.scan(frame, self.reader, self.finder, self.db, 1, False)
        self.mark_listings(lines, singles)
        deals = [d for d in singles if self.worth_buying(d)]
        pc.DEBUG_DIR.mkdir(exist_ok=True)
        cv2.imwrite(str(pc.DEBUG_DIR / "crawler_dry_run.png"), out)
        print(f"UI scale {self.ui_scale}: {len(shops)} shop signs found; would open them in the numbered order")
        if store:
            print(f"a store is open: {len(singles)} single listings, {len(deals)} pass the buy rules; "
                  f"your mesos read as {self.read_mesos(frame)}")
            for d in deals:
                print(f"  would buy {d.name}: asking {d.asking:,}, {d.npc_label}, "
                      f"stock {self.read_stock(frame, d)}, second read agrees: {self.confirm_listing(frame, d)}")
            for d in singles:
                if not self.worth_buying(d) and self.market_deal(d):
                    how = "would buy it" if d.name in self.market_auto else "would ask you"
                    print(f"  market deal ({how}): {d.name} asking {d.asking:,}, "
                          f"24h median {self.market.lowest(d.names)['median_24h']:,}")
                if self.custom_buy(d):
                    print(f"  custom search: would buy {d.name}, asking {d.asking:,}")
        print(f"marked screenshot: {pc.DEBUG_DIR / 'crawler_dry_run.png'}")
        self.doing(f"dry run done: {len(shops)} shops, {len(deals)} deals in the open store (nothing clicked)")

    def check_rights(self):
        if runs_with_more_rights(self.hwnd):
            raise SystemExit("MapleStory runs as administrator, so Windows would silently ignore the "
                             "crawler's clicks.\nStart it with crawler.bat (it asks for admin rights), or "
                             "run your terminal as administrator.")

    def run(self, channels, budget):
        self.budget = budget
        self.update_info()
        rules = ", ".join(f"from {p:,}: +{m:,}" for p, m in self.buy_rules)
        print(f"Budget {budget:,} mesos. Buys when profit per item is at least ({rules}).")
        limit = self.args.minutes * 60 if self.args.minutes else float("inf")  # none: the rotation ends it
        self.controls = Controls(self.hwnd, time.monotonic() + limit)
        print("Switch to MapleStory now. Starting in 3 seconds... (hold ESC for 1s to stop)")
        self.doing("switch to MapleStory: starting in 3 seconds (hold ESC 1s to stop)")
        time.sleep(3)
        started = time.monotonic()
        visited = 0  # rooms crawled: the starting one, then each channel changed to

        def channel_done(name, since):
            print(f"  [channel {name}] done in {duration(time.monotonic() - since)} "
                  f"(running {duration(time.monotonic() - started)})")

        try:
            t = time.monotonic()
            crawl_here, channels = self.start_channel(channels)
            if crawl_here:
                self.crawl_room("start")
                visited += 1
                channel_done("start", t)
            for ch in channels:
                t = time.monotonic()
                changed = self.change_channel(ch)
                if changed is False:
                    summary = (f"stopped at channel {ch}: couldn't change channel "
                               f"({visited} rooms in {duration(time.monotonic() - started)})")
                    break
                if changed:  # None: already on it (the starting channel), or no shops there
                    self.crawl_room(ch)
                    visited += 1
                channel_done(ch, t)
            else:
                summary = (f"full rotation done: {len(channels)} channels ({visited} rooms crawled) "
                           f"in {duration(time.monotonic() - started)}")
        except Stop as e:
            summary = f"stopped: {e} ({visited} rooms in {duration(time.monotonic() - started)})"
        finally:
            self.controls.stop.set()
            self.marks.clear("click", "target")
        print(summary)
        self.doing(summary)
        print(f"\nBought {sum(b[1] for b in self.bought)} items for {self.spent:,} mesos. "
              f"Budget left {self.budget - self.spent:,}.")
        for name, qty, paid, value, kind in self.bought:
            worth = f"NPC pays {value:,}" if kind == "npc" else f"~{value:,} at today's market median"
            print(f"  {qty} x {name}: paid {paid:,}, {worth}")
        print(f"log: {self.log_path}")


def duration(seconds):
    """65 -> '1m 05s', 3725 -> '1h 02m 05s'."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s" if m else f"{s}s"


def parse_channels(spec):
    return market.parse_numbers(spec, 1, 40)


def parse_rooms(spec):
    return market.parse_numbers(spec, 1, 20)


parse_mesos = market.parse_mesos


def stop_requested():
    """Whether the launcher's Stop button was pressed: it creates this file."""
    path = os.environ.get("MAPLE_STOP_FILE")
    return bool(path) and os.path.exists(path)


def ask_budget():
    while True:
        text = input("Budget for this session (mesos, e.g. 50000, 250k, 1.5m): ")
        try:
            budget = parse_mesos(text)
            if budget > 0:
                return budget
        except ValueError:
            pass
        print("  please type a number")


def ask_confirm_mode():
    """True: every purchase waits on its last window for you to press OK."""
    while True:
        text = input("Buy deals automatically, or wait for you to confirm each purchase? "
                     "[A]uto / [C]onfirm: ").strip().lower()
        if text in ("a", "auto"):
            return False
        if text in ("c", "confirm"):
            return True
        print("  please type A or C")


def ask_npc_only():
    """True: only NPC deals are bought, and market prices aren't looked at."""
    while True:
        text = input("Buy only NPC deals, or Free Market deals too? [N]PC only / [M]arket too: ").strip().lower()
        if text in ("n", "npc"):
            return True
        if text in ("m", "market"):
            return False
        print("  please type N or M")


def ask_buy_custom():
    return input("Custom list: buy its items, each by the rules set for it in the launcher? [y/N] ").strip().lower() in ("y", "yes")


def main(sell_loop=None):
    """sell_loop: walker.run_full, for the version that also walks to an NPC and sells."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="only look: mark the shops it would open")
    ap.add_argument("--budget", type=parse_mesos, help="most it may spend this session (asked if not given)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--auto", dest="confirm", action="store_false", default=None,
                      help="buy deals without asking (asked if neither --auto nor --confirm is given)")
    mode.add_argument("--confirm", dest="confirm", action="store_true",
                      help="stop on every purchase window for you to press OK or Cancel")
    deals = ap.add_mutually_exclusive_group()
    deals.add_argument("--npc-only", dest="npc_only", action="store_true", default=None,
                       help="only buy NPC deals (asked if neither --npc-only nor --market-deals is given)")
    deals.add_argument("--market-deals", dest="npc_only", action="store_false",
                       help="buy Free Market deals as well as NPC deals")
    ap.add_argument("--channels", type=parse_channels, default="40-1",
                    help="the only channels to visit, in order: 40-1, or 1,3,20,39, or a mix like 1-5,20")
    ap.add_argument("--no-channel-change", action="store_true", help="only crawl the current room")
    ap.add_argument("--minutes", type=float, default=0,
                    help="stop after this long (default: no limit, it stops after the last channel)")
    ap.add_argument("--match", type=int, default=88, help="item name match strictness 0-100")
    ap.add_argument("--window", default="maplestory.exe")
    ap.add_argument("--market-percent", type=market.parse_percent,
                    help="a market deal is this many %% under the 24h median (asked if not given)")
    custom = ap.add_mutually_exclusive_group()
    custom.add_argument("--buy-custom", dest="buy_custom", action="store_true", default=None,
                        help="buy custom list items, each by its own rules (asked if neither is given)")
    custom.add_argument("--no-custom-buy", dest="buy_custom", action="store_false", help="only point them out")
    if sell_loop:
        ap.add_argument("--rounds", type=int, default=0, help="stop after this many rooms (default: until you stop it)")
        ap.add_argument("--rooms", type=parse_rooms,
                        help="the only rooms to go through, in order: 6-1, or 3,10,15, or a mix like 6-1,16-13 (default: all of them)")
        ap.add_argument("--sell-below", type=float,
                        help="go and sell early under this percent of the budget (default: from the settings)")
    market.add_update_flag(ap)
    ap.add_argument("--no-overlay", action="store_true",
                    help="don't draw what the crawler sees and does over the game")
    ap.add_argument("--debug", action="store_true",
                    help="save a screenshot of every click to debug/clicks (aimed vs actual cursor)")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True, errors="replace")  # OCR text can hold characters the console can't show
    market.update_saved = args.market_update
    if sell_loop:  # only what an NPC pays well for is worth buying to sell there, without asking. Custom
        # list items are bought too if you say so, by their own rules, and are never sold (walker.keep_reason).
        args.confirm, args.npc_only = False, True

    crawler = Crawler(args)
    if not args.dry_run:
        crawler.confirm_all = ask_confirm_mode() if args.confirm is None else args.confirm
        print("  every purchase waits for you to press OK or Cancel" if crawler.confirm_all
              else "  deals are bought automatically")
    crawler.market_deals = not (ask_npc_only() if args.npc_only is None else args.npc_only)
    if crawler.watch.custom:
        crawler.buy_custom = ask_buy_custom() if args.buy_custom is None else args.buy_custom
    needs_prices = crawler.buy_custom and crawler.book.needs_market()
    if crawler.market_deals or needs_prices:
        crawler.load_market()
    if not crawler.market_deals:
        print("  only NPC deals" + ("" if needs_prices else ": Free Market prices aren't looked up"))
    if crawler.buy_custom:
        crawler.update_custom_prices()
        print("  custom list: buying by each item's own rules")
        for item in crawler.book.items.values():
            wanted = custom_items.left(item)
            print(f"    {item['name']}: {custom_items.describe(item)}, "
                  f"{'no limit' if wanted is None else str(wanted) + ' more of ' + str(item['limit'])}, "
                  f"{'asks you' if item['confirm'] else 'auto-buy'}")
    if crawler.market and crawler.market_deals:
        crawler.market_below = args.market_percent or market.ask_percent(crawler.market_below)
        print(f"  market deals: {crawler.market_below:g}% or more under the 24h median; "
              + ("you confirm each one" if crawler.confirm_all
                 else f"the {len(crawler.market_auto)} items on the auto_buy list are bought automatically, "
                      f"you confirm the rest"))
    if args.dry_run:
        job, linger = crawler.dry_run, 15.0
    else:
        crawler.check_rights()
        budget = args.budget or ask_budget()
        channels = [] if args.no_channel_change else args.channels
        if sell_loop:
            sell_below = args.sell_below if args.sell_below is not None else \
                json.loads(SETTINGS_FILE.read_text("utf-8")).get("sell_below_budget_percent", 10)

            def job():
                print(sell_loop(crawler, channels, budget, args.rounds, sell_below))
        else:
            job = lambda: crawler.run(channels, budget)
        linger = 5.0
    if args.no_overlay:
        job()
        return

    # the crawler works on a thread; the overlay (tkinter) has to stay on this one
    finished = []

    def work():
        try:
            job()
        except Exception:
            crawler.marks.doing("crashed: see the console")
            raise
        finally:
            finished.append(time.monotonic())

    overlay = CrawlOverlay()

    def stop(*_):
        if crawler.controls:
            crawler.controls.stop_reason = crawler.controls.stop_reason or "Ctrl+C"
            crawler.controls.stop.set()
        else:
            overlay.root.after(0, overlay.root.destroy)

    signal.signal(signal.SIGINT, stop)

    def tick():
        if stop_requested():
            if crawler.controls:
                crawler.controls.stop_reason = crawler.controls.stop_reason or "Stop button"
            stop()
        region = crawler.screen.region if user32.IsWindow(crawler.hwnd) else None  # marks are in picture pixels
        overlay.draw_marks(region, crawler.marks)
        if finished and time.monotonic() - finished[0] > linger:  # leave the last state up a moment
            overlay.root.destroy()
            return
        overlay.root.after(100, tick)

    threading.Thread(target=work, daemon=True).start()
    tick()
    overlay.root.mainloop()


if __name__ == "__main__":
    main()
