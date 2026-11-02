# ESP32 round display

Shows battery %, solar, home, grid and today's saving on a Waveshare ESP32-S3 1.75" AMOLED round touch board (466x466). Swipe or tap to change page: Overview, Battery, Solar, Grid, Home, Saved today. It only reads `/api/widget` (see [docs/widget.md](../../docs/widget.md)) and never touches battery control.

**Status: flashed on a real board; display and live data work. Touch and the other pages are not yet confirmed.** Pins, the CO5300 display driver and the CST9217 touch read follow Waveshare's published board layout from memory. Check them against your board's wiki; the pins sit at the top of `esp32-round.ino`.

The mock-up's per-phase, volts and amps pages are not here: the widget endpoint doesn't expose them.

## Setup
1. On the dashboard stack set `WIDGET_TOKEN` (16+ characters) and redeploy.
2. Arduino IDE 2: add the ESP32 board package (3.x), select **ESP32S3 Dev Module**, USB CDC on boot enabled, PSRAM **OPI PSRAM**, flash 16MB.
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
