"""The bots' shared server: who's running and doing what, the store scans every bot has made, shared
reference files, events and tasks. Plain Python, no packages, one SQLite file. See README.md here.

Every call but /api/health needs a bot's key (header X-Bot-Key). A bot gets its key once by
joining with the server's admin key (POST /api/join, header X-Admin-Key): the admin key itself is
never kept by a bot.

    GET  /api/health                        no key: "ok"
    POST /api/join     {"name"}             X-Admin-Key -> {"name", "key"}
    POST /api/status   {...}                this bot's latest state (any fields)
    GET  /api/status                        every bot's: [{"name", "seen", "status"}]
    POST /api/scans    {"shops": [...], "own_buys": [...]}   records with an "id" each (kept once)
    GET  /api/scans?after=N&limit=M         the records after server number N, other bots' first
    GET  /api/files                         [{"name", "updated", "by", "size"}]
    GET  /api/files/<name>                  the file
    PUT  /api/files/<name>                  replaces it (body: the file)
    POST /api/events   {"kind", "text"}     GET /api/events?after=N
    POST /api/tasks    {"bot", "kind", "data"}   GET /api/tasks (open tasks for this bot or "*")
    POST /api/tasks/<id>/done {"result"}
                                            GET /api/tasks?max_age=S: only those under S seconds old. Tasks
                                            nobody took within TASK_TTL are closed as "expired", and a task for a
                                            bot that isn't asking for tasks (TAKER_GONE) isn't kept: {"dropped"}

    python server.py    # PORT (8080), DATA_DIR (.), ADMIN_KEY from the environment
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
DB_FILE = DATA_DIR / "maple_server.sqlite"
MAX_BODY = 20 * 1024 * 1024  # a request's body: a big first upload of scans comes in parts below this
MAX_PAGE = 5000
TASK_TTL = float(os.environ.get("TASK_TTL", 600))  # seconds a task waits to be taken (a buyer's tips.MAX_AGE): then closed as expired
TAKER_GONE = 60.0  # a bot that hasn't asked for tasks in this long isn't taking any: tasks for it aren't kept at all
FILE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (name TEXT PRIMARY KEY, key_hash TEXT UNIQUE, created REAL, seen REAL, status TEXT);
CREATE TABLE IF NOT EXISTS records (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, uid TEXT UNIQUE, bot TEXT,
    epoch REAL, body TEXT);
CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, body BLOB, updated REAL, by TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, bot TEXT, at REAL, kind TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, bot TEXT, kind TEXT, data TEXT, created REAL,
    by TEXT, done REAL, done_by TEXT, result TEXT);
CREATE INDEX IF NOT EXISTS records_kind ON records(kind, seq);
"""


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


class Store:
    """The database. One connection, used under a lock: a few bots don't need more."""

    def __init__(self, path=DB_FILE):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def run(self, sql, args=()):
        with self.lock, self.db:
            return self.db.execute(sql, args).fetchall()

    def join(self, name):
        key = secrets.token_urlsafe(24)
        with self.lock, self.db:
            self.db.execute("INSERT INTO bots (name, key_hash, created) VALUES (?, ?, ?) "
                            "ON CONFLICT(name) DO UPDATE SET key_hash = excluded.key_hash", (name, key_hash(key), time.time()))
        return key

    def bot(self, key):
        rows = self.run("SELECT name FROM bots WHERE key_hash = ?", (key_hash(key),)) if key else []
        return rows[0][0] if rows else None

    def add_records(self, bot, kind, items):
        """Records with an "id" each: one already here isn't added again. Returns how many were new."""
        new = 0
        with self.lock, self.db:
            for item in items:
                uid = str(item.get("id") or "")
                if not uid:
                    continue
                cur = self.db.execute("INSERT OR IGNORE INTO records (kind, uid, bot, epoch, body) VALUES (?, ?, ?, ?, ?)",
                                      (kind, uid, bot, item.get("epoch"), json.dumps(item)))
                new += cur.rowcount
        return new


