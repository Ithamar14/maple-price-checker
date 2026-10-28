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

## Disclaimer

A fan-made tool, not affiliated with Nexon. It only reads the screen, but using any
third-party tool alongside the game is at your own risk; check the game's terms.
