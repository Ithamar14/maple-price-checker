"""The Discord end of the server: slash commands come in over HTTP (Discord's "interactions endpoint",
/discord/interactions, signed with Ed25519 and checked here), and messages go out to one channel with the bot's
token. No gateway connection and no packages.

Set on Railway (the Developer Portal's application gives the first three):
    DISCORD_APP_ID       the application's id
    DISCORD_PUBLIC_KEY   its public key, to check what Discord sends
    DISCORD_BOT_TOKEN    its bot's token, to post and to set the commands up
    DISCORD_GUILD_ID     your Discord server's id: the commands are set up there
    DISCORD_CHANNEL_ID   the channel notifications and reports go to
    DISCORD_OWNER_ID     your own user id: you can always buy, and only you can /allow others

Commands: /maplebot (what they all do), /status, /price and /report for everyone; /alert, /alerts and /unalert for everyone too (an alert spends
nothing: it pings whoever set it when a bot's scans show the item at or under their price, check_alerts); /buy,
/requests and /cancel for you and whoever you /allow; /allow and /disallow for you. A buy request goes to a bot's custom list at its next sync (sync.sync_requests),
auto-buying at or under the price given, up to the amount, and is never sold at the NPC.

Notifications, each one turned on or off from the launcher's Server tab (NOTIFY): bots starting and stopping, errors
and bots that stop checking in, scouter finds bought, buy requests' progress, the day's snapshot, an RMT wave, and
the daily report.
"""
import difflib
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request

import ed25519
import market_view

API = "https://discord.com/api/v10"
UA = "DiscordBot (https://github.com/Ithamar14/maple-price-checker, 1)"
NOTIFY = {"runs": "Bots starting and stopping", "errors": "Errors, and bots that stop checking in",
          "finds": "Scouter finds bought", "requests": "Buy requests: taken, bought, done",
          "snapshot": "The day's MeowDB snapshot is in", "rmt": "An RMT wave starting", "report": "The daily market report",
          "alerts": "Price alerts (/alert) pinging whoever set them"}
GONE_AFTER = 300     # seconds without a status from a bot that was running something: it's said to have gone quiet
WATCH_EVERY = 60
MAX_AMOUNT = 100_000
MAX_ALERTS = 10        # open alerts a person may have
ALERT_DAYS = 7         # how long an alert lasts unless told otherwise (1 to 30 days)
ALERT_FRESH = 1800     # seconds: only listings seen this recently ping (not a bot's old scans arriving late)
ALERT_PER_HOUR = 6     # pings an alert may make in an hour
EPHEMERAL = 64       # a reply only the one who asked sees

# slash commands (type 1: chat input). Option types: 3 text, 4 whole number, 6 user
HELP = f"""**Maple bot commands**
**For everyone**
`/maplebot`: this list.
`/status`: what each bot is doing: running or not, its room and channel, its money.
`/price item`: an item's price: MeowDB's 24h and 7-day medians, and what the bots saw asked and sold in the last 3 days.
`/report`: today's market report.
`/alert item max_price [days]`: get pinged here when a bot sees the item at or under that price (per item; a throwing star \
by the set). Lasts {ALERT_DAYS} days unless you say (1 to 30), up to {MAX_ALERTS} each. Pings once a store and price, at most \
{ALERT_PER_HOUR} an hour, a minute or two after the bot saw it.
`/alerts`: your alerts. `/unalert id`: remove one.
**For the owner and whoever they /allow**
`/buy item max_price amount [bot]`: a bot buys up to that many at or under the price, from its own money, while it runs \
with Custom deals on. Leave bot out for whichever takes it first. What's bought is kept, never sold at the NPC: it's \
handed over in the game.
`/requests`: open buy requests and the last finished. `/cancel id`: cancel one (whoever asked, or the owner).
**For the owner**
`/allow @someone`, `/disallow @someone`: who may make buy requests.
Item boxes suggest names as you type. Only you see the answers to /maplebot, /alert, /alerts and /unalert."""

