"""One window for the bots: the full sell loop, the scouter and your store, the custom list, the
equipment finder and the bots' server, and MeowDB's prices kept up to date. Everything you set
stays set (launcher_settings.json).

    launcher.py            the bots (launcher.bat)
    launcher.py overlay    the price checker overlay on its own (overlay.bat)
    launcher.py craft      the crafting calculator on its own (crafting.bat)

Each tool runs as its own program, with what it prints shown in the log at the bottom, so one
crashing leaves the window open. The ones that use the game's screen run one at a time, across
these windows too (data/game_in_use.json): the overlay's boxes would be in a bot's screenshots.

Start the bots with launcher.bat, which asks for the administrator rights they need.
"""
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

import custom_items
import holdings
import market
import sync

HERE = Path(__file__).parent
SETTINGS_FILE = HERE / "launcher_settings.json"
BOT_SETTINGS_FILE = HERE / "crawler_settings.json"
PRICES_FILE = HERE / "data" / "prices.json"
MANUAL_FILE = HERE / "data" / "manual_prices.json"
EQUIP_ROWS = 5  # rows of stats the equipment finder has room for, in two columns
RECIPES_FILE = HERE / "data" / "recipes.json"
CRAFT_PAGE = HERE / "craft_calculator.html"
ICON_FILE = HERE / "launcher.ico"

ALL_ROOMS = "6-1,16-13,12-7,20-17"
STOP_WAIT = 20.0        # seconds a bot gets to stop by itself after Stop, before it's ended by force
SYNC_EVERY = 60.0       # seconds between rounds with the bots' server
SYNC_FILES_EVERY = 600.0
MEOW_READ_EVERY = 5.0   # seconds between looks at the saved prices
MAX_LOG_LINES = 3000
MAX_RESULTS = 200       # items shown in the custom list's search
PROGRESS_RE = re.compile(r"progress (\d+) (\d+)")

TOOLS = {"overlay": "Overlay", "full": "Full sell loop", "scout": "Scouter", "store": "My store", "craft": "Crafting",
         "update": "MeowDB update"}
ON_GAME = ("overlay", "full", "scout", "store")  # these use the game's screen: one at a time
GRACEFUL = ("full", "scout", "store")           # told to stop, and given STOP_WAIT to do it
GAME_LOCK = HERE / "data" / "game_in_use.json"   # which window's tool has the game, across the windows
SCOUT_STATE = HERE / "data" / "scout_state.json"  # where the scouter stopped (walker.SCOUT_STATE_FILE)
DISCORD_NOTIFY = {"runs": "Bots starting and stopping", "errors": "Errors, and bots that stop checking in",
                  "finds": "Scouter finds bought", "requests": "Buy requests: taken, bought, done",
                  "snapshot": "The day's MeowDB snapshot is in", "rmt": "An RMT wave starting",
                  "report": "The daily market report",
                  "alerts": "Price alerts (/alert) pinging whoever set them"}  # as server/discord_link.py's NOTIFY
WINDOWS = {  # which window shows what: title, tabs, whether it talks to the server and updates MeowDB
    "main": ("Maple tools", ("full", "scout", "store", "custom", "finder", "server"), True),
    "overlay": ("Maple overlay", ("overlay",), False),
    "craft": ("Maple crafting", ("craft",), False),
}
PRESS_KEYS = (0x25, 0x26, 0x27, 0x28, 0x12, 0x11, 0x10)  # arrows, alt, ctrl, shift: what a bot can hold down
KEYEVENTF_KEYUP = 2
CREATE_NO_WINDOW = 0x08000000


# ---------------------------------------------------------------- settings

def bot_settings():
    try:
        return json.loads(BOT_SETTINGS_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def defaults():
    cfg = bot_settings()
    percent = str(cfg.get("market", {}).get("below_24h_median_percent", 30))
    craft = cfg.get("crafting", {})
    return {
        "ov_market": True, "ov_percent": percent, "ov_min_profit": "1", "ov_interval": "1.0",
        "ov_tooltips": False, "ov_debug": False,
        "sv_on": False, "sv_url": "", "sv_name": "", "sv_key": "",
        **{f"dn_{k}": True for k in DISCORD_NOTIFY},
        "st_dry": False, "st_place": False, "st_name": "",
        "fl_budget": "", "fl_custom": "off", "fl_npc_money": "", "fl_channels": "40-1", "fl_rooms": ALL_ROOMS,
        "fl_store": False, "fl_store_percent": "15", "fl_wait": False, "fl_minutes": "", "fl_sell_below": str(cfg.get("sell_below_budget_percent", 10)),
        "sc_channels": "40-1", "sc_rooms": ALL_ROOMS, "sc_send_to": "", "sc_minutes": "",
        "cf_margin": str(craft.get("margin_percent", 10)), "cf_disciplines": craft.get("disciplines") or ["Arcforge"],
    }


def load_settings():
    values = defaults()
    try:
        saved = json.loads(SETTINGS_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return values
    if saved.get("fl_custom_stop") is True and "fl_custom" not in saved:  # the tick box this used to be
        saved["fl_custom"] = "limit"
    if "sc_send_to" not in saved and saved.get("fl_scout_for"):  # from when scouting was a tick box on the full sell loop
        saved["sc_send_to"] = saved["fl_scout_for"]
    values.update({k: v for k, v in saved.items() if k in values and isinstance(v, type(values[k]))})
    return values


def save_settings(values):
    """Only the values given change: each window saves its own, and leaves the others' as they are."""
    try:
        saved = json.loads(SETTINGS_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        saved = {}
    known = defaults()
    saved = {k: v for k, v in saved.items() if k in known}  # settings of tabs that are gone
    saved.update(values)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(saved, indent=1), "utf-8")
    tmp.replace(SETTINGS_FILE)


def alive(pid):
    """Whether a program with this id is still running."""
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def game_taken(path=None):
    """The tool another window has running on the game, or None."""
    try:
        lock = json.loads(Path(path or GAME_LOCK).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if lock.get("pid") == os.getpid() or not alive(lock.get("pid", 0)):
        return None
    return lock.get("tool", "a tool")


def take_game(tool, path=None):
    path = Path(path or GAME_LOCK)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "tool": TOOLS[tool]}), "utf-8")


def free_game(path=None):
    path = Path(path or GAME_LOCK)
    try:
        if json.loads(path.read_text("utf-8")).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------- what each tool is started with

def budget_of(text):
    try:
        budget = market.parse_mesos(text)
    except ValueError:
        budget = 0
    if budget <= 0:
        raise ValueError("Type a budget, like 50000, 250k or 1.5m.")
    return str(budget)


def npc_money_of(text, budget):
    """The NPC deals' share of the budget; empty: all of it."""
    try:
        money = market.parse_mesos(text) if text.strip() else int(budget)
    except ValueError:
        raise ValueError("Type the money for NPC deals, like 20k, or leave it empty for the whole budget.") from None
    if not 0 < money <= int(budget):
        raise ValueError("The money for NPC deals has to be more than 0, and no more than the budget.")
    return str(money)


def percent_of(text, what):
    try:
        return str(market.parse_percent(text))
    except ValueError:
        raise ValueError(f"{what} has to be a number between 0 and 100.") from None


def number_of(text, what, whole=False):
    """The text of a number over 0, for a box that can be left empty; '' if it is."""
    text = text.strip()
    if not text:
        return ""
    try:
        value = int(text) if whole else float(text)
    except ValueError:
        value = 0
    if value <= 0:
        raise ValueError(f"{what} has to be a {'whole ' if whole else ''}number over 0, or empty.")
    return text


def channels_of(text):
    try:
        market.parse_numbers(text, 1, 40)
    except ValueError:
        raise ValueError("Channels go from 1 to 40, written like 40-1, 1,3,20,39 or 1-5,20.") from None
    return text.replace(" ", "")


