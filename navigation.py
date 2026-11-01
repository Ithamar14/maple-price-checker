"""Where the character is, read from the game's minimap. Only reads the screen: nothing here
presses a key or moves the mouse, so it's safe to run while playing normally.

Usage:
    python navigation.py        # shows what it reads over the game, and prints each change

While it runs, hold SPACE for 1 second with the game in front to save where you're standing, as the
next point it asks for, in data/nav_points.json. Closing it and starting again carries on where you
left off; delete that file to start over.
"""
import argparse
import json
import re
import threading
import time
import winsound
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

import cv2
import mss
import numpy as np

import price_checker as pc

TITLE_FILE = Path(__file__).parent / "templates" / "minimap_title.png"  # cut at 2560x1440, like the others
TITLE_THRESHOLD = 0.7
SEARCH_BELOW = 420  # how far below / beside the title the map picture can reach, at 2560x1440
SEARCH_LEFT = 40
MIN_DOT = 0.55      # of the expected size of the player's dot: smaller yellow specks aren't it
PLAYER_DOT_AREA = 90  # pixels of the player's dot at 2560x1440
DOT_MIN_GREEN = 208   # average green of the dot; yellow roofs on maps reach about 200
DOT_MIN_BLUE = 30     # and its blue; a yellow banner on Henesys Market's map has about 16
PORTAL_SIZE = (10, 34)  # smallest and largest width of a portal's blue ripple at 2560x1440
SIGNATURE_SIZE = (24, 16)
SAME_MAP_DIFF = 12.0  # average brightness difference under which two pictures count as one map
POLL = 0.5
NAME_AREA = (460, 70)  # size of the top-left corner where a map without a picture shows its name, at 2560x1440 x 0.75
ROOM_RE = re.compile(r"free\s*market\s*<\s*(\d+)\s*>", re.I)
VENDOR_RE = re.compile(r"henesys\s*weapon\s*store", re.I)
ENTRANCE_RE = re.compile(r"free\s*market\s*entrance", re.I)
MARKET_RE = re.compile(r"henesys\s*market", re.I)
POINTS_FILE = Path(__file__).parent / "data" / "nav_points.json"
# what to save, in order: its name, what to tell you, and which map it has to be on
POINTS = [(f"entry_room_{n}", f"the door to room {n}", "entrance") for n in (6, 5, 4, 3, 2, 1)] \
    + [("room_exit", "the exit door inside a room", "room")] \
    + [(f"entry_room_{n}", f"the door to room {n}", "entrance") for n in (16, 15, 14, 13)] \
    + [("teleporter", "the teleporter that goes up to rooms 13-16", "entrance")]
NEEDED_POINTS = 7  # the first ones: the bot can get around without the rest
SPACE = 0x20
HOLD_FOR = 1.0  # seconds SPACE has to be held


@dataclass
class Minimap:
    picture: tuple      # (x0, y0, x1, y1) of the map picture on screen
    player: tuple       # (x, y) on screen, or None when the dot can't be seen
    player_at: tuple    # the same as fractions of the picture (0-1), or None
    portals: list       # (x, y) on screen of the blue ripple circles
    signature: np.ndarray
    player_x: float = 0  # the dot's distance from the middle of the picture, in pixels at 2560x1440


def read_map_name(frame, reader):
    """All the text in the minimap's title area. It holds the map's name when the minimap is set
    to show it, and always on a map without a minimap picture, like the weapon store. Give it the
    capture before trim_bars: a narrow map has black sides, and its name bar sits on one of them."""
    s = frame.shape[0] / 1080
    corner = frame[0:int(170 * s), 0:int(NAME_AREA[0] * s)]
    return " ".join(text for _, text, _ in reader.detect(corner))


def map_kind(name):
    """('room', number), or ('entrance' / 'market' / 'vendor', None), or (None, None) when the
    text doesn't name one of those, from read_map_name's text."""
    room = ROOM_RE.search(name)
    if room:
        return "room", int(room.group(1))
    for kind, pattern in (("entrance", ENTRANCE_RE), ("market", MARKET_RE), ("vendor", VENDOR_RE)):
        if pattern.search(name):
            return kind, None
    return None, None


def same_map(a, b):
    """Whether two signatures show the same map. Players' dots differ, so it can't be exact."""
    return a.shape == b.shape and float(np.abs(a.astype(int) - b.astype(int)).mean()) < SAME_MAP_DIFF


