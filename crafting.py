"""Crafting profit calculator for MapleStory Classic.

For every recipe in the disciplines chosen in crawler_settings.json ("crafting"), works out
what a craft costs (its meso fee plus the materials at their Free Market price, or crafted
yourself when that's cheaper) against what it sells for (the Free Market median after the 3%
shop fee, or an NPC, whichever pays more). Writes craft_calculator.html and opens it: sort,
filter and change the margin there. For each material it shows the most you can pay and still
keep that margin, with the other materials at their market price.

Recipes come from data/recipes.json (fetch_prices.py), prices from MeowDB (market.py, the
same saved prices and once-a-day rule as the price checker).

Usage:
    python crafting.py                 # disciplines from the settings
    python crafting.py --discipline Smithing --margin 15
"""
import argparse
import json
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import market

HERE = Path(__file__).parent
RECIPES_FILE = HERE / "data" / "recipes.json"
PRICES_FILE = HERE / "data" / "prices.json"
SETTINGS_FILE = HERE / "crawler_settings.json"
OUT_FILE = HERE / "craft_calculator.html"
SHOP_FEE = 0.03  # player shops keep this share of every sale


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text("utf-8")).get("crafting", {})
    except (OSError, ValueError):
        return {}


def wait_for_prices(live, names):
    """Ask for every name's market price and wait until the lookups are done."""
    for n in names:
        live.get(n)
    total = sum(1 for n in names if live.pending([n]))
    if not total:
        return
    print(f"Looking up {total} market prices on MeowDB (one request a second, about {2 * total} seconds)...")
    while live.pending(names):
        time.sleep(1)


def build(recipes, live, npc, min_reports):
    """The data the page works with: per recipe its fee, what it sells for, and every
    material's unit price (market, or crafted when that's cheaper and the recipe is here)."""
    by_output = {}
    for r in recipes:
        by_output.setdefault(r["name"], []).append(r)

    def market_price(name):
        e = live.get(name) or {}
        if not e.get("median_24h"):
            return None
        return {"price": e["median_24h"], "low": e.get("low_24h"), "reports": e.get("reports_24h") or 0,
                "trusted": (e.get("reports_24h") or 0) >= min_reports}

    craft_cache = {}

    def craft_cost(name, seen=()):
        """Cost of crafting one `name` yourself (cheapest recipe here), or None."""
        if name in craft_cache:
            return craft_cache[name]
        if name in seen:  # a recipe that (indirectly) needs its own output
            return None
        best = None
        for r in by_output.get(name, []):
            total = r["fee"]
            for ing in r["ingredients"]:
                unit = unit_price(ing["name"], seen + (name,))
                if unit is None:
                    total = None
                    break
                total += ing["qty"] * unit["price"]
            if total is not None and (best is None or total / r["makes"] < best):
                best = total / r["makes"]
        craft_cache[name] = best
        return best

    def unit_price(name, seen=()):
        bought = market_price(name)
        crafted = craft_cost(name, seen) if name in by_output else None
        if crafted is not None and (bought is None or crafted < bought["price"]):
            return {"price": round(crafted), "source": "craft", "reports": None, "trusted": True}
        if bought:
            return {**bought, "source": "market"}
        return None

    out = []
    for r in recipes:
        sell = market_price(r["name"])
        ingredients = []
        for ing in r["ingredients"]:
            unit = unit_price(ing["name"])
            ingredients.append({"name": ing["name"], "qty": ing["qty"], **(unit or {"price": None})})
        out.append({
            "name": r["name"], "discipline": r["discipline"], "level": r["craft_level"],
            "fee": r["fee"], "makes": r["makes"],
            "market": sell,  # None: no market price
            "npc": npc.get(r["name"]) or 0,
            "ingredients": ingredients,
        })
    return out


