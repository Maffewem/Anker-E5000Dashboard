# ESP32 round display

Shows battery %, solar, home, grid and today's saving on a Waveshare ESP32-S3 1.75" AMOLED round touch board (466x466). Swipe or tap to change page: Overview, Battery, Solar, Grid, Home, Saved today. It only reads `/api/widget` (see [docs/widget.md](../../docs/widget.md)) and never touches battery control.

**Status: untested on hardware.** Pins, the CO5300 display driver and the CST9217 touch read follow Waveshare's published board layout from memory. Check them against your board's wiki; the pins sit at the top of `esp32-round.ino`.

The mock-up's per-phase, volts and amps pages are not here: the widget endpoint doesn't expose them.

## Setup
1. On the dashboard stack set `WIDGET_TOKEN` (16+ characters) and redeploy.
2. Arduino IDE 2: add the ESP32 board package (3.x), select **ESP32S3 Dev Module**, USB CDC on boot enabled, PSRAM **OPI PSRAM**, flash 16MB.
3. Library Manager: install **GFX Library for Arduino** and **ArduinoJson** (7.x).
4. Copy `config.example.h` to `config.h` and fill in Wi-Fi, the dashboard URL and the token. `config.h` is git-ignored.
5. Check it works first: `curl -H "Authorization: Bearer $WIDGET_TOKEN" http://<pi-ip>:<port>/api/widget`.
6. Upload. The screen shows "wifi", "bad token" or "widget off" in red if something is wrong; the serial monitor is at 115200.

The token travels in plain http on your LAN. Keep the board on the LAN or Tailscale, not a public tunnel.
