"""Walks the character between maps, from what it reads on screen (see navigation.py).

So far: from a Free Market room or anywhere on the way, to the Henesys weapon store, talks to the
NPC and closes the window, then back to the Free Market Entrance. Hold ESC for 1 second to stop.

Shows over the game: its current action, the map it thinks it's on, the map picture, your dot, the
spot it's walking to, portals and the NPC. Everything it does also goes to logs/walk_<date>.log, and
each failure saves a screenshot to debug/nav_failed_<n>.png. Those, and the ones saved along the way to
debug/nav_steps/<time of the run>/, have what it sees and thinks drawn on them, with the minimap enlarged.

Usage:
    python walker.py                 # the whole trip
    python walker.py --to vendor     # only go to the store
    python walker.py --to market     # only go to Henesys Market
    python walker.py --room 2        # to room 2 (a room on the bottom floor of the Entrance)
    python walker.py --sell-loop 6 5 # from room 6 to the NPC, then back to room 5
    python walker.py --long-loop 6 5 # the same, then checks 3 shops in room 5, changes channel, then to the NPC again
                                     # (buys nothing, unless given a --budget). At the NPC it sells
                                     # everything on the Equip, Use and Etc tabs (not with --no-sell)
    python walker.py --nav-test      # one room on each floor, back to the main portal after each, over and over. Never sells
    python walker.py --jump-key alt

The full loop (full_sell_loop.bat) is run_full below: every channel in a room, sell, another room, and so on.
"""
import argparse
import re
import shutil
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from rapidfuzz import fuzz, process

import crawler
import navigation as nav
import price_checker as pc

JUMP_KEY = "alt"

# Where a portal is on the minimap: its distance from the middle of the map picture, in pixels at
# 2560x1440. Not a share of the picture's width: the window adds side margins on narrow maps. Only
# for maps that fit in the picture; on wider ones (Henesys Market) the picture scrolls: walk_to_ring.
HUB_EXIT = -53       # Free Market Entrance: the big portal to town. Up failed at -49, worked at -53
STORE_HEIGHT = 0.655  # Henesys Market: the store's circle, as a share of the picture's height
FM_HEIGHT = 0.585     # Henesys Market: the Free Market portal's circle
ROOMS_END = HUB_EXIT + 15  # the sweep along the doors stops here, well before the big portal

# which map to go to next, to get from each map to each destination
ROUTE = {"room": "entrance", "entrance": "market"}
BACK = {"vendor": "market", "market": "entrance"}

OVERSHOOT = 12        # minimap pixels at 2560x1440 past a portal before it counts as missed
RING_UNDER_FOOT = 15  # minimap pixels at 2560x1440: a circle this close is the portal we're standing on
RING_JUMP = 20        # how far from where it's expected a circle can still be the same one
RING_HEIGHT_MATCH = 0.02  # how far off its height a circle can be
WALK_SPEED = 16.0     # minimap pixels at 2560x1440 per second, while a circle is hidden
ON_PORTAL = 2         # minimap pixels at 2560x1440 from a circle's middle that count as on the portal
RING_HIDDEN_NEAR = 15  # a circle that vanishes from this close has gone under the dot
SETTLE_STEPS = 10     # small steps at most to get onto a portal
UP_PRESSES = 3
UP_WAIT = 1.2         # seconds after each press of Up for the map to change
NAME_EVERY = 0.4      # seconds between readings of the map's name while walking into a portal
PUSH_LIMIT = 12.0     # seconds to keep walking into a portal whose position isn't known
WALL_AFTER = 1.0      # seconds without moving there before it counts as the end of the map
DOOR_STEP = 0.12      # seconds of each step along the Entrance's doors, which take Up in a narrow spot
DOOR_WAIT = 0.5       # seconds after pressing Up at each step for the map to start changing. Keys sent
                      # before it does carry over into the room: off its door, or straight back out
FADING = 0.8          # the screen this much darker than before pressing Up: the map is changing
BOUNCE_WAIT = 2.0     # seconds after a door before the Entrance's name means it came back out, not that it hasn't left yet
LEVEL_MATCH = 0.03     # how far off a saved height still counts as that level, inside those rooms
TOP_MATCH = 0.012      # how far off the door's height counts as the top of the ladder
CLIMB_SHOWS = 0.015    # this much above the ground means it's on the ladder
CLIMB_START = 0.7      # seconds holding Up for that to show
CLIMB_LIMIT = 6.0      # seconds to get up the ladder
LADDER_SEARCH = 4      # steps of TAP each way to look for the ladder
LOST_HOLD = 20.0      # seconds of Up and Right, the last resort in a room: see last_resort
FLOOR_MARGIN = 0.06   # this share of the map's height above a saved point counts as a floor up
MAX_DROPS = 3         # floors to drop down to reach a saved point
TELEPORT_WAIT = 1.5   # seconds after pressing Up on a teleporter for the character to arrive
DOT_WAIT = 2.0        # seconds to wait for the dot to show before walking to a saved point
ENTRY_SEARCH = 5      # steps of TAP each way to look for a door from its saved point: short, not to step over it
DOOR_SEARCH = 8       # steps of TAP each way to look for a room's door from where it arrived
LADDER_JUMPS = 2      # jumps to get going again when not moving: tapping Up grabs a ladder, and walking doesn't get off it
UP_PAUSE = 0.5        # seconds without tapping Up after such a jump, not to grab the ladder again
BRAKE = 9             # let go of the key this far before the target: the character slides a little
TAP = 0.07            # seconds of a small step
STUCK_AFTER = 0.6     # seconds without moving, while walking, before trying a jump
MAX_JUMPS = 3
WALK_LIMIT = 45.0
MAP_CHANGE_WAIT = 8.0
MAX_FAILURES = 3      # tries in a row on one leg before giving up
CLEAR_DOOR = 0.6      # seconds to walk off a door before tapping Up again, or it's the same door
MAX_SAME_DOOR = 3     # times in a row through one door before the sweep gives up on this go
UP_TAP = 0.05         # seconds of each press of Up while walking into a portal
DARK = 40             # average brightness below which the screen is a loading screen
LAND_WAIT = 0.6       # seconds to land after jumping down from a platform
START_WALK_LEFT = 0.2  # seconds walked left after that, at the start of the sell loop
NPC_FILE = Path(__file__).parent / "templates" / "vendor_npc.png"  # cut at 1920x1080
NPC_THRESHOLD = 0.6
MENU_FILE = Path(__file__).parent / "templates" / "game_menu.png"  # cut at 1920x1080
MENU_THRESHOLD = 0.75
NPC_WINDOW_WAIT = 2.0
WINDOW_CHANGED = 0.08  # share of the middle of the screen that changes when a window opens
WINDOW_GONE = 0.04
SELL_FILE = Path(__file__).parent / "templates" / "sell_item.png"  # the SELL ITEM button, cut at 1920x1080
SELL_THRESHOLD = 0.75
# Parts of the shop window, in pixels from that button's top left corner, at 1920x1080
SELL_TABS = {"Equip": (-176, 103), "Use": (-114, 103), "Etc": (11, 103)}  # Set-up isn't sold from
TAB_PATCH = 12         # half the size of the patch read for a tab's colour
FIRST_ROW = (-43, 157)  # where to double-click the first item
ROW_ICON = (-205, 135, -158, 183)  # the first item's icon (x0, y0, x1, y1)
MESOS_BOX = (-33, 45, 95, 67)
SELL_DIALOGS = (-540, 0, 120, 520)  # where sell boxes open
ROW_NAME = (-152, 132, 74, 157)  # the first item's name
ROW_PITCH = 56.2       # from one row of the list to the next
SELL_ROWS = 6          # rows showing at once
LIST_MATCH = 80        # how alike a name read and a custom list item must be to keep it: errs on keeping
KNOWN_MATCH = 88       # how alike to a known item a name read must be to sell it
ROW_READ_GAP = 0.3     # seconds between the two reads of a row that have to agree before selling it
ROW_READS = 4          # tries at that
SALE_SHOWS = 2.0       # seconds for a sale to show in the mesos or the list
HOW_MANY_RE = re.compile(r"how\s*many", re.I)
ARE_YOU_SURE_RE = re.compile(r"are\s*you\s*sure", re.I)
TAB_ACTIVE_SAT = 90    # the selected tab is red, the others grey
ROW_EMPTY_SPREAD = 20.0  # an empty row's icon is plain (under 10); one with an item is over 50
ROW_CHANGED = 0.1      # share of the first row's icon that differs once another item (or none) is there
SELL_DIALOG_WAIT = 2.0
SELL_SETTLE = 0.5      # seconds for the list to update after a sale
MAX_SALES_PER_TAB = 100
FULL_ROOMS = (6, 5, 4, 3, 2, 1)  # the bottom floor of the Entrance, in the order the sweep reaches them
UPPER_ROOMS = (16, 15, 14, 13)   # two teleporters up: only reached by their saved points
OTHER_LAYOUT = (12, 11, 10, 9, 8, 7, 20, 19, 18, 17)  # one and three teleporters up, and with another inside: see leave_by_ladder
MAX_RECOVERIES = 3     # things going wrong in a row before the full loop gives up
CHANNEL_TRIES = 3      # tries in a row to get into a channel (it may be full) before skipping it
STORE_WALK = 2.5       # seconds to the NPC, when it can't be reached from the door
LOG_DIR = Path(__file__).parent / "logs"
STEPS_DIR = pc.DEBUG_DIR / "nav_steps"
STEP_EVERY = 1.0       # seconds between marked screenshots while walking
MAX_STEPS = 150        # per run; at about 450 KB each
STEPS_LIMIT_MB = 300   # older runs' screenshots go once they add up to more than this
STEPS_RUN_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}")
MINIMAP_ZOOM = 3

