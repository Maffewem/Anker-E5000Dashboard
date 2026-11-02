# ESP32 round display

Shows battery %, solar, home, grid and today's saving on a Waveshare ESP32-S3 1.75" AMOLED round touch board (466x466). Swipe or tap to change page: Overview, Battery, Solar, Grid, Home, Saved today. It only reads `/api/widget` (see [docs/widget.md](../../docs/widget.md)) and never touches battery control.

**Status: flashed on a real board; display and live data work. Touch and the other pages are not yet confirmed.** Pins, the CO5300 display driver and the CST9217 touch read follow Waveshare's published board layout from memory. Check them against your board's wiki; the pins sit at the top of `esp32-round.ino`.

The mock-up's per-phase, volts and amps pages are not here: the widget endpoint doesn't expose them.

## Setup
1. On the dashboard stack set `WIDGET_TOKEN` (16+ characters) and redeploy.
2. Arduino IDE 2: add the ESP32 board package (3.3.12, the version the build workflow uses; newer 3.x should also work), select **ESP32S3 Dev Module**, USB CDC on boot enabled, PSRAM **OPI PSRAM**, flash 16MB, partition scheme **16M Flash (3MB APP/9.9MB FATFS)** (two app slots, needed for updates).
3. Library Manager: install **GFX Library for Arduino**, **U8g2** (smooth fonts) and **ArduinoJson** (7.x).
4. Copy `config.example.h` to `config.h` and fill in Wi-Fi, the dashboard URL and the token. `config.h` is git-ignored.
5. Check it works first: `curl -H "Authorization: Bearer $WIDGET_TOKEN" http://<pi-ip>:<port>/api/widget`.
6. Upload. The screen shows "wifi", "bad token" or "widget off" in red if something is wrong; the serial monitor is at 115200.

The token travels in plain http on your LAN. Keep the board on the LAN or Tailscale, not a public tunnel.

## Updating over Wi-Fi (OTA)
Flash once over USB first. After that the board appears in Arduino IDE under Tools > Port > Network ports as `anker-display`. Pick it and Upload; enter `OTA_PASSWORD` when asked. The screen shows "Updating..." with a progress ring. Add `OTA_PASSWORD` to your existing `config.h` (see `config.example.h`) before the next USB flash, or it won't compile. Keep the partition scheme on one with two app slots (the default 16MB schemes have them), otherwise OTA fails.
The board and your computer must be on the same network, and anyone on that network with the password can replace the firmware, so use a real one.

## Night dimming
Set `DAY_BRIGHTNESS`, `NIGHT_BRIGHTNESS`, `NIGHT_START_HOUR`, `NIGHT_END_HOUR` and `TIMEZONE` in `config.h` (all optional; defaults dim to 15 from 22:00 to 07:00 UK time). The time comes from the internet (NTP) over Wi-Fi, and until it is known the screen stays at day brightness. Set `NIGHT_BRIGHTNESS` to 0 to turn the screen off at night.

## Auto page on and off
Press the **BOOT** button, or hold a finger still on the screen for one second, to turn auto page on or off. A small "auto" label shows at the top while it is on. The choice is remembered after a restart. `AUTO_PAGE_MS` sets the time per page (default 8000); if it is above 0 in `config.h`, auto starts on the first boot. A touch restarts the timer.

## Status and history
- The Battery page and the overview ring are green while charging and orange while discharging.
- The sparklines now cover the last 3 hours (one point a minute, kept in memory, so a restart clears them).
- Four small bars at the bottom show Wi-Fi signal.
- If the dashboard has never answered since boot, a full screen says "Dashboard unreachable" (or "No Wi-Fi") with the reason, such as "bad token" or "widget off". After the first good reply, a later outage shows the last values with a red message instead.

## Automatic updates (no manual upload)
Every merge that changes `firmware/esp32-round/` is built by the **Firmware** GitHub Action, which commits `esp32-round.bin` and its MD5 to `firmware/dist/`. The board checks every `UPDATE_CHECK_HOURS` (default 6, first check a minute after boot) and installs the build by itself if its MD5 differs from what it runs. Pick the source in `config.h`:

- `UPDATE_SOURCE "dashboard"` (default): the board asks your dashboard at `/api/firmware/esp32-round`, using the same `WIDGET_TOKEN`. The `.bin` is copied into the dashboard image, so a merge reaches the board after you redeploy the stack (Portainer repository stack). No extra secret on the board.
- `UPDATE_SOURCE "github"`: the board reads `firmware/dist/` from the repo directly. Create a fine-grained GitHub token limited to this repo with **Contents: read-only**, and put it in `GITHUB_TOKEN`. It updates minutes after the Action runs, with no redeploy. The board does not verify GitHub's TLS certificate, so keep the token read-only.
- `UPDATE_SOURCE "off"`: never check.

Because the shared build has placeholder settings, the board keeps its **Wi-Fi, URL and tokens in its own memory**: every USB or Arduino IDE upload copies your `config.h` into it, while the Action's build only reads what is already there. So flash once from your computer with a real `config.h`, and after that updates are automatic. Changing a setting means one more upload from your computer.

Limits: there is no automatic rollback, so a broken build needs a USB re-flash. If the Action can't push to `main` (branch protection), allow `github-actions[bot]` to bypass it or the build won't be committed.

## Device name
The board announces itself as `DEVICE_NAME` (default `anker-display`) on your router and in Arduino IDE, instead of `esp32s3-XXXXXX`. Set it in `config.h`; use letters, digits and hyphens only. Your router may keep showing the old name until the board reconnects or you release its lease.

## Overview ring colour
The ring on the Overview page shows the battery percentage by its length and the home's current use by its colour: green below `LOAD_GREEN_BELOW_W` (300), red above `LOAD_RED_ABOVE_W` (2000), orange in between. The Battery page still shows charging as green and discharging as orange.

## Settings web page
Open `http://anker-display.local/` (or the board's IP, shown in your router) in a browser on the same network. Sign in with user `admin` and your `OTA_PASSWORD`. You can see the current numbers, jump to a page, switch auto page on or off, set seconds per page, day and night brightness and hours, the ring colour levels, and how often it checks for updates, or check for an update right now. Changes apply straight away and are remembered after a restart; `config.h` values are only the starting defaults. It uses plain http on your home network, so use a real password and don't expose the board to the internet.
