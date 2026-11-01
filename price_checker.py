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
import os
import queue
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

import cv2
import mss
import numpy as np
from rapidfuzz import fuzz, process
from rapidocr import RapidOCR

import custom_items
import market

HERE = Path(__file__).parent
PRICES_FILE = HERE / "data" / "prices.json"
MANUAL_FILE = HERE / "data" / "manual_prices.json"
SETTINGS_FILE = HERE / "crawler_settings.json"
DEBUG_DIR = HERE / "debug"
# overlay colours, shared with the crawler
NPC_DEAL_COLOR = "#a0ffa0"  # light green: sells to an NPC for more than it costs
MARKET_COLOR = "#00d040"    # green: far under its Free Market price
LISTED_COLOR = "#ffd800"    # yellow: a recognised listing, nothing special
REVIEW_COLOR = "#c040ff"    # purple: on the review_items list, worth checking its stats yourself
CUSTOM_COLOR = "#00e5ff"    # cyan: on your custom list
WARN_COLOR = "#ff2020"      # red: something to watch out for
STATUS_COLOR = "#ffffff"    # status lines

cv2.setNumThreads(2)  # the default (all cores) burns CPU for little gain on these small jobs

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


def load_review_items():
    """Normalised names under "review_items" in crawler_settings.json: listings of these are
    marked purple so you can stop and check their stats (often good sales)."""
    try:
        names = json.loads(SETTINGS_FILE.read_text("utf-8")).get("review_items", [])
    except (OSError, ValueError):
        return frozenset()
    return frozenset(norm(n) for n in names if norm(n))


def load_custom_items(db):
    """Normalised names on your custom list. Names that aren't a known item are reported, with the
    closest match, since they'd never show up."""
    names = [i["name"] for i in custom_items.load()]
    for n in names:
        if norm(n) not in db.items:
            close = process.extractOne(norm(n), db.keys, scorer=fuzz.ratio)
            hint = f" (did you mean {db.items[close[0]][0]}?)" if close and close[1] >= 70 else ""
            print(f"  custom search: '{n}' isn't a known item name{hint}")
    if names:
        print(f"  custom search: pointing out {len(names)} items with cyan boxes")
    return frozenset(norm(n) for n in names if norm(n))


class Watchlists:
    """Items you asked to have pointed out: your custom list (cyan, any reason) and
    review_items in crawler_settings.json (purple, check their stats)."""

    def __init__(self, custom=frozenset(), review=frozenset()):
        self.custom, self.review = custom, review

    def kind(self, hit):
        """'custom', 'review' or None. A cut-off name counts if any item it can be is listed."""
        keys = {norm(n) for n in hit.names}
        return "custom" if keys & self.custom else "review" if keys & self.review else None

    def color(self, hit):
        return {"custom": CUSTOM_COLOR, "review": REVIEW_COLOR}.get(self.kind(hit))

    def label(self, hit):
        return {"custom": "CUSTOM SEARCH", "review": "CHECK STATS"}.get(self.kind(hit), "")


# the game cuts long names off with "..." ("Hat Accuracy Scroll: Great...")
CUT_OFF_RE = re.compile(r"\s*(\.\s*){1,3}$|\s*…$")
MIN_PREFIX = 8  # a cut-off name needs at least this many letters to go on
MAX_OPTIONS = 6  # ...and may fit at most this many items


