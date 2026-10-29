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
    python crawler.py                    # asks the budget, crawls this room, then channels 40..1
    python crawler.py --budget 250k --channels 12-1 --minutes 20
    python crawler.py --no-channel-change
"""
import argparse
import csv
import ctypes
import json
import ctypes.wintypes as wt
import re
import sys
import threading
import time
import winsound
from datetime import datetime
from pathlib import Path

import cv2
import mss
import numpy as np

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
TOTAL_RE = re.compile(r"total\s*price\s*is\s*([\d,.]+)\s*meso", re.I)
CH1_CELL = (64, 128)                # from the "CHANGE CHANNEL" title to CH.1's centre
CH_STEP = (131, 41)                 # cell spacing; 8 channels per row
CH_HIGHLIGHT_PATCH = (-45, -30, 6)  # x range and +-y around a cell centre to read its colour

VK = {"esc": 0x1B, "enter": 0x0D, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
      "backspace": 0x08, "end": 0x23, **{str(d): 0x30 + d for d in range(10)}}
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

    def type_number(self, n):
        for d in str(n):
            self.key(d)


# ---------------------------------------------------------------- screen

class Screen:
    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.sct = getattr(mss, "MSS", mss.mss)()
        self.region = pc.client_rect(hwnd)

    def grab(self):
        self.region = pc.client_rect(self.hwnd)
        return np.ascontiguousarray(np.array(self.sct.grab(self.region))[:, :, :3])

    def to_screen(self, x, y):
        return int(self.region["left"] + x), int(self.region["top"] + y)


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
        self.quantity_scale = None
        self.ui_scale = None
        self.controls = None
        LOG_DIR.mkdir(exist_ok=True)
        self.log_path = LOG_DIR / f"crawl_{datetime.now():%Y-%m-%d}.csv"
        settings = json.loads(SETTINGS_FILE.read_text("utf-8"))
        self.buy_rules = sorted((r["price_from"], r["min_profit"]) for r in settings["buy_rules"])
        self.never_buy = [w.lower() for w in settings.get("never_buy", [])]
        self.budget = 0
        self.spent = 0
        self.bought = []  # (name, quantity, paid, npc value)

    def required_profit(self, asking):
        """Minimum profit per item the buy rules ask for at this price."""
        need = None
        for price_from, min_profit in self.buy_rules:
            if asking >= price_from:
                need = min_profit
        return need  # None: below every rule, never buy

    def worth_buying(self, hit):
        if any(w in hit.name.lower() for w in self.never_buy):
            return False
        need = self.required_profit(hit.asking)
        return need is not None and hit.profit >= need

    # ---- helpers

    def scale_guess(self, frame):
        return self.ui_scale or self.finder.last_scale or round(frame.shape[0] / 1440, 2)

    def at(self, origin, offset, s):
        return origin[0] + offset[0] * s, origin[1] + offset[1] * s

    def click_frame(self, x, y, double=False):
        self.controls.click(*self.screen.to_screen(x, y), double=double)

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
        """Click points of every shop sign on screen, top row first."""
        s = self.scale_guess(frame)
        if self.ui_scale is None:  # settle the UI scale once, on a clearly matching coin icon
            v, _, best = max((best_match(frame, c, near(s)) for c in self.coins), key=lambda b: b[0])
            if v >= 0.85:
                self.ui_scale = s = best
        h, w = frame.shape[:2]
        points = []
        for coin in self.coins:
            res = cv2.matchTemplate(frame, scaled(coin, s), cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= SIGN_THRESHOLD)
            for x, y in sorted(zip(xs, ys), key=lambda p: -res[p[1], p[0]]):
                if all(abs(x - a) > 30 * s or abs(y - b) > 30 * s for a, b in points):
                    points.append((int(x), int(y)))
        clicks = []
        for x, y in points:
            cx, cy = self.at((x, y), SIGN_CLICK, s)
            if 10 < cx < w - 10 and 10 < cy < h * 0.9:  # keep clear of the edges and the bottom bar
                clicks.append((cx, cy))
        return sorted(clicks, key=lambda p: (round(p[1] / (60 * s)), p[0]))

    def crawl_room(self, channel):
        frame = self.screen.grab()
        if self.finder.find(frame):
            print("  a store is already open, leaving it first")
            self.leave_store()
            frame = self.screen.grab()
        shops = self.find_shops(frame)
        print(f"[channel {channel}] {len(shops)} shops on screen")
        for i, (x, y) in enumerate(shops, 1):
            self.controls.check()
            print(f"  shop {i}/{len(shops)}")
            self.click_frame(x, y, double=True)
            _, opened = self.wait_for(lambda f: self.finder.find(f), 3.0)
            if not opened:
                print("    didn't open (full, closed, or missed); skipping")
                continue
            self.controls.wait(0.4)  # let the list draw
            self.read_store(channel)
            self.leave_store()

    # ---- inside a store

    def read_store(self, channel):
        handled = set()
        last_page = None
        for _ in range(60):  # pages; a store holds far fewer
            frame = self.screen.grab()
            found = self.finder.find(frame)
            if not found:
                return
            (lx0, ly0, lx1, ly1), _ = found  # the item list
            lines, _, singles, _ = pc.scan(frame, self.reader, self.finder, self.db, 1, False)
            new = [d for d in singles if self.worth_buying(d) and (d.name, d.asking) not in handled]
            if new:
                deal = new[0]
                handled.add((deal.name, deal.asking))
                self.buy(deal, frame, channel)
                continue  # the list changes after a purchase: read this page again
            page = tuple(l.text for l in lines)
            if page == last_page:
                return  # scrolling no longer changes anything: end of the list
            if sum(1 for l in lines if pc.LISTING_RE.match(l.text)) < 5:
                return  # the list shows 6 rows; fewer means it all fits and there's nothing to scroll
            last_page = page
            self.controls.wheel(*self.screen.to_screen((lx0 + lx1) / 2, (ly0 + ly1) / 2))
            self.controls.wait(0.35)

    def read_stock(self, frame, hit):
        """The small number in the bottom-left of the item's icon: how many are for sale.
        None when it can't be read confidently."""
        s = self.finder.last_scale or 1
        x0, _, _, y1 = hit.box
        crop = frame[max(0, int(y1 - 24 * s)):int(y1 + 2 * s), max(0, int(x0 - 80 * s)):max(0, int(x0 - 35 * s))]
        if crop.size == 0:
            return None
        big = cv2.resize(crop, None, fx=3 / s, fy=3 / s, interpolation=cv2.INTER_CUBIC)
        # the spot is known, so skip text detection (it misses these outlined digits)
        result = self.reader.ocr(big, use_det=False, use_cls=False, use_rec=True)[0] or []
        for text, score in result:
            if score >= 0.75 and re.fullmatch(r"\d+", text):
                return int(text)
        return None

    def find_quantity_dialog(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        scales = [self.quantity_scale] if self.quantity_scale else near(self.scale_guess(frame), 0.1, 0.02)
        v, loc, s = best_match(gray, self.quantity_dialog, scales)
        if v >= 0.7:
            self.quantity_scale = s
            return loc
        return None

    def find_single_dialog(self, frame):
        """'Are you sure you want to buy it?': what an unstackable item asks instead of a quantity."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        base = self.quantity_scale or self.scale_guess(frame)
        v, loc, s = best_match(gray, self.single_dialog, near(base, 0.02))
        return (loc, s) if v >= 0.7 else None

    def find_total_dialog(self, frame):
        """The second confirmation ("The total price is X mesos. Will you make the purchase?").
        Same dialog style as the quantity one, so it uses that one's scale when known."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        base = self.quantity_scale or self.scale_guess(frame)
        v, loc, s = best_match(gray, self.total_dialog, near(base, 0.02))
        return (loc, s) if v >= 0.7 else None

    def read_total(self, frame, dialog):
        (x, y), s = dialog
        x0, y0, x1, y1 = TOTAL_LINE
        crop = frame[max(0, int(y + y0 * s)):int(y + y1 * s), max(0, int(x + x0 * s)):int(x + x1 * s)]
        if crop.size == 0:
            return None
        big = cv2.resize(crop, None, fx=2 / s, fy=2 / s, interpolation=cv2.INTER_CUBIC)
        text = " ".join(t for _, t, score in (self.reader.ocr(big)[0] or []) if score >= 0.8)
        m = TOTAL_RE.search(text)
        return int(re.sub(r"[,.]", "", m.group(1))) if m else None

    def read_mesos(self, frame):
        """Your mesos, shown in the store window under the seller. None if not read confidently."""
        if not self.finder.find(frame):
            return None
        (x, y), s = self.finder.last_loc, self.finder.last_scale
        crop = frame[max(0, int(y + MESOS_BOX[1] * s)):int(y + MESOS_BOX[3] * s),
                     max(0, int(x + MESOS_BOX[0] * s)):int(x + MESOS_BOX[2] * s)]
        if crop.size == 0:
            return None
        big = cv2.resize(crop, None, fx=2 / s, fy=2 / s, interpolation=cv2.INTER_CUBIC)
        result = self.reader.ocr(big)[0] or []
        texts = [t for _, t, score in result if score >= 0.9]
        if len(texts) != 1 or not re.fullmatch(r"[\d,.]+", texts[0]):
            return None
        return int(re.sub(r"[,.]", "", texts[0]))

    def confirm_listing(self, frame, deal):
        """Read the deal's row a second time, enlarged, and check it says the same thing:
        a misread price is the most expensive mistake an auto-buyer can make."""
        s = self.finder.last_scale or 1
        x0, y0, x1, y1 = deal.box
        pad = 8 * s
        crop = frame[max(0, int(y0 - pad)):int(y1 + pad), max(0, int(x0 - pad)):int(x1 + 60 * s)]
        big = cv2.resize(crop, None, fx=1.6 / s, fy=1.6 / s, interpolation=cv2.INTER_CUBIC)
        lines = self.reader.read(big)
        for l in lines:
            m = pc.LISTING_RE.match(l.text)
            name = pc.name_above(l, lines) if m else None
            if name and pc.to_int(m.group(1)) == 1 and pc.to_int(m.group(2)) == deal.asking:
                item = self.db.lookup(name.text)
                return bool(item) and item[0] == deal.name
        return False

    def buy(self, deal, frame, channel):
        remaining = self.budget - self.spent
        stock = self.read_stock(frame, deal) or 1
        quantity = min(stock, remaining // deal.asking)
        print(f"  DEAL {deal.name}: asking {deal.asking:,}, NPC pays {deal.npc_pays:,} "
              f"(+{deal.profit:,} each), stock {stock}")
        if quantity < 1:
            print(f"    over budget ({remaining:,} left); skipped")
            self.log(channel, deal, 0, "over budget")
            return
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
        if not self.confirm_listing(frame, deal):
            print("    a second read of the row disagreed; skipped to be safe")
            self.log(channel, deal, 0, "second read disagreed")
            return

        x0, y0, x1, y1 = deal.box
        self.click_frame((x0 + x1) / 2, (y0 + y1) / 2)  # select the row
        self.controls.wait(0.3)
        origin, s = self.finder.last_loc, self.finder.last_scale
        self.click_frame(*self.at(origin, BUY_ITEM, s))
        # stackable items ask "How many?"; single items go straight to "Are you sure?"
        _, dialog = self.wait_for(lambda f: (q := self.find_quantity_dialog(f)) and ("quantity", q)
                                  or (o := self.find_single_dialog(f)) and ("single", o), 3.0)
        if not dialog:
            print("    no buy window appeared; skipped")
            self.log(channel, deal, 0, "no buy window")
            return
        kind, where = dialog

        if kind == "single":
            quantity = 1
            expected = deal.asking
            self.controls.key("enter")  # OK
            _, closed = self.wait_for(lambda f: not self.find_single_dialog(f), 2.0)
            if not closed:
                self.click_frame(*self.at(where[0], SINGLE_OK, where[1]))
                _, closed = self.wait_for(lambda f: not self.find_single_dialog(f), 2.0)
                if not closed:
                    raise Stop("the purchase confirmation won't close")
        else:
            self.controls.key("end")
            self.controls.key("backspace", 6)
            self.controls.type_number(quantity)
            self.controls.key("enter")  # OK
            _, closed = self.wait_for(lambda f: not self.find_quantity_dialog(f), 2.0)
            if not closed:  # Enter didn't confirm: press the OK button
                self.click_frame(*self.at(where, QUANTITY_OK, self.quantity_scale))
                _, closed = self.wait_for(lambda f: not self.find_quantity_dialog(f), 2.0)
                if not closed:
                    raise Stop("the quantity window won't close")

            # second confirmation: it states the total, so check it before saying yes
            expected = quantity * deal.asking
            frame, total_dialog = self.wait_for(self.find_total_dialog, 3.0)
            if not total_dialog:
                raise Stop("the 'total price' confirmation didn't appear")
            total = self.read_total(frame, total_dialog)
            if total != expected:
                self.controls.key("esc")  # Cancel: nothing is spent
                self.wait_for(lambda f: not self.find_total_dialog(f), 2.0)
                print(f"    the game's total read as {total}, expected {expected:,}; cancelled")
                self.log(channel, deal, 0, f"total {total} != expected {expected}, cancelled")
                return
            self.controls.key("enter")  # OK
            _, closed = self.wait_for(lambda f: not self.find_total_dialog(f), 2.0)
            if not closed:
                self.click_frame(*self.at(total_dialog[0], TOTAL_OK, total_dialog[1]))
                _, closed = self.wait_for(lambda f: not self.find_total_dialog(f), 2.0)
                if not closed:
                    raise Stop("the purchase confirmation won't close")

        # check what was actually paid against what was expected
        _, changed = self.wait_for(lambda f: (m := self.read_mesos(f)) is not None and m != before and (m,), 3.0, 0.3)
        paid = before - changed[0] if changed else 0
        if paid == expected:
            self.spent += paid
            self.bought.append((deal.name, quantity, paid, quantity * deal.npc_pays))
            print(f"    BOUGHT {quantity} for {paid:,} (NPC value {quantity * deal.npc_pays:,}). "
                  f"Budget left {self.budget - self.spent:,}")
            self.log(channel, deal, quantity, f"bought for {paid}")
            winsound.MessageBeep(winsound.MB_OK)
            self.controls.wait(0.6)
            return
        self.log(channel, deal, quantity, f"paid {paid}, expected {expected}")
        if paid == 0:
            raise Stop("the purchase didn't go through (inventory full? store changed?)")
        self.spent += max(paid, 0)
        raise Stop(f"mesos went down by {paid:,} but {expected:,} was expected; stopping to be safe")

    def leave_store(self):
        for _ in range(2):
            if not self.finder.find(self.screen.grab()):
                return
            origin, s = self.finder.last_loc, self.finder.last_scale
            self.click_frame(*self.at(origin, LEAVE_STORE, s))
            _, gone = self.wait_for(lambda f: not self.finder.find(f), 3.0)
            if gone:
                self.controls.wait(0.4)
                return
        raise Stop("couldn't leave the store")

    # ---- channels

    def find_channel_window(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        v, loc, s = best_match(gray, self.channel_title, near(self.scale_guess(frame)))
        return (loc, s) if v >= 0.8 else None

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
            return None
        return ranked[0]

    def change_channel(self, target):
        print(f"changing to channel {target}")
        self.controls.key("esc")      # game menu
        self.controls.wait(0.4)
        self.controls.key("enter")    # its first entry: Change Channel
        frame, window = self.wait_for(self.find_channel_window, 2.5)
        if not window:
            self.controls.key("esc")
            print("  the channel window didn't open with Esc -> Enter; stopping channel changes")
            return False
        # park the mouse away from the window: hovering a cell may highlight it too
        h, w = frame.shape[:2]
        self.controls.move(*self.screen.to_screen(w * 0.5, h * 0.04))
        self.controls.wait(0.3)
        frame = self.screen.grab()

        start = self.highlighted_channel(frame, window)
        print(f"  selected when the window opened: {start}")
        if start is None:
            return self.give_up_channel(frame, "couldn't tell which channel is selected")
        if start == target:
            # that's the channel we're on (crawled already); never press Enter without moving
            self.controls.key("esc")
            self.wait_for(lambda f: not self.find_channel_window(f), 2.0)
            print(f"  already on channel {target}; skipping it")
            return None
        current = start
        while current != target:
            diff = target - current
            self.controls.key("down" if diff >= 8 else "up" if diff <= -8 else "right" if diff > 0 else "left")
            self.controls.wait(0.25)
            frame = self.screen.grab()
            now = self.highlighted_channel(frame, window)
            print(f"    {current} -> {now}")
            if now is None or now == current:
                return self.give_up_channel(frame, f"the arrow key didn't move the selection from {current}")
            current = now
        self.controls.key("enter")

        # it really changed only if the room goes away (loading) and then comes back
        _, left = self.wait_for(lambda f: f.mean() < 40 or not self.find_shops(f), 10.0, 0.3)
        if not left:
            return self.give_up_channel(self.screen.grab(), "pressed Enter but the channel didn't change")
        _, loaded = self.wait_for(lambda f: len(self.find_shops(f)) > 0, 25.0, 0.5)
        if not loaded:
            print("  no shops visible after the change; stopping")
            return False
        self.controls.wait(1.5)  # let other players' stores finish appearing
        return True

    def give_up_channel(self, frame, why):
        pc.DEBUG_DIR.mkdir(exist_ok=True)
        cv2.imwrite(str(pc.DEBUG_DIR / "channel_change_failed.png"), frame)
        if self.find_channel_window(frame):
            self.controls.key("esc")
        print(f"  {why}; stopping (screenshot: debug\\channel_change_failed.png)")
        return False

    # ---- entry points

    def dry_run(self):
        print("Switch to MapleStory now. Looking in 3 seconds... (nothing will be clicked)")
        time.sleep(3)
        if user32.GetForegroundWindow() != self.hwnd:
            print("warning: MapleStory isn't the front window, so the screenshot shows whatever covers it")
        frame = self.screen.grab()
        shops = self.find_shops(frame)
        out = frame.copy()
        for i, (x, y) in enumerate(shops, 1):
            cv2.circle(out, (int(x), int(y)), 14, (0, 0, 255), 3)
            cv2.putText(out, str(i), (int(x) + 16, int(y) + 8), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 3)
        lines, store, singles, _ = pc.scan(frame, self.reader, self.finder, self.db, 1, False)
        deals = [d for d in singles if self.worth_buying(d)]
        pc.DEBUG_DIR.mkdir(exist_ok=True)
        cv2.imwrite(str(pc.DEBUG_DIR / "crawler_dry_run.png"), out)
        print(f"UI scale {self.ui_scale}: {len(shops)} shop signs found; would open them in the numbered order")
        if store:
            print(f"a store is open: {len(singles)} single listings, {len(deals)} pass the buy rules; "
                  f"your mesos read as {self.read_mesos(frame)}")
            for d in deals:
                print(f"  would buy {d.name}: asking {d.asking:,}, NPC pays {d.npc_pays:,}, "
                      f"stock {self.read_stock(frame, d)}, second read agrees: {self.confirm_listing(frame, d)}")
        print(f"marked screenshot: {pc.DEBUG_DIR / 'crawler_dry_run.png'}")

    def run(self, channels, budget):
        if runs_with_more_rights(self.hwnd):
            raise SystemExit("MapleStory runs as administrator, so Windows would silently ignore the "
                             "crawler's clicks.\nStart it with crawler.bat (it asks for admin rights), or "
                             "run your terminal as administrator.")
        self.budget = budget
        rules = ", ".join(f"from {p:,}: +{m:,}" for p, m in self.buy_rules)
        print(f"Budget {budget:,} mesos. Buys when profit per item is at least ({rules}).")
        self.controls = Controls(self.hwnd, time.monotonic() + self.args.minutes * 60)
        print("Switch to MapleStory now. Starting in 3 seconds... (hold ESC for 1s to stop)")
        time.sleep(3)
        try:
            self.crawl_room("start")
            for ch in channels:
                changed = self.change_channel(ch)
                if changed is None:  # already on it
                    continue
                if not changed:
                    break
                self.crawl_room(ch)
            print("done")
        except Stop as e:
            print(f"stopped: {e}")
        finally:
            self.controls.stop.set()
        value = sum(b[3] for b in self.bought)
        print(f"\nBought {sum(b[1] for b in self.bought)} items for {self.spent:,} mesos "
              f"(NPC value {value:,}, profit {value - self.spent:,} once sold). Budget left {self.budget - self.spent:,}.")
        for name, qty, paid, npc in self.bought:
            print(f"  {qty} x {name}: paid {paid:,}, NPC pays {npc:,}")
        print(f"log: {self.log_path}")


def parse_channels(spec):
    a, _, b = spec.partition("-")
    a, b = int(a), int(b or a)
    return list(range(a, b - 1, -1)) if a >= b else list(range(a, b + 1))


def parse_mesos(text):
    """'250000', '250,000', '250k', '1.5m' -> int."""
    m = re.fullmatch(r"\s*([\d.,]+)\s*([km]?)\s*", text.lower())
    if not m:
        raise ValueError(text)
    number = float(m.group(1).replace(",", ""))
    return int(number * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)])


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="only look: mark the shops it would open")
    ap.add_argument("--budget", type=parse_mesos, help="most it may spend this session (asked if not given)")
    ap.add_argument("--channels", default="40-1", help="channels to visit after this room, in order (e.g. 40-1)")
    ap.add_argument("--no-channel-change", action="store_true", help="only crawl the current room")
    ap.add_argument("--minutes", type=float, default=30, help="stop after this long")
    ap.add_argument("--match", type=int, default=88, help="item name match strictness 0-100")
    ap.add_argument("--window", default="maplestory.exe")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    crawler = Crawler(args)
    if args.dry_run:
        crawler.dry_run()
    else:
        budget = args.budget or ask_budget()
        crawler.run([] if args.no_channel_change else parse_channels(args.channels), budget)


if __name__ == "__main__":
    main()
