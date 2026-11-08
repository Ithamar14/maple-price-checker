"""Stores passed from a scouter to buyers through the server's task queue (server/server.py, /api/tasks).

The scouter (full sell loop with --scout) reads every store and judges and buys nothing: each store's
listings go up as a task, with where the store is, one copy for each buyer named (--scout-for: a scroll
bot and an equipment bot, say). Each buyer (the full sell loop, with the server on) judges them between
shops by its own rules and money (crawler.judge_visit), goes straight to the most profitable store with
something it wants, buys there, then goes back to what it was doing. Over MAX_AGE old is too late.

The server's address, key and this bot's name are the launcher's Server tab (launcher_settings.json).
Both directions run on a thread: the bots never wait on the server, and a server that can't be reached
only means no finds for now.
"""
import collections
import json
import threading
import time
from pathlib import Path

import sync

HERE = Path(__file__).parent
SETTINGS_FILE = HERE / "launcher_settings.json"
KIND = "buy"
MAX_AGE = 600        # seconds a find is worth going for
POLL_EVERY = 10      # seconds between the buyer's looks at the server


def server_settings(path=None):
    """(url, key, name) from the launcher's Server tab, or None when it's off or not joined."""
    try:
        v = json.loads(Path(path or SETTINGS_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not (v.get("sv_on") and v.get("sv_url") and v.get("sv_key")):
        return None
    return v["sv_url"], v["sv_key"], v.get("sv_name", "")


def visit(place, sign, title, owner, store_title, rows):
    """What a scouter sends of a store: where (room, channel, its sign's spot as fractions of the screen,
    the sign's name and the owner) and every listing still for sale (name, options for a cut-off name,
    bundle, price, what an NPC pays, stock when read)."""
    room, channel = parse_place(place)
    return {"room": room, "channel": channel, "place": place, "sign": [round(sign[0], 4), round(sign[1], 4)] if sign else None,
            "title": title or store_title or "", "store_title": store_title or "", "owner": owner or "", "rows": rows,
            "seen": time.time()}


def hit(row):
    """A sent listing as the price checker's Hit, to judge like one read off the screen."""
    import price_checker as pc
    return pc.Hit(row["name"], row.get("qty", 1), row["asking"], row.get("npc_pays", 0), (0, 0, 0, 0), tuple(row.get("options") or ()))


def fresh(data, now=None):
    """Whether a store's still worth going to: young enough, and with a room and channel to go to."""
    return (now or time.time()) - data.get("seen", 0) <= MAX_AGE and bool(data.get("room")) and bool(data.get("channel"))


def parse_place(place):
    """(room, channel) from "room 4 channel 30"; None for a part that isn't there."""
    words = place.split()
    get = lambda w: int(words[words.index(w) + 1]) if w in words and words.index(w) + 1 < len(words) \
        and words[words.index(w) + 1].isdigit() else None
    return get("room"), get("channel")


class Tips:
    """The server end of it, for either bot. send() queues a store for each buyer (scouter); new() hands
    over the stores come in since it last asked (buyer), each answered with done(). log is called with a
    line for the run's log."""

    def __init__(self, url, key, name="", send_to="*", log=print, client=None):
        self.client = client or sync.Client(url, key)
        self.name, self.log = name, log
        self.send_to = [n.strip() for n in (send_to or "*").split(",") if n.strip()] or ["*"]  # a copy for each
        self.outbox = collections.deque()  # to send, oldest first: each leaves only once the server has it
        self.waiting = {}   # task id -> task: finds on the server for this bot, from the last look
        self.taken = set()  # ids handed over already: never twice
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.last_error = None
        self.thread = None

    @classmethod
    def from_settings(cls, send_to="*", log=print, path=None):
        found = server_settings(path)
        return cls(*found, send_to=send_to, log=log) if found else None

    def start(self, take=False):
        self.thread = threading.Thread(target=self._run, args=(take,), daemon=True)
        self.thread.start()
        return self

    def close(self, wait=5.0):
        """Sends what's still queued (up to wait seconds), then stops."""
        end = time.monotonic() + wait
        while self.outbox and time.monotonic() < end:
            time.sleep(0.1)
        self.stop.set()

    # ---- scouter

    def send(self, data):
        for bot in self.send_to:
            self.outbox.append({"bot": bot, "kind": KIND, "data": data})

    # ---- buyer

    def new(self):
        """The stores come in since the last call, oldest first, as [{"id", "data", "by", ...}]. Each once only."""
        with self.lock:
            tasks = sorted((t for i, t in self.waiting.items() if i not in self.taken), key=lambda t: t["id"])
            self.taken.update(t["id"] for t in tasks)
            self.waiting.clear()
        return tasks

    def done(self, task, result):
        self.outbox.append({"done": task["id"], "result": result})

    # ---- the thread

    def _run(self, take):
        next_poll = 0.0
        while not self.stop.is_set():
            try:
                while self.outbox:
                    item = self.outbox[0]
                    if "done" in item:
                        self.client.call("POST", f"/api/tasks/{item['done']}/done", {"result": item["result"]})
                    else:
                        self.client.call("POST", "/api/tasks", item)
                    self.outbox.popleft()
            except sync.SyncError as e:
                self._error(e)  # what's left goes next round
            if take and time.monotonic() >= next_poll:
                next_poll = time.monotonic() + POLL_EVERY
                try:
                    tasks = self.client.call("GET", "/api/tasks")
                    with self.lock:
                        self.waiting = {t["id"]: t for t in tasks if t.get("kind") == KIND and t["id"] not in self.taken}
                    self.last_error = None
                except sync.SyncError as e:
                    self._error(e)
            self.stop.wait(1.0)

    def _error(self, e):
        if str(e) != self.last_error:  # once, not every second
            self.last_error = str(e)
            self.log(f"server: {e} (finds wait until it answers)")