class Handler(BaseHTTPRequestHandler):
    store = None
    admin_key = ""
    asked = {}  # bot -> when it last asked for tasks: who's taking them now (kept in memory: a restart forgets for a moment)
    server_version = "maple-server/1"

    # ---- plumbing

    def log_message(self, fmt, *args):  # one short line a call, without the keys
        print(f"{self.address_string()} {self.command} {urlparse(self.path).path} {args[1] if len(args) > 1 else ''}", flush=True)

    def reply(self, code, data=None, raw=None, kind="application/json"):
        body = raw if raw is not None else json.dumps(data if data is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        """The request's body, read once (route reads it first: a reply sent before the body was read
        can make Windows drop the connection, and the bot never sees the reply)."""
        if not hasattr(self, "_body"):
            size = int(self.headers.get("Content-Length") or 0)
            if size > MAX_BODY:
                self._body = b""
                raise ValueError("too big")
            self._body = self.rfile.read(size) if size else b""
        return self._body

    def json_body(self):
        raw = self.body()
        return json.loads(raw) if raw else {}

    def who(self):
        return self.store.bot(self.headers.get("X-Bot-Key", ""))

    def route(self, method):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            self.body()
            if parts == ["api", "health"]:
                return self.reply(200, {"ok": True, "time": time.time()})
            if parts == ["api", "join"] and method == "POST":
                given = self.headers.get("X-Admin-Key", "")
                if not self.admin_key or not hmac.compare_digest(given, self.admin_key):
                    return self.reply(403, {"error": "wrong admin key"})
                name = str(self.json_body().get("name") or "").strip()[:40]
                if not name:
                    return self.reply(400, {"error": "a bot needs a name"})
                return self.reply(200, {"name": name, "key": self.store.join(name)})
            bot = self.who()
            if not bot:
                return self.reply(401, {"error": "unknown bot key: join the server first"})
            return self.api(method, parts[1:], query, bot) if parts[:1] == ["api"] else self.reply(404, {"error": "not found"})
        except (ValueError, KeyError, TypeError) as e:
            return self.reply(400, {"error": str(e)})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    # ---- the calls

    def api(self, method, parts, query, bot):
        s = self.store
        if parts == ["status"] and method == "POST":
            s.run("UPDATE bots SET seen = ?, status = ? WHERE name = ?", (time.time(), json.dumps(self.json_body()), bot))
            return self.reply(200, {"ok": True})
        if parts == ["status"]:
            return self.reply(200, [{"name": n, "seen": seen, "status": json.loads(st) if st else None}
                                    for n, seen, st in s.run("SELECT name, seen, status FROM bots ORDER BY name")])
        if parts == ["scans"] and method == "POST":
            data = self.json_body()
            new = {kind: s.add_records(bot, kind, data.get(kind) or []) for kind in ("shops", "own_buys")}
            return self.reply(200, {"new": new})
        if parts == ["scans"]:
            after, limit = int(query.get("after", 0)), min(int(query.get("limit", 1000)), MAX_PAGE)
            rows = s.run("SELECT seq, kind, bot, body FROM records WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit))
            out = {"shops": [], "own_buys": [], "last": rows[-1][0] if rows else after, "more": len(rows) == limit}
            for seq, kind, by, body in rows:
                out.setdefault(kind, []).append({**json.loads(body), "_bot": by, "_seq": seq})
            return self.reply(200, out)
        if parts == ["files"]:
            return self.reply(200, [{"name": n, "updated": u, "by": b, "size": z}
                                    for n, u, b, z in s.run("SELECT name, updated, by, length(body) FROM files ORDER BY name")])
        if len(parts) == 2 and parts[0] == "files":
            name = parts[1]
            if not FILE_NAME.match(name):
                return self.reply(400, {"error": "bad file name"})
            if method == "PUT":
                s.run("INSERT INTO files (name, body, updated, by) VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                      "body = excluded.body, updated = excluded.updated, by = excluded.by", (name, self.body(), time.time(), bot))
                return self.reply(200, {"ok": True})
            rows = s.run("SELECT body FROM files WHERE name = ?", (name,))
            return self.reply(200, raw=rows[0][0], kind="application/octet-stream") if rows else self.reply(404, {"error": "no such file"})
        if parts == ["events"] and method == "POST":
            data = self.json_body()
            s.run("INSERT INTO events (bot, at, kind, text) VALUES (?, ?, ?, ?)", (bot, time.time(), str(data.get("kind", "note"))[:40], str(data.get("text", ""))[:2000]))
            return self.reply(200, {"ok": True})
        if parts == ["events"]:
            rows = s.run("SELECT id, bot, at, kind, text FROM events WHERE id > ? ORDER BY id LIMIT 500", (int(query.get("after", 0)),))
            return self.reply(200, [dict(zip(("id", "bot", "at", "kind", "text"), r)) for r in rows])
        if parts == ["tasks"] and method == "POST":
            data = self.json_body()
            target, now = str(data.get("bot") or "*"), time.time()
            takers = [b for b, at in self.asked.items() if now - at < TAKER_GONE]
            if (target not in takers) if target != "*" else not takers:
                return self.reply(200, {"ok": True, "dropped": f"{'no bot' if target == '*' else target} is taking tasks right now"})
            s.run("INSERT INTO tasks (bot, kind, data, created, by) VALUES (?, ?, ?, ?, ?)",
                  (str(data.get("bot") or "*"), str(data["kind"])[:40], json.dumps(data.get("data")), time.time(), bot))
            return self.reply(200, {"ok": True})
        if parts == ["tasks"]:
            now = time.time()  # a buyer that's off never takes them: they'd only pile up
            self.asked[bot] = now
            s.run("UPDATE tasks SET done = ?, done_by = 'server', result = ? WHERE done IS NULL AND created < ?",
                  (now, json.dumps("expired: nobody took it in time"), now - TASK_TTL))
            try:
                newest = now - float(query.get("max_age", TASK_TTL))
            except ValueError:
                newest = now - TASK_TTL
            rows = s.run("SELECT id, bot, kind, data, created, by FROM tasks WHERE done IS NULL AND bot IN (?, '*') AND created >= ? "
                         "ORDER BY id", (bot, newest))
            return self.reply(200, [{"id": i, "bot": b, "kind": k, "data": json.loads(d), "created": c, "by": by} for i, b, k, d, c, by in rows])
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "done" and method == "POST":
            s.run("UPDATE tasks SET done = ?, done_by = ?, result = ? WHERE id = ? AND done IS NULL",
                  (time.time(), bot, json.dumps(self.json_body().get("result")), int(parts[1])))
            return self.reply(200, {"ok": True})
        return self.reply(404, {"error": "not found"})


def serve(port=None, store=None, admin_key=None):
    Handler.store = store or Store()
    Handler.asked = {}
    Handler.admin_key = admin_key if admin_key is not None else os.environ.get("ADMIN_KEY", "")
    if not Handler.admin_key:
        print("warning: no ADMIN_KEY set, so no bot can join", flush=True)
    port = int(port if port is not None else os.environ.get("PORT", 8080))  # 0: any free port
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    return httpd


if __name__ == "__main__":
    server = serve()
    print(f"serving on port {server.server_address[1]}, data in {DB_FILE}", flush=True)
    server.serve_forever()