def rooms_of(text):
    try:
        market.parse_numbers(text, 1, 20)
    except ValueError:
        raise ValueError(f"Rooms go from 1 to 20, written like {ALL_ROOMS} or 3,10,15.") from None
    return text.replace(" ", "")


def custom_args(on):
    return ["--buy-custom"] if on else ["--no-custom-buy"]


def overlay_args(v):
    args = ["price_checker.py", "--market-update", "no"]
    if v["ov_market"]:
        args += ["--market-percent", percent_of(v["ov_percent"], "The market deal %")]
    else:
        args.append("--no-market")
    args += ["--min-profit", number_of(v["ov_min_profit"], "Minimum profit", whole=True) or "1",
             "--interval", number_of(v["ov_interval"], "The scan interval") or "1.0"]
    return args + ["--tooltip-slots"] * v["ov_tooltips"] + ["--debug"] * v["ov_debug"]


def full_args(v):
    args = ["full_sell_loop.py", "--market-update", "no", "--budget", budget_of(v["fl_budget"]),
            "--channels", channels_of(v["fl_channels"]), "--rooms", rooms_of(v["fl_rooms"]),
            "--sell-below", percent_of(v["fl_sell_below"], "The sell-below %")]
    if v["fl_custom"] == "profits":
        args += ["--custom-from-profits"]
    else:
        args += custom_args(v["fl_custom"] == "limit")
    if v["fl_store"]:
        args += ["--store-deals", "--market-percent", percent_of(v["fl_store_percent"], "The store deal %")]
    if v["fl_custom"] == "profits" or v["fl_store"]:  # NPC deals' share: the rest is for these
        args += ["--npc-money", npc_money_of(v["fl_npc_money"], budget_of(v["fl_budget"]))]
    if v["fl_wait"]:
        if not (v.get("sv_on") and v.get("sv_url", "").strip() and v.get("sv_key")):
            raise ValueError("Join the server on the Server tab first, with sharing on: waiting for finds gets them from there.")
        args += ["--wait-for-finds"]
    minutes = number_of(v["fl_minutes"], "The time limit")
    return args + (["--minutes", minutes] if minutes else [])


def scout_args(v):
    """The full sell loop as a scouter: no money, nothing bought or sold, every store sent to the server."""
    if not (v.get("sv_on") and v.get("sv_url", "").strip() and v.get("sv_key")):
        raise ValueError("Join the server on the Server tab first, with sharing on: the scouter only sends what it sees there.")
    send_to = ", ".join(n.strip() for n in v["sc_send_to"].split(",") if n.strip())
    args = ["full_sell_loop.py", "--market-update", "no", "--budget", "1", "--no-custom-buy",
            "--channels", channels_of(v["sc_channels"]), "--rooms", rooms_of(v["sc_rooms"]), "--scout"]
    args += ["--scout-for", send_to] if send_to else []
    minutes = number_of(v["sc_minutes"], "The time limit")
    return args + (["--minutes", minutes] if minutes else [])


def craft_args(v):
    if not v["cf_disciplines"]:
        raise ValueError("Tick at least one discipline.")
    try:
        margin = float(v["cf_margin"])
    except ValueError:
        margin = -1
    if margin < 0:
        raise ValueError("The margin has to be a number, 0 or more.")
    args = ["crafting.py", "--market-update", "no", "--margin", v["cf_margin"].strip()]
    for discipline in v["cf_disciplines"]:
        args += ["--discipline", discipline]
    return args


def store_args(v):
    args = ["store.py"] + ["--dry-run"] * v["st_dry"]
    if v["st_place"]:
        if not v["st_name"].strip():
            raise ValueError("Type the store's name, to set it up.")
        args += ["--place", v["st_name"].strip()]
    return args


ARGS = {"overlay": overlay_args, "full": full_args, "scout": scout_args, "store": store_args, "craft": craft_args,
        "update": lambda v: ["market.py", "--update-stale"]}


# ---------------------------------------------------------------- the custom list

def known_items():
    """Every item name the price data has."""
    names = set()
    try:
        names.update(it["name"] for it in json.loads(PRICES_FILE.read_text("utf-8"))["items"])
    except (OSError, ValueError, KeyError):
        pass
    try:
        names.update(n for n in json.loads(MANUAL_FILE.read_text("utf-8")) if not n.startswith("_"))
    except (OSError, ValueError):
        pass
    return sorted(names, key=str.lower)


def search_items(names, text, limit=MAX_RESULTS):
    """Names containing all the typed words, those starting with the text first."""
    words = text.lower().split()
    found = [n for n in names if all(w in n.lower() for w in words)]
    found.sort(key=lambda n: not n.lower().startswith(text.strip().lower()))  # stable: stays alphabetical
    return found[:limit]


def disciplines():
    try:
        return sorted({r["discipline"] for r in json.loads(RECIPES_FILE.read_text("utf-8"))["recipes"]})
    except (OSError, ValueError, KeyError):
        return []


# ---------------------------------------------------------------- MeowDB's status

def ago(hours):
    minutes = int(hours * 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 48 * 60:
        return f"{minutes // 60}h {minutes % 60:02d}m ago"
    return f"{minutes // (24 * 60)} days ago"


def meow_status(dump):
    """(text for the status line, how many prices are due)."""
    if not market.ALLOWED:
        last = market.last_fetched(dump)
        return (f"MeowDB blocked this tool on 2026-11-10: nothing is asked, saved prices are used "
                f"(last fetched {ago(last) if last is not None else 'never'})"), 0
    if not total:
        return "MeowDB: no saved prices yet. They're looked up as items show up.", 0
    due = len(market.stale_names(dump))
    last = market.last_fetched(dump)
    when = ago(last) if last is not None else "never"
    return f"MeowDB: last price fetched {when}  |  {due} of {total} need an update (over {market.REFRESH_HOURS}h old)", due


# ---------------------------------------------------------------- running a tool

def release_keys():
    """Let go of everything a bot that was ended by force could still have held down."""
    for vk in PRESS_KEYS:
        ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


class Job:
    """One tool running as its own program, its output going to lines."""

    def __init__(self, tool, args, lines, graceful):
        self.tool, self.lines, self.graceful = tool, lines, graceful
        self.stop_at = None
        self.stop_file = Path(tempfile.gettempdir()) / f"maple_stop_{os.getpid()}_{tool}_{time.monotonic_ns()}"
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "MAPLE_STOP_FILE": str(self.stop_file)}
        python = Path(sys.executable).with_name("python.exe")  # this window runs on pythonw, which prints nowhere
        self.proc = subprocess.Popen([str(python), "-u", *args], cwd=HERE, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                     errors="replace", creationflags=CREATE_NO_WINDOW)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put((self.tool, line.rstrip()))
        self.lines.put((self.tool, None))

    def stop(self):
        """A bot finishes what it holds down and lets go by itself. The others have nothing to finish."""
        if self.stop_at is not None:
            return
        self.stop_at = time.monotonic() + STOP_WAIT
        if self.graceful:
            self.stop_file.touch()
        else:
            self.proc.terminate()

    def overdue(self):
        return self.stop_at is not None and time.monotonic() > self.stop_at and self.proc.poll() is None

    def kill(self):
        self.proc.kill()
        release_keys()

    def clean_up(self):
        self.stop_file.unlink(missing_ok=True)


# ---------------------------------------------------------------- the window