def write_page(rows, settings, prices_updated):
    data = {
        "rows": rows,
        "margin": settings["margin"],
        "shopFee": SHOP_FEE,
        "minReports": settings["min_reports"],
        "disciplines": settings["disciplines"],
        "made": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "pricesUpdated": prices_updated,
    }
    blob = json.dumps(data, ensure_ascii=True).replace("</", "<\\/")
    OUT_FILE.write_text(PAGE.replace("/*DATA*/null", blob), "utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--discipline", action="append", help="check this discipline (repeatable)")
    ap.add_argument("--margin", type=float, help="profit margin, percent of what a craft sells for")
    ap.add_argument("--no-open", action="store_true", help="don't open the page in the browser")
    market.add_update_flag(ap)
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True, errors="replace")
    market.update_saved = args.market_update

    if not RECIPES_FILE.exists():
        raise SystemExit("no recipes yet: run  python fetch_prices.py  first")
    cfg = load_settings()
    disciplines = args.discipline or cfg.get("disciplines") or ["Arcforge"]
    settings = {"disciplines": disciplines,
                "margin": args.margin if args.margin is not None else cfg.get("margin_percent", 10),
                "min_reports": cfg.get("min_reports_24h", 3)}

    # the live game's crafting stops at craft level 8; 9 and 10 were only in the test server
    max_level = cfg.get("max_craft_level", 8)
    recipes = [r for r in json.loads(RECIPES_FILE.read_text("utf-8"))["recipes"]
               if r["discipline"].lower() in {d.lower() for d in disciplines} and r["craft_level"] <= max_level]
    if not recipes:
        raise SystemExit(f"no recipes for {', '.join(disciplines)} in {RECIPES_FILE.name}")
    items = json.loads(PRICES_FILE.read_text("utf-8"))["items"]
    ids = {it["name"]: it["id"] for it in items}
    npc = {it["name"]: it["npc_pays"] for it in items if it.get("npc_pays")}
    print(f"{len(recipes)} recipes ({', '.join(disciplines)})")

    live = market.prepare(ids)
    if not live:
        raise SystemExit("the calculator needs market prices")
    names = sorted({r["name"] for r in recipes} | {i["name"] for r in recipes for i in r["ingredients"]})
    wait_for_prices(live, names)

    rows = build(recipes, live, npc, settings["min_reports"])
    updated = live.dump.get("updated")
    write_page(rows, settings, updated)
    print(f"wrote {OUT_FILE.name}")
    if not args.no_open:
        webbrowser.open(OUT_FILE.as_uri())


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crafting Profits</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --ink: #1b1f24; --muted: #5f6b78; --line: #e2e6eb;
  --good: #11804a; --bad: #c0392b; --warn: #a86a00; --accent: #2f6fde; --row-hover: #f0f4fb;
  --chip: #eef1f5;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14171b; --panel: #1c2026; --ink: #e8ebef; --muted: #96a1ae; --line: #2c323a;
    --good: #4cc98a; --bad: #ff7b6b; --warn: #f0b44c; --accent: #6ea2ff; --row-hover: #232a33;
    --chip: #262c34;
  }
}
:root[data-theme="dark"] {
  --bg: #14171b; --panel: #1c2026; --ink: #e8ebef; --muted: #96a1ae; --line: #2c323a;
  --good: #4cc98a; --bad: #ff7b6b; --warn: #f0b44c; --accent: #6ea2ff; --row-hover: #232a33;
  --chip: #262c34;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { color: var(--muted); margin: 0 0 20px; }
.controls { display: flex; flex-wrap: wrap; gap: 12px 20px; align-items: center; background: var(--panel);
  border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; margin-bottom: 14px; }
.controls label { display: flex; align-items: center; gap: 6px; color: var(--muted); }
.controls input[type=number] { width: 70px; }
.controls input[type=search] { width: 200px; }
input, select { font: inherit; color: var(--ink); background: var(--bg); border: 1px solid var(--line);
  border-radius: 6px; padding: 4px 8px; }
.summary { color: var(--muted); margin: 0 0 10px; }
.table-wrap { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 10px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--line); }
th:first-child, td:first-child { text-align: left; white-space: normal; }
th { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted);
  cursor: pointer; user-select: none; position: sticky; top: 0; background: var(--panel); }
th.sorted::after { content: " ▾"; } th.sorted.asc::after { content: " ▴"; }
tbody tr.recipe { cursor: pointer; }
tbody tr.recipe:hover { background: var(--row-hover); }
.num { font-variant-numeric: tabular-nums; }
.good { color: var(--good); font-weight: 600; } .bad { color: var(--bad); }
.muted { color: var(--muted); } .warn, .chip.warn { color: var(--warn); }
.chip { display: inline-block; background: var(--chip); border-radius: 999px; padding: 0 8px; font-size: 12px;
  color: var(--muted); margin-left: 6px; }
