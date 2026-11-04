"""Takes the screenshots the store bot is built from, one step at a time, in order.

The step to do shows over the game (and here). Do it in the game, then:
    F9   save a screenshot of the game window for this step (most steps then go to the next)
    F10  next step: done with this one, or it can't be done right now
    F11  back a step
Screenshots go to printscreens/store_setup/ (not uploaded: they show your character's name), with
where you were standing on the minimap for the steps about spots (positions.json). The game window
must be in front for the keys to count. At the end it asks a few questions here.

    python store_capture.py [--from 5]    # start at step 5
"""
import argparse
import json
import time
import winsound
from datetime import datetime
from pathlib import Path

import cv2
import mss
import numpy as np

import navigation
import price_checker as pc

OUT = Path(__file__).parent / "printscreens" / "store_setup"
KEYS = {"shot": 0x78, "next": 0x79, "back": 0x7A}  # F9, F10, F11

# (file name, what to do, more than one shot, record where you're standing)
STEPS = [
    ("free_spot", "Stand at a spot in a Free Market room where a store could go (empty floor), with the minimap showing. F9.", False, True),
    ("crowded_room", "A crowded room, or a crowded part of one, where a store couldn't fit. F9.", False, True),
    ("spot_refused", "Try to open a store somewhere it isn't allowed (next to another store, by a portal...). "
                     "F9 on each message the game shows. F10 when you've seen them all.", True, True),
    ("permit_tooltip", "Open the inventory tab with the store permit, and hover the permit so its tooltip shows. F9.", False, False),
    ("permit_used", "Use the permit as you normally would. F9 on what appears straight after.", False, True),
    ("name_box_empty", "The box for the store's name, before typing anything. F9.", False, False),
    ("name_box_typed", "Type the name. F9 before confirming it.", False, False),
    ("setup_window", "Confirm the name. F9 on the store window that opens (before adding items).", False, False),
    ("store_open", "Add an item or two and open the store (OPEN STORE, then OK). F9 on the open store.", False, False),
    ("visitor_entered", "With your other character, enter the store. F9 when the chat log says so.", False, False),
    ("visitor_chat", "Type something in the store's chat with the other character. F9.", False, False),
    ("partly_sold", "With the other character, buy SOME of a stack. F9 on the owner's store (the row, the chat log).", False, False),
    ("sold_out", "Buy the rest of that stack, or a single item. F9 on the owner's store.", False, False),
    ("visitor_left", "Leave the store with the other character. F9 when the chat log says so (and how long, if it does).", False, False),
    ("sold_items", "Press SOLD ITEMS. F9 on what it shows.", False, False),
    ("store_full", "Optional: more visitors than its 3 spots trying to get in. F9 on any message, or F10 to skip.", True, False),
    ("close_confirm", "Press CLOSE STORE. F9 on any confirmation.", False, False),
    ("after_close", "Confirm. F9 on the screen afterwards, with your inventory open (where did the unsold items go?).", False, True),
]
QUESTIONS = [
    ("permit_used_up", "Was the store permit used up when you opened the store? (y/n): "),
    ("permits", "How many permits do you have, and where do you get more? "),
    ("store_name", "The store name the bot should use: "),
    ("mesos_arrive", "Did the sale mesos arrive in your inventory by themselves? (y/n, or how you collect them): "),
    ("afk_kick", "Does anything close the store or kick you after a while (an AFK timer)? "),
    ("room", "Any room or platform you prefer for the store (or none)? "),
]


class Keys:
    """Presses of F9, F10 and F11 while the game is in front, once each."""

    def __init__(self, hwnd):
        self.hwnd, self.down = hwnd, set()

    def pressed(self):
        out = []
        front = pc.user32.GetForegroundWindow() == self.hwnd
        for name, vk in KEYS.items():
            now = front and bool(pc.user32.GetAsyncKeyState(vk) & 0x8000)
            if now and name not in self.down:
                out.append(name)
            (self.down.add if now else self.down.discard)(name)
        return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", type=int, default=1, help="the step to start at")
    ap.add_argument("--window", default="maplestory.exe")
    args = ap.parse_args()
    found = pc.find_window(args.window)
    if not found:
        raise SystemExit(f"{args.window} isn't open (or is minimised)")
    hwnd = found[0]
    OUT.mkdir(parents=True, exist_ok=True)
    positions_file = OUT / "positions.json"
    positions = json.loads(positions_file.read_text("utf-8")) if positions_file.exists() else {}
    sct = getattr(mss, "MSS", mss.mss)()
    minimap, ocr, overlay, keys = navigation.MinimapReader(), pc.Reader(1.0), pc.Overlay(), Keys(hwnd)
    step, shots = args.start - 1, 0
    last = None
    print(__doc__.split("\n\n")[1])
    while step < len(STEPS):
        name, what, many, where = STEPS[step]
        prompt = f"Step {step + 1} of {len(STEPS)}: {what}"
        if prompt != last:
            print(f"\n{prompt}")
            last = prompt
        rect = pc.client_rect(hwnd)
        for key in keys.pressed():
            if key == "shot":
                raw = np.ascontiguousarray(np.array(sct.grab(rect))[:, :, :3])
                shots += 1
                file = f"{step + 1:02d}_{name}" + (f"_{shots}" if many else "") + ".png"
                cv2.imwrite(str(OUT / file), raw)
                note = {"time": datetime.now().isoformat(timespec="seconds"), "window": [rect["width"], rect["height"]]}
                if where:
                    m = minimap.read(raw)
                    kind, room = navigation.map_kind(navigation.read_map_name(raw, ocr))
                    note.update(map=kind, room=room, dot=m.player_at if m and m.player else None,
                                from_middle=round(m.player_x, 1) if m and m.player else None)
                positions[file] = note
                positions_file.write_text(json.dumps(positions, indent=1), "utf-8")
                winsound.MessageBeep(winsound.MB_OK)
                print(f"  saved {file}" + (f" ({note.get('map')} {note.get('room') or ''}, dot {note.get('dot')})" if where else ""))
                if not many:
                    step, shots = step + 1, 0
            elif key == "next":
                step, shots = step + 1, 0
            elif key == "back" and step > 0:
                step, shots = step - 1, 0
        region = {"left": rect["left"], "top": rect["top"], "width": rect["width"], "height": 60}
        if overlay.begin(region, prompt):
            overlay.text(8, 8, prompt, pc.STATUS_COLOR)
            overlay.text(8, 32, "F9 screenshot | F10 next step | F11 back", pc.STATUS_COLOR)
        overlay.root.update()
        time.sleep(0.05)
    overlay.root.destroy()
    print("\nAll steps done. A few questions:")
    answers = {key: input(question).strip() for key, question in QUESTIONS}
    (OUT / "answers.json").write_text(json.dumps(answers, indent=1), "utf-8")
    print(f"\nThanks: everything is in {OUT}")


if __name__ == "__main__":
    main()
