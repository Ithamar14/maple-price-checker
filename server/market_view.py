"""What the server can say about the market, for Discord: one item's prices (/price), the bots (/status), the daily
report, and the check for an RMT wave. From the newest whole MeowDB snapshot (snapshots.py) and the store scans the
bots send (scan_rows, an index of their listings kept beside the records).

The rules the market report keeps (.claude/skills/market-report/SKILL.md): a sold (grey) row is one sale at a
price, never a volume; a throwing star is one item whatever its bundle says; ETC and USE asks over twice the item's
price are troll prices and left out, sold rows over it are kept; equipment prices mean little one by one.
"""
import json
import statistics as st
import time

import snapshots

TROLL = 2.0           # an ETC or USE ask over this many times the item's price is a troll price
MIN_REPORTS = 5       # MeowDB 24h reports needed to use its median
MOVER_REPORTS = 10    # and to call something a mover in the report
HIGH_END = 300_000    # items at least this much: what RMT money went into in early November 2026
RMT_RISE = 0.10       # an RMT wave: the high-end items up this much since the last snapshot (their median)...
RMT_SHARE = 0.6       # ...with at least this share of them up...
RMT_ORES_CALM = 0.05  # ...while ores and gems moved less than this
MAX_MESSAGE = 1900    # Discord's limit is 2,000 characters
_cache = {}


def load_day(store, day):
    """{name: (item, summary)} for a whole snapshot's day ({} if there's none), kept until it changes."""
    rows = store.run("SELECT updated FROM snapshots WHERE day = ? AND done = 1", (day,))
    if not rows:
        return {}
    if _cache.get(day, (None,))[0] != rows[0][0]:
        state = snapshots.unpack(store.run("SELECT body FROM snapshots WHERE day = ?", (day,))[0][0])
        items = {str(i["id"]): i for i in state.get("items") or []}
        named = {items[k]["name"]: (items[k], s) for k, s in state.get("summary", {}).items() if k in items and not s.get("status")}
        _cache[day] = (rows[0][0], named, sorted(i["name"] for i in items.values()))
    return _cache[day][1]


def snapshot(store, before=None):
    """The newest whole snapshot (before a day, if given): (day, {name: (item, summary)}), or (None, {})."""
    rows = store.run("SELECT day FROM snapshots WHERE done = 1" + (" AND day < ?" if before else "") + " ORDER BY day DESC LIMIT 1",
                     (before,) if before else ())
    return (rows[0][0], load_day(store, rows[0][0])) if rows else (None, {})


def item_names(store):
    """Every item MeowDB knows, by name: what Discord's item boxes offer."""
    day, _ = snapshot(store)
    return _cache[day][2] if day else []


def med24(s):
    d = s.get("last24h") or {}
    return d.get("median") if d.get("median") and (d.get("count") or 0) >= MIN_REPORTS else None


def is_star(item):
    return (item or {}).get("subtype") == "Throwing Star"


def scans(store, name, hours=72, item=None, reference=None):
    """(asks, sold) a unit for an item over the last hours: per item, but a throwing star's price is its own.
    ETC and USE asks over TROLL times the reference are left out."""
    rows = store.run("SELECT price, bundle, sold FROM scan_rows WHERE item = ? AND epoch >= ?", (name, time.time() - hours * 3600))
    star = is_star(item)
    each = [(p if star else p / max(b or 1, 1), s) for p, b, s in rows if p]
    if reference and (item or {}).get("type") in ("Etc", "Use"):
        each = [(p, s) for p, s in each if s or p <= TROLL * reference]
    return [p for p, s in each if not s], [p for p, s in each if s]


def money(x):
    return "-" if x is None else f"{x:,.0f}"


def change(a, b):
    return None if not a or not b else a / b - 1


def pct(x):
    return "-" if x is None else f"{x * 100:+.0f}%"


def price_text(store, name):
    """/price: MeowDB's numbers and what the scans saw, for one item."""
    day, snap = snapshot(store)
    item, s = snap.get(name, (None, {}))
    d, w = s.get("last24h") or {}, s.get("last7d") or {}
    lines = [f"**{name}**" + (f" (MeowDB, {day})" if day else "")]
    if s:
        lines.append(f"24h median {money(d.get('median'))} from {d.get('count') or 0} reports, 7-day {money(w.get('median'))} "
                     f"({w.get('count') or 0}): {pct(change(d.get('median'), w.get('median')))} on its week")
        if s.get("cheapestSell") or s.get("bestBuy"):
            lines.append(f"cheapest for sale {money(s.get('cheapestSell'))}, best buy offer {money(s.get('bestBuy'))}")
    else:
        lines.append("MeowDB: nothing on it in the latest snapshot")
    asks, sold = scans(store, name, 72, item, med24(s) or w.get("median"))
    if asks or sold:
        lines.append(f"Scans, last 3 days: {len(asks)} asking, median {money(st.median(asks) if asks else None)}, "
                     f"cheapest {money(min(asks) if asks else None)}; {len(sold)} sold, median {money(st.median(sold) if sold else None)}")
    else:
        lines.append("Scans, last 3 days: not seen in any store")
    if (item or {}).get("type") == "Equip":
        lines.append("Equipment: stats set the price, so one median says little")
    if is_star(item):
        lines.append("A throwing star's price is for the set, whatever the amount")
    return "\n".join(lines)


