# MapleStory Classic price checker

Watches the game once a second. When a player store is open, it reads the listings with OCR
and boxes every single-item listing it recognises in **yellow**, and in **light green** any item a player is selling for less than an NPC will pay you for it.
Buying it and selling it to an NPC is guaranteed profit. Every listing also shows its Free
Market median of the last 24 hours (see "Market deals" under the crawler), and gets a
**green** box when listed far enough under it. At startup it asks the same two questions as
the crawler: update saved market prices, and how far under the median counts as a deal
(`--market-percent 30` skips that question, `--no-market` turns market prices off).

**Scrolled equipment (experimental, off unless you pass `--tooltip-slots`).** Hover an
equipment item and it reads "Remaining
Enhancements: N" from the tooltip and compares it with the upgrade slots the item has when
new. If slots were already used (scrolls passed or failed), that line gets a **red** box saying
how many; an item with every slot left gets a green one. The slot counts come from
[MapleStory Quest](https://maplestory.quest) with the NPC prices.

It only looks at pixels (like a screenshot) and draws a click-through overlay. It never
reads game memory or sends input, so you keep playing normally.

## Quick start (Windows)

1. Download this repo (green **Code** button → **Download ZIP**) and unzip it somewhere,
   e.g. your Desktop. (Don't run it from inside the zip.)
2. Double-click **`price_checker.bat`**. If Windows says it protected your PC, click
   **More info → Run anyway**.
3. Start MapleStory. The overlay appears over the game and follows its window.

You don't need to install Python or anything else. The first start sets everything up inside
the folder (about 1 minute and 400 MB, internet needed once); after that it starts in a few
seconds. It installs nothing on the rest of your PC: deleting the folder removes everything.
If something goes wrong, delete the `.venv` and `tools` folders and run it again.

Options can be passed to `price_checker.bat`, e.g. `price_checker.bat --min-profit 500`.

## Price data

`data/prices.json` holds every item's name, and what an NPC pays for it (1,957 of 2,248
items; scrolls and some others have no NPC price, so they only get market prices). It comes from [MapleStory Quest](https://maplestory.quest), which reads it out of
the MapleStory Classic World client (Grand Launch build, 21 October 2026). Thanks to them
for the data.

To refresh it after a game patch, delete `data/prices.json` and run `price_checker.bat`, or run
`.venv\Scripts\python fetch_prices.py`. That takes about 25 minutes. It goes slowly to be
polite to the site, and you can stop it and run it again to continue. Use
`--max-equip-level 50` to leave out equipment above that level, or `0` for none.

**Cut-off names.** The store cuts long names off ("Hat Accuracy Scroll: Great..."). Those are
matched on their start; when that still fits several items, scrolls are told apart by the
colour of their icon (Lesser blue, Intermediate red, Greater gold, Chaos grey). Anything else
that stays ambiguous is checked against the cheapest item it could be, so it's only a deal if
it's one whichever item it is.

**Custom list.** In the launcher's Custom list tab, list items you want pointed out. When the price
checker or crawler starts, listings of those items get a **cyan** box labelled "CUSTOM SEARCH", and
a name that isn't a known item is reported at startup, with the closest match. The list is
`custom_items.json` and stays on your PC.

Prices you checked in game yourself can go in `data/manual_prices.json`
(`"Item Name": mesos`); they override the download.

## The launcher

`launcher.bat` opens one window for everything, so you don't have to go through a console and its
questions each time. It asks for administrator rights once, because the bots need them. What you set
stays set (`launcher_settings.json`).

- **Tabs:** Overlay, Crawler, Full sell loop and Crafting each have their options and Start and Stop
  buttons. The three that use the game's screen run one at a time. What they print shows in the log
  at the bottom.
- **Stop:** a bot lets go of its keys and ends by itself. If it hasn't after 20 seconds it's ended by
  force. Holding ESC for 1 second still works too.
- **MeowDB, at the top:** when a price was last fetched, how many are over 24 hours old, and a button
  to update just those, about one a second. Bots started from here don't update saved prices by
  themselves; they only look up items never seen before. Items MeowDB doesn't know aren't counted.
- **Custom list tab:** search all items and add or remove them, and set each one's price rule, buy limit
  and whether it waits for your OK. It's saved in `custom_items.json`, with how many were bought, and
  changes apply the next time you press Start. What's on the list is never sold at the NPC.

The `.bat` files for each tool still work as before.

## Running it

- It follows the `MapleStory.exe` window automatically. If the game isn't open yet (or is
  closed/minimised), it shows "waiting for maplestory.exe..." and keeps checking every second.
- Any window size works: the store buttons are searched for at different UI sizes.
- Run the game **windowed or borderless**. Exclusive fullscreen hides overlays.
- `F8` pauses and resumes. `Ctrl+C` in the console quits.
- The status line in the top-left corner of the game shows whether a store is open, how many
  listings were read, and how many deals were found. Deals are also printed in the console.

## How it reads a store

1. It looks for the store's **BUY ITEM / LEAVE STORE** buttons with a fast image match
   (`templates/store_buttons.png`, cut from a 2560x1440 screenshot).
2. If found, it OCRs only the item list to the left of the buttons. Each listing is
   "Item Name" over "N for X mesos".
3. Only listings with **N = 1** count. Bundles and greyed-out (sold) rows are ignored.
4. If `NPC pays - asking >= --min-profit`, the listing gets a box.

If stores are never found, run with `--debug`, open a store, and check `debug\last_frame.png`.
`--full-scan` OCRs the whole window when the buttons aren't matched (works, but ~4s per scan).

Useful options:

| option | what it does |
|---|---|
| `--min-profit 500` | only box deals worth at least 500 mesos |
| `--debug` | save `debug/last_frame.png` + `.txt` every scan, showing what OCR saw |
| `--image shot.png` | test on a screenshot instead of the live screen |
| `--scale 1.5` | upscale before OCR if names are misread (slower) |
| `--window "..."` / `--list-windows` | follow a different window |

`printscreens/fullshopscreenshot.jpg` is a sample store screenshot for trying `--image`:

```
.venv\Scripts\python price_checker.py --image printscreens\fullshopscreenshot.jpg
```

## Crafting calculator

Double-click **`craft_calculator.bat`**. It looks up market prices for every recipe's
item and materials (asking first whether to update saved prices, same as the price checker),
then opens **`craft_calculator.html`** in your browser with every recipe:

- **Cost / craft**: the meso fee plus the materials at their 24-hour market median, or what
  crafting a material yourself costs when that's cheaper.
- **Sells for**: the market median after the 3% shop fee, or what an NPC pays, whichever is
  more (or pick one).
- **Profit / craft** and **Profit %**, sortable. Click a recipe for its materials, with a
  **buy under** price for each: the most you can pay for it and still keep your margin.
- Prices based on few market reports are marked **thin**; treat those as rough.

Change the margin, filter and search on the page itself. Which disciplines it checks, the
default margin, how many reports make a price trustworthy and the highest craft level
(8, where the live game stops) are under `crafting` in
`crawler_settings.json` (it starts with Arcforge only). Recipes come from
[MapleStory Quest](https://maplestory.quest)'s item pages (`data/recipes.json`).

## Crawler (experimental)

`crawler.bat` **takes control of your mouse and keyboard and spends your mesos**. It opens
every player store visible on screen and **buys** single items listed below what an NPC
pays, up to the full stock. After the room it changes channel with the keyboard
(Esc → Enter → arrow keys → Enter), from channel 40 down to 1, and repeats.

**Spending limits:**
- **Auto or confirm.** At startup it asks whether to buy deals automatically or to stop on
  every purchase window, beep, and wait (up to 2 minutes) for you to press OK or Cancel.
  Pass `--auto` or `--confirm` to skip the question.
- **NPC only or market too.** It also asks whether to buy only NPC deals. If you say yes, it
  doesn't look up Free Market prices at all, so it never buys a market deal or stops to ask
  about one. Pass `--npc-only` or `--market-deals` to skip the question.
- **Custom list buying.** With `--buy-custom`, or the box on the launcher's tab, items on your list are
  bought by the rules set for each on the launcher's Custom list tab: at least X% under the 24h median,
  up to X% over it, or at or under a fixed price; how many in all, counted across sessions; and whether
  to wait for your OK. When the bot starts it gets the latest price of everything on the list still to be
  bought before opening a shop. A median needs at least `min_reports_24h` reports, and one over 24 hours
  old is looked up again first. A cut-off name that could be something not on the list isn't bought. Items on
  the list aren't bought as NPC or market deals. `--no-custom-buy` only points them out.
- **Budget.** It asks for a session budget when it starts (or pass `--budget 250k`) and never
  spends past it. If the full stock costs more than what's left, it buys fewer.
- **Buy rules** in `crawler_settings.json`: the minimum profit per item depends on the price.
  The defaults are +1 under 1,000 mesos, +50 from 1,000, and +500 from 10,000, so it won't
  spend 9,999 to make 1.
- **Big buys need a real margin** (`big_buy`, default 25% and 5%): an NPC deal costing more than
  25% of the session budget is skipped unless its profit is at least 5% of the asking price.
- **Minimum profit per purchase** (`min_total_profit`, default 50): quantity × profit per item
  must reach it, so one item at +2 is skipped but 30 of them at +2 are bought. For market deals
  the profit is estimated from selling at the 24h median after the 3% shop fee.
- **Never-buy list** in the same file: items whose name contains any of these words are
  always skipped. The default is `"arrow"`, which covers every arrow for bows and crossbows.

**Safety checks:**
- **Second read.** Before buying, it reads the row again at a different size. If the name or
  price disagree, it skips the item.
- **Total-price check.** The game's second confirmation shows the total price. The crawler
  only confirms when it equals quantity × price exactly; otherwise it cancels and nothing is
  spent.
- **Paid-amount check.** After buying, your mesos (shown in the store window) must drop by
  exactly quantity × price. If they don't drop, for example with a full inventory, or drop
  by a different amount, **it stops**.
- **ESC.** Hold ESC for 1 second to stop it at any time.
- **End of the rotation.** It stops by itself after the last channel (40 down to 1 by default)
  and says how long the whole rotation took; the console also shows the time per channel.
  `--minutes 60` adds a time limit on top.
- **Front window only.** It only acts while MapleStory is the front window; switch away and
  it pauses.

**Overlay.** While it runs, it draws over the game what it sees and does, so you can follow along:
the current action in the top-left corner (with budget and spending under it), and a box around
everything it recognises: shop signs (blue, numbered in the order it opens them; the one it's on in
pink), the store window and dialogs (white), every line of text it read (grey), single-item listings
(yellow; light green for NPC deals it will buy, green for market deals, purple for review items),
warnings such as a second read that disagrees (red), the numbers it reads (stock, your mesos, the total; blue),
the selected channel, and a crosshair wherever it clicks. The overlay is hidden from screenshots, so
it never confuses the crawler itself.

**Market deals (asks you first).** Every listing is also compared with Free Market prices from
[MeowDB](https://meowdb.com/msclassic). A listing at least 30% under the item's median price of
the last 24 hours is a market deal, if that median comes from at least 5 player reports. Unlike NPC
deals these aren't guaranteed profit, so unless an item is on the `auto_buy` list (which it
buys like an NPC deal), the crawler sets up the purchase, beeps, and waits on the final purchase
window for you to press OK or Cancel. Every market item bought (auto-bought, confirmed by you, or
bought as an NPC deal) goes into `logs\market_buys.txt` with the price to sell it back at without
losing money after the 3% shop fee.

Prices are looked up live: the first time an item shows up in a store, its prices are fetched
in the background (one request a second) and saved in `data\market_prices.json`, so a green
box can appear a few seconds after a store opens. At startup it says when the saved prices were
last updated and asks whether to update them. **Yes** updates each saved item once, the first
time it's seen that session. **No** keeps every saved price as it is and only looks up items
that were never looked up before. Once every 24 hours is plenty, and keeps the load on MeowDB low.

It doesn't walk around: stand somewhere that shows the shops you want. Everything it
bought or skipped is logged to `logs\crawl_<date>.csv`, with a summary at the end.

Try it without any input first:

```
crawler.bat --dry-run
```

That marks the shops it would open (in order) on `debug\crawler_dry_run.png`. If a store
is open, it also lists what it would buy and whether the second read agrees.

**Rooms.** With their doors saved by `where_am_i.bat` (`data/nav_points.json`), the bot walks straight to a
room's door, and the loop goes through rooms 6-1, 16-13, 12-7 and 20-17. The upper rooms are reached by the
teleporter, used as many times as it takes. Rooms 7-12 and 17-20 have another layout inside: to leave one from
anywhere the bot goes down to the ground, climbs the small ladder under the door and walks right to it. `nav_test.bat` walks the route over and over without selling.

**When it sells.** Only when under `sell_below_budget_percent` of the budget is left (the launcher's **Sell early
under**), not after every room. Stopping with money left leaves what it bought in your bags.

**What's never sold.** Before each sale the full loop reads the item's name in the shop's list. It keeps
anything on your custom list, and anything whose name it can't match to a known item, and says so in
the log. If six kept items fill the rows showing, the rest of that tab stays unsold on that trip.

**Full loop with selling.** `full_sell_loop.bat` buys NPC deals only, without asking. It crawls every
channel in a Free Market room on the bottom floor of the Entrance (a channel it can't get into after
3 tries, as it may be full, is skipped), then walks to the bottom-left NPC in the Henesys weapon store, **sells
everything on the Equip, Use and Etc tabs**, and starts again in the next room with the budget plus
what the sale brought in. It sells early when under 10% of the budget is left
(`sell_below_budget_percent`). Keep nothing you want to keep on those tabs. Keep the minimap on the
setting that shows the map's name. `--rounds 2` stops after two rooms; otherwise hold ESC to stop.
Log: `logs/walk_<date>.log`. Each run of the full sell loop also writes the custom items it bought,
and at what prices, to `logs/custom_buys_<date>_<time>.txt`.

| option | what it does |
|---|---|
| `--budget 250k` | session budget (otherwise it asks) |
| `--auto` / `--confirm` | buy deals automatically, or wait for you on every purchase (otherwise it asks) |
| `--npc-only` / `--market-deals` | buy only NPC deals, or Free Market deals too (otherwise it asks) |
| `--buy-custom` / `--no-custom-buy` | buy custom list items by their own rules, or only point them out (otherwise it asks) |
| `--channels 40-1` | the only channels to visit, in order: a range, a list like `1,3,20,39`, or a mix like `1-5,20` |
| `--rooms 6-1,16-13` | full sell loop: the only rooms to go through, in order, written the same way (default: all 20) |
| `--no-channel-change` | only do the current room |
| `--minutes 60` | also stop after this long (default: no limit) |
| `--no-overlay` | don't draw what it sees and does over the game |

MapleStory runs as administrator, and Windows silently drops clicks and keys sent to it
by normal programs. So `crawler.bat` asks for administrator rights when it starts (the
Windows "allow changes" prompt). The crawler refuses to run without them rather than
clicking into nothing.

## Disclaimer

A fan-made tool, not affiliated with Nexon. Using third-party tools with the game is at your
own risk, and automating input (the crawler) is against the game's rules and can get an
account banned.