class PriceDB:
    """Every item's name and NPC price (0 when an NPC won't buy it, e.g. scrolls)."""

    def __init__(self, path, cutoff):
        data = json.loads(Path(path).read_text("utf-8"))
        self.cutoff = cutoff
        self.items = {}  # normalized name -> (name, npc_pays)
        self.ids = {}    # name -> game item id
        self.slots = {it["name"]: it["slots"] for it in data["items"] if it.get("slots")}  # equipment
        for it in data["items"]:
            key = norm(it["name"])
            pays = it["npc_pays"] or 0
            old = self.items.get(key)
            # Two items can share a name; keep the lower known price so a "deal" is always real.
            if old is None or (pays and (not old[1] or pays < old[1])):
                self.items[key] = (it["name"], pays)
                self.ids[it["name"]] = it["id"]
        downloaded = sum(1 for _, pays in self.items.values() if pays)
        # prices you checked in game yourself win over the download
        manual = json.loads(MANUAL_FILE.read_text("utf-8")) if MANUAL_FILE.exists() else {}
        for name, pays in manual.items():
            if not name.startswith("_") and isinstance(pays, int) and pays > 0:
                self.items[norm(name)] = (name, pays)
        self.keys = list(self.items)
        print(f"loaded {len(self.items)} items, {downloaded} with an NPC price ({data.get('game')}, "
              f"fetched {data.get('fetched')}), + manual prices from {MANUAL_FILE.name}")

    def lookup(self, text):
        found = self.candidates(text)
        return found[0] if len(found) == 1 else None

    def candidates(self, text):
        """[(name, npc_pays)] the text can be: one item for a full name, or every item that
        starts like a name the game cut off. [] when nothing fits."""
        cut = bool(CUT_OFF_RE.search(text))
        key = norm(CUT_OFF_RE.sub("", text))
        if len(key) < 4:
            return []
        if not cut and key in self.items:
            return [self.items[key]]
        if len(key) >= MIN_PREFIX:
            # a cut-off name (also when OCR missed the dots): every name starting with it
            starts = [self.items[k] for k in self.keys if k.startswith(key) and k != key]
            if starts or cut:
                if not starts:  # OCR got a letter wrong: compare with the start of each name
                    scored = [(fuzz.ratio(key, k[:len(key)]), k) for k in self.keys if len(k) > len(key)]
                    best = max((s for s, _ in scored), default=0)
                    starts = [self.items[k] for s, k in scored if s >= max(best, self.cutoff)]
                return starts
        m = process.extractOne(key, self.keys, scorer=fuzz.ratio, score_cutoff=self.cutoff)
        return [self.items[m[0]]] if m else []


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
    npc_pays: int  # 0: no NPC buys it (or, for a cut-off name, not every item it can be)
    box: tuple
    options: tuple = ()  # for a cut-off name that fits several items: all of them

    @property
    def profit(self):
        return self.npc_pays - self.asking

    @property
    def names(self):
        return self.options or (self.name,)

    @property
    def npc_label(self):
        return f"NPC {self.npc_pays:,}" if self.npc_pays else ""

    @property
    def npc_text(self):
        """'+250 (NPC 1,000)', or '' for an item no NPC buys (scrolls): nothing to show."""
        return f"{self.profit:+,} ({self.npc_label})" if self.npc_pays else ""

    def details(self, fm=None):
        """', NPC price and profit, FM median' for console lines (only the parts there are)."""
        parts = [self.npc_text, fm.label(self) if fm else ""]
        return "".join(f", {p}" for p in parts if p)

    @property
    def could_be(self):
        return f" (could be: {', '.join(self.options)})" if self.options else ""


class ScrollTiers:
    """Which tier a scroll is, from its icon: every scroll type uses the same parchment icon,
    coloured by tier (Lesser blue, Intermediate red, Greater gold, Chaos grey). Tells apart
    scrolls whose name the game cut off before the tier ("Wand Magic Attack Scroll: Inte...")."""

    TIERS = ("Lesser", "Intermediate", "Greater", "Chaos")
    ICON_SCALE = 1440 / 768  # item icons are drawn at window height / 768; templates are 1:1
    MAX_COLOR_DISTANCE = 30  # the icons' colours are 50+ apart; other items' icons 150+

    def __init__(self):
        self.icons = {}
        for tier in self.TIERS:
            icon = cv2.imread(str(HERE / "templates" / f"scroll_{tier.lower()}.png"), cv2.IMREAD_UNCHANGED)
            mask = icon[:, :, 3] > 200
            self.icons[tier] = (icon, np.median(icon[:, :, :3][mask], axis=0))
        self._sized = {}

    def _shape(self, k):
        """(grey icon, mask) at size k; the shape is the same for every tier."""
        k = round(k, 3)
        if k not in self._sized:
            icon = self.icons["Greater"][0]
            big = cv2.resize(icon, None, fx=k, fy=k, interpolation=cv2.INTER_NEAREST if k >= 1 else cv2.INTER_AREA)
            self._sized[k] = (cv2.cvtColor(big[:, :, :3], cv2.COLOR_BGR2GRAY).astype(np.float32),
                              (big[:, :, 3] > 200).astype(np.float32))
        return self._sized[k]

    def tier(self, bgr, name, listing, s):
        """The tier of the scroll icon left of a listing, or None if it isn't a scroll icon."""
        x0, y0 = max(0, int(name.x0 - 75 * s)), max(0, int(name.y0 - 14 * s))
        area = bgr[y0:int(listing.y1 + 10 * s), x0:int(name.x0 - 2 * s)]
        if area.size == 0:
            return None
        gray = cv2.cvtColor(area, cv2.COLOR_BGR2GRAY).astype(np.float32)
        best = None
        for f in (0.94, 0.97, 1.0, 1.03, 1.06):
            shape, mask = self._shape(s * self.ICON_SCALE * f)
            if shape.shape[0] > gray.shape[0] or shape.shape[1] > gray.shape[1]:
                continue
            res = cv2.matchTemplate(gray, shape, cv2.TM_CCORR_NORMED, mask=mask)
            res[~np.isfinite(res)] = 0
            _, score, _, loc = cv2.minMaxLoc(res)
            if best is None or score > best[0]:
                best = (score, loc, mask)
        if best is None:
            return None
        _, (lx, ly), mask = best
        patch = area[ly:ly + mask.shape[0], lx:lx + mask.shape[1]][mask > 0]
        color = np.median(patch, axis=0)
        distance, tier = min((np.linalg.norm(color - c), t) for t, (_, c) in self.icons.items())
        return tier if distance <= self.MAX_COLOR_DISTANCE else None


