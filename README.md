# MapleStory Classic price checker

Watches the game once a second. When a player store is open, it reads the listings with OCR
and boxes every single-item listing it recognises in **yellow**, and in **red** any item a player is selling for less than an NPC will pay you for it.
Buying it and selling it to an NPC is guaranteed profit.

It only looks at pixels (like a screenshot) and draws a click-through overlay. It never
reads game memory or sends input, so you keep playing normally.

## Quick start (Windows)

1. Install [Python 3.10–3.12](https://www.python.org/downloads/windows/) and tick
   **"Add python.exe to PATH"** in the installer.
2. Download this repo (green **Code** button → **Download ZIP**) and unzip it.
3. Double-click **`price_checker.bat`**. The first start installs what it needs (1–2 minutes);
   after that it starts right away.

Options can be passed to `price_checker.bat`, e.g. `price_checker.bat --min-profit 500`.

## Price data

`data/prices.json` holds what an NPC pays for 1,300+ items: use, etc, setup, and equipment up
to level 30. It comes from [MapleStory Quest](https://maplestory.quest), which reads it out of
the MapleStory Classic World client (Grand Launch build, 21 October 2026). Thanks to them
for the data.

To refresh it after a game patch, delete `data/prices.json` and run `price_checker.bat`, or run
`.venv\Scripts\python fetch_prices.py`. That takes about 25 minutes. It goes slowly to be
polite to the site, and you can stop it and run it again to continue. Use
`--max-equip-level 50` to keep more equipment, or `0` for none. Many beginner equips
(Sword, Grey T-Shirt…) have no NPC price at all, so they never get a box.

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
- **Budget.** It asks for a session budget when it starts (or pass `--budget 250k`) and never
  spends past it. If the full stock costs more than what's left, it buys fewer.
- **Buy rules** in `crawler_settings.json`: the minimum profit per item depends on the price.
  The defaults are +1 under 1,000 mesos, +50 from 1,000, and +500 from 10,000, so it won't
  spend 9,999 to make 1.
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
- **Time limit.** It stops by itself after `--minutes` (default 30).
- **Front window only.** It only acts while MapleStory is the front window; switch away and
  it pauses.

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
| `--channels 40-1` | channels to visit after the current room, in order |
| `--no-channel-change` | only do the current room |
| `--minutes 30` | session length |

MapleStory runs as administrator, and Windows silently drops clicks and keys sent to it
by normal programs. So `crawler.bat` asks for administrator rights when it starts (the
Windows "allow changes" prompt). The crawler refuses to run without them rather than
clicking into nothing.

## Disclaimer

A fan-made tool, not affiliated with Nexon. Using third-party tools with the game is at your
own risk, and automating input (the crawler) is against the game's rules and can get an
account banned.
