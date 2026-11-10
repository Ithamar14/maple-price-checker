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
import sqlite3
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
from rapidfuzz import fuzz

import custom_items
import holdings
import market
import price_checker as pc
import pricing
import scan_log
import store_rules
import equip_check

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
TIP_TITLE_MATCH = 75              # how alike (0-100) a sign's name must read to be the shop a scouter found
TIP_OWNER_MATCH = 80              # how alike (0-100) the owner's name must read in the open store to be the find's shop
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


class TipCall(Exception):
    """A scouter's find to go for (tips.py), between shops: carries the task."""
    def __init__(self, task):
        super().__init__(f"a find: {task['data'].get('item')}")
        self.task = task


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

    def type_text(self, text):
        """Types text as key presses (with Shift where a character needs it), for a name box."""
        for ch in text:
            res = user32.VkKeyScanW(ord(ch))
            if res == -1 or res & 0xFF == 0xFF:
                raise ValueError(f"can't type {ch!r} on this keyboard layout")
            vk, shift = res & 0xFF, bool(res & 0x100)
            if shift:
                self.key_down("shift")
            try:
                self.ensure_front()
                scan = user32.MapVirtualKeyW(vk, 0)
                _send(INPUT(1, _INPUTUNION(ki=KEYBDINPUT(vk, scan, 0x0008, 0, 0))))
                time.sleep(0.04)
                _send(INPUT(1, _INPUTUNION(ki=KEYBDINPUT(vk, scan, 0x0008 | 0x0002, 0, 0))))
            finally:
                if shift:
                    self.key_up("shift")
            self.wait(0.08)

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

def hold_items(rules, path=None):
    """{name: mesos over its NPC price} from the "hold" rules in crawler_settings.json: equipment of a
    subtype (e.g. Claw) from min_level up, from data/equipment_stats.json (custom_items.load_equipment)."""
    if not rules:
        return {}
    gear = custom_items.load_equipment() if path is None else json.loads(Path(path).read_text("utf-8"))
    found = {}
    for rule in rules:
        for name, info in gear.items():
            if info.get("subtype") == rule["subtype"] and (info.get("level") or 0) >= rule.get("min_level", 0):
                found[name] = max(found.get(name, 0), rule["over_npc"])
    return found


