// Copy to config.h (git-ignored) and fill in. Never commit config.h.
#pragma once
#define WIFI_SSID      "your-wifi"
#define WIFI_PASSWORD  "your-wifi-password"
// Dashboard address on your LAN (or Tailscale). Plain http is fine on a LAN.
#define WIDGET_URL     "http://192.168.0.10:8081/api/widget"
// Same value as WIDGET_TOKEN on the dashboard stack (16+ characters).
#define WIDGET_TOKEN   "paste-the-WIDGET_TOKEN-here"
#define POLL_MS        5000     // how often to fetch
#define AUTO_PAGE_MS   0        // >0 cycles pages by itself (ms), 0 = swipe/tap only