tr.detail td { background: var(--bg); padding: 10px 14px 14px; }
tr.detail table { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
tr.detail th { position: static; cursor: default; }
.note { color: var(--muted); font-size: 13px; margin-top: 16px; }
.empty { padding: 30px; text-align: center; color: var(--muted); }
</style>
</head>
<body>
<main>
  <h1>Crafting profits</h1>
  <p class="sub" id="sub"></p>
  <div class="controls">
    <label>Margin <input type="number" id="margin" min="0" max="90" step="1"> %</label>
    <label>Sell to
      <select id="sellTo"><option value="best">whichever pays more</option>
        <option value="market">players (market)</option><option value="npc">an NPC</option></select></label>
    <label><input type="checkbox" id="onlyProfit"> only profitable</label>
    <label><input type="checkbox" id="onlyTrusted"> hide thin prices</label>
    <label><input type="search" id="search" placeholder="Search items or materials"></label>
  </div>
  <p class="summary" id="summary"></p>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th data-key="name">Recipe</th><th data-key="level">Lv</th><th data-key="cost">Cost / craft</th>
        <th data-key="revenue">Sells for</th><th data-key="profit" class="sorted">Profit / craft</th>
        <th data-key="pct">Profit %</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
  <p class="note">
    Cost = the meso fee + every material at its 24-hour market median (or what crafting it here costs, when
    that's cheaper). Sells for = the market median after the <span id="feeNote"></span> shop fee, or what an NPC
    pays. "Buy under" is the most you can pay for that material and still keep your margin, with the other
    materials at their usual price. <span class="warn">Thin</span> means fewer than <span id="minRep"></span>
    market reports in the last 24 hours: treat those prices as rough. Click a recipe for its materials.
  </p>
</main>
<script>
const DATA = /*DATA*/null;
const fmt = n => n == null ? "–" : Math.round(n).toLocaleString("en-US");
const $ = id => document.getElementById(id);
let sortKey = "profit", sortAsc = false;
const open = new Set();

function remember(key, value) { try { localStorage.setItem(key, value); } catch (e) {} }
function recall(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

$("margin").value = recall("craftMargin") ?? DATA.margin;
$("sellTo").value = recall("craftSellTo") ?? "best";
$("feeNote").textContent = Math.round(DATA.shopFee * 100) + "%";
$("minRep").textContent = DATA.minReports;
$("sub").textContent = `${DATA.disciplines.join(", ")} · ${DATA.rows.length} recipes · made ${DATA.made}` +
  (DATA.pricesUpdated ? ` · market prices last updated ${DATA.pricesUpdated.slice(0, 16).replace("T", " ")} UTC` : "");

function evaluate(r) {
  const margin = Math.max(0, Math.min(90, +$("margin").value || 0)) / 100;
  const sellTo = $("sellTo").value;
  const marketEach = r.market ? r.market.price * (1 - DATA.shopFee) : null;
  let each = null, via = null;
  if (sellTo !== "npc" && marketEach != null) { each = marketEach; via = "market"; }
  if (sellTo !== "market" && r.npc && (each == null || r.npc > each)) { each = r.npc; via = "NPC"; }
  const missing = r.ingredients.filter(i => i.price == null);
  const matCost = r.ingredients.reduce((s, i) => s + (i.price ?? 0) * i.qty, 0);
  const cost = missing.length ? null : r.fee + matCost;
  const revenue = each == null ? null : each * r.makes;
  const profit = cost == null || revenue == null ? null : revenue - cost;
  const thin = (via === "market" && !r.market.trusted) || r.ingredients.some(i => i.source === "market" && !i.trusted);
  // most you can pay per unit of one material, others at their price, keeping the margin
  const keep = revenue == null ? null : revenue * (1 - margin) - r.fee;
  const buyUnder = r.ingredients.map(i => keep == null || missing.length ? null
    : (keep - (matCost - (i.price ?? 0) * i.qty)) / i.qty);
  return { ...r, each, via, cost, revenue, profit, pct: profit == null ? null : 100 * profit / revenue,
           thin, missing, buyUnder };
}

function render() {
  remember("craftMargin", $("margin").value); remember("craftSellTo", $("sellTo").value);
  const q = $("search").value.trim().toLowerCase();
  let rows = DATA.rows.map(evaluate).filter(r =>
    (!$("onlyProfit").checked || (r.profit != null && r.profit > 0)) &&
    (!$("onlyTrusted").checked || (!r.thin && r.profit != null)) &&
    (!q || r.name.toLowerCase().includes(q) || r.ingredients.some(i => i.name.toLowerCase().includes(q))));
  rows.sort((a, b) => {
    let x = a[sortKey], y = b[sortKey];
    if (x == null) return 1; if (y == null) return -1;
    if (typeof x === "string") return sortAsc ? x.localeCompare(y) : y.localeCompare(x);
    return sortAsc ? x - y : y - x;
  });
  const profitable = rows.filter(r => r.profit > 0).length;
  $("summary").textContent = `${rows.length} shown · ${profitable} profitable at today's prices`;
  const body = $("rows");
  body.innerHTML = "";
  if (!rows.length) { body.innerHTML = `<tr><td colspan="6" class="empty">Nothing matches.</td></tr>`; return; }
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.className = "recipe";
    const why = r.missing.length ? `no price for ${r.missing.map(i => i.name).join(", ")}`
      : r.revenue == null ? "no selling price" : "";
    tr.innerHTML = `
      <td>${esc(r.name)}${r.makes > 1 ? `<span class="chip">makes ${r.makes}</span>` : ""}
        ${r.thin ? `<span class="chip warn">thin</span>` : ""}${why ? `<div class="muted">${esc(why)}</div>` : ""}</td>
      <td class="num">${r.level}</td>
      <td class="num">${fmt(r.cost)}</td>
      <td class="num">${fmt(r.revenue)}${r.via ? `<div class="muted">${r.via}</div>` : ""}</td>
      <td class="num ${r.profit > 0 ? "good" : r.profit != null ? "bad" : ""}">${r.profit == null ? "–" : (r.profit > 0 ? "+" : "") + fmt(r.profit)}</td>
      <td class="num">${r.pct == null ? "–" : r.pct.toFixed(0) + "%"}</td>`;
    tr.onclick = () => { open.has(r.name) ? open.delete(r.name) : open.add(r.name); render(); };
    body.appendChild(tr);
    if (open.has(r.name)) body.appendChild(detail(r));
  }
  document.querySelectorAll("th").forEach(th => {
    th.classList.toggle("sorted", th.dataset.key === sortKey);
    th.classList.toggle("asc", th.dataset.key === sortKey && sortAsc);
  });
}

function detail(r) {
  const tr = document.createElement("tr");
  tr.className = "detail";
  const lines = r.ingredients.map((i, k) => `<tr>
      <td>${i.qty} × ${esc(i.name)}</td>
      <td class="num">${fmt(i.price)}</td>
      <td class="num">${i.price == null ? "–" : fmt(i.price * i.qty)}</td>
      <td class="muted">${i.source === "craft" ? "crafting it is cheaper" : i.source === "market"
        ? `${i.reports} reports${i.trusted ? "" : " (thin)"}${i.low ? ` · low ${fmt(i.low)}` : ""}` : "no market price"}</td>
      <td class="num ${r.buyUnder[k] > 0 ? "good" : "bad"}">${r.buyUnder[k] == null ? "–" : r.buyUnder[k] > 0 ? fmt(Math.floor(r.buyUnder[k])) : "can't"}</td>
    </tr>`).join("");
  const sell = r.market ? `market median ${fmt(r.market.price)} (${r.market.reports} reports, low ${fmt(r.market.low)})` : "no market price";
  tr.innerHTML = `<td colspan="6">
    <table><thead><tr><th>Material</th><th>Price each</th><th>Subtotal</th><th>Based on</th><th>Buy under (each)</th></tr></thead>
    <tbody>${lines}<tr><td>Crafting fee</td><td></td><td class="num">${fmt(r.fee)}</td><td></td><td></td></tr></tbody></table>
    <p class="muted">Sells: ${sell}${r.npc ? ` · NPC pays ${fmt(r.npc)}` : ""} · ${r.discipline}, craft level ${r.level}</p></td>`;
  return tr;
}

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

document.querySelectorAll("th").forEach(th => th.onclick = () => {
  if (sortKey === th.dataset.key) sortAsc = !sortAsc; else { sortKey = th.dataset.key; sortAsc = th.dataset.key === "name"; }
  render();
});
["margin", "sellTo", "onlyProfit", "onlyTrusted", "search"].forEach(id => $(id).addEventListener("input", render));
render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