_tiers = None


def scroll_tier(bgr, name, listing, s):
    global _tiers
    if _tiers is None:
        _tiers = ScrollTiers()
    return _tiers.tier(bgr, name, listing, s)


def resolve(found, bgr, name, listing, s):
    """Narrow down the items a cut-off name can be, using the scroll tier from its icon."""
    if len(found) > 1 and all(":" in n for n, _ in found):
        tier = scroll_tier(bgr, name, listing, s)
        same = [f for f in found if f[0].rsplit(":", 1)[1].strip() == tier]
        if same:
            return same
    return found


def analyze(lines, bgr, db, min_profit, scale=None):
    """Return (store_seen, singles, deals). Bundles (N > 1) and sold-out rows are ignored.
    A name the game cut off that still fits several items becomes one Hit with all of them as
    options, priced as the cheapest so nothing looks like a deal that might not be one."""
    s = scale or bgr.shape[0] / 1440
    singles = []
    for l in lines:
        m = LISTING_RE.match(l.text)
        if not m:
            continue
        name = name_above(l, lines)
        if name is None or is_faded(bgr, name):
            continue
        qty, asking = to_int(m.group(1)), to_int(m.group(2))
        found = db.candidates(name.text) if qty == 1 else []
        if not found:
            continue
        found = resolve(found, bgr, name, l, s)
        if len(found) > MAX_OPTIONS:
            continue  # too little of the name to go on
        box = (min(l.x0, name.x0), name.y0, max(l.x1, name.x1), l.y1)
        if len(found) == 1:
            singles.append(Hit(found[0][0], qty, asking, found[0][1], box))
        else:
            names = tuple(n for n, _ in found)
            pays = min(p for _, p in found)  # 0 if any of them has no NPC price
            shown = os.path.commonprefix(names).rstrip(" :") + "..."
            singles.append(Hit(shown, qty, asking, pays, box, names))

    compact = "".join(l.text.lower() for l in lines).replace(" ", "")
    store = any(k in compact for k in STORE_KEYWORDS) or bool(singles)
    deals = [h for h in singles if h.profit >= min_profit]
    return store, singles, deals


