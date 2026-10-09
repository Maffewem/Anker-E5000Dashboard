# Anker E5000 Dashboard

A small self-hosted dashboard for the **Anker SOLIX Solarbank 4 E5000 (Pro)**, in a single Docker container. You don't need Home Assistant or an Anker cloud login.

It reads the battery directly over your local network with **Modbus TCP**, which Anker added for third-party integrations. The data refreshes every 5 seconds, and the container keeps a year of history in a small SQLite database.

![Dashboard screenshot (simulated data)](docs/screenshot.png)

**What it shows**

- Live solar, home load, battery (charge level, charging or discharging) and grid import/export
- Power history over 6 hours, 24 hours, 7 days or 30 days, plus battery level
- Daily energy totals for solar, home use, grid import and grid export, with a table view
- Device details: model, serial, firmware, operating mode, SOC limits and lifetime totals

**It is read-only.** It never writes to the battery, so the Anker app's mode and schedule stay in charge. Anker's own Home Assistant integration does write: it switches the battery into "third-party control" the first time it connects. This dashboard doesn't do that.

## 1. Turn on Modbus TCP in the Anker app

1. Open the Anker app, go to **Devices**, and pick your Solarbank.
2. Tap the gear icon, then **Three-Party Control Settings**.
3. Turn on **Modbus TCP**, and note the **IP address** it shows.

Give the Solarbank a fixed IP address in your router (a DHCP reservation) so it doesn't move.

## 2. Run it

### Portainer

Go to **Stacks › Add stack › Web editor**, paste in [`docker-compose.yml`](docker-compose.yml), set `SOLARBANK_HOST` to the battery's IP, and deploy. Then open `http://<your-pi-or-nas>:8080`.

The image is built for `linux/amd64` and `linux/arm64`, which covers a 64-bit Raspberry Pi OS and most NASes. It is published to `ghcr.io/maffewem/anker-e5000dashboard`. This repository is private, so the image is private too. You have two options:

- In Portainer, add a registry under **Registries › Add registry › Custom**. Use `ghcr.io`, your GitHub username, and a [personal access token](https://github.com/settings/tokens) with the `read:packages` scope.
- Or make the package public, from your GitHub profile under **Packages › anker-e5000dashboard › Package settings**.

Another option is to build on the device. Use **Stacks › Add stack › Repository** with this repo's URL, and change `image:` to `build: .` in the compose file.

### Plain Docker

```sh
docker run -d --name solarbank-dashboard --restart unless-stopped \
  -p 8080:8080 -e SOLARBANK_HOST=192.168.1.50 -e TZ=Europe/London \
  -v solarbank-data:/data ghcr.io/maffewem/anker-e5000dashboard:latest
```

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `SOLARBANK_HOST` | *(required)* | IP address of the Solarbank |
| `SOLARBANK_PORT` | `502` | Modbus TCP port |
| `SOLARBANK_UNIT_ID` | `1` | Modbus unit id |
| `POLL_SECONDS` | `5` | How often to read the battery |
| `RETENTION_DAYS` | `365` | How long to keep history (`0` keeps everything) |
| `TZ` | `UTC` | Timezone used for daily totals |
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail |

History is stored as one row per minute in `/data/solarbank.db`. That comes to roughly 50 MB a year.

## Try it without a battery

```sh
docker compose -f docker-compose.demo.yml up --build
```

This starts a simulated Solarbank next to the dashboard, at http://localhost:8080.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/live` | Latest reading and connection status |
| `GET /api/history?hours=24` | Average power and battery level over time |
| `GET /api/energy?days=14` | kWh per day |
| `GET /api/raw` | Every decoded register, for troubleshooting |
| `GET /healthz` | `200` when connected to the battery, otherwise `503` |

## Troubleshooting

- **"Can't reach battery"**: check that Modbus TCP is on in the app, the IP is right, and the container's host can reach port 502 on the battery. To test from the host, run `nc -vz <ip> 502`.
- **Some values show "–"**: your firmware may not expose every register. `GET /api/raw` shows what was read.
- **Daily totals start from when the dashboard first ran**: they are worked out from the 5-second power readings. The lifetime totals on the device card come straight from the battery.

## Development

```sh
pip install -r requirements-dev.txt
python -m pytest
python -m simulator.sim --port 5020 &
SOLARBANK_HOST=127.0.0.1 SOLARBANK_PORT=5020 DB_PATH=./data/dev.db uvicorn app.main:app --reload --port 8080
```

The register map comes from Anker's MIT-licensed [official Home Assistant integration](https://github.com/anker-charging/ha-anker-solix-official). See [NOTICE.md](NOTICE.md).