def status_text(store):
    rows = store.run("SELECT name, seen, status FROM bots ORDER BY name")
    if not rows:
        return "No bots have joined the server yet."
    now, lines = time.time(), []
    for name, seen, status in rows:
        st_ = json.loads(status) if status else {}
        ago = "never" if not seen else f"{(now - seen) / 60:.0f} min ago" if now - seen < 7200 else f"{(now - seen) / 3600:.0f} h ago"
        running = ", ".join(st_.get("running") or []) or "nothing running"
        bits = [f"**{name}**: {running}, last seen {ago}"]
        if st_.get("scout_room") and "scout" in (st_.get("running") or []):
            bits.append(f"scouting room {st_['scout_room']}")
        elif st_.get("room"):
            bits.append(f"room {st_['room']}" + (f", {st_['channels_left']} channels left" if st_.get("channels_left") else ""))
        if st_.get("npc_money") is not None:
            bits.append(f"NPC money {money(st_['npc_money'])}" + (f", store money {money(st_['store_money'])}" if st_.get("store_money") else ""))
        if st_.get("held"):
            bits.append(f"{st_['held']} store deals held")
        lines.append(" · ".join(bits))
    return "\n".join(lines)


def _group_change(today, before, pick):
    moves = []
    for name, (item, s) in today.items():
        if name in before and pick(item, s, before[name][1]):
            a, b = med24(s), med24(before[name][1])
            if a and b:
                moves.append(a / b - 1)
    return moves


def high_end(item, s, s_before):
    return item.get("type") == "Use" and max(med24(s) or 0, med24(s_before) or 0) >= HIGH_END


def ores(item, s, s_before):
    return item.get("type") == "Etc" and (item.get("subtype") in ("Ore & Mineral", "Gem") or item["name"].endswith(" Ore"))


def rmt_check(store, day=None):
    """An alert's text if the high-end items jumped together since the last snapshot while ores and gems stayed
    calm (what an RMT wave looked like in early November 2026), else None."""
    day, today = snapshot(store) if day is None else (day, load_day(store, day))
    prev, before = snapshot(store, before=day)
    if not today or not before:
        return None
    top, base = _group_change(today, before, high_end), _group_change(today, before, ores)
    if len(top) < 5:
        return None
    rise, share = st.median(top), sum(m > 0 for m in top) / len(top)
    calm = abs(st.median(base)) < RMT_ORES_CALM if base else True
    if rise >= RMT_RISE and share >= RMT_SHARE and calm:
        return (f"**Possible RMT wave**: items at {HIGH_END:,}+ are up {pct(rise)} since {prev} ({sum(m > 0 for m in top)} of "
                f"{len(top)} rose), while ores and gems moved {pct(st.median(base) if base else None)}. Early November's spike "
                f"looked like this: careful buying high-end items now, and a good time to sell them.")
    return None


def report_text(store, day=None, alert=True):
    """The daily report: short, for a Discord message. alert: with the RMT check's alert in it, if there is one."""
    day, today = snapshot(store) if day is None else (day, load_day(store, day))
    if not today:
        return "No MeowDB snapshot yet."
    prev, before = snapshot(store, before=day)
    since = time.time() - 86400
    visits = store.run("SELECT bot, COUNT(*) FROM records WHERE kind = 'shops' AND epoch >= ? GROUP BY bot", (since,))
    lines = [f"**Market report, {day}**",
             "Scans, last 24h: " + (", ".join(f"{n:,} store visit{'s' if n != 1 else ''} by {b}" for b, n in visits) if visits else "none")]
    if before:
        top, base = _group_change(today, before, high_end), _group_change(today, before, ores)
        if top:
            lines.append(f"High-end items ({HIGH_END:,}+): {pct(st.median(top))} since {prev}, {sum(m > 0 for m in top)} of {len(top)} up"
                         + (f" · ores and gems {pct(st.median(base))}" if base else ""))
    for name in ("Iron Ore", "Elixir"):
        if name in today:
            asks, sold = scans(store, name, 24, today[name][0], med24(today[name][1]))
            w = (today[name][1].get("last7d") or {}).get("median")
            lines.append(f"{name}: {money(med24(today[name][1]))} ({pct(change(med24(today[name][1]), w))} on its week)"
                         + (f", sold at {money(st.median(sold))} in the scans" if sold else ""))
    movers = []
    for name, (item, s) in today.items():
        d, w = s.get("last24h") or {}, (s.get("last7d") or {}).get("median")
        if item.get("type") in ("Etc", "Use") and (d.get("count") or 0) >= MOVER_REPORTS and change(d.get("median"), w) is not None:
            movers.append((change(d.get("median"), w), name, item, s))
    movers.sort()
    picks = [m for m in movers[::-1][:4] if m[0] >= 0.1] + [m for m in movers[:4] if m[0] <= -0.1]
    if picks:
        lines.append("Biggest moves on their week (10+ reports), and what sold in the scans:")
    for move, name, item, s in picks:
        asks, sold = scans(store, name, 24, item, med24(s))
        seen = f" · scans: sold {money(st.median(sold))}" + (f", asking {money(st.median(asks))}" if asks else "") if sold else ""
        lines.append(f"{'▲' if move > 0 else '▼'} {name}: {money(med24(s) or (s.get('last24h') or {}).get('median'))} ({pct(move)}){seen}")
    warning = rmt_check(store, day) if alert else None
    if warning:
        lines.append(warning)
    text = "\n".join(lines)
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE - 1] + "…"