class StoreFinder:
    """Finds an open player store by its BUY ITEM / LEAVE STORE buttons (fast template match),
    so only the item list next to them needs OCR."""

    # Cut from a 2560x1440 screenshot; the game UI is smaller in a smaller window. Each frame
    # tries a few sizes near the expected one (window height / 1440, or the last size that
    # worked), and every SWEEP_EVERY seconds without a match it tries every size.
    # To stay cheap, sizes are first tried on a shrunken copy of the frame (template about
    # COARSE_WIDTH px wide), and only the best spot is confirmed at full resolution.
    TEMPLATE = HERE / "templates" / "store_buttons.png"
    REF_HEIGHT = 1440
    # item list area in template pixels, relative to the top-left of the matched buttons
    LIST_BOX = (-270, 105, 130, 580)
    ALL_SCALES = [round(s, 2) for s in np.arange(0.35, 1.31, 0.02)]
    SWEEP_EVERY = 30.0
    COARSE_WIDTH = 40
    COARSE_THRESHOLD = 0.5

    def __init__(self, threshold=0.7):
        self.tpl = cv2.imread(str(self.TEMPLATE), cv2.IMREAD_GRAYSCALE)
        self.threshold = threshold
        self.last_scale = None
        self.last_loc = None
        self.last_sweep = 0.0
        self.last_scores = None  # (coarse, confirmed), for tuning

    def _shrink_factor(self, scale):
        for d in (0.25, 0.33, 0.5):
            if self.tpl.shape[1] * scale * d >= self.COARSE_WIDTH:
                return d
        return 1.0

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

        # cheap pass on shrunken copies of the frame
        shrunk = {}
        best = (0, None, None, None)
        for s in order:
            d = self._shrink_factor(s)
            if d not in shrunk:
                shrunk[d] = gray if d == 1 else cv2.resize(gray, None, fx=d, fy=d, interpolation=cv2.INTER_AREA)
            score, loc = self._match(shrunk[d], s * d)
            if score > best[0]:
                best = (score, loc, s, d)
        coarse, loc, s, d = best
        self.last_scores = (round(coarse, 3), None)
        if coarse < self.COARSE_THRESHOLD:
            return None

        # confirm at full resolution in a small area around the cheap pass's best spot
        # (the shrunken pass can be ~0.05 off on size, so try that range; the area is tiny)
        pad = int(4 / d) + 6
        th, tw = (round(v * (s + 0.06)) for v in self.tpl.shape)
        rx, ry = max(0, int(loc[0] / d) - pad), max(0, int(loc[1] / d) - pad)
        area = gray[ry:ry + th + 2 * pad, rx:rx + tw + 2 * pad]
        best = (0, None, None)
        for s2 in sorted(np.arange(s - 0.05, s + 0.051, 0.01), key=lambda v: abs(v - s)):
            score, loc2 = self._match(area, round(s2, 2))
            if score > best[0]:
                best = (score, loc2, round(s2, 2))
        score, loc2, s = best
        self.last_scores = (round(coarse, 3), round(score, 3))
        if score < self.threshold:
            return None
        self.last_scale = s
        x, y = rx + loc2[0], ry + loc2[1]
        self.last_loc = (x, y)  # top-left of the matched buttons, for clicking them
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
        lines = reader.read_cached(crop)
        for l in lines:
            if s < 0.95:
                l.x0 *= s; l.x1 *= s; l.y0 *= s; l.y1 *= s
            l.x0 += x0; l.x1 += x0; l.y0 += y0; l.y1 += y0
        store, singles, deals = analyze(lines, bgr, db, min_profit, s)
        return lines, True, singles, deals
    if not full_fallback:
        return [], False, [], []
    lines = reader.read(bgr)
    return (lines, *analyze(lines, bgr, db, min_profit))


class Reader:
    def __init__(self, scale):
        # Game text is horizontal (no angle classifier needed) and big enough to read at its
        # own size. Two threads and no inter-op pool keep a read at ~1 CPU-second instead of ~8.
        self.ocr = RapidOCR(params={
            "Global.use_cls": False,
            "Global.log_level": "error",  # not "text detection result is empty" for every blank area
            "Det.limit_type": "max",
            "Det.limit_side_len": 960,
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })
        self.scale = scale
        self._last = (None, [])

    def detect(self, img):
        """[(box, text, score)] for every text line found in img."""
        # per-call flags stick on the engine, so always pass all of them
        out = self.ocr(img, use_det=True, use_cls=False, use_rec=True)
        if getattr(out, "boxes", None) is None or out.txts is None:
            return []
        return list(zip(out.boxes, out.txts, out.scores))

    def recognize(self, img):
        """[(text, score)] for img read as a single line, skipping text detection."""
        out = self.ocr(img, use_det=False, use_cls=False, use_rec=True)
        return list(zip(out.txts or (), out.scores or ()))

    def read_cached(self, img):
        """Like read(), but reuses the last result while the image hasn't changed
        (a store you're looking at mostly sits still)."""
        prev, lines = self._last
        if prev is None or prev.shape != img.shape or cv2.absdiff(prev, img).mean() > 2:
            lines = self.read(img)
            self._last = (img.copy(), lines)
        return [replace(l) for l in lines]

    def read(self, bgr):
        img = bgr
        if self.scale != 1:
            img = cv2.resize(bgr, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_CUBIC)
        lines = []
        for box, text, score in self.detect(img):
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


def find_window(target):
    """(hwnd, screen area) of the largest window whose title or exe name contains target, or None."""
    target = target.lower()
    best = None
    for hwnd, t, exe in visible_windows():
        tl = t.lower()
        if (target in tl or target in exe.lower()) and not any(i in tl for i in IGNORE_TITLES) and not user32.IsIconic(hwnd):
            r = client_rect(hwnd)
            if r["width"] > 100 and r["height"] > 100 and (best is None or r["width"] * r["height"] > best[1]["width"] * best[1]["height"]):
                best = (hwnd, r)
    return best


