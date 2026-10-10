# Anker E5000 Dashboard

A self-hosted dashboard for the **Anker SOLIX Solarbank 4 E5000**, with optional **Anker Smart Meter Gen 2** support. It runs as one Docker container and reads both devices directly on your network, so you don't need Home Assistant or an Anker cloud login. It only reads, unless you turn on [Battery control](#battery-control).

![Dashboard screenshot (simulated data)](docs/screenshot.png)

You get live solar, home, battery and grid power, power history, daily energy totals and device details. The container keeps a year of history.

## Setup

**1. Turn on Modbus in the Anker app.** For each device (the Solarbank, and the Smart Meter if you have one), open it in the app, tap the gear icon, then **Three-Party Control Settings**. Turn on **Modbus TCP**, and note the IP address it shows. It helps to give each device a fixed IP in your router.

**2. Deploy the stack in Portainer.** Go to **Stacks › Add stack › Repository**, enter this repo's URL, and deploy. Turn on **Authentication** with your GitHub username and token, since the repo is private. The Compose path is `docker-compose.yml`.

**3. Open the dashboard** at `http://<your-pi-or-nas>:8080` and enter the Solarbank's IP address. To add a Smart Meter, click the gear icon (top right) and choose **Smart Meter**.

The image is private too. To let Portainer pull it, add a registry under **Registries › Add registry › Custom**. Use `ghcr.io`, your GitHub username, and a [token](https://github.com/settings/tokens) with `read:packages`.

**Updating.** Each merge to `main` publishes a new `:latest` image once its CI run is green. In Portainer, open the stack, click **Pull and redeploy**, and turn on **Re-pull image** (without it Portainer reuses the image it already has). The dashboard footer shows the version and build date (for example `v1.0.58 · 9 Oct 2026`; hover for the commit), and the container log prints `Solarbank dashboard version …` at startup. Each build on `main` is also tagged with its version, so you can pin one instead of `:latest`. To start a new series, change `VERSION` (major.minor) or push a `v1.2.3` tag.

## If something goes wrong

| Problem | Fix |
|---|---|
| "Port is already allocated" when deploying | Add the stack environment variable `HOST_PORT` = `8090` (or any free port), and open that port instead |
| Setup says **no network route** | Set the stack's Compose path to `docker-compose.host.yml`, which uses host networking |
| Setup says **port 502 is closed** | Modbus TCP is off in the Anker app |
| Setup says **no answer** | The IP is wrong, or the device is offline |

## Settings

You don't need to edit the compose file. Set any of these as **Environment variables** on the Portainer stack.

| Variable | Default | What it does |
|---|---|---|
| `HOST_PORT` | `8080` | Port you open the dashboard on |
| `TZ` | `Europe/London` | Timezone for daily totals |
| `RETENTION_DAYS` | `365` | Days of history to keep (`0` keeps everything) |
| `POLL_SECONDS` | `5` | How often to read the devices |
| `SOLARBANK_HOST`, `METER_HOST` | – | Set an address here instead of on the setup screen. The setup screen then shows it read-only |
| `OCTOPUS_API_KEY`, `OCTOPUS_ACCOUNT` | – | Your Octopus Energy API key and account number, instead of entering them with **Energy supplier** is asked in first-run setup and can be changed in **Edit costs**: Octopus Energy, E.ON Next, EDF, British Gas or Other. Only Octopus has a price feed, so the Octopus parts (Connect Octopus, Intelligent Go slots) only show when it's picked. With any other supplier you type your prices into **Edit costs**, where the supplier's EV tariffs fill in their off-peak hours, and those prices drive the payback and battery control. The tariff comparison works for everyone. Existing installs count as Octopus if it's connected, otherwise Other.

**Connect Octopus** on the dashboard |
| `CONTROL_LIVE` | `0` | Set to `1` to let **Battery control** write to the battery. Until then it's a dry run that only logs what it would do |
| `ADMIN_PASSWORD` | – | Locks every change (settings, battery control, mode, schedules, Octopus, costs) and the full backup behind this password. Viewing stays open. See **Security** below |
| `READ_ONLY` | `false` | `true` turns every change off, even for you. Recording and battery control carry on with the settings they already have |
| `PORT` | `8080` | Port inside the container. Only needed with `docker-compose.host.yml` |
| `RELAY_METER` | `false` | `true` shares the Smart Meter with Home Assistant (see below) |
| `RELAY_HOST_PORT` | `502` | Port the Smart Meter relay is published on |

`SOLARBANK_PORT`, `SOLARBANK_UNIT_ID`, `METER_PORT`, `METER_UNIT_ID` and `LOG_LEVEL` also exist, but you'll rarely need them.

## Octopus Energy

**Connect Octopus** on the dashboard (or the variables above) reads your tariff from your account: Agile, Go, Intelligent Go, Cosy, Flux, Tracker and fixed tariffs, plus your export tariff. It's read-only and never changes your account or the battery.

- The battery payback then uses the price of each half hour you actually paid, including Agile's changing prices and Intelligent Go's extra smart-charge slots, so you only need to enter what the battery cost.
- **Electricity prices** shows the upcoming prices and suggests when charging the battery from the grid is worth it, when to run the house from the battery, and the best export times.
- Find your API key on octopus.energy under **Account > Personal details > API access**. It's stored in the data volume (`settings.json`), readable only inside the container.

## Using the Smart Meter in Home Assistant too

The Smart Meter accepts only one Modbus TCP connection at a time, so the dashboard and Home Assistant can't both connect to it. The relay fixes that: the dashboard keeps the meter's one connection and answers Home Assistant with the same registers, on port 502, as if it were the meter.

1. In Home Assistant, delete the Smart Meter from Anker's integration (Settings > Devices & services), so it lets go of the meter.
2. Add the stack environment variable `RELAY_METER` = `true` and redeploy. The logs say "Relaying the Smart Meter read-only on Modbus TCP port 5020" (502 outside the container).
3. In Home Assistant, add the Anker SOLIX integration again and enter **this host's IP address** instead of the meter's. It's recognised as the Smart Meter.

The relay is read-only: it never sends anything to the meter, and Home Assistant gets an error if it tries to change a setting. If the dashboard loses the meter, Home Assistant shows it unavailable until the dashboard reconnects. Its readings are as fresh as the dashboard's last poll (`POLL_SECONDS`). Use the normal `docker-compose.yml` for this; with `docker-compose.host.yml` the relay can only listen on `RELAY_PORT` (5020), which Home Assistant's add screen doesn't accept. The Solarbank itself accepts several connections, so it doesn't need a relay.

## Battery control

Off by default. When switched on in **Battery control**, the dashboard takes over the battery during cheap hours (your Octopus cheap windows and Intelligent Go slots, or the off-peak hours in **Edit costs**):

- **Hold:** the battery doesn't discharge, so the house runs on cheap grid power and the stored energy is kept for the dear hours.
- **Grid charge (optional):** charges at the power you choose until it reaches your stop level (90% by default).
- **Intelligent Go smart-charge slots (on by default):** when Octopus adds a slot to charge your car, even a short one in the middle of the day (say 13:20-13:40), the whole home pays the off-peak price, so the battery charges to your stop level and doesn't discharge until the slot ends. Slots are re-read from Octopus every 2 minutes, so ones added or cancelled at short notice are picked up. They beat a discharge schedule but not a charge one.

**Your schedules** let you set your own windows: charge (at a power, up to a level), hold, or discharge (at a power, down to a floor), each on the days you pick. They win over cheap hours, and work on their own if the cheap-hour options are unticked. **Battery mode** switches the battery to one of the Anker app's modes straight away. Every change is recorded in the event log.

To do this it puts the battery in Anker's third-party control mode. Outside cheap hours, when you switch control off, or when the container stops, it writes back the mode the battery was in before (Smart, Self-consumption and so on). If you change the mode here or in the Anker app, the dashboard stands back until the current window ends. Nothing is written unless `CONTROL_LIVE=1` is set; without it, the **Activity** list shows what it would have done.

**Battery care** gives tips from the battery's limits and history: time spent full or empty, charge and discharge limits, and cycles so far.

## Security

Out of the box the dashboard trusts everyone who can reach it, which is fine on your home network. Anyone who can open it can also change the battery's mode and schedules, so before you make it reachable from anywhere else:

- **Set `ADMIN_PASSWORD`** on the stack (a long one, it's the only thing between the internet and your battery). The dashboard is still viewable, but a padlock appears at the top and the edit buttons disappear until you sign in with it. Signing in lasts 30 days, or until the container restarts. After 5 wrong passwords from one address, that address has to wait 15 minutes. Or **set `READ_ONLY=true`** if nobody should change anything from the dashboard at all; change the stack's variables to make changes instead.
- **Put it behind a reverse proxy with HTTPS** (Nginx Proxy Manager, Caddy, Traefik or a Cloudflare Tunnel), and publish only that. Without HTTPS the password crosses the internet in plain text. Don't forward the dashboard's port (`HOST_PORT`) or the relay's (`RELAY_HOST_PORT`, 502) straight from your router.
- Better still, keep it private and reach it over a VPN such as Tailscale or WireGuard, or put the proxy's own login (Cloudflare Access, Authelia) in front as well.

What's protected either way: the Octopus API key is never sent to the browser (viewers see only the start of the account number). Changes need a session cookie that other sites can't use, plus a per-session token, so another web page can't make changes on your behalf, and the dashboard can't be shown inside another site's frame. The Smart Meter relay only answers reads. The container runs as a normal user with no extra Linux privileges.

## More

- **Event log:** the dashboard logs when the Solarbank starts or stops charging or discharging (once the new state has lasted a minute), changes mode, charge or discharge limit, backup reserve or firmware, and when either device connects, drops or gets a new address. Changes made while the dashboard was stopped are logged when it starts again.
- **Export:** the Export data card at the bottom of the dashboard downloads readings as CSV or JSON for any date range, or a full backup of the database.
- **Try it without hardware:** `docker compose -f docker-compose.demo.yml up --build` runs simulated devices.
- **API:** `/api/live`, `/api/history?hours=24`, `/api/energy?days=14`, `/api/runtime` (when the battery runs empty or fills), `/api/payback`, `/api/octopus`, `/api/compare` (tariff comparison), `/api/control`, `/api/battery-care`, `/api/export?data=minutes&start=2026-01-01&end=2026-01-31` (also `daily`, `meter`, `slots`, `events`; add `&format=json` for JSON), `/api/export/backup` (the whole SQLite database), `/api/events?kind=charging,mode` (the event log, newest first), `/api/raw` (every register, for troubleshooting) and `/healthz`.
- **Development:** `pip install -r requirements-dev.txt && python -m pytest`, then `python -m simulator.sim --meter-port 5021` and `python -m app`.
- The register maps come from Anker's MIT-licensed [official Home Assistant integration](https://github.com/anker-charging/ha-anker-solix-official). See [NOTICE.md](NOTICE.md).
