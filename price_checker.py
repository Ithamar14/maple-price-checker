"""Watch the screen for MapleStory Classic player stores and box underpriced items.

An item is a "deal" when a player store asks less for it than an NPC pays when
you sell it, so buying it and selling it to any NPC is a guaranteed profit.

The tool only reads pixels from the screen (like a screenshot) and draws a
click-through overlay on top. It never touches the game's memory or input.

Usage:
    python price_checker.py                      # watch the primary monitor
    python price_checker.py --window "MapleStory" # follow a window by title
    python price_checker.py --list-windows
    python price_checker.py --image shot.png     # test on a screenshot
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import queue
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import mss
import numpy as np
from rapidfuzz import fuzz, process
from rapidocr_onnxruntime import RapidOCR

HERE = Path(__file__).parent
PRICES_FILE = HERE / "data" / "prices.json"
MANUAL_FILE = HERE / "data" / "manual_prices.json"
DEBUG_DIR = HERE / "debug"

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixels, so capture and overlay line up
except OSError:
    user32.SetProcessDPIAware()

STORE_KEYWORDS = ("buyitem", "leavestore", "sellerinfo")
VK_F8 = 0x77


# ---------------------------------------------------------------- price data

def norm(text):
    # OCR often drops the spaces in game fonts, so compare without them
    return re.sub(r"[^a-z0-9%]+", "", text.lower())


class PriceDB:
    def __init__(self, path, cutoff):
        data = json.loads(Path(path).read_text("utf-8"))
        self.cutoff = cutoff
        self.items = {}  # normalized name -> (name, npc_pays)
        for it in data["items"]:
            key = norm(it["name"])
            old = self.items.get(key)
            # Two items can share a name; keep the lower price so a "deal" is always real.
            if old is None or it["npc_pays"] < old[1]:
                self.items[key] = (it["name"], it["npc_pays"])
        downloaded = len(self.items)
        # prices you checked in game yourself win over the download
        manual = json.loads(MANUAL_FILE.read_text("utf-8")) if MANUAL_FILE.exists() else {}
        for name, pays in manual.items():
            if not name.startswith("_") and isinstance(pays, int) and pays > 0:
                self.items[norm(name)] = (name, pays)
        self.keys = list(self.items)
        print(f"loaded {downloaded} downloaded prices ({data.get('game')}, fetched {data.get('fetched')})"
              f" + {len(self.items) - downloaded} new from {MANUAL_FILE.name}")

    def lookup(self, text):
        key = norm(text)
        if len(key) < 4:
            return None
        if key in self.items:
            return self.items[key]
        m = process.extractOne(key, self.keys, scorer=fuzz.ratio, score_cutoff=self.cutoff)
        return self.items[m[0]] if m else None


# ---------------------------------------------------------------- OCR text handling

@dataclass
class Line:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2

    @property
    def h(self):
        return self.y1 - self.y0


# A player store listing is two lines: "Wolbi Throwing Stars" / "500 for 3,333 mesos"
NUM = r"([\dOolI][\dOolI,.]*)"
LISTING_RE = re.compile(rf"^\s*{NUM}\s*for\s*{NUM}\s*m\s*e\s*s\s*[o0]\s*[s5]?\s*$", re.I)
DIGIT_FIXES = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"})


def to_int(s):
    return int(re.sub(r"[,.]", "", s.translate(DIGIT_FIXES)))


def name_above(listing, lines):
    """The item name sits directly above its 'N for X mesos' line."""
    best = None
    for l in lines:
        gap = listing.y0 - l.y1
        if -0.3 * listing.h < gap < 1.2 * listing.h and abs(l.x0 - listing.x0) < 2 * listing.h and l is not listing:
            if best is None or gap < listing.y0 - best.y1:
                best = l
    return best


def is_faded(bgr, line):
    """Sold-out listings are drawn in light grey instead of black."""
    crop = bgr[max(0, int(line.y0)):int(line.y1) + 1, max(0, int(line.x0)):int(line.x1) + 1]
    if crop.size == 0:
        return False
    return np.percentile(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), 3) > 120


@dataclass
class Hit:
    name: str
    qty: int
    asking: int
    npc_pays: int
    box: tuple

    @property
    def profit(self):
        return self.npc_pays - self.asking


def analyze(lines, bgr, db, min_profit):
    """Return (store_seen, singles, deals). Bundles (N > 1) are ignored."""
    singles = []
    for l in lines:
        m = LISTING_RE.match(l.text)
        if not m:
            continue
        name = name_above(l, lines)
        if name is None or is_faded(bgr, name):
            continue
        item = db.lookup(name.text)
        qty, asking = to_int(m.group(1)), to_int(m.group(2))
        if item is None or qty != 1:
            continue
        box = (min(l.x0, name.x0), name.y0, max(l.x1, name.x1), l.y1)
        singles.append(Hit(item[0], qty, asking, item[1], box))

    compact = "".join(l.text.lower() for l in lines).replace(" ", "")
    store = any(k in compact for k in STORE_KEYWORDS) or bool(singles)
    deals = [h for h in singles if h.profit >= min_profit]
    return store, singles, deals


class StoreFinder:
    """Finds an open player store by its BUY ITEM / LEAVE STORE buttons (fast template match),
    so only the item list next to them needs OCR."""

    # Cut from a 2560x1440 screenshot; the game UI is smaller in a smaller window. The match
    # only holds within ~0.01 of the right size, so each frame tries a few sizes near the
    # expected one (window height / 1440, or the last size that worked), and every
    # SWEEP_EVERY seconds without a match it tries every size.
    TEMPLATE = HERE / "templates" / "store_buttons.png"
    REF_HEIGHT = 1440
    # item list area in template pixels, relative to the top-left of the matched buttons
    LIST_BOX = (-270, 105, 130, 580)
    ALL_SCALES = [round(s, 2) for s in np.arange(0.35, 1.31, 0.01)]
    SWEEP_EVERY = 30.0

    def __init__(self, threshold=0.7):
        self.tpl = cv2.imread(str(self.TEMPLATE), cv2.IMREAD_GRAYSCALE)
        self.threshold = threshold
        self.last_scale = None
        self.last_sweep = 0.0

    def _match(self, gray, scale):
        tpl = self.tpl if scale == 1 else cv2.resize(self.tpl, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if gray.shape[0] < tpl.shape[0] or gray.shape[1] < tpl.shape[1]:
            return 0, None
        _, score, _, loc = cv2.minMaxLoc(cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED))
        return score, loc

    def find(self, bgr):
        """Return ((x0, y0, x1, y1) of the item list, ui scale) or None."""
        if self.tpl is None:
            return None
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        guess = self.last_scale or gray.shape[0] / self.REF_HEIGHT
        order = sorted({round(guess + d, 2) for d in (-0.02, -0.01, 0, 0.01, 0.02)}, key=lambda s: abs(s - guess))
        if time.monotonic() - self.last_sweep > self.SWEEP_EVERY:
            self.last_sweep = time.monotonic()
            order += self.ALL_SCALES
        best = (0, None, None)
        for s in order:
            score, loc = self._match(gray, s)
            if score > best[0]:
                best = (score, loc, s)
            if score >= 0.85:  # clearly it, stop searching
                break
        score, loc, s = best
        if score < self.threshold:
            return None
        self.last_scale = s
        x, y = loc
        dx0, dy0, dx1, dy1 = (round(d * s) for d in self.LIST_BOX)
        h, w = gray.shape
        return (max(0, x + dx0), max(0, y + dy0), min(w, x + dx1), min(h, y + dy1)), s


def scan(bgr, reader, finder, db, min_profit, full_fallback):
    """OCR just the store list if the store buttons are visible, else the whole frame (if allowed)."""
    found = finder.find(bgr)
    if found:
        (x0, y0, x1, y1), s = found
        crop = bgr[y0:y1, x0:x1]
        if s < 0.95:  # small UI: enlarge the list to the size OCR was tuned on
            crop = cv2.resize(crop, None, fx=1 / s, fy=1 / s, interpolation=cv2.INTER_CUBIC)
        lines = reader.read(crop)
        for l in lines:
            if s < 0.95:
                l.x0 *= s; l.x1 *= s; l.y0 *= s; l.y1 *= s
            l.x0 += x0; l.x1 += x0; l.y0 += y0; l.y1 += y0
        store, singles, deals = analyze(lines, bgr, db, min_profit)
        return lines, True, singles, deals
    if not full_fallback:
        return [], False, [], []
    lines = reader.read(bgr)
    return (lines, *analyze(lines, bgr, db, min_profit))


class Reader:
    def __init__(self, scale):
        self.ocr = RapidOCR()
        self.scale = scale

    def read(self, bgr):
        img = bgr
        if self.scale != 1:
            img = cv2.resize(bgr, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_CUBIC)
        result, _ = self.ocr(img)
        lines = []
        for box, text, score in result or []:
            xs = [p[0] / self.scale for p in box]
            ys = [p[1] / self.scale for p in box]
            lines.append(Line(text, min(xs), min(ys), max(xs), max(ys)))
        return lines


# ---------------------------------------------------------------- windows / capture

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
IGNORE_TITLES = ("price checker", "maple-price-overlay", "visual studio code")


kernel32 = ctypes.windll.kernel32


def process_name(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        kernel32.CloseHandle(h)


def visible_windows():
    """(hwnd, title, exe name) for every visible top-level window."""
    out = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            out.append((hwnd, buf.value, process_name(hwnd)))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def client_rect(hwnd):
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return {"left": pt.x, "top": pt.y, "width": r.right, "height": r.bottom}


def find_region(target):
    """Screen area of the largest window whose title or exe name contains target."""
    target = target.lower()
    best = None
    for hwnd, t, exe in visible_windows():
        tl = t.lower()
        if (target in tl or target in exe.lower()) and not any(i in tl for i in IGNORE_TITLES) and not user32.IsIconic(hwnd):
            r = client_rect(hwnd)
            if r["width"] > 100 and r["height"] > 100 and (best is None or r["width"] * r["height"] > best["width"] * best["height"]):
                best = r
    return best


# ---------------------------------------------------------------- overlay

class Overlay:
    KEY = "#ff00fe"  # this colour is see-through

    def __init__(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.title("maple-price-overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", self.KEY)
        self.root.config(bg=self.KEY)
        self.canvas = tk.Canvas(self.root, bg=self.KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.root.update_idletasks()

        hwnd = int(self.root.wm_frame(), 16)
        GWL_EXSTYLE = -20
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        # layered + transparent = mouse clicks go straight through to the game;
        # toolwindow + noactivate = never in alt-tab, never steals focus
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | 0x80000 | 0x20 | 0x80 | 0x08000000)
        user32.SetWindowDisplayAffinity(hwnd, 0x11)  # keep the overlay out of our own screenshots
        self.geom = None

    def draw(self, region, store, priced, deals, ocr_time, paused, waiting_for=None):
        if region is None:  # game not found: just a status strip in the corner of the main screen
            region = {"left": 0, "top": 0, "width": 600, "height": 40}
        geom = f"{region['width']}x{region['height']}+{region['left']}+{region['top']}"
        if geom != self.geom:
            self.root.geometry(geom)
            self.geom = geom
        self.root.attributes("-topmost", True)
        c = self.canvas
        c.delete("all")
        if paused:
            status = "price checker paused (F8)"
        elif waiting_for:
            status = f"price checker: waiting for {waiting_for}..."
        else:
            status = f"store: {'yes' if store else 'no'} | {len(priced)} priced | {len(deals)} deals | {ocr_time:.1f}s"
        c.create_text(9, 9, text=status, anchor="nw", fill="black", font=("Segoe UI", 10, "bold"))
        c.create_text(8, 8, text=status, anchor="nw", fill="#7CFC00", font=("Segoe UI", 10, "bold"))
        if paused:
            return
        # yellow = recognised single-item listing, red = buy it
        for h in priced:
            x0, y0, x1, y1 = h.box
            color = "#ff2020" if h in deals else "#ffd800"
            c.create_rectangle(x0 - 4, y0 - 4, x1 + 4, y1 + 4, outline=color, width=3)
            label = f"{h.profit:+,} (NPC {h.npc_pays:,})"
            c.create_text(x1 + 9, (y0 + y1) / 2 + 1, text=label, anchor="w", fill="black", font=("Segoe UI", 10, "bold"))
            c.create_text(x1 + 8, (y0 + y1) / 2, text=label, anchor="w", fill=color, font=("Segoe UI", 10, "bold"))


# ---------------------------------------------------------------- debug output

def save_debug(bgr, lines, priced, deals, name="last_frame"):
    DEBUG_DIR.mkdir(exist_ok=True)
    img = bgr.copy()
    for l in lines:
        cv2.rectangle(img, (int(l.x0), int(l.y0)), (int(l.x1), int(l.y1)), (180, 180, 180), 1)
    for h in priced:
        color = (32, 32, 255) if h in deals else (0, 216, 255)
        cv2.rectangle(img, (int(h.box[0]), int(h.box[1])), (int(h.box[2]), int(h.box[3])), color, 2)
    cv2.imwrite(str(DEBUG_DIR / f"{name}.png"), img)
    with open(DEBUG_DIR / f"{name}.txt", "w", encoding="utf-8") as f:
        f.write("OCR lines:\n")
        for l in lines:
            f.write(f"  [{int(l.x0)},{int(l.y0)}] {l.text!r}\n")
        f.write("\nSingle-item listings (bundles and sold-out rows are skipped):\n")
        for h in priced:
            f.write(f"  {h.name}: asking {h.asking:,}, NPC pays {h.npc_pays:,}, profit {h.profit:,}\n")


def report(store, priced, deals):
    print(f"store detected: {store}, {len(priced)} single-item listings read")
    for h in priced:
        tag = "DEAL" if h in deals else "    "
        print(f"  {tag} {h.name}: asking {h.asking:,}, NPC pays {h.npc_pays:,} ({h.profit:+,})")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", default="maplestory.exe",
                    help="follow the window whose title or exe name contains this text ('' = watch a monitor)")
    ap.add_argument("--monitor", type=int, default=1, help="monitor to watch with --window '' (1 = primary)")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between scans")
    ap.add_argument("--scale", type=float, default=1.0, help="upscale before OCR; try 1.5 if names are misread")
    ap.add_argument("--full-scan", action="store_true",
                    help="OCR the whole screen when the store buttons aren't recognised (slow, ~4s per scan)")
    ap.add_argument("--min-profit", type=int, default=1, help="only box items with at least this profit")
    ap.add_argument("--match", type=int, default=88, help="name match strictness 0-100")
    ap.add_argument("--debug", action="store_true", help="save debug/last_frame.png + .txt every scan")
    ap.add_argument("--image", help="analyze one screenshot instead of watching the screen")
    ap.add_argument("--list-windows", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    if args.list_windows:
        for _, t, exe in visible_windows():
            print(f"{exe:<30} {t}")
        return
    if not PRICES_FILE.exists():
        raise SystemExit("no price data yet: run  python fetch_prices.py  first")

    db = PriceDB(PRICES_FILE, args.match)
    reader = Reader(args.scale)
    finder = StoreFinder()

    if args.image:
        bgr = cv2.imread(args.image)
        reader.read(bgr[:64, :64])  # warm up the OCR model so the timing below is a real scan
        t = time.perf_counter()
        lines, store, priced, deals = scan(bgr, reader, finder, db, args.min_profit, True)
        print(f"store buttons found: {finder.find(bgr) is not None}")
        print(f"OCR: {len(lines)} lines in {time.perf_counter() - t:.2f}s")
        report(store, priced, deals)
        save_debug(bgr, lines, priced, deals, Path(args.image).stem + "_checked")
        print(f"annotated copy + OCR text written to {DEBUG_DIR}")
        return

    results = queue.Queue(maxsize=1)
    stop = threading.Event()
    paused = threading.Event()

    def scan_once(sct, state):
        if args.window:
            region = find_region(args.window)
            found = region is not None
            if found != state["found"]:
                print(f"found {args.window}, watching it" if found else f"waiting for {args.window}...")
                state["found"] = found
            if not found:
                return (None, False, [], [], 0.0, args.window)
        else:
            region = sct.monitors[args.monitor]
        if paused.is_set():
            return (region, False, [], [], 0.0, None)

        t0 = time.perf_counter()
        bgr = np.ascontiguousarray(np.array(sct.grab(region))[:, :, :3])
        lines, store, priced, deals = scan(bgr, reader, finder, db, args.min_profit, args.full_scan)
        if args.debug:
            save_debug(bgr, lines, priced, deals)
        current = {(h.name, h.asking) for h in deals}
        for h in deals:
            if (h.name, h.asking) not in state["announced"]:
                print(f"DEAL {h.name}: asking {h.asking:,}, NPC pays {h.npc_pays:,} (+{h.profit:,})")
        state["announced"] = current
        return (region, store, priced, deals, time.perf_counter() - t0, None)

    def scan_loop():
        state = {"found": None, "announced": set()}
        last_error = None
        with getattr(mss, "MSS", mss.mss)() as sct:
            while not stop.is_set():
                t0 = time.perf_counter()
                try:
                    out = scan_once(sct, state)
                    last_error = None
                except Exception as e:  # e.g. window moved off-screen mid-grab; keep going
                    if repr(e) != last_error:
                        print(f"scan failed, retrying: {e!r}")
                        last_error = repr(e)
                    out = None
                if out is not None:
                    try:
                        results.get_nowait()
                    except queue.Empty:
                        pass
                    results.put(out)
                stop.wait(max(0.0, args.interval - (time.perf_counter() - t0)))

    overlay = Overlay()
    threading.Thread(target=scan_loop, daemon=True).start()
    signal.signal(signal.SIGINT, lambda *_: overlay.root.after(0, overlay.root.destroy))
    f8_down = [False]
    last = [None]

    def tick():
        down = bool(user32.GetAsyncKeyState(VK_F8) & 0x8000)
        if down and not f8_down[0]:
            paused.clear() if paused.is_set() else paused.set()
            print("paused" if paused.is_set() else "resumed")
        f8_down[0] = down
        try:
            last[0] = results.get_nowait()
        except queue.Empty:
            pass
        if last[0]:
            region, store, priced, deals, took, waiting_for = last[0]
            overlay.draw(region, store, priced, deals, took, paused.is_set(), waiting_for)
        overlay.root.after(100, tick)

    print("watching... F8 = pause/resume, Ctrl+C here = quit")
    tick()
    overlay.root.mainloop()
    stop.set()


if __name__ == "__main__":
    main()