def trim_bars(bgr, region, last=None):
    """Cut the black bars off a capture. In full screen on a screen that isn't 16:9 (e.g.
    1920x1200), the game draws a 16:9 picture with black bars around it; everything (UI size,
    positions, clicks) has to be measured inside the picture. Returns (picture, its screen region).
    A frame that's all black (loading) reuses last, the (top, bottom, left, right) bars before."""
    h, w = bgr.shape[:2]
    # a row or column only counts as picture when a good part of it is lit: the game draws its
    # cursor over the bars too, and those few pixels mustn't make the bars look uneven
    lit = bgr.max(axis=2) > 8
    rows = lit.mean(axis=1) > 0.1
    cols = lit.mean(axis=0) > 0.1
    if rows.sum() < h * 0.5 or cols.sum() < w * 0.5:  # mostly black: loading, not bars
        bars = last or (0, 0, 0, 0)
    else:
        bars = (int(rows.argmax()), int(rows[::-1].argmax()), int(cols.argmax()), int(cols[::-1].argmax()))
        # real bars are even and centred; anything else is just a dark edge of the map
        if abs(bars[0] - bars[1]) > 2 or bars[0] < 8:
            bars = (0, 0) + bars[2:]
        if abs(bars[2] - bars[3]) > 2 or bars[2] < 8:
            bars = bars[:2] + (0, 0)
    top, bottom, left, right = bars
    picture = bgr[top:h - bottom, left:w - right]
    inner = {"left": region["left"] + left, "top": region["top"] + top,
             "width": w - left - right, "height": h - top - bottom}
    return picture, inner, bars


def find_region(target):
    found = find_window(target)
    return found[1] if found else None


class MarketPrices:
    """Free Market prices from MeowDB, looked up live as items show up (see market.py): a
    label for each listing, and which ones are market deals, that is at least percent under
    the item's median price of the last 24 hours."""

    COLOR = MARKET_COLOR

    def __init__(self, prices, percent, min_reports):
        self.prices, self.percent, self.min_reports = prices, percent, min_reports

    def label(self, hit):
        m = self.prices.lowest(hit.names)
        if not m:
            return ""
        return f"FM 24h median {m['median_24h']:,} ({100 * (hit.asking - m['median_24h']) / m['median_24h']:+.0f}%)"

    def pending(self, hit):
        """Whether a lookup for this listing is still queued at MeowDB."""
        return self.prices.pending(hit.names)

    def is_deal(self, hit):
        limit = market.deal_limit(self.prices.lowest(hit.names), self.percent, self.min_reports)
        return limit is not None and hit.asking <= limit


@dataclass
class Enhancements:
    """What an equipment tooltip says about its scroll slots, against the item when new."""
    name: str
    remaining: int
    most: int  # upgrade slots the item has when new
    box: tuple  # the "Remaining Enhancements" line on screen

    @property
    def clean(self):
        return self.remaining >= self.most


