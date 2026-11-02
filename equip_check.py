"""The equipment finder's check in a store: hover a listing, read its tooltip, and see whether it has
the stats and scroll slots asked for (custom_items, an item's "finder"). A purchase needs every stat
asked for read the same at least twice: anything less is unsure, and unsure doesn't buy.
Every check goes in logs/equipment_finder.csv, with a screenshot in debug/equipment_finder/ of the
ones that matched or were unsure.
"""
import csv
import re
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

import price_checker as pc

LOG_FILE = Path(__file__).parent / "logs" / "equipment_finder.csv"
SHOT_DIR = pc.DEBUG_DIR / "equipment_finder"
KEEP_SHOTS = 50
FIELDS = ["time", "place", "owner", "item", "asking", "wanted", "read", "verdict", "screenshot"]
TEXT_MIN = 130     # tooltip text is brighter than this in every colour channel
AREA = (-80, -480, 560, 480)  # around the cursor, at UI scale 1: tooltips open on any side of it
MIN_READS = 2      # a stat has to be read this many times, all the same
# How tooltips word each stat. Weapon Def., Magic Def., Evasion, Critical Rate, Critical Damage and
# Remaining Enhancements were seen in the game; the rest are what they should be called
PATTERNS = {
    "slots": r"enhan\w*", "wdef": r"weapon\s*def\w*\.?", "mdef": r"magic\s*def\w*\.?", "critRate": r"crit\w*\s*rate", "critDamage": r"crit\w*\s*dam\w*",
    "watk": r"weapon\s*att\w*|\batt\w*\s*power|\battack(?!\s*speed)", "matk": r"magic\s*att\w*", "str": r"\bstr\b", "dex": r"\bdex\b", "int": r"\bint\b", "luk": r"\bluk\b",
    "hp": r"\b(?:max\s*)?hp\b", "mp": r"\b(?:max\s*)?mp\b", "acc": r"accuracy", "avoid": r"evasion|avoid\w*", "speed": r"\bspeed\b", "jump": r"\bjump\b",
}
REGEX = {k: re.compile(rf"(?:{p})\s*[:;.]?\s*\+?\s*(\d+)", re.I) for k, p in PATTERNS.items()}


def stats_in(lines, keys):
    """What lines of text say of these stats. "Critical Rate: +2 (1 +1)" is 2, the total; lines about
    what's required to wear it ("REQ STR : 0") don't count."""
    found = {}
    for text in lines:
        if re.match(r"\W*req\b", text, re.I):
            continue
        for key in keys:
            m = REGEX[key].search(text)
            if m and key not in found:
                found[key] = int(m.group(1))
    return found


def read_area(frame, reader, x, y, s):
    """Lines of text around the cursor at (x, y), twice: as it is, and as black on white, as what
    shows through the tooltip is dimmer than its text. [lines, lines]"""
    x0, y0, x1, y1 = (max(0, int(x + AREA[0] * s)), max(0, int(y + AREA[1] * s)),
                      min(frame.shape[1], int(x + AREA[2] * s)), min(frame.shape[0], int(y + AREA[3] * s)))
    crop = frame[y0:y1, x0:x1]
    if not crop.size:
        return []
    zoom = 1 / s if s < 0.95 else 1
    big = cv2.resize(crop, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_CUBIC) if zoom != 1 else crop
    bright = np.where((big.min(axis=2) >= TEXT_MIN)[..., None], 0, 255).astype(np.uint8).repeat(3, axis=2)
    return [[l.text for l in sorted(reader.read(image), key=lambda l: l.y0)] for image in (big, bright)]


def judge(reads, wanted):
    """("yes" / "no" / "unsure", {stat: the value read}). reads: stats_in's of every read. No: a stat
    was surely under what's wanted. Unsure: anything not read the same at least MIN_READS times."""
    values, sure = {}, True
    verdict_no = False
    for key, least in wanted.items():
        seen = [r[key] for r in reads if key in r]
        if len(seen) < MIN_READS or len(set(seen)) != 1:
            sure = False
            values[key] = "/".join(map(str, sorted(set(seen)))) or "?"
            continue
        values[key] = seen[0]
        if seen[0] < least:
            verdict_no = True
    return ("no" if verdict_no else "yes" if sure else "unsure"), values


def wanted_stats(finder):
    wanted = dict(finder.get("stats") or {})
    if finder.get("slots") is not None:
        wanted["slots"] = finder["slots"]
    return wanted


def record(frame, place, owner, hit, wanted, values, verdict, x, y, log_file=None, shot_dir=None):
    """One line in the log; a screenshot too, of a match or an unsure one, keeping the newest few."""
    log_file, shot_dir = Path(log_file or LOG_FILE), Path(shot_dir or SHOT_DIR)
    name = ""
    if verdict != "no" and frame is not None:
        shot_dir.mkdir(parents=True, exist_ok=True)
        name = f"{datetime.now():%Y-%m-%d_%H%M%S}_{verdict}.jpg"
        image = frame.copy()
        cv2.drawMarker(image, (int(x), int(y)), (0, 0, 255), cv2.MARKER_CROSS, 24, 2)
        label = f"{hit.name} at {hit.asking:,}: {verdict} - read {values}, wanted {wanted}"
        cv2.rectangle(image, (0, 0), (image.shape[1], 34), (0, 0, 0), -1)
        cv2.putText(image, label.encode("ascii", "replace").decode(), (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        cv2.imwrite(str(shot_dir / name), image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        for old in sorted(shot_dir.glob("*.jpg"))[:-KEEP_SHOTS]:
            old.unlink(missing_ok=True)
    log_file.parent.mkdir(exist_ok=True)
    new = not log_file.exists()
    with open(log_file, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FIELDS)
        w.writerow([f"{datetime.now():%Y-%m-%d %H:%M:%S}", place, owner, hit.name, hit.asking, wanted, values, verdict, name])