class MinimapReader:
    def __init__(self):
        self.title = cv2.imread(str(TITLE_FILE), cv2.IMREAD_GRAYSCALE)
        if self.title is None:
            raise SystemExit(f"missing template: {TITLE_FILE}")
        self.scale = None

    def find_title(self, frame):
        """(x, y, scale) of the MINI MAP title, or None. The window can be dragged anywhere."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        guess = frame.shape[0] / 1440
        scales = [self.scale] if self.scale else [guess * (1 + k / 100) for k in range(-10, 11)]
        best = (0, None, None)
        for s in scales:
            tpl = cv2.resize(self.title, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            if tpl.shape[0] > gray.shape[0] or tpl.shape[1] > gray.shape[1]:
                continue
            _, v, _, loc = cv2.minMaxLoc(cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED))
            if v > best[0]:
                best = (v, loc, s)
        if best[0] < TITLE_THRESHOLD:
            self.scale = None  # the window may have been resized: look at every size next time
            return None
        self.scale = best[2]
        return best[1][0], best[1][1], best[2]

    def read(self, frame):
        """A Minimap, or None when the minimap isn't on screen (closed, or a loading screen)."""
        found = self.find_title(frame)
        if not found:
            return None
        tx, ty, s = found
        tw, th = (int(v * s) for v in self.title.shape[1::-1])
        x0, y0 = max(0, int(tx - SEARCH_LEFT * s)), ty + th
        x1, y1 = min(frame.shape[1], int(tx + SEARCH_BELOW * s)), min(frame.shape[0], int(y0 + SEARCH_BELOW * s))
        area = frame[y0:y1, x0:x1]
        picture = self._picture(area)
        if picture is None:
            return None
        px0, py0, px1, py1 = picture
        inside = area[py0:py1, px0:px1]
        dots = self._yellow_dots(inside)
        need = PLAYER_DOT_AREA * s * s * MIN_DOT
        round_dots = [d for d in dots if d[2] >= need and d[3]]  # roofs on the map are a darker yellow
        low, high = PORTAL_SIZE[0] * s, PORTAL_SIZE[1] * s
        player = max(round_dots, key=lambda d: d[2]) if round_dots else None
        portals = [(x0 + px0 + x, y0 + py0 + y) for x, y in self._portals(inside, low, high)]
        w, h = px1 - px0, py1 - py0
        return Minimap((x0 + px0, y0 + py0, x0 + px1, y0 + py1),
                       (x0 + px0 + player[0], y0 + py0 + player[1]) if player else None,
                       (round(player[0] / w, 3), round(player[1] / h, 3)) if player else None,
                       portals,
                       cv2.resize(cv2.cvtColor(inside, cv2.COLOR_BGR2GRAY), SIGNATURE_SIZE, interpolation=cv2.INTER_AREA),
                       (player[0] - w / 2) / s if player else 0)

    @staticmethod
    def _picture(area):
        """The map picture: the biggest thing in the window's white inside that doesn't reach
        the edge of the searched area (what does reach it is the game behind the window)."""
        white = area.min(axis=2) > 225
        count, _, stats, _ = cv2.connectedComponentsWithStats((~white).astype(np.uint8), connectivity=4)
        h, w = area.shape[:2]
        best = None
        for i in range(1, count):
            x, y, bw, bh, pixels = stats[i]
            if x == 0 or y == 0 or x + bw >= w or y + bh >= h or bw < 30 or bh < 20:
                continue
            if pixels / (bw * bh) < 0.5:  # the window's frame is a thin ring
                continue
            if best is None or pixels > best[4]:
                best = stats[i]
        return None if best is None else (int(best[0]), int(best[1]), int(best[0] + best[2]), int(best[1] + best[3]))

    @staticmethod
    def _portals(picture, low, high):
        """(x, y) of every blue ripple: a round blob of the right size (blue sky is far bigger)."""
        hsv = cv2.cvtColor(picture, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (92, 110, 130), (112, 255, 255))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        count, _, stats, centers = cv2.connectedComponentsWithStats(mask, connectivity=8)
        return [(int(centers[i][0]), int(centers[i][1])) for i in range(1, count)
                if low <= stats[i][2] <= high and 0.7 < stats[i][2] / stats[i][3] < 1.4]

    @staticmethod
    def _yellow_dots(picture):
        """(x, y, pixels, has the dot's colour) of every yellow blob. Going by colour rather than
        shape, as the dot is cut in half at the picture's edge."""
        hsv = cv2.cvtColor(picture, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (22, 130, 190), (34, 255, 255))
        count, labels, stats, centers = cv2.connectedComponentsWithStats(mask, connectivity=8)
        dots = []
        for i in range(1, count):
            x, y, w, h, pixels = stats[i]
            blue, green, _ = picture[labels == i].mean(axis=0)
            dots.append((int(centers[i][0]), int(centers[i][1]), int(pixels), green >= DOT_MIN_GREEN and blue >= DOT_MIN_BLUE))
        return dots


class SavedPoints:
    """The points saved so far, in POINTS_FILE, and which is next."""

    def __init__(self):
        try:
            self.saved = json.loads(POINTS_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            self.saved = {}

    def next(self):
        return next((p for p in POINTS if p[0] not in self.saved), None)

    def prompt(self):
        point = self.next()
        if not point:
            return f"all {len(POINTS)} points saved: {POINTS_FILE.name}"
        return f"point {POINTS.index(point) + 1} of {len(POINTS)}: stand on {point[1]}, hold SPACE for 1 second"

    def take(self, m, kind, room):
        """Save the next point from this reading. (saved?, what to tell you)."""
        point = self.next()
        if not point:
            return False, "all points are already saved; delete the file to start over"
        name, what, where = point
        if kind != where:
            return False, f"NOT SAVED: {what} has to be on the {'Free Market Entrance' if where == 'entrance' else 'inside of a room'}, this reads as {kind or 'an unknown map'}"
        if not m or not m.player:
            return False, "NOT SAVED: can't see your dot on the minimap"
        x0, y0, x1, y1 = m.picture
        self.saved[name] = {"x": round(m.player_x, 1), "down": m.player_at[1], "room": room,
                            "picture": [x1 - x0, y1 - y0], "saved": datetime.now().isoformat(timespec="seconds")}
        POINTS_FILE.parent.mkdir(exist_ok=True)
        tmp = POINTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.saved, indent=1), "utf-8")
        tmp.replace(POINTS_FILE)
        return True, f"saved {what}: {m.player_x:+.1f} from the middle, {m.player_at[1]:.0%} down"


