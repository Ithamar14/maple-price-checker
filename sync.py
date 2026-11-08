"""Keeps this PC in step with the bots' server (server/server.py): what this bot is doing, the store
scans every bot made, and the reference files they share. The launcher runs it in the background
when its Server tab is on. Plain Python: nothing to install.

- status: what's running, the full sell loop's room and money, what's held for the store
- scans: this bot's new store visits and purchases go up; every other bot's come down, into the same
  scan database (marked as theirs), so prices are worked out from all of them
- files: recipes and equipment stats, which aren't in the repo, are shared: the newer copy wins
- events: a tool started or stopped, and how

Anything going wrong only skips this round: the bots never wait on the server.
"""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import scan_log

HERE = Path(__file__).parent
SHARED_FILES = [HERE / "data" / "recipes_meowdb.json", HERE / "data" / "equipment_stats.json"]
STATE_FILE = HERE / "data" / "loop_state.json"
SCOUT_STATE_FILE = HERE / "data" / "scout_state.json"
HOLDINGS_FILE = HERE / "data" / "holdings.json"
TIMEOUT = 20
UPLOAD_BATCH = 200   # store visits a request
DOWNLOAD_PAGE = 1000


class SyncError(Exception):
    pass


class Client:
    def __init__(self, url, key=""):
        self.url, self.key = url.rstrip("/"), key

    def call(self, method, path, data=None, raw=None, admin_key=None):
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        req = urllib.request.Request(self.url + path, data=body, method=method)
        req.add_header("Content-Type", "application/json" if raw is None else "application/octet-stream")
        req.add_header("User-Agent", "maple-bot-sync/1")
        if self.key:
            req.add_header("X-Bot-Key", self.key)
        if admin_key:
            req.add_header("X-Admin-Key", admin_key)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                out = r.read()
                return out if "json" not in r.headers.get("Content-Type", "") else json.loads(out or b"{}")
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read()).get("error")
            except (ValueError, AttributeError):
                why = None
            raise SyncError(f"the server said {e.code}{': ' + why if why else ''}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SyncError(f"can't reach the server ({getattr(e, 'reason', e)})") from None


def join(url, admin_key, name):
    """This bot's key, from the server's admin key (which isn't kept)."""
    return Client(url).call("POST", "/api/join", {"name": name}, admin_key=admin_key)["key"]


# ---------------------------------------------------------------- scans

def meta(log, key, default=0):
    row = log.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return type(default)(row[0]) if row else default


def set_meta(log, key, value):
    with log.db:
        log.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def upload(log, client):
    """This bot's store visits and purchases the server doesn't have yet. Returns how many went up."""
    who = log.contributor()
    sent = 0
    while True:
        after = meta(log, "sync_up_shop")
        shops = log.db.execute("SELECT id, at, epoch, place, room, channel, owner, owner_score, title, skipped, complete FROM shops "
                               "WHERE remote IS NULL AND id > ? ORDER BY id LIMIT ?", (after, UPLOAD_BATCH)).fetchall()
        buys_after = meta(log, "sync_up_buy")
        buys = log.db.execute("SELECT rowid, at, epoch, place, owner, item, quantity, price FROM own_buys WHERE remote IS NULL AND rowid > ? "
                              "ORDER BY rowid LIMIT ?", (buys_after, UPLOAD_BATCH)).fetchall()
        if not shops and not buys:
            return sent
        records = []
        for s in shops:
            listings = [dict(zip(("row", "name", "item", "bundle", "price", "stock", "sold"), r)) for r in log.db.execute(
                "SELECT row, name, item, bundle, price, stock, sold FROM listings WHERE shop_id = ? ORDER BY row", (s[0],))]
            records.append({"id": f"{who}:s{s[0]}", **dict(zip(("at", "epoch", "place", "room", "channel", "owner", "owner_score",
                                                               "title", "skipped", "complete"), s[1:])), "listings": listings})
        purchases = [{"id": f"{who}:b{b[0]}", **dict(zip(("at", "epoch", "place", "owner", "item", "quantity", "price"), b[1:]))} for b in buys]
        client.call("POST", "/api/scans", {"shops": records, "own_buys": purchases})
        if shops:
            set_meta(log, "sync_up_shop", shops[-1][0])
        if buys:
            set_meta(log, "sync_up_buy", buys[-1][0])
        sent += len(shops) + len(buys)


def download(log, client):
    """Every other bot's records the server has that we don't, into the scan database as theirs."""
    who = log.contributor()
    got = 0
    while True:
        after = meta(log, "sync_down_seq")
        page = client.call("GET", f"/api/scans?after={after}&limit={DOWNLOAD_PAGE}")
        with log.db:
            for s in page.get("shops", []):
                if str(s.get("id", "")).startswith(f"{who}:"):
                    continue  # our own, back again
                cur = log.db.execute(
                    "INSERT OR IGNORE INTO shops (at, epoch, place, room, channel, owner, owner_score, title, skipped, complete, rows, remote) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (s.get("at"), s.get("epoch"), s.get("place"), s.get("room"), s.get("channel"), s.get("owner"), s.get("owner_score"),
                     s.get("title"), s.get("skipped"), s.get("complete"), len(s.get("listings") or []), s["id"]))
                if cur.rowcount:
                    shop = cur.lastrowid
                    log.db.executemany("INSERT INTO listings (shop_id, row, name, item, bundle, price, stock, sold) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                       [(shop, l.get("row"), l.get("name"), l.get("item"), l.get("bundle"), l.get("price"), l.get("stock"), l.get("sold"))
                                        for l in s.get("listings") or []])
                    got += 1
            for b in page.get("own_buys", []):
                if str(b.get("id", "")).startswith(f"{who}:"):
                    continue
                cur = log.db.execute("INSERT OR IGNORE INTO own_buys (at, epoch, place, owner, item, quantity, price, remote) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                     (b.get("at"), b.get("epoch"), b.get("place"), b.get("owner"), b.get("item"), b.get("quantity"), b.get("price"), b["id"]))
                got += cur.rowcount
        set_meta(log, "sync_down_seq", page.get("last", after))
        if not page.get("more"):
            return got


# ---------------------------------------------------------------- files, status, events

def sync_files(client, files=None):
    """The newer copy of each shared file wins: ours goes up, or the server's comes down. Returns
    what moved, as text."""
    files = files or SHARED_FILES
    there = {f["name"]: f for f in client.call("GET", "/api/files")}
    moved = []
    for path in files:
        path = Path(path)
        theirs = there.get(path.name)
        mine = path.stat().st_mtime if path.exists() else None
        if mine and (not theirs or mine > theirs["updated"] + 60):
            client.call("PUT", f"/api/files/{path.name}", raw=path.read_bytes())
            moved.append(f"{path.name} up")
        elif theirs and (not mine or theirs["updated"] > mine + 60):
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(path.name + ".part")
            part.write_bytes(client.call("GET", f"/api/files/{path.name}"))
            part.replace(path)
            moved.append(f"{path.name} down")
    return moved


def read_json(path):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def status(running, last_outcome=""):
    """What this bot is doing, for the server: the tools running, and the full sell loop's place."""
    state = read_json(STATE_FILE) or {}
    held = (read_json(HOLDINGS_FILE) or {}).get("items", {})
    return {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "running": sorted(running),
            "last": last_outcome, "room": state.get("room"), "channels_left": len(state.get("todo") or []),
            "npc_money": (state.get("budget") or 0) - (state.get("spent") or 0) if state else None,
            "store_money": state.get("profit_pool") if state else None, "held": len(held),
            "scout_room": (read_json(SCOUT_STATE_FILE) or {}).get("room")}


def run_once(client, log_path=None, running=(), last_outcome="", files=True):
    """One round: status, scans both ways, and the shared files. Returns a summary line."""
    client.call("POST", "/api/status", status(running, last_outcome))
    log = scan_log.ScanLog(log_path)
    try:
        up, down = upload(log, client), download(log, client)
    finally:
        log.close()
    moved = sync_files(client) if files else []
    return f"{up} up, {down} down" + (f", {', '.join(moved)}" if moved else "")


def event(client, kind, text):
    try:
        client.call("POST", "/api/events", {"kind": kind, "text": text})
    except SyncError:
        pass