COMMANDS = [
    {"name": "maplebot", "description": "What every command does, and who can use it"},
    {"name": "status", "description": "What each bot is doing"},
    {"name": "price", "description": "An item's price: MeowDB, and what the bots' scans saw",
     "options": [{"type": 3, "name": "item", "description": "The item", "required": True, "autocomplete": True}]},
    {"name": "report", "description": "Today's market report"},
    {"name": "buy", "description": "Ask a bot to buy an item at or under a price",
     "options": [{"type": 3, "name": "item", "description": "The item", "required": True, "autocomplete": True},
                 {"type": 4, "name": "max_price", "description": "The most to pay for one", "required": True, "min_value": 1},
                 {"type": 4, "name": "amount", "description": "How many", "required": True, "min_value": 1, "max_value": MAX_AMOUNT},
                 {"type": 3, "name": "bot", "description": "Which bot (default: whichever takes it first)", "required": False,
                  "autocomplete": True}]},
    {"name": "requests", "description": "Buy requests: open ones and the last finished"},
    {"name": "alert", "description": "Get pinged when a bot sees an item at or under a price",
     "options": [{"type": 3, "name": "item", "description": "The item", "required": True, "autocomplete": True},
                 {"type": 4, "name": "max_price", "description": "Ping at this price or less (a throwing star: for the set)",
                  "required": True, "min_value": 1},
                 {"type": 4, "name": "days", "description": f"How long it lasts (default {ALERT_DAYS})", "required": False,
                  "min_value": 1, "max_value": 30}]},
    {"name": "alerts", "description": "Your price alerts"},
    {"name": "unalert", "description": "Remove one of your price alerts",
     "options": [{"type": 4, "name": "id", "description": "The alert's number (see /alerts)", "required": True}]},
    {"name": "cancel", "description": "Cancel a buy request",
     "options": [{"type": 4, "name": "id", "description": "The request's number (see /requests)", "required": True}]},
    {"name": "allow", "description": "Let someone make buy requests (owner only)",
     "options": [{"type": 6, "name": "user", "description": "Who", "required": True}]},
    {"name": "disallow", "description": "Stop someone making buy requests (owner only)",
     "options": [{"type": 6, "name": "user", "description": "Who", "required": True}]},
]


def config():
    keys = ("APP_ID", "PUBLIC_KEY", "BOT_TOKEN", "GUILD_ID", "CHANNEL_ID", "OWNER_ID")
    return {k.lower(): os.environ.get(f"DISCORD_{k}", "").strip() for k in keys}


def options(data):
    return {o["name"]: o.get("value") for o in data.get("options") or []}


def who(payload):
    user = (payload.get("member") or {}).get("user") or payload.get("user") or {}
    return str(user.get("id", "")), user.get("global_name") or user.get("username") or "someone"


def reply(text, private=False):
    return {"type": 4, "data": {"content": text[:1990], "flags": EPHEMERAL if private else 0,
                                "allowed_mentions": {"parse": []}}}


