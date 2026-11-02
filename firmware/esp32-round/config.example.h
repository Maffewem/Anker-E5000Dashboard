// Copy to config.h (git-ignored) and fill in. Never commit config.h.
#pragma once
#define WIFI_SSID      "your-wifi"
#define WIFI_PASSWORD  "your-wifi-password"
// Dashboard address on your LAN (or Tailscale). Plain http is fine on a LAN.
#define WIDGET_URL     "http://192.168.0.10:8081/api/widget"
// Same value as WIDGET_TOKEN on the dashboard stack (16+ characters).
#define WIDGET_TOKEN   "paste-the-WIDGET_TOKEN-here"
#define POLL_MS        5000     // how often to fetch
#define AUTO_PAGE_MS   8000     // time per page when auto page is on (ms); 0 = default 8000 and auto starts off
// Password for Wi-Fi uploads (OTA). Arduino IDE asks for it when you upload over the network.
#define OTA_PASSWORD   "choose-a-password"
// Night dimming (optional). Brightness is 0-255; the screen dims from NIGHT_START_HOUR until NIGHT_END_HOUR.
#define DAY_BRIGHTNESS    180
#define NIGHT_BRIGHTNESS  15
#define NIGHT_START_HOUR  22
#define NIGHT_END_HOUR    7
#define TIMEZONE          "GMT0BST,M3.5.0/1,M10.5.0"  // POSIX TZ string; this is UK time with daylight saving
// Automatic updates. "dashboard" = ask your dashboard (needs the dashboard to be updated too); "github" = read the build
// from the repo with a read-only token; "off" = never check. The board checks every UPDATE_CHECK_HOURS.
#define UPDATE_SOURCE        "dashboard"
#define UPDATE_CHECK_HOURS   6
#define GITHUB_REPO          "Maffewem/Anker-E5000Dashboard"
#define GITHUB_BRANCH        "main"
#define GITHUB_TOKEN         ""   // only for UPDATE_SOURCE "github": fine-grained token, repo Contents = read-only
#define DEVICE_NAME          "anker-display"   // name on your router and in Arduino IDE ports; letters, digits, hyphens only
#define LOAD_GREEN_BELOW_W   300    // Overview ring: green below this home use (watts)...
#define LOAD_RED_ABOVE_W     2000   // ...red above this, orange in between