class HoldWatcher(threading.Thread):
    """Sets `held` once for each time SPACE is held for HOLD_FOR with the game in front."""

    def __init__(self, hwnd):
        super().__init__(daemon=True)
        self.hwnd = hwnd
        self.held = threading.Event()

    def run(self):
        since, used = None, False
        while True:
            down = bool(pc.user32.GetAsyncKeyState(SPACE) & 0x8000) and pc.user32.GetForegroundWindow() == self.hwnd
            if not down:
                since, used = None, False
            else:
                since = since or time.monotonic()
                if not used and time.monotonic() - since >= HOLD_FOR:
                    used = True
                    self.held.set()
            time.sleep(0.05)


def load_points():
    """The saved points by name, or None unless all the needed ones are there."""
    try:
        saved = json.loads(POINTS_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return saved if all(p[0] in saved for p in POINTS[:NEEDED_POINTS]) else None


def describe(m):
    if m is None:
        return "no minimap on screen"
    where = f"dot at {m.player_at[0]:.0%} across, {m.player_at[1]:.0%} down" if m.player else "can't see your dot"
    if m.player:
        where += f" ({m.player_x:+.0f} from the middle)"
    x0, y0, x1, y1 = m.picture
    return f"map picture {x1 - x0}x{y1 - y0}, {where}, {len(m.portals)} portals"


def watch(window):
    found = pc.find_window(window)
    if not found:
        raise SystemExit(f"{window} isn't open (or is minimised)")
    hwnd = found[0]
    sct = getattr(mss, "MSS", mss.mss)()
    reader = MinimapReader()
    ocr = pc.Reader(1.0)
    overlay = pc.Overlay()
    maps = []  # signatures seen, to number the maps
    bars = None
    last = None
    points = SavedPoints()
    hold = HoldWatcher(hwnd)
    hold.start()
    said = ""
    print("Reading the minimap. Walk around; press Ctrl+C here to stop.")
    print(points.prompt())
    try:
        while True:
            rect = pc.client_rect(hwnd)
            raw = np.array(sct.grab(rect))[:, :, :3]
            frame, region, bars = pc.trim_bars(raw, rect, bars)
            m = reader.read(np.ascontiguousarray(frame))
            kind, room = map_kind(read_map_name(np.ascontiguousarray(raw), ocr))
            place = {"room": f"Free Market room {room}", "vendor": "weapon store", "entrance": "Free Market Entrance",
                     "market": "Henesys Market"}.get(kind, "map name not showing")
            number = None
            if m:
                number = next((i for i, s in enumerate(maps, 1) if same_map(s, m.signature)), None)
                if number is None:
                    maps.append(m.signature)
                    number = len(maps)
            text = f"{place} | " + (f"map #{number}: {describe(m)}" if m else describe(m))
            if hold.held.is_set():
                hold.held.clear()
                name = points.next()
                done, said = points.take(m, kind, room)
                if done:  # to look at later if a number turns out wrong
                    pc.DEBUG_DIR.mkdir(exist_ok=True)
                    cv2.imwrite(str(pc.DEBUG_DIR / f"nav_point_{name[0]}.png"), raw)
                winsound.MessageBeep(winsound.MB_OK if done else winsound.MB_ICONHAND)
                print(f"{time.strftime('%H:%M:%S')} {said}")
                print(points.prompt())
            if text != last:
                print(f"{time.strftime('%H:%M:%S')} {text}")
                last = text
            prompt = points.prompt()
            content = (text, m and m.player, m and tuple(m.portals), prompt, said)
            if overlay.begin(region, content):
                overlay.text(8, 8, text, pc.STATUS_COLOR)
                overlay.text(8, 30, prompt, pc.STATUS_COLOR)
                if said:
                    overlay.text(8, 52, said, pc.STATUS_COLOR)
                if m:
                    overlay.canvas.create_rectangle(*m.picture, outline=pc.STATUS_COLOR, width=2)
                    for x, y in m.portals:
                        overlay.canvas.create_rectangle(x - 4, y - 4, x + 4, y + 4, outline="#00e5ff", width=2)
                    if m.player:
                        x, y = m.player
                        overlay.canvas.create_rectangle(x - 7, y - 7, x + 7, y + 7, outline="#ff40ff", width=2)
            overlay.root.update()
            time.sleep(POLL)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", default="maplestory.exe")
    watch(ap.parse_args().window)
