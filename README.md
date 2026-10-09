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

Go to **Stacks › Add stack › Web editor**, paste in [`docker-compose.yml`](docker-compose.yml) and deploy. Then open `http://<your-pi-or-nas>:8080`.

The first time you open it, a setup screen asks for the battery's IP address. It tests the connection, shows the model and serial it finds, and saves the address in the data volume. To change it later, use the gear button at the top right.

If port 8080 is already in use on your machine (Portainer reports "port is already allocated"), change the first number in the `ports:` line, for example `"8090:8080"`, and open `http://<your-pi-or-nas>:8090`.

If the setup screen says there's **no network route** to the battery, the container can't see your home network from Docker's default bridge network. Switch the stack to host networking: remove the `ports:` section and add `network_mode: host`. With host networking there's no port mapping, so if 8080 is taken, set `PORT` (for example `PORT: "8090"`) to choose the port the dashboard listens on.

The image is built for `linux/amd64` and `linux/arm64`, which covers a 64-bit Raspberry Pi OS and most NASes. It is published to `ghcr.io/maffewem/anker-e5000dashboard`. This repository is private, so the image is private too. You have two options:

- In Portainer, add a registry under **Registries › Add registry › Custom**. Use `ghcr.io`, your GitHub username, and a [personal access token](https://github.com/settings/tokens) with the `read:packages` scope.
- Or make the package public, from your GitHub profile under **Packages › anker-e5000dashboard › Package settings**.

Another option is to build on the device. Use **Stacks › Add stack › Repository** with this repo's URL, and change `image:` to `build: .` in the compose file.

### Plain Docker

```sh
docker run -d --name solarbank-dashboard --restart unless-stopped \
  -p 8080:8080 -e TZ=Europe/London \
  -v solarbank-data:/data ghcr.io/maffewem/anker-e5000dashboard:latest
```

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `SOLARBANK_HOST` | *(unset)* | Optional. Sets the battery IP here instead of on the setup screen, which then shows it read-only |
| `SOLARBANK_PORT` | `502` | Modbus TCP port |
| `SOLARBANK_UNIT_ID` | `1` | Modbus unit id |
| `POLL_SECONDS` | `5` | How often to read the battery |
| `RETENTION_DAYS` | `365` | How long to keep history (`0` keeps everything) |
| `PORT` | `8080` | Port the dashboard listens on inside the container (mainly for `network_mode: host`) |
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
| `GET/POST /api/settings` | The saved battery address (POST tests it, then saves it) |
| `GET /healthz` | `200` when connected to the battery, otherwise `503` |

## Troubleshooting

- **"Can't reach battery"**: the red banner gives the reason.
  - *No network route*: the address isn't on a network the container can reach. Check the IP, or use `network_mode: host`.
  - *Port 502 is closed*: the device is there, but Modbus TCP is off in the Anker app.
  - *No answer*: the IP is probably wrong, or the battery is offline.
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
