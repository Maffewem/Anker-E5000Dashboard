# Anker E5000 Dashboard

A self-hosted dashboard for the **Anker SOLIX Solarbank 4 E5000**, with optional **Anker Smart Meter Gen 2** support. It runs as one Docker container and reads both devices directly on your network, so you don't need Home Assistant or an Anker cloud login. It only ever reads and never changes any settings.

![Dashboard screenshot (simulated data)](docs/screenshot.png)

You get live solar, home, battery and grid power, power history, daily energy totals and device details. The container keeps a year of history.

## Setup

**1. Turn on Modbus in the Anker app.** For each device (the Solarbank, and the Smart Meter if you have one), open it in the app, tap the gear icon, then **Three-Party Control Settings**. Turn on **Modbus TCP**, and note the IP address it shows. It helps to give each device a fixed IP in your router.

**2. Deploy the stack in Portainer.** Go to **Stacks › Add stack › Repository**, enter this repo's URL, and deploy. Turn on **Authentication** with your GitHub username and token, since the repo is private. The Compose path is `docker-compose.yml`.

**3. Open the dashboard** at `http://<your-pi-or-nas>:8080` and enter the Solarbank's IP address. To add a Smart Meter, click the gear icon (top right) and choose **Smart Meter**.

The image is private too. To let Portainer pull it, add a registry under **Registries › Add registry › Custom**. Use `ghcr.io`, your GitHub username, and a [token](https://github.com/settings/tokens) with `read:packages`.

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
| `PORT` | `8080` | Port inside the container. Only needed with `docker-compose.host.yml` |

`SOLARBANK_PORT`, `SOLARBANK_UNIT_ID`, `METER_PORT`, `METER_UNIT_ID` and `LOG_LEVEL` also exist, but you'll rarely need them.

## More

- **Try it without hardware:** `docker compose -f docker-compose.demo.yml up --build` runs simulated devices.
- **API:** `/api/live`, `/api/history?hours=24`, `/api/energy?days=14`, `/api/payback`, `/api/raw` (every register, for troubleshooting) and `/healthz`.
- **Development:** `pip install -r requirements-dev.txt && python -m pytest`, then `python -m simulator.sim --meter-port 5021` and `python -m app`.
- The register maps come from Anker's MIT-licensed [official Home Assistant integration](https://github.com/anker-charging/ha-anker-solix-official). See [NOTICE.md](NOTICE.md).