class TooltipReader:
    """Reads the tooltip the game shows while you hover an item (experimental). For equipment
    it finds "Remaining Enhancements: N" and compares it with the upgrade slots the item has
    when new: fewer means scrolls were already used on it (passed or failed)."""

    BACKGROUND = np.array((132, 85, 83))  # the tooltip's navy, BGR
    TOLERANCE = 14
    TEXT_MIN = 130  # tooltip text is brighter than this in every colour channel
    ENHANCE_RE = re.compile(r"main[a-z]*enhan[a-z]*(\d+)")  # "Remaining Enhancements: 7", spaces removed

    def __init__(self, reader, db):
        self.reader, self.db = reader, db
        self._last = (None, None)  # (tooltip image, result) so a still tooltip is read once
        self.debug = False  # save every tooltip read, and what OCR made of it, to debug/

    def find(self, bgr, s):
        """(x0, y0, x1, y1) of the tooltip on screen, or None."""
        f = 4  # look on a shrunken copy: it only has to be found, not read
        small = bgr[::f, ::f].astype(np.int16)
        b, g, r = small[..., 0], small[..., 1], small[..., 2]
        # the tooltip is see-through, so its shade shifts with what's behind it: any dark navy
        # counts for its extent, but most of it has to be its usual colour
        navy = ((b - r >= 35) & (b - g >= 35) & (b <= 170)).astype(np.uint8)
        exact = np.abs(small - self.BACKGROUND).max(axis=2) <= self.TOLERANCE
        n, _, stats, _ = cv2.connectedComponentsWithStats(navy)
        best = None
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            # a tooltip is a mostly-filled box at least ~120 x 60 (at full UI size)
            if (w * f >= 120 * s and h * f >= 60 * s and area >= 0.4 * w * h
                    and exact[y:y + h, x:x + w].mean() >= 0.3 and (best is None or area > best[4])):
                best = stats[i]
        if best is None:
            return None
        x, y, w, h, _ = best
        x0, y0, x1, y1 = x * f, y * f, min(bgr.shape[1], (x + w) * f), min(bgr.shape[0], (y + h) * f)
        # the navy can run into the store row above the tooltip; its real top is the first
        # row that's mostly its own colour (so a listing name above isn't read as its title)
        rows = (np.abs(bgr[y0:y1, x0:x1].astype(np.int16) - self.BACKGROUND).max(axis=2)
                <= self.TOLERANCE).mean(axis=1)
        solid = np.flatnonzero(rows >= 0.5)
        if len(solid):
            y0 = max(y0, y0 + int(solid[0]) - 2)
        return x0, y0, x1, y1

    def read(self, bgr, s):
        """Enhancements for a hovered equipment item, or None (no tooltip, not equipment, or
        the line couldn't be read)."""
        found = self.find(bgr, s)
        if found is None:
            self._last = (None, None)
            return None
        x0, y0, x1, y1 = found
        crop = bgr[y0:y1, x0:x1]
        prev, result = self._last
        if prev is not None and prev.shape == crop.shape and cv2.absdiff(prev, crop).mean() < 2:
            return result
        big = crop if s >= 0.95 else cv2.resize(crop, None, fx=1 / s, fy=1 / s, interpolation=cv2.INTER_CUBIC)
        k = 1 if s >= 0.95 else s
        # the tooltip is see-through, but its own text is near-white while whatever shows through
        # it is dim: keep only bright pixels, as black text on white
        big = np.where((big.min(axis=2) >= self.TEXT_MIN)[..., None], 0, 255).astype(np.uint8).repeat(3, axis=2)
        lines = sorted(self.reader.read(big), key=lambda l: l.y0)
        result = None
        for l in lines:
            m = self.ENHANCE_RE.search(norm(l.text))
            if not m:
                continue
            # the item's name is the tooltip's title: the first line that's a known item
            for title in lines:
                found_names = self.db.candidates(title.text) if title.y0 < l.y0 else []
                most = self.db.slots.get(found_names[0][0]) if len(found_names) == 1 else None
                if most:
                    box = tuple(float(v) for v in (x0 + l.x0 * k, y0 + l.y0 * k, x0 + l.x1 * k, y0 + l.y1 * k))
                    result = Enhancements(found_names[0][0], int(m.group(1)), most, box)
                    break
            break
        self._last = (crop.copy(), result)
        if self.debug:
            DEBUG_DIR.mkdir(exist_ok=True)
            cv2.imwrite(str(DEBUG_DIR / "tooltip.png"), crop)
            with open(DEBUG_DIR / "tooltip.txt", "w", encoding="utf-8") as f:
                f.write(f"tooltip at {found}, UI scale {s}\nresult: {result}\nOCR lines:\n")
                f.writelines(f"  {l.text!r}\n" for l in lines)
            print(f"  [debug] tooltip read: {result or 'no Remaining Enhancements / item name matched'} "
                  f"(debug\\tooltip.png, tooltip.txt)")
        return result


def load_market(args, db):
    """MarketPrices, after the same questions the crawler asks (update saved prices? how far
    under the median?), or None with --no-market or when you said no."""
    if args.no_market:
        return None
    prices = market.prepare(db.ids)
    if not prices:
        return None
    cfg = market.load_settings()
    percent = args.market_percent or market.ask_percent(cfg.get("below_24h_median_percent", 30))
    print(f"  market deals: {percent:g}% or more under the 24h median (purple boxes)")
    return MarketPrices(prices, percent, cfg.get("min_reports_24h", 5))


# ---------------------------------------------------------------- overlay