class App:
    def __init__(self, root, window="main"):
        self.root = root
        self.window = window
        title, self.tabs, self.server = WINDOWS[window]
        self.lines = queue.Queue()
        self.jobs = {}
        self.vars = {}
        self.buttons = {}  # tool -> (start, stop, status)
        self.outcome = {}  # tool -> how it last ended
        self.values = load_settings()
        self.dump = market.load()
        self.dump_read = time.monotonic()
        self.dump_time = self.file_time()
        self.progress = None
        self.closing = False
        self.items = known_items()
        self.results = []
        self.listed = []

        self.sync_now = threading.Event()   # Sync now, or something to tell the server at once
        self.sync_events = queue.Queue()     # ("started" / "ended", text) for the server's events
        self.server_cfg, self.running_now, self.last_outcome = {}, set(), ""
        self.discipline_vars, self.list_time = {}, None
        self.notify_pending = None  # the Discord toggles, changed here and not yet sent
        if self.server:  # one window talks to the server
            threading.Thread(target=self.sync_loop, daemon=True).start()

        root.title(title)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.build()
        root.update_idletasks()  # never smaller than what the tallest tab needs, as far as the screen allows
        root.minsize(root.winfo_reqwidth(), min(root.winfo_reqheight(), root.winfo_screenheight() - 80))
        self.refresh_buttons()
        self.tick_meow()
        self.drain()

    # ---- building it

    def build(self):
        self.build_meow()
        panes = ttk.PanedWindow(self.root, orient="vertical")
        panes.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        pages = {"overlay": ("Overlay", self.build_overlay), "full": ("Full sell loop", self.build_full),
                 "scout": ("Scouter", self.build_scout), "store": ("My store", self.build_store), "craft": ("Crafting", self.build_craft),
                 "custom": ("Custom list", self.build_custom), "finder": ("Equipment finder", self.build_finder),
                 "server": ("Server", self.build_server)}
        tabs = ttk.Notebook(panes) if len(self.tabs) > 1 else None
        for name in self.tabs:
            title, make = pages[name]
            page = ttk.Frame(tabs if tabs else panes, padding=12)
            page.columnconfigure(1, weight=1)
            if tabs:
                tabs.add(page, text=title)
            make(page)
        panes.add(tabs or page, weight=3)
        self.build_log(panes)

    def build_meow(self):
        bar = ttk.Frame(self.root, padding=(8, 8))
        bar.pack(fill="x")
        self.meow_text = tk.StringVar()
        ttk.Label(bar, textvariable=self.meow_text).pack(side="left")
        self.meow_button = ttk.Button(bar, command=self.meow_clicked)
        self.meow_button.pack(side="right")

    def build_log(self, panes):
        frame = ttk.Frame(panes)
        panes.add(frame, weight=2)
        self.log_box = tk.Text(frame, height=10, wrap="word", state="disabled", font=("Consolas", 9))
        scroll = ttk.Scrollbar(frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_box.pack(fill="both", expand=True)

    def var(self, key):
        value = self.values[key]
        self.vars[key] = tk.BooleanVar(value=value) if isinstance(value, bool) else tk.StringVar(value=value)
        return self.vars[key]

    def entry(self, page, row, label, key, hint=""):
        ttk.Label(page, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 10))
        line = ttk.Frame(page)
        line.grid(row=row, column=1, sticky="w")
        ttk.Entry(line, textvariable=self.var(key), width=14).pack(side="left")
        ttk.Label(line, text=hint, foreground="#666").pack(side="left", padx=(8, 0))

    def check(self, page, row, label, key):
        ttk.Checkbutton(page, text=label, variable=self.var(key)).grid(row=row, column=0, columnspan=2, sticky="w", pady=3)

    def choice(self, page, row, label, key, options):
        ttk.Label(page, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 10))
        line = ttk.Frame(page)
        line.grid(row=row, column=1, sticky="w")
        var = self.var(key)
        for value, text in options:
            ttk.Radiobutton(line, text=text, value=value, variable=var).pack(side="left", padx=(0, 12))

    def controls(self, page, row, tool, extra=None):
        line = ttk.Frame(page)
        line.grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 0))
        start = ttk.Button(line, text="Start", command=lambda: self.start(tool))
        stop = ttk.Button(line, text="Stop", command=lambda: self.stop(tool))
        status = tk.Label(line, anchor="w")
        start.pack(side="left")
        stop.pack(side="left", padx=6)
        if extra:
            extra(line).pack(side="left", padx=(0, 6))
        status.pack(side="left", padx=6)
        self.buttons[tool] = (start, stop, status)

    def note(self, page, row, text, color="#666"):
        tk.Label(page, text=text, fg=color, justify="left", anchor="w", wraplength=660).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6))

    def build_overlay(self, page):
        self.note(page, 0, "Boxes deals over the game while you browse shops. It doesn't touch the game. F8 pauses it.")
        self.check(page, 1, "Compare with Free Market prices", "ov_market")
        self.entry(page, 2, "Market deal", "ov_percent", "% or more under the 24h median")
        self.entry(page, 3, "Minimum NPC profit", "ov_min_profit", "mesos per item")
        self.entry(page, 4, "Scan every", "ov_interval", "seconds")
        self.check(page, 5, "Check hovered equipment's scroll slots (experimental)", "ov_tooltips")
        self.check(page, 6, "Save debug screenshots", "ov_debug")
        self.controls(page, 7, "overlay")

    def build_full(self, page):
        self.note(page, 0, "SELLS EVERYTHING ON THE EQUIP, USE AND ETC TABS at the NPC each trip, except what's on the custom list. "
                           "Move or list what you want to keep first.", "#c0392b")
        self.note(page, 1, "Every channel of each room, going to sell when the money runs low. Buys NPC deals without asking. "
                           "Stand in a Free Market room with the minimap showing the map's name. With the Server tab on, it "
                           "also goes for the stores a scouter sends it, by these same rules.")
        self.entry(page, 2, "Budget", "fl_budget", "like 50000, 250k or 1.5m")
        self.entry(page, 3, "Channels", "fl_channels", "only these, in order: 40-1, or 1,3,20,39")
        self.entry(page, 4, "Sell when under", "fl_sell_below", "% of the budget left")
        self.entry(page, 10, "Rooms", "fl_rooms", "each once, in this order, then it sells and ends: all are " + ALL_ROOMS + ", or 3,10,15")
        self.choice(page, 5, "Custom deals", "fl_custom", (("off", "Ignore them"), ("limit", "Buy up to each item's limit"),
                                                           ("profits", "Split the budget, no limit")))
        self.entry(page, 6, "NPC deals get", "fl_npc_money", "of the budget, and no more; the rest is for store and split custom deals (empty: all)")
        self.entry(page, 8, "Time limit", "fl_minutes", "minutes (empty: none)")
        self.check(page, 9, "Wait for the scouters' finds: stay put, crawl nothing, and only move to buy what they find "
                            "(by the rules and money above; Rooms and Channels aren't used)", "fl_wait")
        self.check(page, 12, "Store deals: also buy market deals from profits, to sell in my store (ends at 10 items)", "fl_store")
        self.entry(page, 13, "Store deal", "fl_store_percent", "% or more under the price the store would list it at")
        self.controls(page, 14, "full", lambda parent: ttk.Button(parent, text="Reset where it stopped", command=self.reset_loop))
        self.note(page, 15, "Stopped (Esc held, Stop, the time limit), it goes on next time from the room and channel it was on, with "
                            "the money it had. Reset starts the next run fresh, with the budget above, and asks which items held "
                            "for your store to carry over.")

    def build_scout(self, page):
        self.note(page, 0, "Goes round the rooms and channels below for as long as it runs, buying and selling nothing, and sends "
                           "every store's listings to the buyer bots through the server. Each buyer judges them by its own rules "
                           "and money. Join the server on the Server tab first. Stand in a Free Market room with the minimap "
                           "showing the map's name.")
        self.entry(page, 1, "Rooms", "sc_rooms", "in this order, then round again: all are " + ALL_ROOMS + ", or 3,10,15")
        self.entry(page, 2, "Channels", "sc_channels", "in each room, in order: 40-1, or 1,3,20,39")
        self.entry(page, 3, "Send stores to", "sc_send_to", "buyer bots' names on the server, like PC1, ScrollBot: a copy each "
                                                            "(empty: whichever bot takes it first)")
        self.entry(page, 4, "Time limit", "sc_minutes", "minutes (empty: until stopped)")
        self.controls(page, 5, "scout", lambda parent: ttk.Button(parent, text="Reset where it stopped", command=self.reset_scout))
        self.note(page, 6, "Stopped, it goes on next time from the room and channel it was on. It keeps its own place, apart "
                           "from the full sell loop's.")

    def build_store(self, page):
        self.note(page, 0, "Lists what's on the custom list and in your inventory in your own store, priced from MeowDB and the "
                           "bot's scans, and opens it. Find a spot and set the store up yourself first, with your inventory "
                           "window showing beside it. It places everything at its own prices, then asks before it opens the store.")
        self.note(page, 1, "Never listed: Only 1 and equipment finder items, equipment, and anything not on the custom list.")
        self.check(page, 2, "Dry run: read and price only, place nothing", "st_dry")
        self.check(page, 3, "Set the store up first: find a free spot on this floor and use the store permit", "st_place")
        self.entry(page, 4, "Store name", "st_name", "the name it's set up with")
        self.controls(page, 5, "store", lambda parent: ttk.Button(parent, text="Forget store deals held", command=self.forget_holdings))

    def build_server(self, page):
        self.note(page, 0, "Shares this bot's store scans with your other bots, and theirs with it, through your server "
                           "(see server/README.md): prices are then worked out from all of them. Also what each bot is doing. "
                           "Join once with the server's address and its admin key; the admin key isn't kept.")
        self.entry(page, 1, "Server address", "sv_url", "like https://maple-server.up.railway.app")
        self.entry(page, 2, "This bot's name", "sv_name", "how the other bots and Discord see it")
        line = ttk.Frame(page)
        line.grid(row=3, column=1, sticky="w", pady=3)
        ttk.Button(line, text="Join the server", command=self.join_server).pack(side="left")
        ttk.Button(line, text="Sync now", command=self.sync_now.set).pack(side="left", padx=6)
        self.server_state = tk.StringVar()
        tk.Label(line, textvariable=self.server_state, fg="#666").pack(side="left", padx=6)
        self.check(page, 4, "Share scans and status with the server (every minute, in the background)", "sv_on")
        self.var("sv_key")  # this bot's key from joining: kept with the settings, not shown
        self.show_server_state()
        box = ttk.LabelFrame(page, text="Discord notifications (for every bot: the server's, changed from any launcher)", padding=8)
        box.grid(row=5, column=0, columnspan=2, sticky="we", pady=(12, 0))
        for i, (kind, label) in enumerate(DISCORD_NOTIFY.items()):
            ttk.Checkbutton(box, text=label, variable=self.var(f"dn_{kind}"), command=self.notify_changed).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, 16), pady=1)
        self.discord_state = tk.StringVar(value="Discord: not checked yet")
        tk.Label(box, textvariable=self.discord_state, fg="#666").grid(row=(len(DISCORD_NOTIFY) + 1) // 2, column=0, columnspan=2,
                                                                       sticky="w", pady=(6, 0))

    def notify_changed(self):
        """A toggle clicked: the server gets all of them at the next sync, and they hold for every bot."""
        self.notify_pending = {k: self.vars[f"dn_{k}"].get() for k in DISCORD_NOTIFY}
        self.sync_now.set()

    def show_notify(self, settings):
        """The server's toggles, shown (from the sync thread, through the log queue)."""
        for kind, on in (settings.get("notify") or {}).items():
            if f"dn_{kind}" in self.vars and self.notify_pending is None:
                self.vars[f"dn_{kind}"].set(bool(on))
        self.discord_state.set("Discord: set up on the server" if settings.get("on") else
                               "Discord: not set up on the server yet (see server/README.md)")

    def show_server_state(self):
        joined = bool(self.vars["sv_key"].get())
        self.server_state.set("joined" if joined else "not joined yet")

    def join_server(self):
        url, name = self.vars["sv_url"].get().strip(), self.vars["sv_name"].get().strip()
        if not url.startswith(("http://", "https://")) or not name:
            messagebox.showerror("Server", "Type the server's address (starting with https://) and a name for this bot first.")
            return
        admin = simpledialog.askstring("Server", "The server's admin key (ADMIN_KEY in Railway). It's only used to join, not kept:",
                                       show="*", parent=self.root)
        if not admin:
            return
        try:
            key = sync.join(url, admin.strip(), name)
        except sync.SyncError as e:
            messagebox.showerror("Server", f"Couldn't join: {e}")
            return
        self.vars["sv_key"].set(key)
        self.vars["sv_on"].set(True)
        self.remember()
        self.show_server_state()
        self.log("server", f"joined {url} as {name}: sharing is on")
        self.sync_now.set()

    def sync_loop(self):
        """The server, every SYNC_EVERY seconds (or at once on Sync now), on its own thread: it never
        holds up the window or a bot. Its lines go to the log through the same queue as the tools'."""
        last_files, said = 0.0, ""
        while True:
            self.sync_now.wait(SYNC_EVERY)
            self.sync_now.clear()
            cfg, running = dict(self.server_cfg), set(self.running_now)
            if not (cfg.get("on") and cfg.get("url") and cfg.get("key")):
                continue
            client = sync.Client(cfg["url"], cfg["key"])
            try:
                while not self.sync_events.empty():
                    sync.event(client, *self.sync_events.get_nowait())
                pending, self.notify_pending = self.notify_pending, None
                settings = sync.discord_settings(client, pending)
                if pending is not None:
                    self.lines.put(("server", "Discord notifications on: " + (", ".join(k for k, on in pending.items() if on) or "none")))
                self.lines.put(("__discord__", settings))
                files = time.monotonic() - last_files > SYNC_FILES_EVERY
                summary = sync.run_once(client, running=running, last_outcome=cfg.get("last", ""), files=files)
                if files:
                    last_files = time.monotonic()
                text = f"synced: {summary}"
                if not summary.startswith("0 up, 0 down") or said.startswith("can't"):
                    self.lines.put(("server", text))
                said = text
            except (sync.SyncError, OSError, ValueError) as e:  # the bots carry on regardless: tried again next round
                text = f"can't sync: {e}"
                if text != said:
                    self.lines.put(("server", text))
                said = text

    def build_craft(self, page):
        self.note(page, 0, "Works out what's profitable to craft, from saved prices, and opens the results in your browser.")
        ttk.Label(page, text="Disciplines").grid(row=1, column=0, sticky="nw", pady=3, padx=(0, 10))
        line = ttk.Frame(page)
        line.grid(row=1, column=1, sticky="w")
        self.discipline_vars = {}
        for i, name in enumerate(disciplines()):
            var = tk.BooleanVar(value=name in self.values["cf_disciplines"])
            self.discipline_vars[name] = var
            ttk.Checkbutton(line, text=name, variable=var).grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 12))
        self.entry(page, 2, "Margin", "cf_margin", "% of what a craft sells for")
        self.controls(page, 3, "craft", lambda parent: ttk.Button(parent, text="Open last results", command=self.open_page))

    def build_custom(self, page):
        page.columnconfigure(0, weight=1)
        page.columnconfigure(1, weight=2)
        page.rowconfigure(1, weight=1)
        self.note(page, 0, "Everything on this list gets a cyan box and is never sold at the NPC. The bots buy by these rules "
                           "when you tick Buy custom list items on their tab. Changes apply the next time you press Start.")
        left = ttk.Frame(page)
        right = ttk.Frame(page)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        right.grid(row=1, column=1, sticky="nsew")
        ttk.Label(left, text="Find an item").pack(anchor="w")
        self.search_text = tk.StringVar()
        self.search_text.trace_add("write", lambda *_: self.show_results())
        search = ttk.Entry(left, textvariable=self.search_text)
        search.pack(fill="x", pady=(2, 4))
        search.bind("<Return>", lambda _: self.add_selected())
        self.result_box = tk.Listbox(left, height=6, exportselection=False)
        self.result_box.pack(fill="both", expand=True)
        self.result_box.bind("<Double-Button-1>", lambda _: self.add_selected())
        ttk.Button(left, text="Add to list", command=self.add_selected).pack(anchor="e", pady=(4, 0))

        scale = self.root.winfo_fpixels("1i") / 96  # column sizes and row height are in pixels
        ttk.Style().configure("Treeview", rowheight=int(20 * scale))
        columns = {"name": ("Item", 120), "when": ("Buy when", 135), "bought": ("Bought", 55), "ask": ("Purchase", 60)}
        self.list_box = ttk.Treeview(right, columns=list(columns), show="headings", height=6, selectmode="browse")
        for column, (title, width) in columns.items():
            self.list_box.heading(column, text=title)
            self.list_box.column(column, width=int(width * scale), stretch=column == "name")
        self.list_box.pack(fill="both", expand=True)
        self.list_box.bind("<<TreeviewSelect>>", lambda _: self.show_item())
        self.list_box.bind("<Delete>", lambda _: self.remove_selected())

        form = ttk.Frame(right)
        form.pack(fill="x", pady=(6, 0))
        self.item_title = tk.StringVar(value="Pick an item above to set its rules")
        ttk.Label(form, textvariable=self.item_title).grid(row=0, column=0, columnspan=4, sticky="w")
        self.item_when = tk.StringVar(value="under")
        self.item_percent, self.item_price, self.item_limit = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.item_confirm = tk.BooleanVar(value=True)
        ttk.Radiobutton(form, text="Under median by", value="under", variable=self.item_when).grid(row=1, column=0, sticky="w")
        ttk.Radiobutton(form, text="Up to over median by", value="over", variable=self.item_when).grid(row=2, column=0, sticky="w")
        ttk.Radiobutton(form, text="At or under a price of", value="price", variable=self.item_when).grid(row=3, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.item_percent, width=8).grid(row=1, column=1, rowspan=2, sticky="w", padx=4)
        ttk.Label(form, text="%").grid(row=1, column=2, rowspan=2, sticky="w")
        ttk.Entry(form, textvariable=self.item_price, width=10).grid(row=3, column=1, sticky="w", padx=4)
        hp_rate, mp_rate = custom_items.ratio()
        ttk.Radiobutton(form, text="By HP/MP value: one restores", value="value", variable=self.item_when).grid(row=4, column=0, sticky="w")
        self.item_hp, self.item_mp = tk.StringVar(), tk.StringVar()
        restores = ttk.Frame(form)
        restores.grid(row=4, column=1, columnspan=3, sticky="w", padx=4)
        ttk.Entry(restores, textvariable=self.item_hp, width=6).pack(side="left")
        ttk.Label(restores, text="HP").pack(side="left", padx=(2, 8))
        ttk.Entry(restores, textvariable=self.item_mp, width=6).pack(side="left")
        ttk.Label(restores, text="MP").pack(side="left", padx=2)
        ttk.Label(form, text=f"pays up to 1 meso per {hp_rate:g} HP and per {mp_rate:g} MP (hp_per_meso / mp_per_meso in crawler_settings.json)",
                  foreground="#666").grid(row=5, column=0, columnspan=4, sticky="w")
        ttk.Label(form, text="Buy limit").grid(row=6, column=0, sticky="w", pady=(4, 0))
        self.limit_entry = ttk.Entry(form, textvariable=self.item_limit, width=8)
        self.limit_entry.grid(row=6, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(form, text="empty: no limit", foreground="#666").grid(row=6, column=2, columnspan=2, sticky="w", pady=(4, 0))
        self.item_only_one = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Only 1: never buy more than one, in any mode (an upgrade, not for merching)",
                        variable=self.item_only_one, command=self.only_one_changed).grid(row=7, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.item_store = tk.BooleanVar(value=False)
        self.store_box = ttk.Checkbutton(form, text="Sell it in my store (My store tab); otherwise it's only held", variable=self.item_store)
        self.store_box.grid(row=8, column=0, columnspan=4, sticky="w")
        ttk.Radiobutton(form, text="Wait for my OK", value=True, variable=self.item_confirm).grid(row=9, column=0, sticky="w", pady=(4, 0))
        ttk.Radiobutton(form, text="Buy without asking", value=False, variable=self.item_confirm).grid(row=9, column=1, columnspan=3, sticky="w", pady=(4, 0))
        buttons = ttk.Frame(right)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Save item", command=self.save_item).pack(side="left")
        ttk.Button(buttons, text="Reset bought count", command=self.reset_item).pack(side="left", padx=6)
        ttk.Button(buttons, text="Remove", command=self.remove_selected).pack(side="right")
        self.list_time = None
        self.show_results()
        self.show_list()

    # ---- the equipment finder

    def build_finder(self, page):
        page.columnconfigure(0, weight=1)
        page.columnconfigure(1, weight=2)
        page.rowconfigure(1, weight=1)
        self.note(page, 0, "Finds one piece of equipment with the stats you want: it checks the tooltip before buying, and only buys when it's sure. "
                           "Always 1 item, never sold at the NPC. Buys when Custom deals on the bot's tab allows it.")
        left = ttk.Frame(page)
        right = ttk.Frame(page)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        right.grid(row=1, column=1, sticky="nsew")
        self.equipment = custom_items.load_equipment()
        ttk.Label(left, text="Find equipment").pack(anchor="w")
        self.equip_search = tk.StringVar()
        self.equip_search.trace_add("write", lambda *_: self.show_equipment())
        ttk.Entry(left, textvariable=self.equip_search).pack(fill="x", pady=(2, 4))
        self.equip_box = tk.Listbox(left, height=8, exportselection=False)
        self.equip_box.pack(fill="both", expand=True)
        self.equip_box.bind("<<ListboxSelect>>", lambda _: self.pick_equipment())
        self.equip_get = ttk.Button(left, text="Get the equipment list from MeowDB", command=self.fetch_equipment)
        self.equip_title = tk.StringVar(value="Pick a piece of equipment")
        ttk.Label(right, textvariable=self.equip_title).pack(anchor="w")
        ttk.Label(right, text="The least to buy at; empty boxes aren't checked. Ranges are estimates.", foreground="#666").pack(anchor="w")
        # room for the most an item has, as the window doesn't grow to fit what's put in it later
        self.equip_stats = ttk.Frame(right, height=int(EQUIP_ROWS * 22 * self.root.winfo_fpixels("1i") / 96))
        self.equip_stats.grid_propagate(False)
        self.equip_stats.pack(fill="x", pady=(4, 0))
        self.equip_vars, self.equip_slots, self.equip_name = {}, tk.StringVar(), None
        form = ttk.Frame(right)
        form.pack(fill="x", pady=(6, 0))
        self.equip_when = tk.StringVar(value="price")
        self.equip_percent, self.equip_price = tk.StringVar(value="20"), tk.StringVar()
        self.equip_confirm = tk.BooleanVar(value=True)
        ttk.Radiobutton(form, text="Under median by", value="under", variable=self.equip_when).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(form, text="Up to over median by", value="over", variable=self.equip_when).grid(row=1, column=0, sticky="w")
        ttk.Radiobutton(form, text="At or under a price of", value="price", variable=self.equip_when).grid(row=2, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.equip_percent, width=8).grid(row=0, column=1, rowspan=2, sticky="w", padx=4)
        ttk.Label(form, text="%").grid(row=0, column=2, rowspan=2, sticky="w")
        ttk.Entry(form, textvariable=self.equip_price, width=10).grid(row=2, column=1, sticky="w", padx=4)
        ttk.Radiobutton(form, text="Wait for my OK", value=True, variable=self.equip_confirm).grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Radiobutton(form, text="Buy without asking", value=False, variable=self.equip_confirm).grid(row=3, column=1, columnspan=3, sticky="w", pady=(4, 0))
        buttons = ttk.Frame(right)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Save item", command=self.save_equipment).pack(side="left")
        ttk.Button(buttons, text="Reset bought count", command=self.reset_equipment).pack(side="left", padx=6)
        ttk.Button(buttons, text="Remove", command=self.remove_equipment).pack(side="right")
        below = ttk.Frame(page)
        below.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        scale = self.root.winfo_fpixels("1i") / 96
        columns = {"name": ("Item", 150), "stats": ("Has at least", 230), "price": ("Buy when", 150), "bought": ("Bought", 60), "ask": ("Purchase", 65)}
        self.finder_box = ttk.Treeview(below, columns=list(columns), show="headings", height=5, selectmode="browse")
        for column, (title, width) in columns.items():
            self.finder_box.heading(column, text=title)
            self.finder_box.column(column, width=int(width * scale), stretch=column in ("name", "stats"))
        self.finder_box.pack(fill="both", expand=True)
        self.finder_box.bind("<<TreeviewSelect>>", lambda _: self.pick_finder_row())
        self.finder_box.bind("<Delete>", lambda _: self.remove_equipment())
        self.show_equipment()
        self.show_finder_list()

    def show_finder_list(self, select=None):
        """The equipment finder's items as they are on disk, which a bot may be counting in."""
        self.finder_listed = [i for i in custom_items.load() if i.get("finder")]
        chosen = select or self.finder_chosen()
        self.finder_box.delete(*self.finder_box.get_children())
        for number, item in enumerate(self.finder_listed):
            self.finder_box.insert("", "end", iid=str(number), values=(item["name"], custom_items.describe_finder(item["finder"]), custom_items.describe_price(item),
                                                                       f"{item['bought']} / 1", "asks me" if item["confirm"] else "auto"))
            if chosen and custom_items.key(chosen) == custom_items.key(item["name"]):
                self.finder_box.selection_set(str(number))

    def finder_chosen(self):
        chosen = self.finder_box.selection()
        return self.finder_listed[int(chosen[0])]["name"] if chosen and int(chosen[0]) < len(self.finder_listed) else None

    def pick_finder_row(self):
        """Its saved values in the form, unless it's already there: the list refreshes while you type."""
        name = self.finder_chosen()
        if not name or name == self.equip_name:
            return
        if name in self.equipment:
            self.show_equipment_form(name)
        else:
            self.equip_name = name
            self.equip_title.set(f"{name}: not in the equipment list, so it can't be edited here, only removed")
            for widget in self.equip_stats.winfo_children():
                widget.destroy()
            self.equip_vars = {}

    def reset_equipment(self):
        name = self.finder_chosen()
        if name:
            custom_items.update(name, bought=0)
            self.log("list", f"bought count of {name} reset")
            self.show_finder_list(select=name)

    def show_equipment(self):
        self.equip_results = search_items(list(self.equipment), self.equip_search.get())
        self.equip_box.delete(0, "end")
        self.equip_box.insert("end", *self.equip_results)
        if self.equipment:
            self.equip_get.pack_forget()
        else:
            self.equip_get.pack(anchor="e", pady=(4, 0))

    def fetch_equipment(self):
        self.equip_get.configure(state="disabled")
        self.log("list", "getting the equipment list from MeowDB...")
        result = queue.Queue()

        def work():
            try:
                result.put(f"{custom_items.fetch_equipment()} pieces of equipment")
            except (OSError, ValueError) as e:
                result.put(f"couldn't get the equipment list: {e}")

        def check():
            if result.empty():
                self.root.after(300, check)
                return
            self.log("list", result.get())
            self.equip_get.configure(state="normal")
            self.equipment = custom_items.load_equipment()
            self.show_equipment()

        threading.Thread(target=work, daemon=True).start()
        check()

    def pick_equipment(self):
        chosen = self.equip_box.curselection()
        if chosen:
            self.show_equipment_form(self.equip_results[chosen[0]])

    def show_equipment_form(self, name):
        """A box for each stat the item has, its usual range next to it, and what's saved of it."""
        info = self.equipment[name]
        self.equip_name = name
        saved = custom_items.find(custom_items.load(), name)
        finder = (saved or {}).get("finder") or {}
        job = f", {info['job']}" if info.get("job") else ""
        self.equip_title.set(f"{name} ({info['subtype']}, level {info['level']}{job}): at least...")
        for widget in self.equip_stats.winfo_children():
            widget.destroy()
        self.equip_vars = {}

        def stat_row(number, name, var, hint):
            row, column = number % EQUIP_ROWS, 3 * (number // EQUIP_ROWS)
            ttk.Label(self.equip_stats, text=name).grid(row=row, column=column, sticky="w", padx=(0 if column == 0 else 12, 0))
            ttk.Entry(self.equip_stats, textvariable=var, width=5).grid(row=row, column=column + 1, sticky="w", padx=3)
            ttk.Label(self.equip_stats, text=hint, foreground="#666").grid(row=row, column=column + 2, sticky="w")

        least = finder.get("slots")
        self.equip_slots.set("" if least is None else str(least))
        stat_row(0, "Slots left", self.equip_slots, f"0-{info['slots']}")
        for number, (key, usual) in enumerate(info["stats"].items(), 1):
            low, high = custom_items.stat_range(usual)
            self.equip_vars[key] = tk.StringVar(value=str((finder.get("stats") or {}).get(key, "")))
            stat_row(number, custom_items.STAT_NAMES[key], self.equip_vars[key], f"{low}-{high}")
        if saved and finder:
            self.equip_when.set(saved["when"])
            self.equip_percent.set(f"{saved['percent']:g}")
            self.equip_price.set(str(saved["price"]) if saved["price"] else "")
            self.equip_confirm.set(saved["confirm"])

    def save_equipment(self):
        name = self.equip_name
        if not name:
            return
        try:
            finder = custom_items.parse_finder({k: v.get() for k, v in self.equip_vars.items()}, self.equip_slots.get())
            rules = custom_items.parse_rules(self.equip_when.get(), self.equip_percent.get(), self.equip_price.get(), "1")
        except ValueError as e:
            messagebox.showerror("Equipment finder", str(e))
            return
        result = custom_items.save_finder(name, finder, self.equip_confirm.get(), rules)
        if result == "taken":
            messagebox.showerror("Equipment finder", f"{name} is already on the Custom list tab. There's only ever one of an item: remove it there first.")
            return
        self.log("list", f"{result} {name}: {custom_items.describe_finder(finder)}, 1 only")
        self.list_time = self.list_file_time()
        self.show_finder_list(select=name)

    def remove_equipment(self):
        name = self.finder_chosen() or self.equip_name
        saved = name and custom_items.find(custom_items.load(), name)
        if not saved or not saved.get("finder"):
            return
        custom_items.remove(name)
        self.log("list", f"removed {name}")
        self.list_time = self.list_file_time()
        self.finder_box.selection_set(())
        self.show_finder_list()
        if name in self.equipment:
            self.show_equipment_form(name)

    # ---- the custom list

    def show_results(self):
        self.results = search_items(self.items, self.search_text.get())
        self.result_box.delete(0, "end")
        self.result_box.insert("end", *self.results)
        if self.results:
            self.result_box.selection_set(0)

    def show_list(self, select=None):
        """The list as it is on disk, which a bot may be counting in."""
        known = {custom_items.key(n) for n in self.items}
        chosen = select or self.chosen_name()
        self.listed = [i for i in custom_items.load() if not i.get("finder")]  # those have their own tab
        self.list_time = self.list_file_time()
        if hasattr(self, "finder_box"):
            self.show_finder_list()
        self.list_box.delete(*self.list_box.get_children())
        for number, item in enumerate(self.listed):
            name = item["name"] if custom_items.key(item["name"]) in known else f"{item['name']} (not a known item)"
            bought = f"{item['bought']} / only 1" if item["only_one"] else f"{item['bought']} / {item['limit']}" if item["limit"] else f"{item['bought']}"
            self.list_box.insert("", "end", iid=str(number), values=(name, custom_items.describe(item), bought,
                                                                    "asks me" if item["confirm"] else "auto"))
            if chosen and custom_items.key(chosen) == custom_items.key(item["name"]):
                self.list_box.selection_set(str(number))

    def list_file_time(self):
        try:
            return custom_items.FILE.stat().st_mtime
        except OSError:
            return None

    def chosen_name(self):
        chosen = self.list_box.selection()
        return self.listed[int(chosen[0])]["name"] if chosen and int(chosen[0]) < len(self.listed) else None

    def show_item(self):
        name = self.chosen_name()
        item = name and custom_items.find(self.listed, name)
        if not item:
            return
        self.item_title.set(f"{item['name']}: buy it when...")
        self.item_when.set(item["when"])
        self.item_percent.set(f"{item['percent']:g}")
        self.item_price.set(str(item["price"]) if item["price"] else "")
        hp, mp = (item.get("hp"), item.get("mp")) if item.get("hp") or item.get("mp") else (custom_items.known_restore(item["name"]) or (0, 0))
        self.item_hp.set(str(hp) if hp else "")
        self.item_mp.set(str(mp) if mp else "")
        self.item_limit.set("" if item["limit"] is None else str(item["limit"]))
        self.item_confirm.set(item["confirm"])
        self.item_only_one.set(item["only_one"])
        self.item_store.set(item["store"])
        self.only_one_changed()

    def only_one_changed(self):
        if self.item_only_one.get():
            self.item_limit.set("1")
        self.limit_entry.configure(state="disabled" if self.item_only_one.get() else "normal")
        if self.item_only_one.get():  # an upgrade to wear: never sold
            self.item_store.set(False)
        self.store_box.configure(state="disabled" if self.item_only_one.get() else "normal")

    def save_item(self):
        name = self.chosen_name()
        if not name:
            messagebox.showinfo("Custom list", "Pick the item in the list above first. If it isn't there yet, search for it on the "
                                               "left and add it, then pick it, set its rules and save.")
            return
        try:
            changes = custom_items.parse_rules(self.item_when.get(), self.item_percent.get(), self.item_price.get(), self.item_limit.get(),
                                               self.item_hp.get(), self.item_mp.get())
        except ValueError as e:
            messagebox.showerror("Custom list", str(e))
            return
        custom_items.update(name, confirm=self.item_confirm.get(), only_one=self.item_only_one.get(),
                            store=self.item_store.get() and not self.item_only_one.get(), **changes)
        self.log("list", f"saved {name}")
        self.show_list(select=name)

    def reset_item(self):
        name = self.chosen_name()
        if name:
            custom_items.update(name, bought=0)
            self.log("list", f"bought count of {name} reset")
            self.show_list(select=name)

    def add_selected(self):
        chosen = self.result_box.curselection()
        if not chosen:
            return
        name = self.results[chosen[0]]
        self.log("list", f"added {name}: starts as 1 item, 20% under median, waiting for your OK" if custom_items.add(name)
                 else f"{name} is already on the list, here or on the Equipment finder tab")
        self.show_list(select=name)
        self.show_item()

    def remove_selected(self):
        name = self.chosen_name()
        if name:
            custom_items.remove(name)
            self.log("list", f"removed {name}")
            self.item_title.set("Pick an item above to set its rules")
            self.show_list()

    # ---- settings

    def current(self):
        """This window's settings, and the ones it starts tools with: the other windows' as last saved."""
        values = {key: var.get() for key, var in self.vars.items()}
        if self.discipline_vars:
            values["cf_disciplines"] = [n for n, var in self.discipline_vars.items() if var.get()] or self.values["cf_disciplines"]
        return values

    def everything(self):
        return {**load_settings(), **self.current()}

    def remember(self):
        try:
            save_settings(self.current())
        except OSError as e:
            self.log("launcher", f"couldn't save the settings: {e}")

    # ---- starting and stopping

    def start(self, tool):
        values = self.everything()
        if tool == "craft":
            values["cf_disciplines"] = [n for n, var in self.discipline_vars.items() if var.get()]
        try:
            args = ARGS[tool](values)
        except ValueError as e:
            messagebox.showerror(TOOLS[tool], str(e))
            return
        other = game_taken() if tool in ON_GAME else None
        if other:
            messagebox.showerror(TOOLS[tool], f"{other} is running in another window: one tool on the game at a time. Stop it there first.")
            return
        self.remember()
        try:
            self.jobs[tool] = Job(tool, args, self.lines, graceful=tool in GRACEFUL)
        except OSError as e:
            messagebox.showerror(TOOLS[tool], f"Couldn't start it: {e}")
            return
        if tool in ON_GAME:
            take_game(tool)
        self.outcome.pop(tool, None)
        self.log(tool, "started: " + " ".join(args))
        if self.server:
            self.sync_events.put(("started", TOOLS[tool]))
        self.refresh_buttons()

    def stop(self, tool):
        job = self.jobs.get(tool)
        if job:
            self.log(tool, "stopping...")
            job.stop()
            self.refresh_buttons()

    def ended(self, tool):
        job = self.jobs.pop(tool)
        code = job.proc.wait()
        job.clean_up()
        if not any(t in ON_GAME for t in self.jobs):
            free_game()
        if job.stop_at is not None or code == 0:
            self.outcome[tool] = ("stopped" if job.stop_at is not None else "finished", "#666")
        else:
            self.outcome[tool] = (f"ended with an error ({code}): see the log", "#c0392b")
        self.log(tool, self.outcome[tool][0])
        if self.server:
            self.sync_events.put(("ended", f"{TOOLS[tool]}: {self.outcome[tool][0]}"))
        self.last_outcome = f"{TOOLS[tool]} {self.outcome[tool][0]}"
        if tool == "update":
            self.progress = None
            self.read_dump(force=True)
            self.tick_status()
        self.refresh_buttons()
        if self.closing and not self.jobs:
            self.root.destroy()

    def refresh_buttons(self):
        game_job = next((t for t in ON_GAME if t in self.jobs), None)
        for tool, (start, stop, status) in self.buttons.items():
            job = self.jobs.get(tool)
            blocked = tool in ON_GAME and game_job and game_job != tool
            start.state(["disabled"] if job or blocked else ["!disabled"])
            stop.state(["!disabled"] if job else ["disabled"])
            if job:
                text, color = ("stopping...", "#a86a00") if job.stop_at is not None else ("running", "#11804a")
            elif blocked:
                text, color = f"{TOOLS[game_job]} is running: one at a time", "#666"
            else:
                text, color = self.outcome.get(tool, ("not running", "#666"))
            status.configure(text=text, fg=color)

    def reset_loop(self):
        state = HERE / "data" / "loop_state.json"
        held = holdings.load()
        if not state.exists() and not held:
            messagebox.showinfo(TOOLS["full"], "Nothing to reset: the next run starts fresh.")
            return
        if not messagebox.askyesno(TOOLS["full"], "Forget where the last run stopped, and its money? The next run starts fresh, with the "
                                                  "budget on this tab."):
            return
        if held:
            carried = self.ask_carry_over(held)
            if carried is None:  # closed without choosing: nothing changes
                return
            holdings.save(carried)
            kept = ", ".join(f"{h['quantity']:,} {n}" for n, h in carried.items()) or "nothing"
            self.log("full", f"carried over: {kept}"
                             + (f"; forgot {', '.join(sorted(set(held) - set(carried)))}" if set(held) - set(carried) else ""))
        state.unlink(missing_ok=True)
        self.log("full", "reset: the next run starts fresh")

    def reset_scout(self):
        if not SCOUT_STATE.exists():
            messagebox.showinfo(TOOLS["scout"], "Nothing to reset: the next run starts from the first room.")
            return
        if messagebox.askyesno(TOOLS["scout"], "Forget where the scouter stopped? The next run starts from the first room on this tab."):
            SCOUT_STATE.unlink(missing_ok=True)
            self.log("scout", "reset: the next run starts from the first room")

    def ask_carry_over(self, held):
        """Which store deals held go on into the next run, and how many of each: {name: held} as
        holdings keeps them (the cost scaled to the amount kept), or None if the window was closed."""
        win = tk.Toplevel(self.root)
        win.title("Carry over items")
        win.transient(self.root)
        tk.Label(win, text="Items held for your store from the last run. Tick the ones to carry over, and how many: "
                           "the others are forgotten, and sold at the NPC on the next sell trip if they're still in your inventory.",
                 wraplength=460, justify="left").grid(row=0, column=0, columnspan=4, sticky="w", padx=10, pady=(10, 6))
        rows = {}
        yours = bot_settings().get("store_deals", {}).get("stack_sizes", {})  # as the crawler counts a stack
        for i, (name, h) in enumerate(sorted(held.items()), 1):
            keep = tk.BooleanVar(value=True)
            amount = tk.StringVar(value=str(min(h["quantity"], yours.get(name) or holdings.stack_size(name))))
            ttk.Checkbutton(win, text=name, variable=keep).grid(row=i, column=0, sticky="w", padx=10)
            ttk.Entry(win, textvariable=amount, width=7).grid(row=i, column=1, sticky="w")
            tk.Label(win, text=f"of {h['quantity']:,} held", fg="#666").grid(row=i, column=2, sticky="w", padx=6)
            rows[name] = (keep, amount)
        full = tk.BooleanVar(value=True)
        end = len(rows) + 1
        ttk.Checkbutton(win, text="Count the items carried over as full store slots", variable=full).grid(
            row=end, column=0, columnspan=4, sticky="w", padx=10, pady=(8, 0))
        result = {}

        def done():
            out = {}
            for name, (keep, amount) in rows.items():
                if not keep.get():
                    continue
                try:
                    count = int(amount.get().strip())
                except ValueError:
                    count = 0
                if count <= 0:
                    messagebox.showerror("Carry over items", f"{name}: type how many to carry over (a whole number over 0), "
                                                             "or untick it.", parent=win)
                    return
                h = held[name]
                out[name] = {**h, "quantity": count, "paid": round(h["paid"] * count / h["quantity"]) if h["quantity"] else 0}
                out[name].pop("full", None)
                if full.get():
                    out[name]["full"] = True
            result["items"] = out
            win.destroy()

        buttons = ttk.Frame(win)
        buttons.grid(row=end + 1, column=0, columnspan=4, sticky="e", padx=10, pady=10)
        ttk.Button(buttons, text="Carry over and reset", command=done).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="left", padx=(6, 0))
        self.carry_window = win  # for the tests
        win.grab_set()
        self.root.wait_window(win)
        return result.get("items")

    def forget_holdings(self):
        held = holdings.load()
        if not held:
            messagebox.showinfo(TOOLS["store"], "No store deals are held.")
            return
        if messagebox.askyesno(TOOLS["store"], f"Forget the {len(held)} store deals held ({', '.join(sorted(held))})? Do this once they've "
                                               "sold. Any still in your inventory get sold at the NPC on the next sell trip."):
            holdings.save({})
            self.log("store", f"forgot {len(held)} store deals held")

    def open_page(self):
        if CRAFT_PAGE.exists():
            webbrowser.open(CRAFT_PAGE.as_uri())
        else:
            messagebox.showinfo(TOOLS["craft"], "No results yet: press Start first.")

    # ---- MeowDB

    def file_time(self):
        try:
            return market.DUMP_FILE.stat().st_mtime
        except OSError:
            return None

    def read_dump(self, force=False):
        """The saved prices again if they've changed, every few seconds at most: a tool saving
        them can't while they're being read."""
        if not force and time.monotonic() - self.dump_read < MEOW_READ_EVERY:
            return
        self.dump_read = time.monotonic()
        changed = self.file_time()
        if changed != self.dump_time:
            dump = market.load()
            if dump is not None:
                self.dump, self.dump_time = dump, changed

    def meow_clicked(self):
        if "update" in self.jobs:
            self.stop("update")
        else:
            self.start("update")

    def tick_status(self):
        text, due = meow_status(self.dump)
        job = self.jobs.get("update")
        if job and self.progress:
            done, total = self.progress
            text = f"MeowDB: updating, {done} of {total} done (one a second)"
        elif job:
            text = "MeowDB: starting the update..."
        self.meow_text.set(text)
        if job:
            self.meow_button.configure(text="Stop update")
            self.meow_button.state(["disabled"] if job.stop_at is not None else ["!disabled"])
        elif not market.ALLOWED:
            self.meow_button.configure(text="MeowDB blocked")
            self.meow_button.state(["disabled"])
        elif not self.server:  # the main window keeps the prices up to date: two updates at once would clash
            self.meow_button.configure(text="Update them in Maple tools")
            self.meow_button.state(["disabled"])
        else:
            self.meow_button.configure(text=f"Update {due} price{'s' if due != 1 else ''} now" if due else "All up to date")
            self.meow_button.state(["!disabled"] if due else ["disabled"])

    def tick_meow(self):
        # what the sync thread works from: read here, on the window's own thread
        if "sv_on" in self.vars:
            self.server_cfg = {"on": self.vars["sv_on"].get(), "url": self.vars["sv_url"].get().strip(),
                               "key": self.vars["sv_key"].get(), "last": self.last_outcome}
        self.running_now = set(self.jobs)
        self.read_dump()
        if "custom" in self.tabs and self.list_file_time() != self.list_time:  # a bot bought something
            self.show_list()
        self.tick_status()
        self.root.after(1000, self.tick_meow)

    # ---- the log, and closing

    def log(self, tool, text):
        at_end = self.log_box.yview()[1] >= 0.999
        name = TOOLS.get(tool, tool)
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{time.strftime('%H:%M:%S')} [{name}] {text}\n")
        excess = int(self.log_box.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if excess > 0:
            self.log_box.delete("1.0", f"{excess + 1}.0")
        self.log_box.configure(state="disabled")
        if at_end:
            self.log_box.see("end")

    def drain(self):
        for _ in range(200):
            try:
                tool, line = self.lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                self.ended(tool)
                continue
            if tool == "__discord__":  # the server's notification toggles, from the sync thread
                if "server" in self.tabs:
                    self.show_notify(line)
                continue
            progress = PROGRESS_RE.fullmatch(line) if tool == "update" else None
            if progress:
                self.progress = int(progress[1]), int(progress[2])
                self.tick_status()
            else:
                self.log(tool, line)
        for tool, job in list(self.jobs.items()):
            if job.overdue():
                self.log(tool, f"didn't stop within {STOP_WAIT:.0f} seconds: ending it, and letting go of any keys")
                job.kill()
        self.root.after(100, self.drain)

    def close(self):
        if self.jobs:
            running = ", ".join(TOOLS[t] for t in self.jobs)
            if not messagebox.askyesno(self.root.title(), f"{running} is still running. Stop it and close?"):
                return
            self.closing = True
            for tool in list(self.jobs):
                self.stop(tool)
            self.remember()
            return
        self.remember()
        self.root.destroy()


def sharp_on_scaled_displays():
    """Without this, Windows scales the window up after tk has measured its text, which cuts the
    ends off labels on a display that isn't at 100%."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except OSError:
        pass


def main():
    window = sys.argv[1] if len(sys.argv) > 1 else "main"
    if window not in WINDOWS:
        sys.exit(f"launcher.py [{'|'.join(w for w in WINDOWS if w != 'main')}]")
    sharp_on_scaled_displays()
    # without its own id Windows files the window under Python, with Python's icon on the taskbar
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"maple.tools.{'launcher' if window == 'main' else window}")
    root = tk.Tk()
    try:
        root.iconbitmap(default=str(ICON_FILE))
    except tk.TclError:
        pass  # only a missing icon
    App(root, window)
    root.mainloop()


if __name__ == "__main__":
    main()