MAP_COLOR = crawler.FOUND
YOU = "#ff40ff"
TARGET = "#ffffff"
PORTAL = "#00e5ff"
NPC_COLOR = "#ffd800"
PLACE_NAMES = {"room": "Free Market room", "entrance": "Free Market Entrance", "market": "Henesys Market",
               "vendor": "weapon store", None: "unknown map"}


class Lost(Exception):
    pass


def rooms_for(points):
    """The rooms to go round, in order: the upper ones only with their doors and the teleporter saved."""
    if not points or "teleporter" not in points:
        return FULL_ROOMS
    rooms = FULL_ROOMS
    for more in (UPPER_ROOMS, OTHER_LAYOUT):
        if all(f"entry_room_{r}" in points for r in more):
            rooms += more
    return rooms


def keep_reason(text, keep, known):
    """Why the item in a row whose name reads as text mustn't be sold, or None if it can be. keep:
    the normalised custom list, known: every item's normalised name. A name that can't be read as
    an item is kept too: selling is for when it's sure, and a wrong guess can't be undone."""
    cut_off = text.rstrip().endswith(("..", "\u2026"))
    key = pc.norm(text)
    if len(key) < 3:
        return f"can't read its name ('{text}')"
    if any(fuzz.ratio(key, k) >= LIST_MATCH or (cut_off and k.startswith(key)) for k in keep):
        return f"'{text}' is on the custom list"
    if cut_off:
        return None if any(k.startswith(key) for k in known) else f"'{text}' isn't a known item"
    if key in known or process.extractOne(key, known, scorer=fuzz.ratio, score_cutoff=KNOWN_MATCH):
        return None
    return f"'{text}' isn't a known item"


