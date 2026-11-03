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
- The Battery page is green while charging and orange while discharging.
- The sparklines now cover the last 3 hours (one point a minute, kept in memory, so a restart clears them).
- Four small bars at the bottom show Wi-Fi signal.
- If the dashboard has never answered since boot, a full screen says "Dashboard unreachable" (or "No Wi-Fi") with the reason, such as "bad token" or "widget off". After the first good reply, a later outage shows the last values with a red message instead.

## Automatic updates (no manual upload)
Every merge that changes `firmware/esp32-round/` is built by the **Firmware** GitHub Action, which commits `esp32-round.bin` and its MD5 to `firmware/dist/`. The board checks every `UPDATE_CHECK_HOURS` (default 6, first check a minute after boot) and installs the build by itself if its MD5 differs from what it runs. Pick the source in `config.h`:

- `UPDATE_SOURCE "dashboard"` (default): the board asks your dashboard at `/api/firmware/esp32-round`, using the same `WIDGET_TOKEN`. The `.bin` is copied into the dashboard image, so a merge reaches the board after you redeploy the stack (Portainer repository stack). No extra secret on the board.
- `UPDATE_SOURCE "github"`: the board reads `firmware/dist/` from the repo directly. Create a fine-grained GitHub token limited to this repo with **Contents: read-only**, and put it in `GITHUB_TOKEN`. It updates minutes after the Action runs, with no redeploy. The board does not verify GitHub's TLS certificate, so keep the token read-only.
- `UPDATE_SOURCE "off"`: never check.

Because the shared build has placeholder settings, the board keeps its **Wi-Fi, URL and tokens in its own memory**: every USB or Arduino IDE upload copies your `config.h` into it, while the Action's build only reads what is already there. So flash once from your computer with a real `config.h`, and after that updates are automatic. Changing a setting means one more upload from your computer.

Limits: there is no automatic rollback, so a broken build needs a USB re-flash. Firmware work lives on the `esp32-dev` branch, and the Action publishes each build there, so `main` and the Docker image never change because of the display. Open firmware pull requests against `esp32-dev`. To get updates from it, set the display's update source to GitHub with branch `esp32-dev`. If the Action can't push to `esp32-dev` (branch protection), allow `github-actions[bot]` to bypass it.

## Device name
The board announces itself as `DEVICE_NAME` (default `anker-display`) on your router and in Arduino IDE, instead of `esp32s3-XXXXXX`. Set it in `config.h`; use letters, digits and hyphens only. Your router may keep showing the old name until the board reconnects or you release its lease.

## Overview rings
The outer ring shows the battery percentage by its length. Its colour follows the level: green from 67%, orange from 34%, red below 34%. The thin inner ring is live home use, full at 4 kW, coloured green below `LOAD_GREEN_BELOW_W` (300), red above `LOAD_RED_ABOVE_W` (2000), orange in between.

## Settings web page
Open `http://anker-display.local/` (or the board's IP, shown in your router) in a browser on the same network. Sign in with user `admin` and your `OTA_PASSWORD`. You can see the current numbers, jump to a page, switch auto page on or off, set seconds per page, switch the solar line and page off if you have no panels, show a dim clock at night, set the battery size and the "Battery limited" level, see a live round preview of the screen, day and night brightness and hours, the ring colour levels, and how often it checks for updates, or check for an update right now. Changes apply straight away and are remembered after a restart; `config.h` values are only the starting defaults. It uses plain http on your home network, so use a real password and don't expose the board to the internet.

## Core updates
The Arduino core is part of the firmware, so updating it means building and installing new firmware. The **Core update** workflow checks weekly for a newer stable arduino-esp32 release and opens a PR that bumps `ARDUINO_ESP32` in `.github/workflows/firmware.yml`, then builds that branch so you can see if it compiles. Merge it and the board installs the result like any update. It needs the repo setting Actions > General > "Allow GitHub Actions to create and approve pull requests". Merging is still your decision, because a broken build has no automatic rollback.

## Time left, alerts, tariff, self-sufficiency
- **Time left**: the Battery page shows "2h 47m left" while discharging and "full in 1h 10m" while charging, worked out on the board from the battery level, its power averaged over about 10 minutes (so it doesn't jump about), rounded to 5 minutes, shown as ">24h" when very long, and the battery size set on the settings page (default 5000 Wh, usable energy). With solar switched off, the Overview shows it at the top.
- **Alert banner**: a red pill at the top says "Battery offline" when the dashboard hasn't had fresh battery data, or "Battery limited" when the level is at or below the alert level (default 10%).
- **Tariff pill**: the Overview shows "cheap until 05:30" (or "peak until ...") from the dashboard's tariff. Nothing shows on a flat tariff.
- **Self-sufficiency**: a page showing the share of today's home use that didn't come from the grid.
- **Night clock**: tick it on the settings page to show a dim clock during the night hours instead of the pages. A touch shows the pages for 15 seconds. It needs the clock from the internet.

The tariff pill and self-sufficiency need the dashboard from this version (`/api/widget` gained `rate_p`, `rate_kind`, `rate_until` and `self_sufficiency`).

## Update safety net (automatic go-back)
After the board installs an update it is "on trial". It is confirmed once Wi-Fi is up and data has come from the dashboard. If that doesn't happen within the confirm time (default 180 seconds, changeable on the settings page), it goes back to the previous firmware by itself. The same happens if it restarts three times while still on trial, which catches a crash loop. If Wi-Fi is up but the dashboard is simply unreachable when the time runs out, it keeps the new firmware, since that isn't the firmware's fault. The settings page says when a new firmware is on trial or when it has gone back, and a build it went back from isn't installed again; the next different build is.

This needs the older firmware to still be in the other app slot. It can't help the first USB flash, and it can't save a build so broken that it never reaches the point of counting its restarts (for example one that fails before the screen starts). That still needs a USB flash.

When the dashboard is recent enough, the time-left line comes from the dashboard's own runtime estimate instead, which follows how the battery is usually used and the next planned charge: "lasts to 05:30", "empty 21:40", "full 14:20", "80% 07:00" or "at reserve". Without it (an older dashboard, or no estimate yet) the board falls back to its own averaged calculation.

## Charging indicator

While the battery is charging (more than 20 W in), the Overview ring breathes slowly and a small blue bolt shows above the percentage. The ring keeps its level colour (red when low), so you can see both "low" and "recovering". It redraws about five times a second and only on the Overview page. Switch it off on the settings page ("Breathing ring and bolt while the battery is charging"). The settings preview shows the same effect.