class Overlay:
    KEY = "#ff00fe"  # this colour is see-through
    LABEL_BACK = "#101010"  # behind every label

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
        self.drawn = None
        self.last_topmost = 0.0

    def begin(self, region, content):
        """Cover region and clear the canvas, unless content is what's already drawn there.
        Returns False when there's nothing to redraw."""
        if time.monotonic() - self.last_topmost > 2:  # the game can push us behind it
            self.root.attributes("-topmost", True)
            self.last_topmost = time.monotonic()
        if region is None:  # game not found: just a status strip in the corner of the main screen
            region = {"left": 0, "top": 0, "width": 600, "height": 40}
        geom = f"{region['width']}x{region['height']}+{region['left']}+{region['top']}"
        # repainting a full-screen see-through window is the costly part, so only do it on change
        if (geom, content) == self.drawn:
            return False
        self.drawn = (geom, content)
        if geom != self.geom:
            self.root.geometry(geom)
            self.geom = geom
        self.canvas.delete("all")
        return True

    def text(self, x, y, text, color, anchor="nw"):
        """Text on a dark box, readable on any background."""
        label = self.canvas.create_text(x, y, text=text, anchor=anchor, fill=color, font=("Segoe UI", 10, "bold"))
        x0, y0, x1, y1 = self.canvas.bbox(label)
        back = self.canvas.create_rectangle(x0 - 4, y0 - 1, x1 + 4, y1 + 1, fill=self.LABEL_BACK, outline="")
        self.canvas.tag_lower(back, label)

    def draw(self, region, store, priced, deals, paused, waiting_for=None, fm=None, watch=Watchlists(),
             enhance=None):
        # market prices arrive a moment after a listing shows up, so their labels count as content
        content = (store, [(h.name, h.asking, h.box, fm.label(h) if fm else "", fm.pending(h) if fm else False)
                           for h in priced],
                   len(deals), paused, waiting_for, enhance)
        if not self.begin(region, content):
            return
        if paused:
            status = "price checker paused (F8)"
        elif waiting_for:
            status = f"price checker: waiting for {waiting_for}..."
        else:
            status = f"store: {'yes' if store else 'no'} | {len(priced)} priced | {len(deals)} deals"
            if fm:
                status += f" | {sum(1 for h in priced if h not in deals and fm.is_deal(h))} market deals"
        self.text(8, 8, status, STATUS_COLOR)
        if paused:
            return
        # yellow = recognised single-item listing, light green = NPC deal, cyan = your custom
        # search, purple = review it yourself, green = market deal
        for h in priced:
            x0, y0, x1, y1 = h.box
            color = (NPC_DEAL_COLOR if h in deals else watch.color(h)
                     or (MARKET_COLOR if fm and fm.is_deal(h) else LISTED_COLOR))
            self.canvas.create_rectangle(x0 - 4, y0 - 4, x1 + 4, y1 + 4, outline=color, width=3)
            label = " | ".join(part for part in (
                h.npc_text,
                fm.label(h) if fm else "",
                "getting data..." if fm and fm.pending(h) else "",
                f"could be {len(h.options)} items" if h.options else "",
                watch.label(h)) if part)
            self.text(x1 + 8, (y0 + y1) / 2, label, color, anchor="w")
        if enhance:
            # red: scrolls were already used on it; green: every slot is still there
            x0, y0, x1, y1 = enhance.box
            color = MARKET_COLOR if enhance.clean else WARN_COLOR
            self.canvas.create_rectangle(x0 - 4, y0 - 3, x1 + 4, y1 + 3, outline=color, width=3)
            label = (f"all {enhance.most} slots left" if enhance.clean else
                     f"{enhance.remaining} of {enhance.most} slots left: {enhance.most - enhance.remaining} already used")
            self.text(x0 - 4, y1 + 6, label, color)


# ---------------------------------------------------------------- debug output

def bgr_color(hex_color):
    """'#rrggbb' -> (b, g, r) for OpenCV."""
    return tuple(int(hex_color[i:i + 2], 16) for i in (5, 3, 1))


def save_debug(bgr, lines, priced, deals, name="last_frame", fm=None, watch=Watchlists()):
    DEBUG_DIR.mkdir(exist_ok=True)
    img = bgr.copy()
    for l in lines:
        cv2.rectangle(img, (int(l.x0), int(l.y0)), (int(l.x1), int(l.y1)), (180, 180, 180), 1)
    for h in priced:
        color = bgr_color(NPC_DEAL_COLOR if h in deals else watch.color(h)
                          or (MARKET_COLOR if fm and fm.is_deal(h) else LISTED_COLOR))
        cv2.rectangle(img, (int(h.box[0]), int(h.box[1])), (int(h.box[2]), int(h.box[3])), color, 2)
    cv2.imwrite(str(DEBUG_DIR / f"{name}.png"), img)
    with open(DEBUG_DIR / f"{name}.txt", "w", encoding="utf-8") as f:
        f.write("OCR lines:\n")
        for l in lines:
            f.write(f"  [{int(l.x0)},{int(l.y0)}] {l.text!r}\n")
        f.write("\nSingle-item listings (bundles and sold-out rows are skipped):\n")
        for h in priced:
            f.write(f"  {h.name}: asking {h.asking:,}{h.details(fm)}{h.could_be}{' MARKET DEAL' if fm and h not in deals and fm.is_deal(h) else ''}\n")


