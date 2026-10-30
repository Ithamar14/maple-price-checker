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

**Custom search.** Double-click **`edit_custom_search.bat`** to open your own list of items to
look out for, for any reason. Put one item name per line and save. The next time the price
checker or crawler starts, listings of those items get a **cyan** box labelled "CUSTOM SEARCH",
and the crawler beeps and keeps the store open a few seconds. A name that isn't a known item
is reported at startup, with the closest match. The list is `custom_search.txt` and stays on
your PC (it isn't uploaded to GitHub).

Prices you checked in game yourself can go in `data/manual_prices.json`
(`"Item Name": mesos`); they override the download.

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

## Crawler (experimental)

`crawler.bat` **takes control of your mouse and keyboard and spends your mesos**. It opens
every player store visible on screen and **buys** single items listed below what an NPC
pays, up to the full stock. After the room it changes channel with the keyboard
(Esc → Enter → arrow keys → Enter), from channel 40 down to 1, and repeats.

**Spending limits:**
- **Auto or confirm.** At startup it asks whether to buy deals automatically or to stop on
  every purchase window, beep, and wait (up to 2 minutes) for you to press OK or Cancel.
  Pass `--auto` or `--confirm` to skip the question.
- **Budget.** It asks for a session budget when it starts (or pass `--budget 250k`) and never
  spends past it. If the full stock costs more than what's left, it buys fewer.
- **Buy rules** in `crawler_settings.json`: the minimum profit per item depends on the price.
  The defaults are +1 under 1,000 mesos, +50 from 1,000, and +500 from 10,000, so it won't
  spend 9,999 to make 1.
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

| option | what it does |
|---|---|
| `--budget 250k` | session budget (otherwise it asks) |
| `--auto` / `--confirm` | buy deals automatically, or wait for you on every purchase (otherwise it asks) |
| `--channels 40-1` | channels to visit after the current room, in order |
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