class Walker:
    points = None       # the saved spots of the doors (data/nav_points.json); None: only the sweep
    allow_sweep = True  # when a saved point doesn't work, fall back on the old way of finding a door
    shops = None        # the crawler, for changing channel; without it there's no last resort

    def __init__(self, hwnd, controls, screen, ocr, jump_key=JUMP_KEY):
        self.hwnd, self.controls, self.screen, self.ocr = hwnd, controls, screen, ocr
        self.jump_key = jump_key
        self.marks = crawler.Marks()
        LOG_DIR.mkdir(exist_ok=True)
        self.log_path = LOG_DIR / f"walk_{datetime.now():%Y-%m-%d}.log"
        self.target = None      # the minimap spot being walked to
        self.on_door = False    # still standing on the door of the room it just went into
        self.heading = "left"   # the way it last walked, to keep going
        self.here = "starting"  # what the map name last said
        self.shots = 0
        self.steps = 0
        self.step_due = 0.0
        self.steps_dir = STEPS_DIR / f"{datetime.now():%Y-%m-%d_%H%M%S}"
        self.minimap = nav.MinimapReader()
        self.points = nav.load_points()
        self.selling = False    # sell at the NPC rather than only opening its window
        self.mesos_from_sales = 0
        self.keep = frozenset()  # normalised names of what's never sold: the custom list
        self.known = None        # normalised names of every item; without them nothing is sold
        self.sell_button = cv2.imread(str(SELL_FILE))
        if self.sell_button is None:
            raise SystemExit(f"missing template: {SELL_FILE}")
        self.game_menu = cv2.imread(str(MENU_FILE), cv2.IMREAD_GRAYSCALE)
        if self.game_menu is None:
            raise SystemExit(f"missing template: {MENU_FILE}")
        self.npc = cv2.imread(str(NPC_FILE))
        if self.npc is None:
            raise SystemExit(f"missing template: {NPC_FILE}")

    # ---- debugging: what shows on screen and in the log

    def note(self, text):
        """A line for the console and the log, without changing the action shown on screen."""
        line = f"{datetime.now():%H:%M:%S} {text}"
        print(line)
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def doing(self, text):
        self.marks.doing(text)
        self.note(text)
        self.save_step(text)

    def marked_picture(self):
        """The latest capture with the overlay's marks drawn on it, since the overlay itself is
        kept out of captures, and the marked minimap enlarged underneath the real one."""
        img = self.screen.frame.copy()
        action, info, items = self.marks.snapshot()

        def label(x, y, text, color):
            cv2.putText(img, text, (int(x), int(y)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (16, 16, 16), 3, cv2.LINE_AA)
            cv2.putText(img, text, (int(x), int(y)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)

        for kind, where, color, text in items:
            bgr = (int(color[5:7], 16), int(color[3:5], 16), int(color[1:3], 16))
            if kind == "cross":
                x, y = where
                cv2.line(img, (x - 7, y), (x + 7, y), bgr, 1)
                cv2.line(img, (x, y - 7), (x, y + 7), bgr, 1)
                cv2.circle(img, (x, y), 5, bgr, 1)
            else:
                x, y, x1, y1 = where
                cv2.rectangle(img, (x, y), (x1, y1), bgr, 1)
            if text:
                label(x + 10, y - 8, text, bgr)
        minimap = next((i for i in items if i[0] != "cross" and i[2] == MAP_COLOR), None)
        if minimap:
            x0, y0, x1, y1 = minimap[1]
            big = cv2.resize(img[y0:y1 + 1, x0:x1 + 1], None, fx=MINIMAP_ZOOM, fy=MINIMAP_ZOOM, interpolation=cv2.INTER_NEAREST)
            top = y1 + 30
            h = min(big.shape[0], img.shape[0] - top)
            w = min(big.shape[1], img.shape[1] - x0)
            img[top:top + h, x0:x0 + w] = big[:h, :w]
        for i, line in enumerate((action, info)):
            label(max(8, img.shape[1] // 3), 22 + i * 20, line, (0, 255, 128))
        return img

    def save_step(self, text):
        """A marked screenshot along the way, named after what it was doing."""
        if self.steps >= MAX_STEPS or getattr(self.screen, "frame", None) is None:
            return
        if self.steps == 0:
            prune_steps()
        self.steps += 1
        self.step_due = time.monotonic() + STEP_EVERY
        self.steps_dir.mkdir(parents=True, exist_ok=True)
        name = "".join(c if c.isalnum() else "_" for c in text)[:50]
        cv2.imwrite(str(self.steps_dir / f"{self.steps:03d}_{name}.jpg"), self.marked_picture(),
                    [cv2.IMWRITE_JPEG_QUALITY, 80])

    def show_position(self, m):
        """Mark the minimap's picture, the dot, the portals and the spot being walked to."""
        x0, y0, x1, y1 = m.picture
        items = [crawler.box(x0, y0, x1, y1, MAP_COLOR)]
        items += [crawler.box(x - 6, y - 6, x + 6, y + 6, PORTAL) for x, y in m.portals]
        where = "dot not visible"
        if m.player:
            items.append(("cross", m.player, YOU, f"you {m.player_x:+.0f}"))
            where = f"dot {m.player_x:+.0f}"
        if self.target is not None:
            x = (x0 + x1) / 2 + self.target * self.minimap.scale
            items.append(("cross", (x, m.player[1] if m.player else (y0 + y1) / 2), TARGET, f"to {self.target:+.0f}"))
            where += f" | walking to {self.target:+.0f}"
        self.marks.set("minimap", items)
        self.marks.tell(f"map: {self.here} | {where}")
        if time.monotonic() >= self.step_due:
            self.save_step(f"{self.here} {where}")

    def shot(self, why):
        """Save the current screen for a failure, and say so."""
        self.shots += 1
        path = pc.DEBUG_DIR / f"nav_failed_{self.shots}.png"
        pc.DEBUG_DIR.mkdir(exist_ok=True)
        self.screen.grab()
        self.marks.doing(f"FAILED: {why}")
        cv2.imwrite(str(path), self.marked_picture())
        self.note(f"FAILED: {why} (screenshot: debug\\{path.name})")
        return path.name

    # ---- reading the screen

    def place(self):
        """'room', 'entrance', 'market', 'vendor' or None, and the room number if it's a room."""
        self.screen.grab()
        text = nav.read_map_name(self.screen.raw, self.ocr)
        kind, room = nav.map_kind(text)
        name = PLACE_NAMES[kind] + (f" {room}" if room else "")
        if name != self.here:
            self.note(f"map is now: {name}" + ("" if kind else f" (the map name area reads: '{text}')"))
            self.here = name
            self.marks.clear("minimap", "npc")  # those were on the map we just left
            self.target = None
        self.marks.tell(f"map: {name}")
        return kind, room

    def dot(self):
        """The dot's distance from the middle of the map picture, or None if it can't be seen."""
        m = self.minimap.read(self.screen.grab())
        if not m:
            self.marks.clear("minimap")
            self.marks.tell(f"map: {self.here} | no minimap found")
            return None
        self.show_position(m)
        return m.player_x if m.player else None

    def menu_open(self):
        """Whether the game menu (what Esc opens) is showing."""
        gray = cv2.cvtColor(self.screen.grab(), cv2.COLOR_BGR2GRAY)
        guess = gray.shape[0] / 1080
        for k in range(-10, 11):
            s = guess * (1 + k / 100)
            tpl = cv2.resize(self.game_menu, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            if cv2.minMaxLoc(cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED))[1] >= MENU_THRESHOLD:
                return True
        return False

    def middle(self):
        """The middle of the whole capture: where windows open. Before trimming, so it's the same
        area whatever the trim makes of an opened window."""
        self.screen.grab()
        h, w = self.screen.raw.shape[:2]
        return self.screen.raw[int(h * 0.15):int(h * 0.8), int(w * 0.25):int(w * 0.75)].copy()

    @staticmethod
    def changed(a, b):
        return 1.0 if a.shape != b.shape else float((cv2.absdiff(a, b).max(axis=2) > 30).mean())

    # ---- moving

    def walk_through(self, target, expected):
        """Walk to the portal at a fixed spot on the minimap, stop on it and press Up, or else walk
        through it while tapping Up. For a map that fits in the minimap's picture, which doesn't
        scroll; players' dots often cover the circles on those crowded maps."""
        at = self.dot()
        self.target = target
        self.note(f"walking to the portal at {target:+.0f} on the minimap, from {at}")
        if at is None:
            self.push_through(self.heading, expected)
            return
        key = "right" if target > at else "left"
        sign = 1 if key == "right" else -1

        def ahead():
            now = self.dot()
            return None if now is None else (target - now) * sign

        if abs(target - at) > BRAKE:
            self._walk_near(key, ahead, time.monotonic() + WALK_LIMIT)
        self.stop_on_portal(key, ahead, lambda: 0)
        if not self.press_up_into(expected):
            self.note("pressing Up didn't work: walking through while tapping Up instead")
            self.push_through(key, expected, ahead)

    def walk_to_ring(self, key, expected, height=None):
        """Walk to a blue circle on the minimap, stop on it and press Up. For a map wider than the
        minimap's picture: that picture scrolls and the dot stays near its middle, so only the
        distance from the dot to the circle means anything.

        height: the circle's height in the picture (which doesn't scroll), which tells it apart
        from others; without one, the next circle this way, where one right under the dot is the
        portal we came out of and doesn't count. A circle can be hidden by another player's dot
        for a while, then its distance is worked out from the walking speed. Walking on until a
        circle shows again would run past it, and on to some other portal."""
        def circles():
            m = self.minimap.read(self.screen.grab())
            if not m or not m.player:
                return m, []
            self.show_position(m)
            y0, y1 = m.picture[1], m.picture[3]
            return m, [(x - m.player[0]) / self.minimap.scale for x, y in m.portals
                       if height is None or abs((y - y0) / (y1 - y0) - height) <= RING_HEIGHT_MATCH]

        _, gaps = circles()
        if height is not None and gaps:  # in sight: that settles which way to go
            key = "right" if min(gaps, key=abs) > 0 else "left"
        sign = 1 if key == "right" else -1
        tracked = {"gap": None, "at": 0.0, "walking": True}

        def ahead():
            m, gaps = circles()
            now = time.monotonic()
            gaps = [g * sign for g in gaps]
            if tracked["gap"] is None:
                candidates = gaps if height is not None else [g for g in gaps if g > RING_UNDER_FOOT]
                if not candidates:
                    return None
                tracked["gap"] = min(candidates, key=abs)
            else:
                walked = WALK_SPEED * (now - tracked["at"]) if tracked["walking"] else 0
                expected_gap = tracked["gap"] - walked
                near = [g for g in gaps if abs(g - expected_gap) <= RING_JUMP]
                if near:  # the same circle as last time
                    tracked["gap"] = min(near, key=lambda g: abs(g - expected_gap))
                elif tracked["walking"]:
                    tracked["gap"] = expected_gap
                else:
                    return None
            tracked["at"] = now
            if m and m.player:
                self.target = m.player_x + tracked["gap"] * sign
            return tracked["gap"]

        gap = ahead()
        if gap is None and height is None:
            raise Lost(f"no portal showing to the {key} on the minimap")
        self.note(f"portal to the {key}: " + (f"{gap:.0f} away on the minimap" if gap is not None else "not in sight yet"))
        if gap is None or gap > BRAKE:
            self._walk_near(key, ahead, time.monotonic() + WALK_LIMIT)
        tracked["walking"] = False
        self.stop_on_portal(key, ahead, lambda: tracked["gap"])
        here = self.place()[0]
        # the circle goes under the dot while it's still a little short, so on towards it first
        if not self.press_up_into(expected, presses=1) and not self.door_nearby(key, here, expected):
            self.note("pressing Up didn't work: walking through while tapping Up instead")
            tracked["walking"] = True
            tracked["at"] = time.monotonic()
            self.push_through(key, expected, ahead)

    def stop_on_portal(self, key, ahead, last_gap):
        """Small steps until the portal's circle is under the dot. Right on a portal the dot hides
        its circle completely, so a circle vanishing from close by counts as arriving."""
        other = "left" if key == "right" else "right"
        for _ in range(SETTLE_STEPS):
            self.controls.wait(0.2)  # slides to a stop first
            gap = ahead()
            if gap is None:
                if abs(last_gap()) <= RING_HIDDEN_NEAR:
                    self.note(f"on the portal: its circle went under the dot ({last_gap():+.0f} away before that)")
                    return
                continue
            if abs(gap) <= ON_PORTAL:
                self.note(f"on the portal: {gap:+.0f} from its circle")
                return
            self.note(f"small step: {gap:+.0f} from the portal's circle")
            self.controls.hold(key if gap > 0 else other, TAP)
        self.note("couldn't settle on the portal: trying from here")

    def press_up_into(self, expected, presses=UP_PRESSES):
        """Standing still on a portal: press Up (a few times). True once on the expected map."""
        self.target = None
        start = self.wait_for_map()
        for attempt in range(1, presses + 1):
            self.doing(f"standing on the portal to {PLACE_NAMES[expected]}: pressing Up ({attempt} of {presses})")
            self.controls.key("up")
            end = time.monotonic() + UP_WAIT
            while time.monotonic() < end:
                self.controls.wait(0.2)
                self.screen.grab()
                kind = self.wait_for_map() if self.screen.raw.mean() < DARK else self.place()[0]
                if kind == expected:
                    return True
                if kind not in (None, start):
                    raise Lost(f"expected {PLACE_NAMES[expected]}, but this is {PLACE_NAMES[kind]}")
        return False

    def push_through(self, key, expected, ahead=None):
        """Hold a direction and keep tapping Up until the map is expected. If that doesn't do it, the same the
        other way. ahead: gives the distance to the portal, to tell when it's been passed."""
        for direction in (key, "left" if key == "right" else "right"):
            self.doing(f"walking {direction} tapping Up, into the portal to {PLACE_NAMES[expected]}")
            self.heading = direction
            sign = 1 if direction == key else -1
            if self._push(direction, expected, ahead and (lambda: (lambda g: None if g is None else g * sign)(ahead()))):
                self.target = None
                return
        self.target = None
        raise Lost(f"walked past the portal to {PLACE_NAMES[expected]} both ways without going through")

    def _push(self, key, expected, ahead):
        """True once on the expected map; False after passing the portal, or the time running out."""
        start = self.wait_for_map()
        if start == expected:
            return True
        end = time.monotonic() + (WALK_LIMIT if ahead else PUSH_LIMIT)
        name_due = 0
        last, moved_at = None, time.monotonic()
        jumps, up_after = 0, 0
        try:
            self.controls.key_down(key)
            while True:
                self.controls.check()
                self.screen.grab()
                if self.screen.raw.mean() < DARK:
                    break  # loading: let go at once, so as not to walk off the new map's portal
                if time.monotonic() >= up_after:
                    self.controls.hold("up", UP_TAP)
                if time.monotonic() >= name_due:  # reading the name is slow: not on every look
                    name_due = time.monotonic() + NAME_EVERY
                    if self.place()[0] != start:
                        break
                if ahead:
                    gap = ahead()
                    if gap is not None and gap < -OVERSHOOT:
                        self.note(f"passed the portal: {-gap:.0f} beyond it")
                        return False
                else:
                    at = self.dot()
                    if at is not None and (last is None or abs(at - last) > 1):
                        last, moved_at, jumps = at, time.monotonic(), 0
                    elif at is not None and time.monotonic() - moved_at > WALL_AFTER:
                        if jumps < LADDER_JUMPS:
                            jumps += 1
                            self.note(f"not moving at {at:+.0f} walking {key}, maybe on a ladder: jump {jumps} of {LADDER_JUMPS}")
                            self.controls.key(self.jump_key)
                            moved_at = time.monotonic()
                            up_after = moved_at + UP_PAUSE
                            continue
                        self.note(f"reached the end of the map walking {key}, at {at:+.0f}")
                        return False
                if time.monotonic() > end:
                    self.note(f"no map change after walking {key} for a while")
                    return False
        finally:
            self.controls.key_up(key)
        end = time.monotonic() + MAP_CHANGE_WAIT
        while time.monotonic() < end:
            kind = self.place()[0]
            if kind == expected:
                return True
            if kind is not None and kind != start:
                raise Lost(f"expected {PLACE_NAMES[expected]}, but this is {PLACE_NAMES[kind]}")
            self.controls.wait(0.3)
        self.note("the screen went dark but the map didn't change")
        return False

    def wait_for_map(self):
        """The map we're on, waiting out a loading screen. None if it still can't be read."""
        end = time.monotonic() + MAP_CHANGE_WAIT
        kind = self.place()[0]
        while kind is None and time.monotonic() < end:
            self.controls.wait(0.3)
            kind = self.place()[0]
        return kind

    def _walk_near(self, key, ahead, end):
        """Walk until ahead() (the distance still to go) is down to BRAKE. If that stops shrinking
        there's a wall or ledge in the way, which gets jumped at."""
        last, moved_at, jumps = None, time.monotonic(), 0
        self.controls.key_down(key)
        try:
            while time.monotonic() < end:
                if crawler.user32.GetForegroundWindow() != self.hwnd:
                    self.controls.key_up(key)
                    self.controls.ensure_front()
                    self.controls.key_down(key)
                self.controls.check()
                gap = ahead()
                if gap is None:
                    self.controls.wait(0.05)
                    continue
                if gap <= BRAKE:
                    return
                if last is None or abs(gap - last) > 1:
                    last, moved_at = gap, time.monotonic()
                elif time.monotonic() - moved_at > STUCK_AFTER:
                    jumps += 1
                    if jumps > MAX_JUMPS:
                        raise Lost(f"stuck walking {key}, {gap:.0f} from the portal, even after jumping")
                    self.doing(f"blocked walking {key}, {gap:.0f} from the portal: jump {jumps} of {MAX_JUMPS}")
                    self.controls.key(self.jump_key)
                    moved_at = time.monotonic()
            raise Lost(f"walking {key} to the portal took over {WALK_LIMIT:.0f}s")
        finally:
            self.controls.key_up(key)

    def jump_down(self):
        """Down + jump drops through the platform the character is standing on."""
        self.controls.key_down("down")
        try:
            self.controls.wait(0.1)
            self.controls.key(self.jump_key)
        finally:
            self.controls.key_up("down")
        self.controls.wait(LAND_WAIT)

    def channel_elsewhere(self):
        """Change to a channel near this one, trying others if it's full. True once that's done."""
        if not self.shops:
            self.note("no way to change channel here")
            return False
        now = self.shops.channel_now()
        if now is None:
            self.note("can't tell which channel this is")
            return False
        for step in range(1, CHANNEL_TRIES + 1):
            target = now - step if now - step >= 1 else now + step
            if self.shops.change_channel(target) and not self.shops.channel_refused:
                self.note(f"now on channel {target}")
                self.on_door = False
                return True
            self.note(f"couldn't get into channel {target}")
            self.recover()
        return False

    def leave_by_ladder(self):
        """Out of a room of the other layout (OTHER_LAYOUT), from anywhere: down to the ground, to
        the small ladder under and left of the door, up it, and right to the door."""
        door, ladder = self.points.get("room_exit_type2"), self.points.get("room_ladder_type2")
        if not door or not ladder:
            raise Lost("the door and ladder of this room layout aren't saved")
        for _ in range(MAX_DROPS + 1):
            now = self.height()
            if now is None:
                raise Lost("can't see the dot to leave this room")
            if abs(now - door["down"]) <= LEVEL_MATCH:
                break
            if abs(now - ladder["down"]) <= LEVEL_MATCH:
                self.climb_ladder(ladder, door)
                break
            self.note(f"{now:.0%} down the map, above the door: jumping down")
            self.jump_down()
        else:
            raise Lost("couldn't get down to the ladder or the door")
        self.doing(f"walking to the door at {door['x']:+.1f}")
        self.walk_to_point(door["x"])
        if not self.press_up_into("entrance") and not self.door_nearby():
            raise Lost("Up didn't work at this room's door, or close by")

    def climb_ladder(self, ladder, door):
        """Walk to the ladder and climb it. Whether Up took shows in the height. Its spot is worked
        out from a screenshot, so small steps either side; and its foot hangs above the ground, so
        lastly a jump."""
        self.doing(f"walking to the ladder at {ladder['x']:+.1f}")
        self.walk_to_point(ladder["x"])
        if self.try_climb(ladder, door):
            return
        for key, steps in (("right", LADDER_SEARCH), ("left", 2 * LADDER_SEARCH)):
            for _ in range(steps):
                self.note(f"Up didn't take the ladder here: looking {key} of it")
                self.controls.hold(key, TAP)
                if self.try_climb(ladder, door):
                    return
        self.walk_to_point(ladder["x"])
        self.note("Up didn't take the ladder: jumping at it")
        if self.try_climb(ladder, door, jump=True):
            return
        raise Lost(f"couldn't get on the ladder around {ladder['x']:+.1f}")

    def try_climb(self, ladder, door, jump=False):
        """Hold Up, and go on holding it while the height goes up. True at the top; False if it
        never left the ground."""
        self.controls.key_down("up")
        try:
            if jump:
                self.controls.key(self.jump_key)
            begun = time.monotonic()
            climbing = False
            while time.monotonic() - begun < CLIMB_LIMIT:
                self.controls.wait(0.1)
                now = self.height()
                if now is None:
                    continue
                if abs(now - door["down"]) <= TOP_MATCH:
                    self.controls.wait(0.3)  # on up onto the ledge
                    self.note(f"up the ladder: {now:.0%} down the map")
                    return True
                climbing = climbing or now < ladder["down"] - CLIMB_SHOWS
                if not climbing and time.monotonic() - begun > CLIMB_START:
                    return False
        finally:
            self.controls.key_up("up")
        self.controls.key(self.jump_key)  # let go of it
        self.controls.wait(LAND_WAIT)
        raise Lost("on the ladder, but not at the top of it")

    def leave_room(self, on_portal=False):
        """Back to the Entrance by the room's portal. on_portal: just arrived through it, so still
        standing on it, and walking would only lead away from it. With no portal showing on the
        minimap, or one on another floor, it just keeps going the way it was going."""
        if on_portal and (self.press_up_into("entrance") or self.door_nearby()):
            return
        if self.points:
            try:
                self.leave_by_point()
                return
            except Lost as e:
                if not self.allow_sweep:
                    raise
                self.shot(f"{e}: trying the old way")
        m = self.minimap.read(self.screen.grab())
        if m and m.player and m.portals:
            nearest = min(m.portals, key=lambda p: abs(p[0] - m.player[0]))
            if abs(nearest[1] - m.player[1]) <= 8 * self.minimap.scale:
                self.walk_to_ring("right" if nearest[0] > m.player[0] else "left", "entrance")
                return
            self.note("the room's portal is on another floor")
        else:
            self.note(f"no portal seen on the minimap (minimap {'found' if m else 'not found'})")
        self.push_through(self.heading, "entrance")

    def walk_to_point(self, target):
        """Walk to a saved spot on the minimap and stop there, jumping anything in the way."""
        end = time.monotonic() + DOT_WAIT
        at = self.dot()
        while at is None and time.monotonic() < end:
            self.controls.wait(0.1)
            at = self.dot()
        if at is None:
            raise Lost("can't see the dot to walk to a saved spot")
        self.target = target
        self.note(f"walking to the saved spot at {target:+.1f}, from {at:+.1f}")
        key = "right" if target > at else "left"
        sign = 1 if key == "right" else -1

        def ahead():
            now = self.dot()
            return None if now is None else (target - now) * sign

        if abs(target - at) > BRAKE:
            self._walk_near(key, ahead, time.monotonic() + WALK_LIMIT)
        self.stop_on_portal(key, ahead, lambda: 0)
        self.target = None

    def height(self):
        """How far down the map the dot is, or None if it can't be seen."""
        m = self.minimap.read(self.screen.grab())
        return m.player_at[1] if m and m.player else None

    def reach_floor(self, down):
        """Get level with a saved spot on the Entrance: down by jumping, up by the teleporters."""
        now = self.height()
        if now is None or abs(now - down) <= FLOOR_MARGIN:
            return
        if now < down:
            self.drop_to_floor(down)
        else:
            self.take_teleporter(down)

    def take_teleporter(self, down):
        """Stand on the teleporter and press Up until level with down, looking after each hop: one
        press too many goes on to the next level up, as there's another teleporter on each."""
        spot = self.points.get("teleporter")
        if not spot:
            raise Lost("the teleporter isn't saved: save it with where_am_i.bat")
        self.doing(f"to the teleporter at {spot['x']:+.1f}")
        self.walk_to_point(spot["x"])
        misses = 0
        while True:
            self.controls.check()
            before = self.height()
            if before is None:
                raise Lost("can't see the dot on the teleporter")
            if abs(before - down) <= FLOOR_MARGIN:
                self.note(f"on the right level: {before:.0%} down the map")
                return
            if before < down:
                raise Lost(f"went past the level: {before:.0%} down the map, wanted {down:.0%}")
            if misses >= UP_PRESSES:
                raise Lost(f"the teleporter doesn't work from here, at {before:.0%} down the map")
            self.doing(f"on the teleporter, {before:.0%} down the map: pressing Up")
            self.controls.key("up")
            end = time.monotonic() + TELEPORT_WAIT
            moved = False
            while time.monotonic() < end and not moved:
                self.controls.wait(0.15)
                now = self.height()
                moved = now is not None and abs(now - before) > FLOOR_MARGIN
            misses = 0 if moved else misses + 1

    def drop_to_floor(self, down):
        """Jump down until level with a saved spot, from a platform above it."""
        for _ in range(MAX_DROPS):
            m = self.minimap.read(self.screen.grab())
            if not m or not m.player or m.player_at[1] >= down - FLOOR_MARGIN:
                return
            self.note(f"{m.player_at[1]:.0%} down the map, and the door is at {down:.0%}: jumping down")
            self.jump_down()

    def leave_by_point(self):
        """From anywhere in a room, walk to its exit and go through. All the rooms are the same."""
        spot = self.points["room_exit"]
        self.doing(f"leaving by the saved exit at {spot['x']:+.1f}")
        self.drop_to_floor(spot["down"])
        self.walk_to_point(spot["x"])
        if not self.press_up_into("entrance") and not self.door_nearby():
            raise Lost("Up didn't work at the saved exit, or close by")

    def enter_by_point(self, room):
        """From the Entrance, walk straight to a room's door and go in."""
        spot = self.points.get(f"entry_room_{room}")
        if not spot:
            raise Lost(f"the door of room {room} isn't saved")
        self.reach_floor(spot["down"])
        self.doing(f"to room {room}: walking to its saved door at {spot['x']:+.1f}")
        self.walk_to_point(spot["x"])
        steps = [None] * UP_PRESSES + [("right", 1)] * ENTRY_SEARCH + [("left", ENTRY_SEARCH + 1)] + [("left", 1)] * (ENTRY_SEARCH - 1)
        for step in steps:
            self.controls.check()
            if step:
                self.note(f"Up did nothing here: looking {step[0]} of it")
                self.controls.hold(step[0], TAP * step[1])
            if not self.up_and_look("entrance"):
                continue
            number = self.room_behind_door()
            if number == room:
                self.doing(f"in room {room}")
                self.on_door = True
                return
            if number is not None:
                self.on_door = True
                raise Lost(f"the saved door of room {room} led to room {number}")
            self.note("came straight back out of the door: pressing Up again from here")
        raise Lost(f"Up didn't work at the saved door of room {room}, or close by")

    def door_nearby(self, first="right", here="room", expected="entrance"):
        """Up did nothing, so it's slightly off the door: small steps either side of here, first
        this way, stopping to press Up at each. True once on the expected map."""
        other = "left" if first == "right" else "right"
        for key, steps in ((first, DOOR_SEARCH), (other, 2 * DOOR_SEARCH)):
            self.doing(f"looking for the door close by: stepping {key}, pressing Up at each step")
            for _ in range(steps):
                self.controls.hold(key, TAP)
                if self.up_and_look(here):
                    end = time.monotonic() + MAP_CHANGE_WAIT
                    while time.monotonic() < end:
                        if self.place()[0] == expected:
                            return True
                        self.controls.wait(0.3)
        return False

    def step(self, here, there):
        """One map further, from here to the next map."""
        self.doing(f"{here} -> {there}")
        if here == "room":
            if self.place()[1] in OTHER_LAYOUT:
                if not (self.on_door and self.press_up_into("entrance")):
                    self.leave_by_ladder()
            else:
                self.leave_room(on_portal=self.on_door)
            self.on_door = False
        elif here == "entrance":
            if self.points:  # the rooms upstairs lead back to their own level
                self.reach_floor(self.points["entry_room_6"]["down"])
            self.walk_through(HUB_EXIT, "market")
        elif (here, there) == ("market", "vendor"):
            self.walk_to_ring("right", "vendor", STORE_HEIGHT)
        elif (here, there) == ("market", "entrance"):
            self.walk_to_ring("left", "entrance", FM_HEIGHT)
        elif here == "vendor":
            self.push_through("right", "market")  # the door is on the right of the store

    def next_map(self, here, target):
        if target == "vendor":
            return "vendor" if here == "market" else ROUTE.get(here)
        if target == "market":
            return "market" if here in ("entrance", "vendor") else ROUTE.get(here)
        return BACK.get(here) if here in BACK else ROUTE.get(here)  # target: the Entrance

    def go_to(self, target):
        """Go to target from wherever we are. Anything going wrong falls back to: close what's
        open, see which map this is, go on from there."""
        failures = 0
        resorted = False  # once only: last_resort
        while True:
            self.controls.check()
            here = self.place()[0]
            if here == target:
                return
            if here is None:
                failures += 1
                why = "can't tell which map this is (is the minimap showing the map's name?)"
                self.shot(why)
                self.give_up_if(failures, why)
                self.recover()
                continue
            try:
                self.step(here, self.next_map(here, target))
                failures = 0
            except Lost as e:
                failures += 1
                self.shot(e)
                if here == "room" and self.place()[1] not in OTHER_LAYOUT and failures >= MAX_FAILURES and not resorted:
                    resorted = True
                    self.recover()
                    if self.last_resort():
                        failures = 0
                        continue
                self.give_up_if(failures, str(e))
                self.recover()

    def last_resort(self):
        """Lost in a Free Market room: holding Up and Right for LOST_HOLD then changing channel
        always leaves the character where jumping down and going left lands on the exit. True if
        that was done; the leaving itself is left to the caller, standing on the door."""
        if not self.shops:
            self.note("no way to change channel here: no last resort")
            return False
        self.doing(f"last resort: holding Up and Right for {LOST_HOLD:.0f}s, then changing channel")
        self.controls.key_down("up")
        try:
            self.controls.hold("right", LOST_HOLD)
        finally:
            self.controls.key_up("up")
        if not self.channel_elsewhere():
            return False
        self.drop_to_portal()
        self.on_door = True
        return True

    def go_to_room(self, room):
        """Into the given Free Market room, from wherever we are."""
        for attempt in range(1, MAX_FAILURES + 1):
            if self.place() == ("room", room):
                return
            self.go_to("entrance")
            try:
                if self.points:
                    try:
                        self.enter_by_point(room)
                        return
                    except Lost as e:
                        if not self.allow_sweep or room not in FULL_ROOMS:  # the sweep only knows the bottom floor
                            raise
                        self.shot(f"{e}: trying the old way")
                        self.go_to("entrance")
                self.sweep_rooms(room)
                return
            except Lost as e:
                self.shot(e)
                self.give_up_if(attempt, str(e))
                self.recover()

    def sweep_rooms(self, room):
        """The doors don't show on the minimap, so: walk to the far right of the Entrance, then
        left in small steps, pressing Up at each, which goes in at the first door reached. A wrong room is left
        again by its portal, and the walk goes on to the left."""
        self.target = None
        if self.points:
            self.reach_floor(self.points["entry_room_6"]["down"])
        self.doing(f"looking for room {room}: walking to the far right")
        self.walk_to_end("right")
        last, repeats = None, 0
        while True:
            self.doing(f"looking for room {room}: stepping left, pressing Up at each step")
            entered = self.walk_left_into_a_door()
            if entered is None:
                raise Lost(f"walked past all the doors without finding room {room}")
            if entered == room:
                self.doing(f"in room {room}")
                self.on_door = True
                return
            repeats = repeats + 1 if entered == last else 0
            last = entered
            if repeats >= MAX_SAME_DOOR:
                raise Lost(f"keeps going back into room {entered} instead of walking on")
            self.doing(f"this is room {entered}, not {room}: going back out")
            self.leave_room(on_portal=True)
            self.controls.hold("left", CLEAR_DOOR)

    def walk_to_end(self, key):
        """Walk until the map ends. Blocked means jump first: it may be a ledge."""
        end = time.monotonic() + WALK_LIMIT
        last, moved_at, jumps = None, time.monotonic(), 0
        self.controls.key_down(key)
        try:
            while time.monotonic() < end:
                self.controls.check()
                at = self.dot()
                if at is None:
                    self.controls.wait(0.05)
                elif last is None or abs(at - last) > 1:
                    last, moved_at, jumps = at, time.monotonic(), 0
                elif time.monotonic() - moved_at > STUCK_AFTER:
                    if jumps >= 2:
                        self.note(f"reached the end of the map at {at:+.0f}")
                        return
                    jumps += 1
                    self.note(f"blocked at {at:+.0f} walking {key}: jump {jumps} of 2")
                    self.controls.key(self.jump_key)
                    moved_at = time.monotonic()
        finally:
            self.controls.key_up(key)
        raise Lost(f"didn't reach the end of the map within {WALK_LIMIT:.0f}s")

    def walk_left_into_a_door(self):
        """The number of the room this ended up in, or None after passing all the doors."""
        end = time.monotonic() + WALK_LIMIT
        last, moved_at = None, time.monotonic()
        walk = True
        while time.monotonic() < end:
            self.controls.check()
            if walk:
                self.controls.hold("left", DOOR_STEP)
            walk = True
            if self.up_and_look("entrance"):
                number = self.room_behind_door()
                if number is not None:
                    self.note(f"went through a door: room {number}")
                    return number
                self.note("came straight back out of the door: pressing Up again from here")
                walk = False
                last, moved_at = None, time.monotonic()
                continue
            at = self.dot()
            if at is None:
                continue
            if at <= ROOMS_END:
                self.note(f"passed all the doors: at {at:+.0f}")
                return None
            if last is None or abs(at - last) > 1:
                last, moved_at = at, time.monotonic()
            elif time.monotonic() - moved_at > STUCK_AFTER * 3:
                raise Lost(f"stuck at {at:+.0f} walking left along the doors")
        raise Lost(f"walking along the doors took over {WALK_LIMIT:.0f}s")

    def up_and_look(self, here):
        """Press Up once, sending no other keys until it's clear whether that started a map change.
        True if it did."""
        self.screen.grab()
        before = self.screen.raw.mean()
        self.controls.key("up")
        end = time.monotonic() + DOOR_WAIT
        while time.monotonic() < end:
            self.controls.wait(0.05)
            self.screen.grab()
            if self.screen.raw.mean() < before * FADING:
                return True
        return self.place()[0] != here

    def room_behind_door(self):
        """The number of the room a door led to, or None if this is the Entrance again: it came
        back out, or the door never opened. Until the screen's been dark or BOUNCE_WAIT has passed,
        the Entrance's name only means it hasn't left yet."""
        started = time.monotonic()
        end = started + MAP_CHANGE_WAIT
        dark = False
        while time.monotonic() < end:
            self.controls.wait(0.3)
            self.screen.grab()
            if self.screen.raw.mean() < DARK:
                dark = True
                continue
            kind, number = self.place()
            if kind == "room":
                return number
            if kind == "entrance":
                if dark or time.monotonic() - started >= BOUNCE_WAIT:
                    return None
            elif kind is not None:
                raise Lost(f"walking along the doors ended up in {PLACE_NAMES[kind]}")
            else:
                dark = True  # no name to read: a loading screen, however bright
        raise Lost("went through a door but can't read which room this is")

    def give_up_if(self, failures, why):
        if failures >= MAX_FAILURES:
            raise crawler.Stop(f"{why} (tried {failures} times; screenshots: debug\\nav_failed_*.png)")

    def recover(self):
        """Close whatever is open, and leave the game menu closed. Esc closes an open window; with
        nothing open, it opens the game menu instead, and then it needs closing again."""
        self.doing("recovering: closing anything that's open")
        if not self.menu_open():
            self.controls.key("esc")
            self.controls.wait(0.5)
            if not self.menu_open():
                self.note("Esc closed a window")
                return
        self.note("closing the game menu")
        self.controls.key("esc")
        self.controls.wait(0.5)
        if self.menu_open():
            raise crawler.Stop("the game menu won't close")

    # ---- the store

    def find_npc(self):
        """Where to click the NPC on screen, or None."""
        frame = self.screen.grab()
        guess = frame.shape[0] / 1080
        best = (0, None, None)
        for k in range(-10, 11):
            s = guess * (1 + k / 100)
            tpl = cv2.resize(self.npc, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            _, v, _, loc = cv2.minMaxLoc(cv2.matchTemplate(frame, tpl, cv2.TM_CCOEFF_NORMED))
            if v > best[0]:
                best = (v, loc, tpl.shape)
        if best[0] < NPC_THRESHOLD:
            return None
        (x, y), (th, tw) = best[1], best[2][:2]
        self.marks.set("npc", [crawler.box(x, y, x + tw, y + th, NPC_COLOR, f"NPC ({best[0]:.2f})"),
                               ("cross", (x + tw / 2, y + th / 2), crawler.CLICK, "")])
        self.note(f"NPC found at ({x + tw // 2}, {y + th // 2}), match {best[0]:.2f}")
        return self.screen.to_screen(x + tw / 2, y + th / 2)

    def talk_to_npc(self):
        """Double-click the NPC, and close its window. Tried from the door first (no walking if
        the click reaches); otherwise walks over and back. True when a window opened and closed."""
        for walk in (False, True):
            spot = self.find_npc()
            if not spot:
                raise Lost("can't see the NPC")
            if walk:
                self.doing("walking to the NPC")
                self.controls.hold("left", STORE_WALK)
                self.controls.wait(0.3)
                spot = self.find_npc() or spot
            before = self.middle()
            self.doing("double-clicking the NPC")
            self.controls.click(*spot, double=True)
            self.park()
            end = time.monotonic() + NPC_WINDOW_WAIT
            while time.monotonic() < end:
                self.controls.wait(0.2)
                change = self.changed(before, self.middle())
                if change > WINDOW_CHANGED:
                    self.note(f"the NPC's window opened ({change:.0%} of the middle of the screen changed)")
                    pc.DEBUG_DIR.mkdir(exist_ok=True)
                    self.controls.wait(0.3)  # let it finish drawing
                    self.screen.grab()
                    cv2.imwrite(str(pc.DEBUG_DIR / "npc_window.png"), self.screen.raw)  # a clean copy, no marks
                    self.sell()
                    self.close_window(before)
                    if walk:
                        self.doing("walking back to the door")
                        self.controls.hold("right", STORE_WALK)
                        self.controls.wait(0.3)
                    self.marks.clear("npc")
                    return
            self.note(f"no window after double-clicking the NPC{' next to it' if walk else ' from the door'}")
        raise Lost("the NPC's window didn't open")

    # ---- selling, with the NPC's shop window open

    def find_sell_button(self, frame):
        """(x, y, scale) of the SELL ITEM button's top left corner, or None: the shop isn't open."""
        guess = frame.shape[0] / 1080
        best = (0, None, None)
        for k in range(-10, 11):
            s = guess * (1 + k / 100)
            tpl = cv2.resize(self.sell_button, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            if tpl.shape[0] > frame.shape[0] or tpl.shape[1] > frame.shape[1]:
                continue
            _, v, _, loc = cv2.minMaxLoc(cv2.matchTemplate(frame, tpl, cv2.TM_CCOEFF_NORMED))
            if v > best[0]:
                best = (v, loc, s)
        return (best[1][0], best[1][1], best[2]) if best[0] >= SELL_THRESHOLD else None

    @staticmethod
    def crop(frame, spot, box):
        x, y, s = spot
        x0, y0, x1, y1 = box
        h, w = frame.shape[:2]
        return frame[max(0, int(y + y0 * s)):min(h, int(y + y1 * s)), max(0, int(x + x0 * s)):min(w, int(x + x1 * s))]

    def active_tab(self, frame, spot):
        """The selected tab of the ones sold from, by its red colour; None if it's another one."""
        for name, (dx, dy) in SELL_TABS.items():
            patch = self.crop(frame, spot, (dx - TAB_PATCH, dy - TAB_PATCH, dx + TAB_PATCH, dy + TAB_PATCH))
            if cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)[..., 1].mean() >= TAB_ACTIVE_SAT:
                return name
        return None

    @staticmethod
    def in_row(box, row):
        x0, y0, x1, y1 = box
        return x0, y0 + row * ROW_PITCH, x1, y1 + row * ROW_PITCH

    def row_used(self, frame, spot, row=0):
        """Whether this row of the list has an item in it."""
        return float(self.crop(frame, spot, self.in_row(ROW_ICON, row)).std()) > ROW_EMPTY_SPREAD

    def row_icon(self, frame, spot, row=0):
        return self.crop(frame, spot, self.in_row(ROW_ICON, row)).copy()

    def row_name(self, frame, spot, row):
        patch = self.crop(frame, spot, self.in_row(ROW_NAME, row))
        patch = cv2.resize(patch, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        return " ".join(text for text, _ in self.ocr.recognize(patch)).strip()

    def settled_row_name(self, spot, row):
        """The row's name once two reads in a row agree, or '' if they don't: the list can still
        be moving up after a sale, and the wrong name here would sell the wrong item."""
        last = None
        for _ in range(ROW_READS):
            name = self.row_name(self.screen.grab(), spot, row)
            if name and name == last:
                return name
            last = name
            self.controls.wait(ROW_READ_GAP)
        return ""

    def read_mesos(self, frame, spot):
        """Your mesos, from the shop window, or None if they can't be read."""
        patch = cv2.resize(self.crop(frame, spot, MESOS_BOX), None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        digits = re.sub(r"\D", "", " ".join(text for text, _ in self.ocr.recognize(patch)))
        return int(digits) if digits else None

    def dialog_text(self, frame, spot):
        """The text in the part of the screen where a sell box opens."""
        return " ".join(text for _, text, _ in self.ocr.detect(self.crop(frame, spot, SELL_DIALOGS)))

    def wait_for_sell_box(self, spot):
        """'many', 'sure', or None if no box opened."""
        end = time.monotonic() + SELL_DIALOG_WAIT
        while time.monotonic() < end:
            self.controls.wait(0.2)
            text = self.dialog_text(self.screen.grab(), spot)
            if HOW_MANY_RE.search(text):
                return "many"
            if ARE_YOU_SURE_RE.search(text):
                return "sure"
        return None

    def sell_row(self, spot, row):
        """Double-click the row's item, and press Enter on the box that opens. A stack asks how
        many, and that box already holds the whole stack; anything else asks if you're sure."""
        x, y, s = spot
        self.controls.click(*self.screen.to_screen(x + FIRST_ROW[0] * s, y + (FIRST_ROW[1] + row * ROW_PITCH) * s), double=True)
        self.park()
        if not self.wait_for_sell_box(spot):
            self.note("no sell box opened")
            return False
        self.controls.key("enter")
        self.controls.wait(SELL_SETTLE)
        return True

    def park(self):
        """The cursor off the shop window: it can cover what's being read."""
        self.controls.move(*self.screen.to_screen(self.screen.region["width"] * crawler.PARK_AT[0],
                                                  self.screen.region["height"] * crawler.PARK_AT[1]))

    def sell(self):
        """Sell what's on the Equip, Use and Etc tabs, except anything on the custom list or whose
        name it can't be sure of: the first row that can be sold, over and over, as the next item
        moves up into it. Anything unexpected stops that tab, and never clicks on."""
        if not self.selling:
            self.note("selling is off for this test: nothing was sold")
            return
        if not self.known:
            raise crawler.Stop("no item names to check what's sold against: not selling anything")
        self.park()
        self.controls.wait(0.3)
        spot = self.find_sell_button(self.screen.grab())
        if not spot:
            raise Lost("the NPC's window opened, but it isn't the shop")
        first = self.read_mesos(self.screen.frame, spot)
        for tab, (dx, dy) in SELL_TABS.items():
            self.doing(f"selling: {tab} tab")
            x, y, s = spot
            self.controls.click(*self.screen.to_screen(x + dx * s, y + dy * s))
            self.park()
            self.controls.wait(0.4)
            frame = self.screen.grab()
            active = self.active_tab(frame, spot)
            if active != tab:
                self.shot(f"clicked the {tab} tab, but {active} is selected: not selling from it")
                continue
            row, sold = 0, 0
            while sold < MAX_SALES_PER_TAB:
                if row >= SELL_ROWS:
                    self.note(f"{tab} tab: all {SELL_ROWS} rows showing are being kept: anything below them stays too")
                    break
                frame = self.screen.grab()
                if not self.row_used(frame, spot, row):
                    self.note(f"{tab} tab: done after {sold} sales, keeping {row}")
                    break
                name = self.settled_row_name(spot, row)
                why = keep_reason(name, self.keep, self.known)
                if why:
                    self.note(f"{tab} tab: keeping row {row + 1}: {why}")
                    row += 1
                    continue
                frame = self.screen.frame
                before = self.read_mesos(frame, spot)
                icon = self.row_icon(frame, spot, row)
                self.save_step(f"selling {name} from {tab}")
                if not self.sell_row(spot, row):
                    self.shot(f"couldn't sell from the {tab} tab")
                    break
                sold += 1
                frame = self.wait_for_sale(spot, row, before, icon)
                after = self.read_mesos(frame, spot)
                self.note(f"{tab} tab: sold {name} for {'?' if None in (before, after) else f'{after - before:,}'} mesos (now {after})")
                # some items sell for 0, so no mesos alone doesn't mean it failed: the row changes too.
                # Neither means the item is still there, and going on would only repeat it.
                if before is not None and after == before and self.changed(icon, self.row_icon(frame, spot, row)) < ROW_CHANGED:
                    self.shot(f"selling from the {tab} tab changed nothing")
                    break
        last = self.read_mesos(self.screen.grab(), spot)
        if first is not None and last is not None:
            self.mesos_from_sales += last - first
            self.doing(f"sold everything: +{last - first:,} mesos this trip (now {last:,})")
        else:
            self.doing("selling done, but your mesos couldn't be read")

    def wait_for_sale(self, spot, row, before, icon):
        """The screen once a sale shows in your mesos or in the list, or after SALE_SHOWS."""
        end = time.monotonic() + SALE_SHOWS
        while True:
            frame = self.screen.grab()
            mesos = self.read_mesos(frame, spot)
            if time.monotonic() >= end or (mesos is not None and mesos != before) \
                    or self.changed(icon, self.row_icon(frame, spot, row)) >= ROW_CHANGED:
                return frame
            self.controls.wait(0.2)

    def close_window(self, before):
        self.doing("closing the NPC's window")
        for _ in range(2):
            self.controls.key("esc")
            self.controls.wait(0.6)
            change = self.changed(before, self.middle())
            self.note(f"after Esc, {change:.0%} of the middle of the screen is still different")
            if change < WINDOW_GONE:
                return
        raise Lost("the NPC's window didn't close")

    def trip(self):
        self.go_to("vendor")
        for attempt in range(1, MAX_FAILURES + 1):
            try:
                self.talk_to_npc()
                break
            except Lost as e:
                self.shot(e)
                self.give_up_if(attempt, str(e))
                self.recover()
        self.go_to("entrance")

    def sell_loop(self, start, back):
        """From room start to the NPC and back to room back. Doesn't start from any other map."""
        kind, room = self.place()
        if (kind, room) != ("room", start):
            raise crawler.Stop(f"please start in Free Market room {start}: this reads as {self.here} "
                               f"(is the minimap showing the map's name?)")
        self.drop_to_portal()
        self.trip()
        self.go_to_room(back)
        if self.place() != ("room", back):
            raise crawler.Stop(f"expected to end in room {back}, but this is {self.here}")

    def nav_test(self, cycles=0):
        """One room on each floor of the Entrance, back to the main portal after each without going
        through it, and again until stopped (or for this many cycles). Each cycle takes the next
        room of each floor, so in time every door gets tried. Rooms of the other layout are left by
        their door one cycle and from the ground, by the ladder, the next. No selling, and no falling back on
        the sweep, so that how the saved points do shows. Any problem still gets retried."""
        if not self.points:
            raise crawler.Stop("no saved points: save them with where_am_i.bat first")
        kind, _ = self.place()
        if kind not in ("room", "entrance"):
            raise crawler.Stop(f"please start in a Free Market room or on the Entrance: this reads as {self.here} "
                               f"(is the minimap showing the map's name?)")
        self.allow_sweep = False
        rooms = rooms_for(self.points)
        floors = [FULL_ROOMS, (7, 8, 9, 10, 11, 12), sorted(UPPER_ROOMS), (17, 18, 19, 20)]
        floors = [f for f in floors if f[0] in rooms]
        cycle, done = 0, 0
        begun = time.monotonic()

        def leg(name, go):
            nonlocal done
            self.controls.check()
            shots = self.shots
            started = time.monotonic()
            self.doing(f"cycle {cycle}: {name}")
            go()
            done += 1
            self.note(f"-- {name}: {time.monotonic() - started:.1f}s"
                      + (f", {self.shots - shots} PROBLEMS on the way" if self.shots != shots else ""))

        while not cycles or cycle < cycles:
            cycle += 1
            for floor in floors:
                room = floor[(cycle - 1) % len(floor)]
                from_ground = room in OTHER_LAYOUT and cycle % 2 == 0
                leg(f"into room {room}", lambda: self.go_to_room(room))
                if from_ground:
                    self.note("down off the door's ledge, and away from it")
                    self.jump_down()
                    self.controls.hold("left", 1.5)
                    self.on_door = False
                leg(f"out of room {room}" + (" by the ladder" if from_ground else ""), lambda: self.go_to("entrance"))
                leg("back to the main portal", self.to_main_portal)
            self.note(f"-- cycle {cycle} done: {done} legs, {self.shots} problems so far, {crawler.duration(time.monotonic() - begun)}")

    def to_main_portal(self):
        """Stand on the Entrance's big portal, without going through it."""
        self.reach_floor(self.points["entry_room_6"]["down"])
        self.walk_to_point(HUB_EXIT)

    def drop_to_portal(self):
        """From where a character starts out in a room, to where its portal can be reached."""
        self.doing("jumping down, then walking left")
        self.jump_down()
        self.controls.hold("left", START_WALK_LEFT)
        self.heading = "left"
        self.controls.wait(0.2)

    def next_channel(self, shops_crawler):
        """Change to the channel below this one (from channel 1, to 2)."""
        now = shops_crawler.channel_now()
        if now is None:
            raise crawler.Stop("can't tell which channel this is")
        target = now - 1 if now > 1 else 2
        self.doing(f"changing channel: {now} to {target}")
        if shops_crawler.change_channel(target) is False:
            raise crawler.Stop(f"couldn't change from channel {now} to {target}")
        self.on_door = False  # a channel change puts the character at the map's starting spot
        self.note(f"now on channel {target}")

    def long_loop(self, start, back, shops_crawler, shops=3):
        """sell_loop, check some shops in the room it ended up in, change channel, leave the room
        the way it does at the start, then to the NPC again."""
        begun = time.monotonic()
        self.sell_loop(start, back)
        self.note(f"-- first sell trip took {crawler.duration(time.monotonic() - begun)}")
        begun = time.monotonic()
        self.doing(f"checking {shops} shops in room {back}")
        self.marks.clear("minimap")
        shops_crawler.crawl_room(f"room {back}", max_shops=shops)
        shops_crawler.marks.clear("shops", "target", "listings")
        self.note(f"-- checking the shops took {crawler.duration(time.monotonic() - begun)}")
        begun = time.monotonic()
        self.next_channel(shops_crawler)
        self.note(f"-- changing channel took {crawler.duration(time.monotonic() - begun)}")
        begun = time.monotonic()
        self.drop_to_portal()
        self.trip()
        self.note(f"-- second sell trip took {crawler.duration(time.monotonic() - begun)}")


def next_room(room, rooms=FULL_ROOMS):
    """The room after this one, so each round is in a different room."""
    return rooms[(rooms.index(room) + 1) % len(rooms)] if room in rooms else rooms[0]


def prune_steps(folder=None, limit_mb=STEPS_LIMIT_MB):
    """Deletes the oldest runs' folders of step screenshots until the rest fit in limit_mb, always
    leaving the newest. Only folders named like a run's. One that can't be deleted now stays for next time."""
    folder = Path(folder or STEPS_DIR)
    if not folder.is_dir():
        return
    runs = sorted((d for d in folder.iterdir() if d.is_dir() and STEPS_RUN_RE.fullmatch(d.name)), key=lambda d: d.name, reverse=True)
    total = 0
    for i, run in enumerate(runs):
        total += sum(f.stat().st_size for f in run.iterdir() if f.is_file())
        if i and total > limit_mb * 1024 * 1024:
            shutil.rmtree(run, ignore_errors=True)


def run_full(shops, channels, budget, rounds=0, sell_below=10):
    """Every channel asked for in a room (skipping any it can't get into, as they may be full), then the
    same in the next room. When under sell_below percent of the budget is left, to the NPC to
    sell everything, and on to the next room with what the budget has become. Only NPC deals
    are bought. Goes on until you stop it (or for this many rounds). Anything going wrong falls
    back to closing what's open, working out where it is and carrying on from there."""
    # the overall budget, of which some can be set aside for custom deals from the start
    start = (getattr(shops.args, "custom_start", 0) or 0) if getattr(shops, "custom_from_profits", False) else 0
    if start >= budget:
        return f"stopped: {start:,} for custom deals leaves nothing of the {budget:,} budget for NPC deals"
    shops.npc_budget = budget
    shops.budget = budget - start
    shops.profit_pool = shops.custom_reserve = start
    shops.spent = 0
    shops.sell_below = sell_below
    shops.update_info()
    limit = shops.args.minutes * 60 if shops.args.minutes else float("inf")
    deadline = time.monotonic() + limit
    controls = shops.controls = crawler.Controls(shops.hwnd, deadline)
    walker = Walker(shops.hwnd, controls, shops.screen, shops.reader)
    walker.marks = shops.marks
    walker.shops = shops
    walker.selling = True
    walker.keep, walker.known = frozenset(shops.watch.custom) | pc.STAR_KEYS, shops.db.keys
    rooms = rooms_for(walker.points)
    asked = getattr(shops.args, "rooms", None)
    if asked:
        unknown = [r for r in asked if r not in rooms]
        if unknown:
            controls.stop.set()
            return f"stopped: no saved door for room {', '.join(map(str, unknown))}"
        rooms = tuple(asked)
    print(f"Rooms, in order: {', '.join(map(str, rooms))}")
    print(f"Channels, in order: {', '.join(map(str, channels)) or 'only the one you are on'}")
    if getattr(shops, "custom_from_profits", False):
        print(f"{budget - start:,} of the budget is only for NPC deals. Custom list items are bought with no limit, from the "
              f"{start:,} set aside, then only from profits once the money is past {budget:,}.")
    print(f"Budget {budget:,} mesos. At the NPC it SELLS EVERYTHING on the Equip, Use and Etc tabs, except "
          f"the {len(shops.watch.custom)} items on your custom list and throwing stars.")
    print("Switch to MapleStory now. Starting in 3 seconds... (hold ESC for 1s to stop)")
    walker.doing("switch to MapleStory: starting in 3 seconds (hold ESC 1s to stop)")
    time.sleep(3)
    started = time.monotonic()
    shops.start_custom_log()
    finished_rounds, problems, earned = 0, 0, 0
    todo = None  # channels left in this round; None: the round hasn't started
    room = None
    sell_next = False

    def duration():
        return crawler.duration(time.monotonic() - started)

    def profits():
        return f", for custom deals {shops.profit_pool:,}" if getattr(shops, "custom_from_profits", False) else ""

    try:
        while True:
            try:
                controls.ensure_front()
                if room is None:
                    kind, number = walker.place()
                    room = number if kind == "room" and number in rooms else rooms[0]
                if sell_next:
                    if not walker.on_door and room not in OTHER_LAYOUT:
                        walker.drop_to_portal()
                    walker.on_door = False
                    walker.mesos_from_sales = 0
                    walker.trip()
                    earned += walker.mesos_from_sales
                    shops.sold_for(walker.mesos_from_sales)
                    shops.update_info()
                    walker.note(f"-- sold for {walker.mesos_from_sales:,}: budget is now {shops.budget:,}{profits()} ({duration()} so far)")
                    sell_next = False
                    if rounds and finished_rounds >= rounds:
                        return f"{finished_rounds} rounds done"
                    if shops.budget <= 0:
                        return "no budget left, even after selling"
                    room = next_room(room, rooms)
                if walker.place() != ("room", room):
                    walker.go_to_room(room)
                if todo is None:
                    walker.note(f"-- round {finished_rounds + 1}: room {room}, budget {shops.budget:,} ({duration()} so far)")
                    walker.marks.clear("minimap", "npc")
                    walker.target = None
                    crawl_here, todo = shops.start_channel(channels)
                    if crawl_here:
                        shops.crawl_room(f"room {room}")
                while todo and not shops.money_low():
                    target = todo[0]
                    walker.target = None
                    walker.marks.clear("minimap", "npc")
                    changed = None
                    for attempt in range(1, CHANNEL_TRIES + 1):
                        changed = shops.change_channel(target)
                        if changed is not False and not shops.channel_refused:
                            break
                        walker.note(f"couldn't get into channel {target} (try {attempt} of {CHANNEL_TRIES})")
                        walker.recover()  # a "channel is full" notice or the like
                    else:
                        walker.note(f"-- skipping channel {target}: couldn't get in after {CHANNEL_TRIES} tries")
                    if changed:
                        walker.on_door = False
                        shops.crawl_room(f"room {room} channel {target}")
                    todo.pop(0)
                    problems = 0
                low = shops.money_low()
                walker.note(f"-- room {room} done" + (": budget low, going to sell" if low else "") + f" ({duration()} so far)")
                finished_rounds += 1
                todo = None
                if low:
                    sell_next = True
                    continue
                if rounds and finished_rounds >= rounds:
                    return f"{finished_rounds} rounds done"
                following = next_room(room, rooms)
                if following != room:
                    if not walker.on_door and room not in OTHER_LAYOUT:
                        walker.drop_to_portal()
                    room = following
            except crawler.Stop as e:
                if controls.stop.is_set() or time.monotonic() > deadline:
                    raise
                problems += 1
                walker.shot(f"{e} (problem {problems} of {MAX_RECOVERIES} in a row)")
                if problems >= MAX_RECOVERIES:
                    raise crawler.Stop(f"{e}, {problems} times in a row")
                if todo:
                    todo.pop(0)  # a channel that keeps failing isn't tried for ever
                walker.recover()
    except crawler.Stop as e:
        return f"stopped: {e}"
    finally:
        controls.stop.set()
        walker.marks.clear("click", "target", "minimap", "npc")
        walker.note(f"{finished_rounds} rounds in {duration()}; {earned:,} mesos from the NPC; budget left {shops.budget - shops.spent:,}{profits()}")
        shops.write_custom_log(duration())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", choices=["vendor", "market", "entrance"], help="only walk there (no NPC)")
    ap.add_argument("--room", type=int, help="only go to this Free Market room (bottom floor of the Entrance)")
    ap.add_argument("--sell-loop", type=int, nargs=2, metavar=("FROM", "TO"),
                    help="from this Free Market room to the NPC, then back to the other room")
    ap.add_argument("--long-loop", type=int, nargs=2, metavar=("FROM", "TO"),
                    help="--sell-loop, then check 3 shops in the second room, change channel, then to the NPC again")
    ap.add_argument("--nav-test", action="store_true",
                    help="one room on each floor, back to the main portal after each, over and over. Never sells")
    ap.add_argument("--shops", type=int, default=3, help="how many shops --long-loop checks")
    ap.add_argument("--budget", type=crawler.parse_mesos, default=0,
                    help="most --long-loop may spend in those shops (default: nothing, it only looks)")
    ap.add_argument("--no-sell", action="store_true", help="--long-loop only opens the NPC's window")
    ap.add_argument("--jump-key", default=JUMP_KEY, choices=sorted(crawler.VK))
    ap.add_argument("--window", default="maplestory.exe")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True, errors="replace")

    found = pc.find_window(args.window)
    if not found:
        raise SystemExit(f"{args.window} isn't open (or is minimised)")
    hwnd = found[0]
    controls = crawler.Controls(hwnd, float("inf"))
    shops_crawler = None
    if args.long_loop or args.nav_test:  # the nav test only for changing channel: it buys and sells nothing
        shops_crawler = crawler.Crawler(argparse.Namespace(window=args.window, match=88, debug=False))
        shops_crawler.controls = controls
        shops_crawler.budget = args.budget
        print(f"Budget for the shops: {args.budget:,} mesos" + ("" if args.budget else " (it only looks, and buys nothing)"))
    walker = Walker(hwnd, controls, shops_crawler.screen if shops_crawler else crawler.Screen(hwnd),
                    shops_crawler.reader if shops_crawler else pc.Reader(1.0), args.jump_key)
    if shops_crawler:
        shops_crawler.marks = walker.marks  # the overlay draws these
        walker.shops = shops_crawler
        walker.selling = bool(args.long_loop) and not args.no_sell
        walker.keep, walker.known = shops_crawler.watch.custom, shops_crawler.db.keys
        print("At the NPC it SELLS EVERYTHING on the Equip, Use and Etc tabs, except your custom list" if walker.selling
              else "At the NPC it sells nothing")
    finished = []

    def work():
        walker.doing("switch to MapleStory: starting in 3 seconds (hold ESC 1s to stop)")
        time.sleep(3)
        started = time.monotonic()
        try:
            controls.ensure_front()
            if args.nav_test:
                walker.nav_test()
            elif args.long_loop:
                walker.long_loop(*args.long_loop, shops_crawler, args.shops)
            elif args.sell_loop:
                walker.sell_loop(*args.sell_loop)
            elif args.room:
                walker.go_to_room(args.room)
            elif args.to:
                walker.go_to(args.to)
            else:
                walker.trip()
            walker.doing(f"done in {crawler.duration(time.monotonic() - started)}")
        except crawler.Stop as e:
            walker.doing(f"stopped: {e}")
        except Exception:
            walker.doing("crashed: see the console")
            raise
        finally:
            controls.stop.set()
            finished.append(time.monotonic())

    # the walking runs on a thread; the overlay (tkinter) has to stay on this one
    overlay = crawler.CrawlOverlay()

    def stop(*_):
        controls.stop_reason = "Ctrl+C"
        controls.stop.set()

    signal.signal(signal.SIGINT, stop)

    def tick():
        overlay.draw_marks(walker.screen.region, walker.marks)
        if finished and time.monotonic() - finished[0] > 10:  # leave the last state up to read it
            overlay.root.destroy()
            return
        overlay.root.after(100, tick)

    threading.Thread(target=work, daemon=True).start()
    tick()
    overlay.root.mainloop()


if __name__ == "__main__":
    main()