class Discord:
    def __init__(self, store, cfg=None, send=None, log=print):
        self.store, self.cfg, self.log = store, cfg if cfg is not None else config(), log
        self.send = send or self._send  # (method, path, body) -> reply; tests give their own
        self.outbox = queue.Queue()
        self.quiet = set()  # bots already said to have gone quiet
        self.public_key = bytes.fromhex(self.cfg["public_key"]) if self.cfg.get("public_key") else b""

    @property
    def on(self):
        return all(self.cfg.get(k) for k in ("app_id", "public_key", "bot_token", "channel_id"))

    # ---- talking to Discord

    def _send(self, method, path, body=None):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None, method=method)
        req.add_header("Authorization", f"Bot {self.cfg['bot_token']}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", UA)
        for _ in range(5):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    out = r.read()
                    return json.loads(out) if out else None
            except urllib.error.HTTPError as e:
                if e.code == 429:  # too fast: Discord says how long to wait
                    try:
                        wait = float(json.loads(e.read()).get("retry_after", 2))
                    except ValueError:
                        wait = 2
                    time.sleep(min(wait, 30))
                    continue
                raise
        return None

    def register(self):
        """The slash commands, set up on your Discord server (there, they're ready at once)."""
        if self.on and self.cfg.get("guild_id"):
            self.send("PUT", f"/applications/{self.cfg['app_id']}/guilds/{self.cfg['guild_id']}/commands", COMMANDS)
            self.log(f"discord: {len(COMMANDS)} commands set up")

    def post(self, text, ping=None):
        """To the channel; ping: a user id, the only one it may ping."""
        if self.on:
            self.outbox.put((text[:1990], ping))

    def notify(self, kind, text, ping=None):
        """A notification, if that kind is on (the launcher's Server tab)."""
        if self.toggles().get(kind, True):
            self.post(text, ping)

    def toggles(self):
        rows = self.store.run("SELECT value FROM settings WHERE key = 'notify'")
        saved = json.loads(rows[0][0]) if rows else {}
        return {k: bool(saved.get(k, True)) for k in NOTIFY}

    def set_toggles(self, changes):
        now = {**self.toggles(), **{k: bool(v) for k, v in changes.items() if k in NOTIFY}}
        self.store.run("INSERT INTO settings (key, value) VALUES ('notify', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                       (json.dumps(now),))
        return now

    def _poster(self):
        while True:
            text, ping = self.outbox.get()
            try:
                self.send("POST", f"/channels/{self.cfg['channel_id']}/messages",
                          {"content": text, "allowed_mentions": {"parse": [], "users": [ping] if ping else []}})
            except Exception as e:  # Discord down or a bad setting: logged, never taken further
                self.log(f"discord: couldn't post: {e!r}")

    def _watch(self):
        """Bots that were running something and stopped checking in: said once, until they're back."""
        while True:
            time.sleep(WATCH_EVERY)
            try:
                self.check_quiet()
            except Exception as e:
                self.log(f"discord: {e!r}")

    def check_quiet(self, now=None):
        now = now or time.time()
        for name, seen, status in self.store.run("SELECT name, seen, status FROM bots"):
            running = (json.loads(status) if status else {}).get("running") or []
            if seen and now - seen > GONE_AFTER and running:
                if name not in self.quiet:
                    self.quiet.add(name)
                    self.notify("errors", f"⚠️ **{name}** stopped checking in {(now - seen) / 60:.0f} minutes ago while running "
                                          f"{', '.join(running)}: the PC, the launcher or the internet there may be down.")
            elif name in self.quiet and seen and now - seen <= GONE_AFTER:
                self.quiet.discard(name)
                self.notify("errors", f"✅ **{name}** is checking in again.")

    def start(self):
        if not self.on:
            self.log("discord: not set up (DISCORD_ variables), so it's off")
            return self
        threading.Thread(target=self._poster, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()
        try:
            self.register()
        except Exception as e:
            self.log(f"discord: couldn't set the commands up: {e!r}")
        return self

    # ---- what comes in

    def interaction(self, headers, body):
        """(HTTP status, reply) for what Discord sent to /discord/interactions."""
        try:
            signature = bytes.fromhex(headers.get("X-Signature-Ed25519", ""))
        except ValueError:
            signature = b""
        stamp = headers.get("X-Signature-Timestamp", "").encode()
        if not self.public_key or not ed25519.verify(self.public_key, stamp + body, signature):
            return 401, {"error": "bad signature"}
        payload = json.loads(body)
        kind = payload.get("type")
        if kind == 1:  # Discord checking the address works
            return 200, {"type": 1}
        data = payload.get("data") or {}
        if kind == 4:  # typing in an item or bot box
            return 200, {"type": 8, "data": {"choices": self.suggest(data)}}
        if kind != 2:
            return 400, {"error": "not handled"}
        try:
            return 200, self.command(data.get("name"), options(data), payload)
        except Exception as e:
            self.log(f"discord: /{data.get('name')} failed: {e!r}")
            return 200, reply(f"Something went wrong: {e}", private=True)

    def suggest(self, data):
        focused = next((o for o in data.get("options") or [] if o.get("focused")), {})
        typed = str(focused.get("value") or "").lower()
        if focused.get("name") == "bot":
            names = ["*"] + [n for (n,) in self.store.run("SELECT name FROM bots ORDER BY name")]
            return [{"name": "any bot" if n == "*" else n, "value": n} for n in names if typed in n.lower()][:25]
        names = market_view.item_names(self.store)
        starts = [n for n in names if n.lower().startswith(typed)]
        inside = [n for n in names if typed in n.lower() and n not in starts]
        return [{"name": n[:100], "value": n[:100]} for n in (starts + inside)[:25]]

    def item(self, typed):
        """The item's name as MeowDB spells it, and a suggestion if there's no such item."""
        names = market_view.item_names(self.store)
        exact = next((n for n in names if n.lower() == str(typed).strip().lower()), None)
        if exact or not names:
            return exact or str(typed).strip(), None
        return None, difflib.get_close_matches(str(typed), names, n=3, cutoff=0.6)

    def may_buy(self, user):
        return user == self.cfg.get("owner_id") or bool(self.store.run("SELECT 1 FROM discord_users WHERE id = ?", (user,)))

    def command(self, name, opts, payload):
        user, user_name = who(payload)
        if name == "maplebot":
            return reply(HELP, private=True)
        if name == "status":
            return reply(market_view.status_text(self.store))
        if name == "report":
            return reply(market_view.report_text(self.store))
        if name == "price":
            item, close = self.item(opts.get("item", ""))
            if not item:
                return reply(f"No item called '{opts.get('item')}'." + (f" Did you mean: {', '.join(close)}?" if close else ""), private=True)
            return reply(market_view.price_text(self.store, item))
        if name in ("allow", "disallow"):
            if user != self.cfg.get("owner_id"):
                return reply("Only the owner can do that.", private=True)
            target = str(opts.get("user"))
            target_name = ((payload.get("data") or {}).get("resolved", {}).get("users", {}).get(target) or {}).get("username", target)
            if name == "allow":
                self.store.run("INSERT OR REPLACE INTO discord_users (id, name, added) VALUES (?, ?, ?)", (target, target_name, time.time()))
                return reply(f"{target_name} can make buy requests now.")
            self.store.run("DELETE FROM discord_users WHERE id = ?", (target,))
            return reply(f"{target_name} can't make buy requests any more.")
        if name == "requests":
            return reply(self.requests_text())
        if name == "alert":
            item, close = self.item(opts.get("item", ""))
            if not item:
                return reply(f"No item called '{opts.get('item')}'." + (f" Did you mean: {', '.join(close)}?" if close else ""), private=True)
            now = time.time()
            if self.store.run("SELECT COUNT(*) FROM alerts WHERE by_id = ? AND status = 'open' AND expires > ?", (user, now))[0][0] >= MAX_ALERTS:
                return reply(f"You have {MAX_ALERTS} alerts already: /unalert one first.", private=True)
            days = int(opts.get("days") or ALERT_DAYS)
            self.store.run("INSERT INTO alerts (item, max_price, by_id, by_name, created, expires, status) VALUES (?, ?, ?, ?, ?, ?, 'open')",
                           (item, int(opts["max_price"]), user, user_name, now, now + days * 86400))
            aid = self.store.run("SELECT MAX(id) FROM alerts")[0][0]
            return reply(f"Alert #{aid}: you'll be pinged here when a bot sees **{item}** at {int(opts['max_price']):,} or less, "
                         f"for the next {days} day{'s' if days != 1 else ''}.", private=True)
        if name == "alerts":
            rows = self.store.run("SELECT id, item, max_price, expires FROM alerts WHERE by_id = ? AND status = 'open' AND expires > ? "
                                  "ORDER BY id", (user, time.time()))
            return reply("\n".join(f"#{a} {item} at {price:,} or less, until <t:{int(exp)}:d>" for a, item, price, exp in rows)
                         or "You have no price alerts.", private=True)
        if name == "unalert":
            rows = self.store.run("SELECT by_id, item FROM alerts WHERE id = ? AND status = 'open'", (int(opts["id"]),))
            if not rows or rows[0][0] not in (user, self.cfg.get("owner_id")):
                return reply(f"You have no alert #{opts['id']}.", private=True)
            self.store.run("UPDATE alerts SET status = 'removed' WHERE id = ?", (int(opts["id"]),))
            return reply(f"Alert #{opts['id']} ({rows[0][1]}) removed.", private=True)
        if name == "buy":
            if not self.may_buy(user):
                return reply("You can't make buy requests: ask the owner to /allow you.", private=True)
            item, close = self.item(opts.get("item", ""))
            if not item:
                return reply(f"No item called '{opts.get('item')}'." + (f" Did you mean: {', '.join(close)}?" if close else ""), private=True)
            price, amount, bot = int(opts["max_price"]), int(opts["amount"]), str(opts.get("bot") or "*").strip() or "*"
            if bot != "*" and not self.store.run("SELECT 1 FROM bots WHERE name = ?", (bot,)):
                return reply(f"There's no bot called {bot}.", private=True)
            self.store.run("INSERT INTO requests (item, max_price, amount, bot, by_id, by_name, created, status, bought, added, removed) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 0, 0, 0)", (item, price, amount, bot, user, user_name, time.time()))
            rid = self.store.run("SELECT MAX(id) FROM requests")[0][0]
            return reply(f"Request #{rid}: {amount:,} × **{item}** at {price:,} or less, for {user_name}, by "
                         f"{'whichever bot takes it first' if bot == '*' else bot}. It goes onto the bot's custom list at its next sync "
                         f"(the bot buys custom list items only with Custom deals on).")
        if name == "cancel":
            rows = self.store.run("SELECT by_id, status, item FROM requests WHERE id = ?", (int(opts["id"]),))
            if not rows:
                return reply(f"There's no request #{opts['id']}.", private=True)
            by, status, item = rows[0]
            if user not in (by, self.cfg.get("owner_id")):
                return reply("Only whoever asked, or the owner, can cancel it.", private=True)
            if status != "open":
                return reply(f"Request #{opts['id']} is already {status}.", private=True)
            self.store.run("UPDATE requests SET status = 'cancelled', updated = ? WHERE id = ?", (time.time(), int(opts["id"])))
            return reply(f"Request #{opts['id']} ({item}) cancelled: it comes off the bot's custom list at its next sync.")
        return reply(f"Unknown command /{name}.", private=True)

    def requests_text(self):
        rows = self.store.run("SELECT id, item, max_price, amount, bought, bot, claimed_by, by_name, status, note FROM requests "
                              "WHERE status = 'open' OR updated >= ? ORDER BY status != 'open', id DESC LIMIT 20", (time.time() - 3 * 86400,))
        if not rows:
            return "No buy requests."
        out = []
        for rid, item, price, amount, bought, bot, claimed, by, status, note in rows:
            whose = claimed or ("any bot" if bot == "*" else bot)
            out.append(f"#{rid} {item}: {bought:,} of {amount:,} at {price:,} or less, for {by}, {whose} · {status}" + (f" ({note})" if note else ""))
        return "\n".join(out)

    def check_alerts(self, shops, by, now=None):
        """Store visits a bot just sent (server.py's /api/scans): listings at or under an open alert's price ping whoever
        set it, once a store and price, ALERT_PER_HOUR an hour at most, and only if seen in the last ALERT_FRESH seconds."""
        now = now or time.time()
        alerts = self.store.run("SELECT id, item, max_price, by_id FROM alerts WHERE status = 'open' AND expires > ?", (now,))
        if not alerts:
            return
        wanted = {}
        for a in alerts:
            wanted.setdefault(a[1].lower(), []).append(a)
        _, snap = market_view.snapshot(self.store)
        for shop in shops:
            if not shop.get("epoch") or now - shop["epoch"] > ALERT_FRESH:
                continue
            for listing in shop.get("listings") or []:
                hits = wanted.get(str(listing.get("item") or "").lower())
                if not hits or listing.get("sold") or not listing.get("price"):
                    continue
                star = market_view.is_star(snap.get(hits[0][1], (None,))[0])
                each = listing["price"] if star else listing["price"] / max(listing.get("bundle") or 1, 1)
                for aid, item, price, user in hits:
                    if each > price or self.store.run("SELECT 1 FROM alert_hits WHERE alert_id = ? AND shop = ? AND price = ?",
                                                      (aid, shop.get("id"), each)):
                        continue
                    if self.store.run("SELECT COUNT(*) FROM alert_hits WHERE alert_id = ? AND at > ?", (aid, now - 3600))[0][0] >= ALERT_PER_HOUR:
                        continue
                    self.store.run("INSERT INTO alert_hits (alert_id, shop, price, at) VALUES (?, ?, ?, ?)", (aid, shop.get("id"), each, now))
                    where = f"room {shop.get('room')} channel {shop.get('channel')}" if shop.get("channel") else (shop.get("place") or "a store")
                    amount = f"{listing.get('bundle')} for {listing['price']:,}" if (listing.get("bundle") or 1) > 1 and not star else f"{each:,.0f}"
                    self.notify("alerts", f"\U0001f514 <@{user}> **{item}** at {amount} (your alert #{aid}: {price:,} or less) in {where}, "
                                          f"{shop.get('title') or 'a shop'}" + (f" by {shop['owner']}" if shop.get("owner") else "")
                                          + f", seen <t:{int(shop['epoch'])}:R> by {by}.", ping=user)

    # ---- the bots' side of buy requests (server.py's /api/requests)

    def for_bot(self, bot):
        """The requests a bot should act on: open ones for it or any bot not yet taken by another, and ones it took
        that were cancelled and not yet dealt with on its list (what was bought stays listed, so it's never sold at the
        NPC; a finished request's item stays listed for the same reason)."""
        rows = self.store.run("SELECT id, item, max_price, amount, bought, bot, claimed_by, by_name, status, added, removed FROM requests "
                              "WHERE (status = 'open' AND bot IN (?, '*') AND (claimed_by IS NULL OR claimed_by = ?)) "
                              "OR (status = 'cancelled' AND claimed_by = ? AND added = 1 AND removed = 0)", (bot, bot, bot))
        keys = ("id", "item", "max_price", "amount", "bought", "bot", "claimed_by", "by_name", "status", "added", "removed")
        return [dict(zip(keys, r)) for r in rows]

    def claim(self, rid, bot):
        self.store.run("UPDATE requests SET claimed_by = ?, updated = ? WHERE id = ? AND claimed_by IS NULL AND status = 'open' "
                       "AND bot IN (?, '*')", (bot, time.time(), rid, bot))
        rows = self.store.run("SELECT claimed_by, item, amount, max_price, by_name FROM requests WHERE id = ?", (rid,))
        ok = bool(rows) and rows[0][0] == bot
        if ok:
            _, item, amount, price, by = rows[0]
            self.notify("requests", f"🛒 **{bot}** took request #{rid}: {amount:,} × {item} at {price:,} or less, for {by}.")
        return ok

    def progress(self, rid, bot, data):
        rows = self.store.run("SELECT item, amount, bought, by_name, status FROM requests WHERE id = ? AND claimed_by = ?", (rid, bot))
        if not rows:
            return False
        item, amount, before, by, status = rows[0]
        now = time.time()
        if data.get("added"):
            self.store.run("UPDATE requests SET added = 1, updated = ? WHERE id = ?", (now, rid))
        if data.get("removed"):
            self.store.run("UPDATE requests SET removed = 1, updated = ? WHERE id = ?", (now, rid))
        if data.get("refused"):
            self.store.run("UPDATE requests SET status = 'refused', note = ?, updated = ? WHERE id = ?", (str(data["refused"])[:200], now, rid))
            self.notify("requests", f"❌ **{bot}** can't take request #{rid} ({item}): {data['refused']}.")
        bought = data.get("bought")
        if bought is not None and int(bought) != before and status == "open":
            bought = int(bought)
            done = bought >= amount
            self.store.run("UPDATE requests SET bought = ?, status = ?, updated = ? WHERE id = ?", (bought, "done" if done else "open", now, rid))
            self.notify("requests", f"{'✅' if done else '🛍️'} **{bot}** bought {bought - before:,} × {item} for {by}: "
                                    f"{bought:,} of {amount:,}" + (" — request done." if done else "."))
        return True