def report(store, priced, deals, fm=None):
    print(f"store detected: {store}, {len(priced)} single-item listings read")
    for h in priced:
        tag = "DEAL  " if h in deals else "MARKET" if fm and fm.is_deal(h) else "      "
        print(f"  {tag} {h.name}: asking {h.asking:,}{h.details(fm)}{h.could_be}")


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
    ap.add_argument("--market-percent", type=market.parse_percent,
                    help="a market deal is this many %% under the 24h median (asked if not given)")
    ap.add_argument("--no-market", action="store_true", help="don't compare with Free Market prices")
    market.add_update_flag(ap)
    ap.add_argument("--tooltip-slots", action="store_true",
                    help="experimental, off by default: check hovered equipment's remaining scroll slots")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True, errors="replace")  # OCR text can hold characters the console can't show
    market.update_saved = args.market_update

    if args.list_windows:
        for _, t, exe in visible_windows():
            print(f"{exe:<30} {t}")
        return
    if not PRICES_FILE.exists():
        raise SystemExit("no price data yet: run  python fetch_prices.py  first")

    db = PriceDB(PRICES_FILE, args.match)
    fm = load_market(args, db)
    watch = Watchlists(load_custom_items(db), load_review_items())
    reader = Reader(args.scale)
    finder = StoreFinder()
    tooltips = TooltipReader(reader, db) if args.tooltip_slots else None
    if tooltips:
        tooltips.debug = args.debug

    if args.image:
        bgr = cv2.imread(args.image)
        reader.read(bgr[:64, :64])  # warm up the OCR model so the timing below is a real scan
        t = time.perf_counter()
        lines, store, priced, deals = scan(bgr, reader, finder, db, args.min_profit, True)
        print(f"store buttons found: {finder.find(bgr) is not None}")
        print(f"OCR: {len(lines)} lines in {time.perf_counter() - t:.2f}s")
        report(store, priced, deals, fm)
        enhance = tooltips and tooltips.read(bgr, finder.last_scale or bgr.shape[0] / 1440)
        if tooltips:
            print(f"hovered equipment: {enhance}" if enhance else "no equipment tooltip read")
        save_debug(bgr, lines, priced, deals, Path(args.image).stem + "_checked", fm, watch)
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
                return (None, False, [], [], 0.0, args.window, None)
        else:
            region = sct.monitors[args.monitor]
        if paused.is_set():
            return (region, False, [], [], 0.0, None, None)

        t0 = time.perf_counter()
        bgr = np.ascontiguousarray(np.array(sct.grab(region))[:, :, :3])
        bgr, region, state["bars"] = trim_bars(bgr, region, state.get("bars"))
        bgr = np.ascontiguousarray(bgr)
        lines, store, priced, deals = scan(bgr, reader, finder, db, args.min_profit, args.full_scan)
        if args.debug:
            save_debug(bgr, lines, priced, deals, fm=fm, watch=watch)
        market_deals = [h for h in priced if fm and h not in deals and fm.is_deal(h)]
        watched = [h for h in priced if watch.kind(h)]
        current = {(h.name, h.asking) for h in deals + market_deals + watched}
        for h in watched:
            if (h.name, h.asking) not in state["announced"]:
                print(f"{watch.label(h)} {h.name}: asking {h.asking:,}{h.could_be}")
        for h in deals:
            if (h.name, h.asking) not in state["announced"]:
                print(f"DEAL {h.name}: asking {h.asking:,}, NPC pays {h.npc_pays:,} (+{h.profit:,})")
        for h in market_deals:
            if (h.name, h.asking) not in state["announced"]:
                print(f"MARKET DEAL {h.name}: asking {h.asking:,}{h.details(fm)}{h.could_be}")
        state["announced"] = current
        enhance = tooltips and tooltips.read(bgr, finder.last_scale or bgr.shape[0] / 1440)
        if enhance and enhance != state.get("enhance"):
            print(f"HOVERED {enhance.name}: {enhance.remaining} of {enhance.most} upgrade slots left"
                  + ("" if enhance.clean else f" - {enhance.most - enhance.remaining} already used"))
        state["enhance"] = enhance
        return (region, store, priced, deals, time.perf_counter() - t0, None, enhance)

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
            region, store, priced, deals, took, waiting_for, enhance = last[0]
            overlay.draw(region, store, priced, deals, paused.is_set(), waiting_for, fm, watch, enhance)
        overlay.root.after(100, tick)

    print("watching... F8 = pause/resume, Ctrl+C here = quit")
    tick()
    overlay.root.mainloop()
    stop.set()


if __name__ == "__main__":
    main()
