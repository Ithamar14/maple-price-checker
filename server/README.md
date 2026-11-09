# The bots' server

One small program every bot talks to: what each bot is doing, the store scans they all make (so
prices come from all of them), the reference files they share (recipes, equipment stats), events
and tasks. Plain Python with no packages, its data in one SQLite file. The Discord bot will use it
too.

Nothing secret is in this folder: the admin key is set on Railway, and each bot's own key lives in
that PC's `launcher_settings.json`, which isn't uploaded.

## Putting it on Railway

This makes a **new project** of its own. It doesn't touch any other project on your account: don't
open or change those while doing this. Railway bills by what a project uses, on your account's one
bill; this one needs very little (check your usage page after a day or two).

1. **The code on GitHub.** The server deploys from this repository, so push it first.
2. **A new project.** On railway.com: **New** (top right) > **Deploy from GitHub repo** >
   `maple-price-checker`. It creates a project with one service in it.
3. **Point it at this folder.** In that service: **Settings** > **Source** > **Root Directory**:
   `/server`. Under **Deploy**, the start command should read `python server.py` (from
   `railway.json`); type it in if it's empty.
4. **A place for its data.** Right-click the service (or **+ New** in the project) > **Volume**,
   attached to this service, mount path `/data`. Without it, the data is lost on every redeploy.
5. **Its settings.** The service's **Variables**:
   - `DATA_DIR` = `/data`
   - `ADMIN_KEY` = a long random secret. Make one on your PC with
     `.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"` and keep it
     somewhere safe: every new bot needs it once, to join.
6. **An address.** **Settings** > **Networking** > **Generate Domain**. You get something like
   `https://maple-server-production.up.railway.app`.
7. **Check it.** Open `https://<that address>/api/health` in a browser: it should show `"ok": true`.

## Hooking a bot up

In the launcher's **Server** tab: the address from step 6, a name for this bot (like `PC1`), then
**Join the server** and paste the admin key. From then on it syncs every minute in the background,
while the launcher is open; the log shows what went up and down.

## A second PC

1. Get the code: on GitHub, **Code** > **Download ZIP**, and unzip it anywhere (or `git clone`).
2. Run `launcher.bat`. The first time, it sets up its own Python and packages in that folder
   (a few minutes, about 400 MB): nothing needs installing first, and no code editor.
3. **Server** tab: the same address, another name (`PC2`), join with the admin key. Its first
   sync brings down every scan so far, and the recipes and equipment stats.
4. The saved door spots (`data/nav_points.json`) come with the code. If that PC's game window
   isn't 1920x1080, or the rooms aren't walked right, save them again with `where_am_i.bat`.
5. Its custom list and launcher settings are its own: set them in its launcher.

## Running it on your PC instead (to try it)

`.venv\Scripts\python server\server.py` with `ADMIN_KEY` set, then the address
`http://127.0.0.1:8080`. Only that PC can reach it.

Your other PC can reach it over your home network too: on that PC the address is
`http://<this PC's IPv4 address>:8080` (`ipconfig` shows the address). The first time, Windows asks
whether to let Python through the firewall: allow it on private networks. That's enough to run a
scouter and a buyer before the server is on Railway.

## MeowDB's daily numbers

The server pulls MeowDB's numbers for the market report once a day by itself, from 08:00 Israel time
(06:00 UTC; the `PULL_HOUR` variable changes it), about 10 minutes at one request a second. MeowDB
only shows the last day and week at the moment you ask, so this way no day is missed even with every
PC off. Each day takes about 140 KB. The launcher brings new days down into
`data/meowdb_snapshots/` when it syncs, and sends up days only that PC has.

## Discord

The server is also a Discord bot: it posts to one channel, and answers slash commands that Discord sends it over
HTTP (no extra program running anywhere). Until the variables below are set, it's off and the rest works as before.

**Commands.** Everyone in your Discord server: `/status` (what each bot is doing), `/price <item>` (MeowDB and
what the scans saw), `/report` (today's market report). You and whoever you `/allow`: `/buy <item> <max_price>
<amount> [bot]`, `/requests`, `/cancel <id>`. Only you: `/allow @someone`, `/disallow @someone`.

A buy request goes onto a bot's custom list at its next sync, bought automatically at or under the price, up to the
amount, from that bot's custom money. It's only bought while that bot runs with **Custom deals** on. What's bought
stays on the list, so it's never sold at the NPC: hand it over in the game. A running bot picks new requests up
between shops.

**Notifications** go to the channel: bots starting and stopping, errors and bots that stop checking in, scouter
finds bought, buy requests' progress, the day's snapshot, an RMT wave, and the daily report. Each is turned on or
off in the launcher's Server tab, for every bot at once.

**Setting it up**
1. [discord.com/developers/applications](https://discord.com/developers/applications) > **New Application**,
   any name. On its **General Information** page, copy the **Application ID** and the **Public Key**.
2. **Bot** (left) > **Reset Token** > copy the token. It's a password: it only goes into Railway.
3. **OAuth2** > **URL Generator**: tick **bot** and **applications.commands**; under bot permissions, **View
   Channels** and **Send Messages**. Open the URL at the bottom and add the bot to your Discord server.
4. In Discord: **User Settings** > **Advanced** > **Developer Mode** on. Then right-click your server's icon >
   **Copy Server ID**, the channel for notifications > **Copy Channel ID**, and your own name > **Copy User ID**.
5. On Railway, the service's **Variables**: `DISCORD_APP_ID`, `DISCORD_PUBLIC_KEY`, `DISCORD_BOT_TOKEN`,
   `DISCORD_GUILD_ID` (the server ID), `DISCORD_CHANNEL_ID` and `DISCORD_OWNER_ID` (your user ID). Railway
   restarts the server with them; it sets the slash commands up on your Discord server when it starts.
6. Back on the application's **General Information** page, **Interactions Endpoint URL**:
   `https://<your server's address>/discord/interactions` > **Save Changes**. Discord checks it there and then,
   so do this after step 5's restart.
7. In Discord, type `/`: the commands are there. `/allow` the friends who may make buy requests.

## A scouter and a buyer

A scouter (the launcher's **Scouter** tab) goes round the rooms and channels you give it, for as
long as it runs. It buys, sells and judges nothing, and needs no budget. After each store, everything still for sale
there goes to the server, with where the store is and who owns it. Each buyer named in **Send
stores to** gets its own copy, so a scroll bot and an equipment bot can each pick what they want.

A buyer (the full sell loop, with its **Server** tab on) judges the scouter's stores between shops,
by its own rules and money. It goes straight to the store with the most profit in it, finds the
shop by the name on its sign or by its owner, and buys. Then it goes back to the room and channel
it was on. Stores over 10 minutes old are too late. The server keeps a store only for a buyer that's running and asking for stores,
and only for those 10 minutes, so nothing piles up while a buyer is off.

A buyer can also just wait for finds (**Wait for the scouters' finds** on the Full sell loop tab):
it stays where it is, crawls nothing, and only moves to buy what the scouters find, by the same
rules and money. It waits again wherever the last find was, and still goes to sell when the
money runs low. Every PC needs the server joined. The game
windows can be different sizes, as shops are found by name and owner, not by screen position.