class Crawler:
    # defaults, for a crawler made without __init__ (tests); __init__ sets them for a run
    hold = {}
    scout = False
    tips = None
    on_tip = False
    current_sign, current_title = None, ""

    custom_from_profits = False  # the full sell loop's split: see sold_for
    profit_pool = 0     # all the money for custom deals
    custom_reserve = 0  # how much of it is what's left of the overall budget set aside at the start
    npc_budget = 0      # the overall budget
    npc_cap = 0         # with --npc-money: NPC deals' money is filled back up to this and no more
    scans = None  # the store log (scan_log)
    store_deals = False  # the full sell loop's market deals: bought from profits, to sell in your store
    review_pause = True  # keep a store open REVIEW_PAUSE for check stats / custom search items, for you to look
    store_ready = False  # as many store deals held as a store takes: the loop ends
    prices = None  # pricing.Prices, once market prices are loaded
    store_rules = None  # store_rules.Rules, the same
    scan_owner = ""
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
        cfg = settings.get("store_deals", {})
        self.store_cfg = {"below_reference_percent": cfg.get("below_reference_percent", 15),
                          "min_slot_profit": cfg.get("min_slot_profit", 2000), "min_sold_7d": cfg.get("min_sold_7d", 5),
                          "max_budget_percent": cfg.get("max_budget_percent", 20), "big_deal_profit": cfg.get("big_deal_profit", 10000),
                          "full_after_hours": cfg.get("full_after_hours", 24), "max_items": cfg.get("max_items", 10),
                          "max_held": cfg.get("max_held", 15), "max_waiting_percent": cfg.get("max_waiting_percent", 20),
                          "ready_when_broke": cfg.get("ready_when_broke", 8), "keep_scanning": cfg.get("keep_scanning", True),
                          "npc_sell_below_percent": cfg.get("npc_sell_below_percent", 30),
                          "demand_caps": cfg.get("demand_caps", [[50, 20], [15, 10], [5, 5]]),
                          "allow_drops": cfg.get("allow_drops", ["Leather", "Fragment of Magic"]),
                          "refined_premium": cfg.get("refined_premium", 1.25),
                          "min_scroll_price": cfg.get("min_scroll_price", 0), "scroll_allow": cfg.get("scroll_allow", []),
                          "consumables": cfg.get("consumables", {}), "hp_per_meso": cfg.get("hp_per_meso", 2),
                          "mp_per_meso": cfg.get("mp_per_meso", 1), "stack_sizes": cfg.get("stack_sizes", {}),
                          "reserved_slots": cfg.get("reserved_slots", {"consumable": 2, "scroll": 3})}
        self.hold = hold_items(settings.get("hold", []))  # bought and kept, never sold at the NPC: name -> mesos over its NPC price
        self.market = market.Live(enabled=False)  # Free Market prices, after load_market()
        self.channel_refused = False  # the last channel change didn't take: the channel may be full
        self.sell_below = None  # stop opening shops once under this percent of the budget is left
        self.confirm_all = False  # every purchase waits on its last window for you to press OK
        self.market_deals = True  # False: NPC deals only (prices may still be loaded for custom_percent)
        self.buy_custom = False  # buy custom list items, each by its own rules
        self.book = custom_items.Book()
        self.custom_time = self.custom_file_time()  # the custom list is read again when it changes (refresh_custom)
        self.watch = pc.Watchlists(pc.load_custom_items(self.db), pc.load_review_items())
        self.budget = 0
        self.spent = 0
        self.bought = []  # (name, quantity, paid, value, "npc" or "market")
        self.scout = False  # a scouter: reads every store, buys nothing, sends what it would buy (tips.py)
        self.tips = None  # tips.Tips: the server end, for a scouter (sends) or a buyer (takes)
        self.on_tip = False  # going for a find: no other find interrupts it
        self.current_sign, self.current_title = None, ""  # the shop open now: its sign's spot (screen fractions), its name
        self.scans = None
        try:
            self.scans = scan_log.ScanLog(hours=settings.get("scan_log", {}).get("hours_between_same_shop", scan_log.HOURS_BETWEEN))
        except (sqlite3.Error, OSError) as e:
            print(f"Not logging store listings: {e}")
        self.custom_log = None  # the full sell loop's list of custom items bought
        self.custom_started = None
        self.custom_bought = []  # (time, name, quantity, paid, where, how)
        self.marks = Marks()

    @staticmethod
    def custom_file_time():
        try:
            return custom_items.FILE.stat().st_mtime
        except OSError:
            return None

    def refresh_custom(self):
        """The custom list again if it changed on disk since it was read: a buy request from Discord, or an edit in
        the launcher, is then bought without a restart. Between shops."""
        changed = self.custom_file_time()
        if changed == self.custom_time:
            return
        self.custom_time = changed
        self.book = custom_items.Book()
        self.watch = pc.Watchlists(pc.load_custom_items(self.db), self.watch.review)

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
        if market and self.store_deals and self.prices:
            reference = self.prices.reference(hit.name)[0]
            return int((reference or 0) * (1 - SHOP_FEE)) - hit.asking
        if market:
            return int(self.market.lowest(hit.names)["median_24h"] * (1 - SHOP_FEE)) - hit.asking
        return hit.profit

    def hold_buy(self, hit):
        """Whether this listing is one to buy and keep (the "hold" rules): at most its NPC price plus
        the rule's mesos. Not a cut-off name that could be something else."""
        overs = [self.hold.get(n) for n in hit.names]
        return all(o is not None for o in overs) and bool(overs) and hit.npc_pays > 0             and hit.asking <= hit.npc_pays + min(overs)

    def stacks(self, hit):
        """Whether it piles up in one inventory slot: not equipment, and not throwing stars (one each)."""
        return not hit.stars and hit.name not in self.db.slots

    def worth_buying(self, hit):
        if self.watch.kind(hit) == "custom":  # bought by its own rules, and never sold to make the profit
            return False
        if any(w in n.lower() for n in hit.names for w in self.never_buy):
            return False
        need = self.required_profit(hit.asking)
        return need is not None and hit.profit >= need

    def sold_for(self, mesos):
        """Mesos from the NPC go to what's left of the budget. With custom_from_profits the budget is
        for NPC deals, and together with what's unspent of the money set aside at the start it's
        only filled back up to the overall budget: anything past that is profit, the only other
        money for custom deals, which then have no limit."""
        money = self.budget - self.spent + mesos
        most = self.npc_cap or self.npc_budget - self.custom_reserve
        if (self.custom_from_profits or self.store_deals) and money > most:
            self.profit_pool += money - most
            money = most
        self.budget, self.spent = money, 0

    def pay_custom(self, mesos):
        """Comes off the money set aside first: once it's spent, only profits are left."""
        self.profit_pool -= mesos
        self.custom_reserve = max(0, self.custom_reserve - mesos)

    def money_low(self):
        """Under sell_below percent of the money for NPC deals left. When they only have a share of the
        budget (--npc-money), under npc_sell_below_percent of it at least: a small share runs dry
        before 10% of it is left, and has nothing to spend in the meantime."""
        if self.sell_below is None:
            return False
        percent = max(self.sell_below, self.store_cfg["npc_sell_below_percent"]) if self.npc_cap else self.sell_below
        return self.budget - self.spent < self.budget * percent / 100

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
            if item["when"] == "value":  # a potion, by what it restores
                tops.append(custom_items.value_price(item, (self.store_cfg["hp_per_meso"], self.store_cfg["mp_per_meso"])))
                continue
            if not self.market.fresh(hit.names):
                return None
            percent = item["percent"] if item["when"] == "under" else -item["percent"]
            top = market.deal_limit(self.market.lowest(hit.names), percent, self.market_min_reports)
            if top is None:
                return None
            tops.append(top)
        return min(tops)

    def custom_from_pool(self):
        """Whether custom deals are paid from the money for custom and store deals, not the NPC deals':
        whenever the full sell loop splits the budget (custom deals with no limit, or store deals on).
        Custom deals by their limits then still stop at each item's limit."""
        return self.custom_from_profits or self.store_deals

    def custom_left(self, hit):
        """How many more of this listing's item may be bought; None: no limit."""
        counts = [custom_items.left(i, self.custom_from_profits) for i in self.custom_entries(hit) or ()]
        counts = [n for n in counts if n is not None]
        return min(counts) if counts else None

    def custom_buy(self, hit):
        """Whether to buy this listing, by its own item's rules: its limit, then its price."""
        entries = self.buy_custom and self.custom_entries(hit)
        if not entries or self.custom_left(hit) == 0:
            return False
        if self.custom_from_pool() and hit.asking > self.profit_pool:
            return False
        top = self.custom_top_price(entries, hit)
        return top is not None and hit.asking <= top

    def custom_needs_price(self, hit):
        """Whether this listing's item goes by the market, with buying left to do."""
        entries = self.buy_custom and self.custom_entries(hit)
        return bool(entries) and self.custom_left(hit) != 0 and any(i["when"] not in ("price", "value") for i in entries)

    def too_thin_for_budget(self, deal, quantity):
        """Whether an NPC deal ties up too much of the session budget for its profit: it costs
        more than over_budget_percent of the budget, and earns under min_flip_percent of its price."""
        return (quantity * deal.asking * 100 > self.budget * self.big_buy_percent
                and deal.profit * 100 < deal.asking * self.big_buy_flip)

    def load_market(self):
        """Ask about and load the Free Market price dump (see market.py)."""
        self.market = market.prepare(self.db.ids)
        self.prices = pricing.Prices(self.market, scan_log.FILE, self.store_cfg["consumables"])
        self.store_rules = store_rules.Rules(self.db.ids, lambda name: self.prices.reference(name)[0],
                                             self.store_cfg["allow_drops"], self.store_cfg["refined_premium"],
                                             min_scroll_price=self.store_cfg["min_scroll_price"],
                                             scroll_allow=self.store_cfg["scroll_allow"])

    def update_custom_prices(self):
        """The latest prices of everything on the custom list still to be bought, before the first
        shop opens: one that turns up first thing shouldn't be missed for a price still on its way."""
        names = []
        for item in self.book.items.values():
            known = self.db.items.get(pc.norm(item["name"]))
            if known and custom_items.left(item, self.custom_from_profits) != 0:
                names.append(known[0])  # as MeowDB's lookups and the listings spell it
        if not names or not self.market:
            return
        if not market.ALLOWED:
            print(f"  custom list: {market.BLOCKED_NOTE}; using the saved prices")
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

    def is_full(self, name, held):
        cfg = self.store_cfg
        return holdings.is_full(held, self.prices.reference(name)[0] if self.prices else None, cfg["min_slot_profit"],
                                cfg["full_after_hours"], SHOP_FEE)

    def store_full(self, held=None):
        """How many held store deals take a store slot yet (see holdings.is_full)."""
        held = holdings.load() if held is None else held
        return sum(self.is_full(name, h) for name, h in held.items())

    def stack_size(self, name):
        """Yours (stack_sizes in crawler_settings.json's store_deals) first, then MeowDB's, then 100."""
        return self.store_cfg.get("stack_sizes", {}).get(name) or holdings.stack_size(name, self.db.ids.get(name))

    def slot_kind(self, name):
        return store_rules.slot_kind(name, self.db.ids, self.store_cfg["consumables"])

    def slot_counts(self, held):
        """{kind: store slots its full items take}."""
        counts = {}
        for name, h in held.items():
            if self.is_full(name, h):
                counts[self.slot_kind(name)] = counts.get(self.slot_kind(name), 0) + 1
        return counts

    def slot_free(self, name, held):
        """Whether a new item of this one's kind would have a store slot (reserved_slots: some are kept
        for consumables and scrolls, so other items can't take them all)."""
        return store_rules.fits(self.slot_kind(name), self.slot_counts(held), self.store_cfg["reserved_slots"], self.store_cfg["max_items"])

    def waiting_money(self, held=None):
        """Mesos in held store deals that don't take a slot yet: stacks still building up."""
        held = holdings.load() if held is None else held
        return sum(h["paid"] for name, h in held.items() if not self.is_full(name, h))

    def waiting_cap(self):
        return int(self.npc_budget * self.store_cfg["max_waiting_percent"] / 100)

    def store_deal(self, hit):
        """Whether to buy this listing to sell in your store: an ETC or USE item (not equipment, whose
        stats set its price, nor a custom list item with buying left to do) at least below_reference_percent under its
        reference price (pricing.py: the one the store lists it by), that's sold at least min_sold_7d
        times in the bot's scans of the last week, from what's set aside and profits only. Out of
        money with ready_when_broke slots full, the store's items count as ready. At most
        max_held different items are held, and stacks still too small for a slot can't have more than
        max_waiting_percent of the budget in them."""
        cfg = self.store_cfg
        # a custom list item is the list's to buy, until its limit's reached: then it's a store deal like any other
        custom = self.watch.kind(hit) == "custom" and not (self.custom_entries(hit) and self.custom_left(hit) == 0)
        if not self.prices or hit.options or custom \
                or hit.name in self.db.slots or any(w in hit.name.lower() for w in self.never_buy):
            return False
        consumable = cfg["consumables"].get(hit.name)  # [HP, MP] it restores: bought by its value, not its market
        if not consumable and self.store_rules and self.store_rules.why_not(hit.name):  # not worth trading (store_rules.py)
            return False
        reference = self.prices.reference(hit.name)[0]
        if consumable:  # at the NPC store's best value (Meat's HP, Lemon's MP a meso) or better
            limit = consumable[0] / cfg["hp_per_meso"] + consumable[1] / cfg["mp_per_meso"]
            if reference:  # and selling it at its value has to make something after the store's 3%
                limit = min(limit, int(reference * (1 - SHOP_FEE)) - 1)
        else:
            limit = reference * (100 - self.market_below) / 100 if reference else 0
        if not reference or hit.asking > limit:
            return False
        # cheap enough: an opportunity, logged whether it's bought or not
        sold = self.prices.sold(hit.name)
        held = holdings.load()
        if not self.store_ready and hit.asking > self.profit_pool and sold >= cfg["min_sold_7d"]:
            full = self.store_full(held)
            if full >= cfg["ready_when_broke"]:
                self.set_store_ready(f"out of money for store deals with {full} store slots full")
        stack = self.stack_size(hit.name)
        why = ("the store's items are ready" if self.store_ready
               else f"a full stack held ({held[hit.name]['quantity']:,} of {stack:,})" if held.get(hit.name, {}).get("quantity", 0) >= stack
               else f"sold only {sold} times in a week" if sold < cfg["min_sold_7d"]
               else f"no money for store deals yet ({self.profit_pool:,})" if hit.asking > self.profit_pool
               else f"already {len(held)} different items held" if hit.name not in held and len(held) >= cfg["max_held"]
               else f"no {self.slot_kind(hit.name)} slot left: {self.slot_counts(held)}" if hit.name not in held and not self.slot_free(hit.name, held)
               else f"{self.waiting_money(held):,} already in stacks not worth a slot yet"
               if self.waiting_money(held) + hit.asking > self.waiting_cap() and not (hit.name in held and self.is_full(hit.name, held[hit.name]))
               else "")
        self.log_opportunity(hit, reference, sold, why or "to buy")
        return not why

    def set_store_ready(self, why):
        self.store_ready = True
        then = ("store deals stop; it carries on crawling for NPC deals and the scans" if self.store_cfg["keep_scanning"]
                else "the loop ends after this room")
        print(f"    STORE READY: {why}: {then}")
        self.doing(f"store ready ({why}): {then}")

    def log_opportunity(self, hit, reference, sold, verdict):
        """logs/store_opportunities.csv: every listing cheap enough to be a store deal, once per store
        visit, with why it wasn't bought if it wasn't."""
        key = (getattr(self, "current_place", ""), self.scan_owner, hit.name, hit.asking)
        seen = self.__dict__.setdefault("opportunities_seen", set())
        if key in seen:
            return
        seen.add(key)
        path = LOG_DIR / "store_opportunities.csv"
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "place", "owner", "item", "asking", "reference", "under_percent", "profit_each", "sold_7d", "verdict"])
            w.writerow([f"{datetime.now():%Y-%m-%d %H:%M:%S}", key[0], key[1], hit.name, hit.asking, round(reference),
                        round(100 * (1 - hit.asking / reference)), int(reference * (1 - SHOP_FEE)) - hit.asking, sold, verdict])

    def item_cap_percent(self, name):
        """How much of the budget one item may have in it, by how often the scans saw it sell in a
        week (demand_caps: [[sales, percent], ...], most first): a slow seller ties money up longer."""
        sold = self.prices.sold(name) if self.prices else 0
        for least, percent in self.store_cfg["demand_caps"]:
            if sold >= least:
                return min(percent, self.store_cfg["max_budget_percent"])
        return 0

    def store_deal_cap(self, deal, quantity):
        """Fewer of a store deal than quantity when all of that item held would cost over its share of
        the budget (item_cap_percent), unless each one makes big_deal_profit or more. And when the item
        still wouldn't take a slot after buying them, no more than fits under the cap on money in
        stacks still building up. Never more than fills one stack (one store slot), big deal or not."""
        cfg = self.store_cfg
        held = holdings.load()
        was = held.get(deal.name, {"quantity": 0, "paid": 0, "first": time.time()})
        quantity = max(0, min(quantity, self.stack_size(deal.name) - was["quantity"]))
        if self.profit_each(deal, True) < cfg["big_deal_profit"]:  # all of it held, not this purchase alone
            room = int(self.npc_budget * self.item_cap_percent(deal.name) / 100) - was["paid"]
            quantity = max(0, min(quantity, room // max(deal.asking, 1)))
        after = {**was, "quantity": was["quantity"] + quantity, "paid": was["paid"] + quantity * deal.asking}
        if quantity and self.is_full(deal.name, after):
            return quantity  # this purchase makes it worth a slot
        waiting = self.waiting_money({k: v for k, v in held.items() if k != deal.name}) + was["paid"]
        return max(0, min(quantity, (self.waiting_cap() - waiting) // max(deal.asking, 1)))

    def market_deal(self, hit):
        if self.store_deals:
            return self.store_deal(hit)
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
        split = f" | for custom deals {self.profit_pool:,}" if self.custom_from_profits else ""
        info = (f"budget {self.budget:,} | spent {self.spent:,}{split} | bought {sum(b[1] for b in self.bought)} items\n"
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
            self.refresh_custom()
            self.check_tips()
            skip = next((w for w in self.skip_shops_with if w in pc.norm(names[i - 1])), None)
            if skip:
                print(f"  shop {i}/{len(shops)} '{names[i - 1]}': name has '{skip}'; skipped")
                continue
            print(f"  shop {i}/{len(shops)} '{names[i - 1]}'")
            self.marks.set("target", [box(*coin, TARGET, f"shop {i}/{len(shops)}")])
            self.doing(f"opening shop {i}/{len(shops)}")
            self.current_sign = (x / frame.shape[1], y / frame.shape[0])
            self.current_title = names[i - 1]
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

    def read_shop_names(self, frame, shops, always=False):
        """The text on each shop's sign (sitting above its coin icon), for skipping shops by
        name before opening them, and for finding a scouter's shop by it. Shops to skip are boxed
        orange on the overlay, with a red box where the skip word was read; --debug also saves all
        of it to debug/shop_names.png. Not read when nothing needs it."""
        if not (self.skip_shops_with or always or self.scout):
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
        if getattr(getattr(self, "args", None), "debug", False):
            self.save_shop_names(frame, areas, marks)
        return names

    def tip_order(self, d, frame, shops, names):
        """The order to try the shops in for a scouter's find: [(index into shops, why)]. Signs reading the
        find's shop name first, whatever the game window's size; then signs whose name couldn't be read;
        then every other shop. Each group nearest where the scouter saw the sign first (a share of the
        screen, so only roughly right when the windows aren't the same size)."""
        h, w = frame.shape[:2]
        sign = d.get("sign")
        away = lambda i: math.hypot(shops[i][0][0] - sign[0] * w, shops[i][0][1] - sign[1] * h) if sign else 0.0
        title = pc.norm(d.get("title") or "")
        scores = [fuzz.ratio(pc.norm(n), title) if n and len(title) >= 3 else 0 for n in names]
        named = sorted((i for i, sc in enumerate(scores) if sc >= TIP_TITLE_MATCH), key=lambda i: (-scores[i] // 5, away(i)))
        unread = sorted((i for i, n in enumerate(names) if len(pc.norm(n)) < 3 and i not in named), key=away)
        rest = sorted((i for i in range(len(shops)) if i not in named and i not in unread), key=away)
        return ([(i, f"its sign reads '{names[i]}'") for i in named] + [(i, "its sign couldn't be read") for i in unread]
                + [(i, f"one of the other shops ('{names[i]}')") for i in rest])

    def is_tip_shop(self, d, frame, by_name):
        """Whether the store open is the find's: its owner is the one the scouter read. Without an owner to
        go by, a sign reading the shop's name is enough, and otherwise the item at its price has to be
        on the first page."""
        if not self.find_store(frame):
            return False
        owner = pc.norm(d.get("owner") or "")
        if len(owner) >= 3:
            loc, s = self.finder.last_loc, self.finder.last_scale or 1
            here = scan_log.read_box(frame, self.reader, scan_log.OWNER_BOX, loc, s)[0]
            return fuzz.ratio(pc.norm(here), owner) >= TIP_OWNER_MATCH
        if by_name:
            return True
        _, _, singles, _ = pc.scan(frame, self.reader, self.finder, self.db, 1, False)
        return any(h.name == d.get("item") and h.asking == d.get("asking") for h in singles)  # the best of what's wanted

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
        """Buys what's worth buying, and logs everything listed (see scan_log)."""
        visit = scan_log.ShopVisit(channel)
        self.scan_owner = ""
        self.current_place = channel
        self.opportunities_seen = set()  # a new store: its listings are new opportunities
        self.park_mouse()  # a cursor on the list brings up a tooltip over its rows
        visit.listed = {}  # a scouter's: every listing read, by (name, price, options)
        try:
            visit.complete = self.read_store_pages(channel, visit)
        finally:
            self.save_scan(visit)
        if self.scout:
            self.send_visit(channel, visit)

    def equipment_ok(self, deal, visit, page, channel):
        """Whether an item from the equipment finder has the stats asked for: hover it twice, read its
        tooltip each time. Only a sure yes buys. What's asked of a row is only worked out once, even
        if the row is on the page read again."""
        entries = self.custom_entries(deal)
        finder = entries and len(entries) == 1 and entries[0].get("finder")
        if not finder:
            return not (entries and any(i.get("finder") for i in entries))  # a cut-off name can't be checked
        s = self.finder.last_scale or 1
        x0, y0, x1, y1 = deal.box
        key = (deal.name, deal.asking, round(y0 / (20 * s)), page)
        if key in visit.equip_checked:
            return visit.equip_checked[key]
        wanted = equip_check.wanted_stats(finder)
        spots = (("icon", x0 - 40 * s, (y0 + y1) / 2), ("text", (x0 + x1) / 2, (y0 + y1) / 2))
        self.doing(f"equipment finder: checking the stats of {deal.name}")
        reads, frame, x, y = [], None, x0, y0
        for hover, x, y in spots:  # if the icon brings up nothing, the row's text might
            for _ in range(2):
                self.controls.move(*self.screen.to_screen(x, y))
                self.controls.wait(0.6)
                frame = self.screen.grab()
                try:
                    reads += [equip_check.stats_in(lines, wanted) for lines in equip_check.read_area(frame, self.reader, x, y, s)]
                except (cv2.error, ValueError) as e:
                    print(f"    equipment finder: couldn't read it: {e}")
                self.park_mouse()  # so the next look is a tooltip opened afresh
            if any(reads):
                break
        verdict, values = equip_check.judge(reads, wanted)
        print(f"  EQUIPMENT FINDER {deal.name} at {deal.asking:,}: read {values}, wanted at least {wanted}: "
              + {"yes": "it matches", "no": "not this one", "unsure": "couldn't be sure, so not buying it"}[verdict])
        self.log(channel, deal, 0, f"equipment finder: {verdict}, read {values}")
        try:
            equip_check.record(frame, channel, self.scan_owner, deal, wanted, values, verdict, x, y)
        except OSError as e:
            print(f"    equipment finder: couldn't log it: {e}")
        visit.equip_checked[key] = verdict == "yes"
        return visit.equip_checked[key]

    def scan_page(self, visit, page_number, frame, lines):
        """Adds a page to what's logged of this store. Only the first look at a page, as buying
        from it turns rows grey. Never gets in the way of the crawling."""
        if not self.scans or page_number in visit.pages:
            return
        visit.pages.add(page_number)
        try:
            if not visit.begun:
                loc, s = self.finder.last_loc, self.finder.last_scale or 1
                visit.owner, visit.score = scan_log.read_box(frame, self.reader, scan_log.OWNER_BOX, loc, s)
                visit.title, _ = scan_log.read_box(frame, self.reader, scan_log.TITLE_BOX, loc, s)
                visit.begun = True
                self.scan_owner = visit.owner
            visit.rows = scan_log.merge_page(visit.rows, scan_log.read_rows(lines, frame))
        except Exception as e:  # the log isn't worth stopping for
            print(f"    couldn't log this store's page: {e}")

    def save_scan(self, visit):
        if not self.scans:
            return

        def match(name):
            found = self.db.candidates(name)
            return found[0][0] if len(found) == 1 else None

        try:
            result = self.scans.save(visit, match)
            if result:
                print(f"    store of {visit.owner or '(name not read)'}: " + (f"{len(visit.rows)} listings logged" if result == "logged"
                      else f"logged less than {self.scans.hours:g}h ago, not added again"))
        except Exception as e:
            print(f"    couldn't log this store: {e}")
        if self.scans.export_due():
            self.export_scans()

    def export_scans(self):
        if self.scans:
            try:
                self.scans.export()
            except (sqlite3.Error, OSError) as e:  # another program can have the file open: next time
                print(f"    couldn't update {scan_log.EXPORT_FILE.name}: {e}")

    def log_own_buy(self, channel, deal, quantity):
        if self.scans:
            try:
                self.scans.bought(channel, self.scan_owner, deal.name, quantity, deal.asking)
            except sqlite3.Error as e:
                print(f"    couldn't log this purchase: {e}")

    def read_store_pages(self, channel, visit):
        """True when it got to the end of the store's list, False if the store went away first."""
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
                return False
            (lx0, ly0, lx1, ly1), _ = found  # the item list
            row_height = (ly1 - ly0) / LIST_ROWS

            def row_key(hit):
                return hit.name, hit.asking, page_number, round((hit.box[1] - ly0) / row_height)

            self.doing("reading the store's listings")
            lines, _, singles, _ = pc.scan(frame, self.reader, self.finder, self.db, 1, False)
            self.mark_listings(lines, singles)
            self.log_page(channel, page_number, lines, singles, done, row_key)
            self.scan_page(visit, page_number, frame, lines)
            if self.scout:  # it only collects: what's listed goes to the buyers, who judge it (send_visit)
                for h in singles:
                    visit.listed.setdefault((h.name, h.asking, h.options), h)
            else:
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
                    if self.review_pause and not customs and not (self.buy_custom and all(self.watch.kind(h) == "custom" for h in new)):
                        self.controls.wait(REVIEW_PAUSE)  # your rules already say what to do with custom items
                if customs:  # before any other deal: it's what you're looking for
                    deal = customs[0]
                    key = row_key(deal)
                    if not self.equipment_ok(deal, visit, tuple(l.text for l in lines), channel):
                        done.add(key)
                        continue
                    buys[key] += 1
                    ask = self.confirm_all or any(i["confirm"] for i in self.custom_entries(deal))
                    if not self.buy(deal, frame, channel, market=True, ask_you=ask, custom=True) \
                            or buys[key] >= MAX_BUYS_PER_LISTING:
                        done.add(key)
                    continue
                # NPC deals (guaranteed profit) are bought on their own; market deals wait for you
                npc = [d for d in singles if (self.worth_buying(d) or self.hold_buy(d)) and row_key(d) not in done]
                mkt = [d for d in singles if not self.worth_buying(d) and self.market_deal(d) and row_key(d) not in done]
                if npc or mkt:
                    deal = (npc or mkt)[0]
                    key = row_key(deal)
                    # a listing that's still there (not grey) after a purchase has stock left, because its
                    # stock number was read low: it gets bought again (a stack it can't read is skipped)
                    buys[key] += 1
                    ask = self.confirm_all or (not npc and not self.store_deals and deal.name not in self.market_auto)
                    if not self.buy(deal, frame, channel, market=not npc, ask_you=ask) or buys[key] >= MAX_BUYS_PER_LISTING:
                        done.add(key)
                    continue  # the list changes after a purchase: read this page again
            page = tuple(l.text for l in lines)
            # the same again may just be rows that look alike: only twice in a row is the end of the list
            same_pages = same_pages + 1 if page == last_page else 0
            if same_pages >= SAME_PAGES_AT_END:
                return True
            if not any(l.y1 > ly1 - row_height * 1.5 for l in lines):
                return True  # nothing in the bottom row: it all fits and there's nothing to scroll. Not by
                # counting rows, as pale sold-out ones can go unread
            last_page = page
            page_number += 1
            self.doing("scrolling the store list")
            self.controls.wheel(*self.screen.to_screen((lx0 + lx1) / 2, (ly0 + ly1) / 2))
            self.park_mouse()
            self.controls.wait(0.2)
        return True

    def log_page(self, channel, page, lines, singles, done, row_key):
        """logs/store_reads_<date>.txt: what was read on each page of each store, and what it made of
        each listing, to look into a deal that was missed."""
        rows = []
        for h in singles:
            verdict = ("done with" if row_key(h) in done else "custom buy" if self.custom_buy(h) else "NPC deal" if self.worth_buying(h)
                       else "to hold" if self.hold_buy(h)
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
            if name and pc.to_int(m.group(1)) == deal.qty and pc.to_int(m.group(2)) == deal.asking:
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
        store_deal = market and not custom and self.store_deals
        hold = not market and not custom and self.hold_buy(deal)  # kept, not flipped: paid like a custom list item
        from_profits = ((custom or hold) and self.custom_from_pool()) or store_deal
        remaining = self.profit_pool if from_profits else self.budget - self.spent
        self.doing(f"{'market deal' if market else 'deal'} on {deal.name}: reading the stock")
        # None when unreadable, which is normal for equipment (its icon shows no number). Which buy
        # window opens settles it: "Are you sure?" means a single item; "How many?" means something
        # that stacks with no number on its icon, which the game shows for a stock of 1: it buys 1
        # (the total is still checked, and a row that doesn't go grey is topped up as usual).
        # the number on the icon of throwing stars is how many stars: they're always one item to buy
        stock_read = None if deal.stars else self.read_stock(frame, deal)
        stock = stock_read or 1
        quantity = self.buy_quantity(deal, stock, remaining, custom)
        if store_deal:
            capped = self.store_deal_cap(deal, quantity)
            if capped < quantity:
                print(f"    {quantity} would be over one stack ({self.stack_size(deal.name):,}), over {self.item_cap_percent(deal.name):g}% "
                      f"of the budget, or put too much in stacks not worth a slot yet ({self.waiting_money():,} of {self.waiting_cap():,}): buying {capped}")
            quantity = capped
            reference = self.prices.reference(deal.name)[0]
            print(f"  STORE DEAL {deal.name}: asking {deal.asking:,}, reference {reference:,.0f} "
                  f"({100 * (deal.asking - reference) / reference:+.0f}%), sold {self.prices.sold(deal.name)} times in a week, "
                  f"stock {stock_read or 'not shown'}; auto-buy")
        elif market:
            median = (self.market.lowest(deal.names) or {}).get("median_24h")  # a fixed price needs none
            against = f"24h median {median:,} ({100 * (deal.asking - median) / median:+.0f}%)" if median else "no market price"
            print(f"  {'CUSTOM SEARCH' if custom else 'MARKET DEAL'} {deal.name}: asking {deal.asking:,}, {against}, "
                  f"stock {stock_read or 'not shown'}{'; asks you' if ask_you else '; auto-buy'}")
        elif hold:
            print(f"  TO HOLD {deal.name}: asking {deal.asking:,}, NPC pays {deal.npc_pays:,} (up to {min(self.hold[n] for n in deal.names):,} over); keeping it")
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
        # a custom list item is wanted, not flipped; a store deal, and an NPC deal on something that stacks,
        # add up from small buys (a stack takes one inventory slot however many trips it's bought on)
        if not custom and not store_deal and not hold and not (not market and self.stacks(deal)):
            total = quantity * self.profit_each(deal, market)
            if total < self.min_total_profit:
                print(f"    {quantity} x {self.profit_each(deal, market):+,} = {total:+,} profit, under the "
                      f"{self.min_total_profit:,} minimum per purchase; skipped")
                self.log(channel, deal, 0, f"total profit {total} < {self.min_total_profit}")
                return
        if not market and not hold and self.too_thin_for_budget(deal, quantity):
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
            quantity = 1  # already at most 1 (stock taken as 1), so this only says it out loud
            print("    it stacks, but its icon shows no number (a stock of 1): buying 1")

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
            if from_profits:
                self.pay_custom(expected)
            else:
                self.spent += expected
            self.log_own_buy(channel, deal, quantity)
            if custom:
                self.book.record(deal.name, quantity)
                self.log_custom_buy(channel, deal, quantity, expected, "you confirmed" if ask_you else "auto-bought")
                wanted = self.custom_left(deal)
                print(f"    custom list: {'no limit' if wanted is None else f'{wanted} more to buy'} of {deal.name}")
            if store_deal:
                held = holdings.record(deal.name, quantity, expected)
                full = self.store_full()
                print(f"    held for your store: {held['quantity']:,} x {deal.name}, {full} of {self.store_cfg['max_items']} store slots full")
                if full >= self.store_cfg["max_items"]:
                    self.set_store_ready(f"{full} store slots full")
            if market:
                value = quantity * ((self.prices.reference(deal.name)[0] or 0) if store_deal
                                    else (self.market.lowest(deal.names) or {}).get("median_24h", 0))
                self.bought.append((deal.name, quantity, expected, value, "market"))
                worth = f"about {value:,} at today's market median"
                how += " (market deal, you confirmed)" if ask_you else " (market deal, auto-bought)"
                self.log_market_buy(channel, deal, quantity, "you confirmed" if ask_you else "auto-bought")
            else:
                value = quantity * deal.npc_pays
                self.bought.append((deal.name, quantity, expected, value, "npc"))
                worth = f"NPC value {value:,}"
                if hold:
                    how += " (to hold: never sold at the NPC)"
                if ask_you:
                    how += " (you confirmed)"
                if self.market.lowest(deal.names):  # a market item too: log it with the others
                    self.log_market_buy(channel, deal, quantity, f"NPC deal, NPC pays {deal.npc_pays:,}")
            left = f"Left for custom deals {self.profit_pool:,}" if from_profits else f"Budget left {self.budget - self.spent:,}"
            print(f"    BOUGHT {quantity} for {expected:,}{how} ({worth}). {left}")
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
        if from_profits:
            self.pay_custom(max(paid, 0))
        else:
            self.spent += max(paid, 0)
        if paid > 0:
            self.log_own_buy(channel, deal, max(1, paid // deal.asking))
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
        npc = [b for b in getattr(self, "bought", []) if b[4] == "npc"]
        if npc:  # counted when bought, sold yet or not
            paid, worth = sum(b[2] for b in npc), sum(b[3] for b in npc)
            text += ["", f"NPC deals: {sum(b[1] for b in npc):,} items for {paid:,} mesos, worth {worth:,} at the NPC: {worth - paid:+,} profit"]
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

    # ---- scouting: finds sent to a buyer, and a buyer going for them (tips.py)

    def find_profit(self, deal, kind, quantity):
        """About what a find makes, to rank finds by: NPC deals at the NPC's price, store and market deals
        at their price after the shop fee, custom items at the market median. Held items: nothing."""
        if kind == "npc":
            return deal.profit * quantity
        if kind in ("store", "market"):
            ref = (self.prices.reference(deal.name)[0] if kind == "store" and self.prices
                   else (self.market.lowest(deal.names) or {}).get("median_24h")) or 0
            return int((ref * (1 - SHOP_FEE) - deal.asking) * quantity)
        if kind == "custom":
            median = (self.market.lowest(deal.names) or {}).get("median_24h")
            return int((median - deal.asking) * quantity) if median else 0
        return 0

    def send_visit(self, channel, visit):
        """A scouter's store: everything still for sale in it, and where it is, to the buyers. They each
        judge it by their own rules (judge_visit). Nothing is judged or bought here."""
        import tips
        rows = []
        for (name, asking, options), h in visit.listed.items():
            stock = None if h.stars else next((r["stock"] for r in visit.rows if r["price"] == asking and not r["sold"]
                                                and pc.norm(r["name"])[:8] == pc.norm(name.rstrip("."))[:8]), None)
            rows.append({"name": name, "options": list(options), "qty": h.qty, "asking": asking, "npc_pays": h.npc_pays, "stock": stock})
        if not rows:
            return
        if self.tips:
            self.tips.send(tips.visit(channel, self.current_sign, self.current_title, visit.owner or self.scan_owner, visit.title, rows))
        self.doing(f"store sent to the buyers: {len(rows)} listings")

    def judge_visit(self, d):
        """A scouter's store, by this bot's own rules and money: [(row, kind, profit)] of what it would buy,
        best first. Kinds as in buying: custom, npc (NPC deals, while there's money for them), hold, then
        store or market deals."""
        import tips
        wanted = []
        was = (self.current_place, self.scan_owner)
        self.current_place, self.scan_owner = d.get("place", ""), d.get("owner", "")  # for the opportunities log
        try:
            for r in d.get("rows", []):
                hit = tips.hit(r)
                kind = ("custom" if self.custom_buy(hit) else "npc" if self.worth_buying(hit) and not self.money_low()
                        else "hold" if self.hold_buy(hit) else None)
                if kind is None and not self.worth_buying(hit) and self.market_deal(hit):
                    kind = "store" if self.store_deals else "market"
                if kind:
                    wanted.append((r, kind, self.find_profit(hit, kind, r.get("stock") or 1)))
        finally:
            self.current_place, self.scan_owner = was
        return sorted(wanted, key=lambda w: -w[2])

    def check_tips(self):
        """A buyer, between shops: the scouters' new stores judged (judge_visit); one with something to buy
        goes first, the most profitable first (TipCall), unless it's on a find already. Stores with
        nothing for this bot, and ones too old to be worth going for, are marked done."""
        if not self.tips or self.scout or self.on_tip:
            return
        import tips
        queue = self.__dict__.setdefault("tip_queue", [])
        for task in self.tips.new():
            d = task["data"]
            if not tips.fresh(d):
                self.tips.done(task, "too old")
                continue
            wanted = self.judge_visit(d)
            if not wanted:
                self.tips.done(task, "nothing for this bot")
                continue
            top = wanted[0][0]
            d.update(item=top["name"], asking=top["asking"], kind=wanted[0][1], profit=sum(p for _, _, p in wanted),
                     wanted=[f"{r['name']} at {r['asking']:,} ({k})" for r, k, _ in wanted])
            queue.append(task)
        for task in [t for t in queue if not tips.fresh(t["data"])]:
            queue.remove(task)
            self.tips.done(task, "too old by the time it came up")
        if queue:
            task = max(queue, key=lambda t: (t["data"]["profit"], t["data"].get("seen", 0)))
            queue.remove(task)
            raise TipCall(task)

    def visit_tip(self, task):
        """In the find's room and channel: the find's shop (tip_order, is_tip_shop), read like any other
        (buying by this bot's own rules and money). A shop that turns out not to be it is left at once,
        nothing bought. Returns what happened."""
        d = task["data"]
        place = f"room {d['room']} channel {d['channel']}"
        self.on_tip = True
        try:
            frame = self.screen.grab()
            if self.find_store(frame):
                self.leave_store()
            end = time.monotonic() + NO_SHOPS_WAIT
            while True:
                frame = self.screen.grab()
                shops = self.find_shops(frame)
                if shops or time.monotonic() > end:
                    break
                self.controls.wait(0.5)
            if not shops:
                return "the shop wasn't found: no shops on screen"
            names = self.read_shop_names(frame, shops, always=True)
            h, w = frame.shape[:2]
            tried = 0
            for i, why in self.tip_order(d, frame, shops, names):
                self.controls.check()
                (x, y), coin = shops[i]
                self.marks.set("target", [box(*coin, TARGET, f"find: {d.get('title') or 'the shop'}?")])
                self.doing(f"looking for the find's shop: {why}")
                self.click_frame(x, y, double=True)
                tried += 1
                _, opened = self.wait_for(self.find_store, SHOP_OPEN_WAIT)
                if not opened:
                    continue
                self.controls.wait(0.4)
                if not self.is_tip_shop(d, self.screen.grab(), why.startswith("its sign reads")):
                    self.leave_store()
                    continue
                print(f"  the find's shop: {why} (shop {tried} tried)")
                self.current_sign, self.current_title = (x / w, y / h), names[i] or d.get("title", "")
                bought = len(self.bought)
                self.read_store(place)
                self.leave_store()
                new = self.bought[bought:]
                if not new:
                    return f"found ({why}), nothing bought: {d['item']} gone, or not a deal by this bot's rules"
                return f"found ({why}), bought " + ", ".join(f"{q} x {n} for {p:,}" for n, q, p, _, _ in new)
            return f"the shop wasn't found: {tried} of {len(shops)} shops opened, none was {d.get('owner') or d.get('title') or 'it'}'s (closed or moved)"
        finally:
            self.on_tip = False
            self.marks.clear("target")

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
        custom.add_argument("--custom-from-profits", action="store_true",
                            help="the budget is only for NPC deals; custom list items are bought with no limit, only from profits past it")
        ap.add_argument("--custom-start", type=parse_mesos, default=0,
                        help="with --custom-from-profits or --store-deals: how much of the budget they may spend from the start (default 0)")
        ap.add_argument("--npc-money", type=parse_mesos,
                        help="with --store-deals or --custom-from-profits: NPC deals get this much of the budget and no more; "
                             "the rest, and every profit past it, is for store and custom deals (instead of --custom-start)")
        ap.add_argument("--fresh", action="store_true",
                        help="start fresh instead of going on from where the last run stopped (data/loop_state.json)")
        ap.add_argument("--store-deals", action="store_true",
                        help="also buy market deals, from profits only, to sell in your own store (see store_deals in crawler_settings.json); "
                             "--market-percent is how far under their reference price")
    if sell_loop:
        ap.add_argument("--rounds", type=int, default=0, help="stop after this many rooms (default: until you stop it)")
        ap.add_argument("--rooms", type=parse_rooms,
                        help="the only rooms to go through, in order: 6-1, or 3,10,15, or a mix like 6-1,16-13 (default: all of them)")
        ap.add_argument("--sell-below", type=float,
                        help="go and sell early under this percent of the budget (default: from the settings)")
        ap.add_argument("--scout", action="store_true",
                        help="a scouter: every channel of every room, over and over, buying and selling nothing; what the "
                             "buyer would buy goes to it through the server (tips.py, the launcher's Server tab)")
        ap.add_argument("--scout-for", default="*", metavar="BOT",
                        help="with --scout: the buyer's name on the server (default: any bot that takes finds)")
        ap.add_argument("--no-finds", action="store_true", help="a buyer that doesn't go for a scouter's finds")
        ap.add_argument("--wait-for-finds", action="store_true",
                        help="a buyer that stays where it is, crawls nothing, and only moves to buy the scouters' finds "
                             "(by its own rules and money); it still sells when the money runs low")
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
        args.confirm, args.npc_only = False, not args.store_deals

    crawler = Crawler(args)
    crawler.store_deals = bool(getattr(args, "store_deals", False))
    if sell_loop:
        import tips
        crawler.scout = bool(args.scout)
        crawler.waiting = bool(args.wait_for_finds) and not args.scout
        if args.scout or not args.no_finds or crawler.waiting:
            crawler.tips = tips.Tips.from_settings(send_to=args.scout_for)
            if crawler.tips:
                crawler.tips.start(take=not args.scout)
                print(f"  scouting: buys nothing, sends finds to {args.scout_for if args.scout_for != '*' else 'any buyer'} through the server"
                      if args.scout else "  waits where it is for the scouters' finds, and only moves to buy them" if crawler.waiting
                      else "  takes the scouter's finds from the server, the most profitable first")
            elif args.scout:
                print("  scouting, but the server isn't set up (the launcher's Server tab): finds are only logged")
    if sell_loop:  # nobody is watching the full sell loop: those items are only logged, not waited on
        crawler.review_pause = False
    if not args.dry_run:
        crawler.confirm_all = ask_confirm_mode() if args.confirm is None else args.confirm
        print("  every purchase waits for you to press OK or Cancel" if crawler.confirm_all
              else "  deals are bought automatically")
    crawler.market_deals = not (ask_npc_only() if args.npc_only is None else args.npc_only)
    if crawler.watch.custom:
        crawler.custom_from_profits = getattr(args, "custom_from_profits", False)
        crawler.buy_custom = crawler.custom_from_profits or (ask_buy_custom() if args.buy_custom is None else args.buy_custom)
    needs_prices = crawler.buy_custom and crawler.book.needs_market(crawler.custom_from_profits)
    if crawler.market_deals or needs_prices:
        crawler.load_market()
    if not crawler.market_deals:
        print("  only NPC deals" + ("" if needs_prices else ": Free Market prices aren't looked up"))
    if crawler.buy_custom:
        crawler.update_custom_prices()
        print("  custom list: from what's set aside and profits past the budget, with no limit on any item" if crawler.custom_from_profits
              else "  custom list: buying by each item's own rules")
        for item in crawler.book.items.values():
            wanted = custom_items.left(item, crawler.custom_from_profits)
            amount = "only 1" if item["only_one"] else str(item["limit"])
            print(f"    {item['name']}: {custom_items.describe(item)}, "
                  f"{'no limit' if wanted is None else str(wanted) + ' more of ' + amount}, "
                  f"{'asks you' if item['confirm'] else 'auto-buy'}")
    if crawler.store_deals:
        crawler.market_below = args.market_percent or crawler.store_cfg["below_reference_percent"]
        cfg = crawler.store_cfg
        print(f"  store deals: {crawler.market_below:g}% or more under their reference price, sold {cfg['min_sold_7d']}+ times in "
              f"the last week, from profits only, up to {cfg['max_budget_percent']:g}% of the budget each (no cap at "
              f"{cfg['big_deal_profit']:,}+ profit each); ends once {cfg['max_items']} items are ready for your store")
    elif crawler.market and crawler.market_deals:
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
            crawler.export_scans()
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
