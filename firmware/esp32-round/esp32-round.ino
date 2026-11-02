// Anker Solarbank overview for the Waveshare ESP32-S3 1.75" AMOLED round touch board (466x466).
// Polls the dashboard's read-only /api/widget and draws swipeable pages. Display only: it never writes to the battery.
// Libraries: "GFX Library for Arduino" (moononournation) and "U8g2" (for the smooth fonts). UNTESTED on hardware, see README.md.
#include <Arduino_GFX_Library.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <ArduinoOTA.h>
#include <Preferences.h>
#include <HTTPUpdate.h>
#include <WiFiClientSecure.h>
#include "config.h"

// Optional settings: an older config.h without these still compiles with the defaults.
#ifndef DAY_BRIGHTNESS
#define DAY_BRIGHTNESS 180        // 0-255
#endif
#ifndef NIGHT_BRIGHTNESS
#define NIGHT_BRIGHTNESS 15       // 0-255, 0 = off
#endif
#ifndef NIGHT_START_HOUR
#define NIGHT_START_HOUR 22       // dim from this hour (0-23)...
#endif
#ifndef NIGHT_END_HOUR
#define NIGHT_END_HOUR 7          // ...until this hour. Set both equal to disable dimming.
#endif
#ifndef LOAD_GREEN_BELOW_W
#define LOAD_GREEN_BELOW_W 300    // home use below this: ring is green
#endif
#ifndef LOAD_RED_ABOVE_W
#define LOAD_RED_ABOVE_W 2000     // home use above this: ring is red; orange in between
#endif
#ifndef DEVICE_NAME
#define DEVICE_NAME "anker-display"  // name on your router and in Arduino IDE's network ports (no spaces)
#endif
#ifndef UPDATE_SOURCE
#define UPDATE_SOURCE "dashboard"   // "dashboard", "github" or "off"
#endif
#ifndef UPDATE_CHECK_HOURS
#define UPDATE_CHECK_HOURS 6
#endif
#ifndef GITHUB_REPO
#define GITHUB_REPO "Maffewem/Anker-E5000Dashboard"
#endif
#ifndef GITHUB_BRANCH
#define GITHUB_BRANCH "main"
#endif
#ifndef GITHUB_TOKEN
#define GITHUB_TOKEN ""             // fine-grained token, read-only "Contents" on that repo
#endif
#ifndef TIMEZONE
#define TIMEZONE "GMT0BST,M3.5.0/1,M10.5.0"  // UK time with daylight saving
#endif

// ---- Board pins (Waveshare ESP32-S3-Touch-AMOLED-1.75; check against your board's wiki) ----
#define LCD_CS 12
#define LCD_SCK 38
#define LCD_D0 4
#define LCD_D1 5
#define LCD_D2 6
#define LCD_D3 7
#define LCD_RST 39
#define TP_SDA 15
#define TP_SCL 14
#define TP_INT 11
#define TP_ADDR 0x5A  // CST9217

#define PANEL_COL_OFFSET 6  // this 466x466 CO5300 panel starts 6 columns in; 0 leaves a stray line at the right edge
Arduino_DataBus *bus = new Arduino_ESP32QSPI(LCD_CS, LCD_SCK, LCD_D0, LCD_D1, LCD_D2, LCD_D3);
Arduino_CO5300 *panel = new Arduino_CO5300(bus, LCD_RST, 0, 466, 466, PANEL_COL_OFFSET, 0, 0, 0);
// If a thin stray line shows at an edge, this offset is wrong: try 0 (no offset) or 6.
Arduino_Canvas *gfx = new Arduino_Canvas(466, 466, panel);  // draw off-screen, then flush: no flicker

const int W = 466, C = 233;
const uint16_t BG = 0x0000, DIM = 0x18E3, WHITE = 0xFFFF, YEL = 0xFE60, GRN = 0x2FEA, RED = 0xF9A6, ORG = 0xFD20, PUR = 0xBA1F;
enum { P_OVERVIEW, P_BATTERY, P_SOLAR, P_GRID, P_HOME, P_SAVING, P_COUNT };
int page = 0;
#define BTN_PIN 0  // BOOT button on the board
bool autoPage = false;  // toggled by the BOOT button or a long press; remembered across restarts
unsigned long lastPage = 0;
Preferences prefs;

// Wi-Fi, URL and tokens live in the board's own memory (NVS), so a firmware built in CI (which has placeholder
// values) never overwrites them. A normal USB or Arduino IDE build copies config.h into NVS at boot; a CI build only reads NVS.
struct Cfg { String ssid, pass, url, token, otaPw, updSrc, ghRepo, ghBranch, ghToken; } cfg;
void loadConfig() {
#ifdef CI_BUILD
  cfg.ssid = prefs.getString("ssid"); cfg.pass = prefs.getString("pass"); cfg.url = prefs.getString("url");
  cfg.token = prefs.getString("token"); cfg.otaPw = prefs.getString("otapw"); cfg.updSrc = prefs.getString("updsrc", "dashboard");
  cfg.ghRepo = prefs.getString("ghrepo"); cfg.ghBranch = prefs.getString("ghbranch", "main"); cfg.ghToken = prefs.getString("ghtoken");
#else
  cfg = {WIFI_SSID, WIFI_PASSWORD, WIDGET_URL, WIDGET_TOKEN, OTA_PASSWORD, UPDATE_SOURCE, GITHUB_REPO, GITHUB_BRANCH, GITHUB_TOKEN};
  prefs.putString("ssid", cfg.ssid); prefs.putString("pass", cfg.pass); prefs.putString("url", cfg.url);
  prefs.putString("token", cfg.token); prefs.putString("otapw", cfg.otaPw); prefs.putString("updsrc", cfg.updSrc);
  prefs.putString("ghrepo", cfg.ghRepo); prefs.putString("ghbranch", cfg.ghBranch); prefs.putString("ghtoken", cfg.ghToken);
#endif
}
void setAuto(bool on) { autoPage = on; lastPage = millis(); prefs.putBool("auto", on); }


struct Data { float soc = NAN, solar = NAN, home = NAN, batt = NAN, grid = NAN, saved = NAN; bool online = false; long age = -1; } d;
String err = "starting";
#define HN 180        // history points per series
#define HIST_MS 60000 // one point a minute = the last 3 hours
float hist[P_COUNT][HN]; int hn = 0; unsigned long lastHist = 0;
bool haveData = false;
uint16_t battColour() { return isnan(d.batt) || d.batt < -20 ? GRN : d.batt > 20 ? ORG : DIM; }  // charging green, discharging orange

void fetch() {
  if (WiFi.status() != WL_CONNECTED) { err = "wifi"; return; }
  HTTPClient http;
  http.setTimeout(4000);
  http.begin(cfg.url);
  http.addHeader("Authorization", "Bearer " + cfg.token);
  int code = http.GET();
  if (code != 200) { err = code == 401 ? "bad token" : code == 404 ? "widget off" : "http " + String(code); http.end(); return; }
  JsonDocument j;
  if (deserializeJson(j, http.getString())) { err = "bad json"; http.end(); return; }
  http.end();
  err = ""; haveData = true;
  d.soc = j["soc"] | NAN; d.solar = j["solar_w"] | NAN; d.home = j["home_w"] | NAN;
  d.batt = j["battery_w"] | NAN; d.grid = j["grid_w"] | NAN; d.saved = j["saved_today"] | NAN;
  d.online = j["online"] | false; d.age = j["age_s"] | -1L;
  float v[P_COUNT] = {d.solar, d.soc, d.solar, d.grid, d.home, d.saved};
  if (!lastHist || millis() - lastHist >= HIST_MS) {
    lastHist = millis() ? millis() : 1;
    if (hn == HN) for (int p = 0; p < P_COUNT; p++) memmove(hist[p], hist[p] + 1, (HN - 1) * sizeof(float)); else hn++;
    for (int p = 0; p < P_COUNT; p++) hist[p][hn - 1] = v[p];
  } else for (int p = 0; p < P_COUNT; p++) hist[p][hn - 1] = v[p];  // keep the newest point current
}

String kw(float w) { if (isnan(w)) return "--"; return fabsf(w) >= 1000 ? String(w / 1000, 2) + "kW" : String((int)w) + "W"; }
void ctext(const String &s, int y, int size, uint16_t col) {
  // Smooth U8g2 fonts (needs the "U8g2" library). size 6 = big number, 3 = medium, 2 = small.
  gfx->setFont(size >= 6 ? u8g2_font_logisoso58_tr : size >= 3 ? u8g2_font_helvB24_tf : u8g2_font_helvB14_tf);
  gfx->setTextColor(col);
  int16_t x1, y1; uint16_t w, h; gfx->getTextBounds(s, 0, 0, &x1, &y1, &w, &h);
  gfx->setCursor(C - w / 2 - x1, y - y1); gfx->print(s);
}
void arc(float frac, uint16_t col, int r0, int r1) {  // ring from 7 o'clock, sweeping 270 degrees
  frac = isnan(frac) ? 0 : constrain(frac, 0.f, 1.f);
  // Round dots along the ring: smooth, rounded ends, no angle wrap-around (fillArc left a stray sliver past 360 degrees).
  int rm = (r0 + r1) / 2, rd = (r1 - r0) / 2;
  for (int a = 0; a <= 270; a++) {
    float t = (135 + a) * DEG_TO_RAD;
    gfx->fillCircle(C + rm * cosf(t), C + rm * sinf(t), rd, a <= 270 * frac && frac > 0 ? col : DIM);
  }
}
void spark(int p, uint16_t col) {
  if (hn < 2) return;
  float lo = 1e9, hi = -1e9;
  for (int i = 0; i < hn; i++) { if (isnan(hist[p][i])) continue; lo = min(lo, hist[p][i]); hi = max(hi, hist[p][i]); }
  if (hi <= lo) hi = lo + 1;
  for (int i = 1; i < hn; i++) {
    if (isnan(hist[p][i]) || isnan(hist[p][i - 1])) continue;
    int x0 = 90 + (i - 1) * 286 / (hn - 1), x1 = 90 + i * 286 / (hn - 1);
    gfx->drawLine(x0, 330 - (hist[p][i - 1] - lo) / (hi - lo) * 40, x1, 330 - (hist[p][i] - lo) / (hi - lo) * 40, col);
  }
}
void dots() {
  for (int i = 0; i < P_COUNT; i++) gfx->fillCircle(C + (i - (P_COUNT - 1) / 2.0f) * 22, 440, 4, i == page ? WHITE : DIM);
}
void big(const char *title, const String &v, const String &sub, uint16_t col, int p) {
  ctext(title, 70, 2, WHITE); ctext(v, 150, 6, col); ctext(sub, 235, 2, DIM); spark(p, col);
}

// Overview ring colour follows how much the home is using: green when low, orange in between, red when high.
uint16_t loadColour() { return isnan(d.home) ? DIM : d.home < LOAD_GREEN_BELOW_W ? GRN : d.home > LOAD_RED_ABOVE_W ? RED : ORG; }

void wifiBars() {  // four small bars from the Wi-Fi signal, bottom right of centre
  int rssi = WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : -100;
  int n = rssi > -55 ? 4 : rssi > -65 ? 3 : rssi > -75 ? 2 : rssi > -85 ? 1 : 0;
  for (int i = 0; i < 4; i++) gfx->fillRect(C + 85 + i * 7, 446 - (i + 1) * 5, 5, (i + 1) * 5, i < n ? WHITE : DIM);
}

void drawUnreachable() {
  gfx->fillScreen(BG);
  ctext(WiFi.status() == WL_CONNECTED ? "Dashboard" : "No Wi-Fi", 150, 3, RED);
  ctext(WiFi.status() == WL_CONNECTED ? "unreachable" : "connecting...", 190, 3, RED);
  ctext(err, 250, 2, WHITE);
  ctext("retrying every few seconds", 300, 2, DIM);
  wifiBars();
  gfx->flush();
}

void draw() {
  if (err.length() && !haveData) { drawUnreachable(); return; }
  gfx->fillScreen(BG);
  switch (page) {
    case P_OVERVIEW: {
      arc(d.soc / 100, loadColour(), 202, 222);
      ctext(String(isnan(d.soc) ? 0 : (int)d.soc) + "%", 190, 6, WHITE);
      ctext("sun " + kw(d.solar), 100, 3, YEL);
      ctext("home " + kw(d.home), 270, 3, RED);
      ctext("grid " + kw(d.grid), 320, 3, PUR);
      break; }
    case P_BATTERY: {
      String state = isnan(d.batt) ? "" : d.batt > 20 ? "discharging" : d.batt < -20 ? "charging" : "idle";
      big("Battery", String(isnan(d.soc) ? 0 : (int)d.soc) + "%", kw(d.batt) + " " + state, battColour(), P_BATTERY); break; }
    case P_SOLAR: big("Solar", kw(d.solar), "generating now", YEL, P_SOLAR); break;
    case P_GRID: big("Grid", kw(d.grid), isnan(d.grid) ? "" : d.grid > 0 ? "importing" : "exporting", PUR, P_GRID); break;
    case P_HOME: big("Home", kw(d.home), "using now", RED, P_HOME); break;
    case P_SAVING: big("Saved today", isnan(d.saved) ? "--" : String(d.saved, 2), "from the dashboard tariff", GRN, P_SAVING); break;
  }
  if (autoPage) ctext("auto", 30, 2, DIM);
  if (err.length()) ctext(err, 380, 2, RED);
  else if (!d.online || d.age > 120) ctext("battery offline", 380, 2, RED);
  dots(); wifiBars();
  gfx->flush();
}

// ---- Touch: swipe left/right, tap = next page. Raw CST9217 read, the least certain part of this sketch. ----
bool touchRead(int &x, int &y) {
  if (digitalRead(TP_INT) == HIGH) return false;  // INT is active low while touched
  Wire.beginTransmission(TP_ADDR); Wire.write(0xD0); Wire.write(0x00); Wire.write(0x00);
  if (Wire.endTransmission(false) != 0) return false;
  uint8_t b[7]; if (Wire.requestFrom(TP_ADDR, 7) != 7) return false;
  for (auto &v : b) v = Wire.read();
  if ((b[0] & 0x0F) != 0x06 && (b[0] & 0x0F) != 0x0A) return false;  // not a pressed finger
  x = (b[1] << 4) | (b[3] >> 4); y = (b[2] << 4) | (b[3] & 0x0F);
  return true;
}
int downX = -1, lastX = -1; unsigned long lastTouch = 0, downT = 0; bool longDone = false;
void handleTouch() {
  int x, y;
  if (touchRead(x, y)) {
    if (downX < 0) { downX = x; downT = millis(); longDone = false; }
    lastX = x; lastTouch = millis();
    // Hold still for 1 second: turn auto page on or off.
    if (!longDone && abs(lastX - downX) < 30 && millis() - downT > 1000) { longDone = true; setAuto(!autoPage); draw(); }
    return;
  }
  if (downX >= 0 && millis() - lastTouch > 80) {
    if (!longDone) {
      int dx = lastX - downX;
      if (dx < -60) page = (page + 1) % P_COUNT;
      else if (dx > 60) page = (page + P_COUNT - 1) % P_COUNT;
      else page = (page + 1) % P_COUNT;
      lastPage = millis();  // a touch restarts the auto timer
      draw();
    }
    downX = -1;
  }
}
// BOOT button: press to turn auto page on or off.
void handleButton() {
  static bool was = false; static unsigned long t = 0;
  bool down = digitalRead(BTN_PIN) == LOW;
  if (down && !was && millis() - t > 250) { t = millis(); setAuto(!autoPage); draw(); }
  was = down;
}

// Over-the-air updates: after one USB flash, upload from Arduino IDE (Tools > Port > anker-display) over Wi-Fi.
bool otaOn = false;
void otaStart() {
  ArduinoOTA.setHostname(DEVICE_NAME);
  ArduinoOTA.setPassword(cfg.otaPw.c_str());
  ArduinoOTA.onStart([]() { gfx->fillScreen(BG); ctext("Updating...", 200, 3, WHITE); gfx->flush(); });
  ArduinoOTA.onProgress([](unsigned int done, unsigned int total) {
    arc((float)done / total, GRN, 202, 222); gfx->flush();
  });
  ArduinoOTA.begin();
  otaOn = true;
}

// ---- Automatic updates: the board checks every UPDATE_CHECK_HOURS and installs a newer build by itself. ----
// "dashboard": asks your dashboard (/api/firmware/esp32-round, same token). "github": reads firmware/dist/ from the repo
// with a read-only token. Either way the CI build's MD5 is compared with the running firmware's.
// Security note: GitHub mode talks https without checking the server certificate (setInsecure); the token is read-only.
void updateProgress(int done, int total) { arc((float)done / total, GRN, 202, 222); gfx->flush(); }

bool httpGet(const String &url, const char *auth, bool raw, String &out) {
  bool tls = url.startsWith("https");
  WiFiClientSecure sec; WiFiClient plain; if (tls) sec.setInsecure();
  HTTPClient h; h.setTimeout(8000); h.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  if (!(tls ? h.begin(sec, url) : h.begin(plain, url))) return false;
  if (*auth) h.addHeader("Authorization", String("Bearer ") + auth);
  if (raw) { h.addHeader("Accept", "application/vnd.github.raw+json"); h.addHeader("User-Agent", DEVICE_NAME); }
  int code = h.GET();
  if (code == 200) out = h.getString();
  h.end();
  return code == 200;
}

void checkUpdate() {
  if (cfg.updSrc == "off" || WiFi.status() != WL_CONNECTED) return;
  bool gh = cfg.updSrc == "github";
  String base, md5, body;
  if (gh) {
    if (!cfg.ghToken.length()) return;
    base = "https://api.github.com/repos/" + cfg.ghRepo + "/contents/firmware/dist/esp32-round";
    if (!httpGet(base + ".md5?ref=" + cfg.ghBranch, cfg.ghToken.c_str(), true, body)) return;
    md5 = body;
  } else {
    base = cfg.url; base.replace("/api/widget", "/api/firmware/esp32-round");
    if (!httpGet(base, cfg.token.c_str(), false, body)) return;
    JsonDocument j; if (deserializeJson(j, body)) return;
    md5 = String((const char *)(j["md5"] | ""));
  }
  md5.trim(); md5.toLowerCase();
  if (md5.length() != 32) return;
  // Skip if we already run it, or already installed it once (stops an install loop if the two checksums ever differ).
  if (md5 == ESP.getSketchMD5() || md5 == prefs.getString("lastmd5")) return;

  gfx->fillScreen(BG); ctext("Updating...", 200, 3, WHITE); gfx->flush();
  httpUpdate.rebootOnUpdate(false);
  httpUpdate.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  httpUpdate.onProgress(updateProgress);
  String binUrl = base + ".bin" + (gh ? "?ref=" + cfg.ghBranch : "");
  if (gh) { httpUpdate.addHeader("Authorization", "Bearer " + cfg.ghToken); httpUpdate.addHeader("Accept", "application/vnd.github.raw+json"); httpUpdate.addHeader("User-Agent", "anker-display"); }
  else httpUpdate.addHeader("Authorization", "Bearer " + cfg.token);
  WiFiClientSecure sec; WiFiClient plain; if (gh) sec.setInsecure();
  t_httpUpdate_return r = gh ? httpUpdate.update(sec, binUrl) : httpUpdate.update(plain, binUrl);
  if (r == HTTP_UPDATE_OK) { prefs.putString("lastmd5", md5); ESP.restart(); }
  err = "update failed"; draw();
}

// Dim between NIGHT_START_HOUR and NIGHT_END_HOUR using NTP time. Until the clock is set it stays at day brightness.
int curBright = -1;
void updateBrightness() {
  static unsigned long last = 0;
  if (millis() - last < 30000 && last) return;
  last = millis();
  struct tm t;
  int target = DAY_BRIGHTNESS;
  if (getLocalTime(&t, 0) && NIGHT_START_HOUR != NIGHT_END_HOUR) {
    int h = t.tm_hour;
    bool night = NIGHT_START_HOUR < NIGHT_END_HOUR ? (h >= NIGHT_START_HOUR && h < NIGHT_END_HOUR)
                                                   : (h >= NIGHT_START_HOUR || h < NIGHT_END_HOUR);
    if (night) target = NIGHT_BRIGHTNESS;
  }
  if (target != curBright) { curBright = target; panel->setBrightness(target); }
}

void setup() {
  Serial.begin(115200);
  pinMode(BTN_PIN, INPUT_PULLUP);
  prefs.begin("display", false); loadConfig(); autoPage = prefs.getBool("auto", AUTO_PAGE_MS > 0);
  Wire.begin(TP_SDA, TP_SCL); pinMode(TP_INT, INPUT_PULLUP);
  gfx->begin(); panel->setBrightness(DAY_BRIGHTNESS);
  configTzTime(TIMEZONE, "pool.ntp.org", "time.google.com");
  WiFi.setHostname(DEVICE_NAME);
  WiFi.mode(WIFI_STA); WiFi.begin(cfg.ssid.c_str(), cfg.pass.c_str());
  for (int i = 0; i < 6; i++) for (int p = 0; p < HN; p++) hist[i][p] = NAN;
  draw();
}

void loop() {
  static unsigned long lastPoll = 0;
  unsigned long now = millis();
  if (now - lastPoll >= POLL_MS || lastPoll == 0) { lastPoll = now; fetch(); draw(); }
  if (autoPage && now - lastPage >= (AUTO_PAGE_MS > 0 ? AUTO_PAGE_MS : 8000)) { lastPage = now; page = (page + 1) % P_COUNT; draw(); }
  static unsigned long lastCheck = 0;
  if (millis() > 60000 && (!lastCheck || now - lastCheck >= UPDATE_CHECK_HOURS * 3600000UL)) { lastCheck = now; checkUpdate(); }
  if (!otaOn && WiFi.status() == WL_CONNECTED) otaStart();
  if (otaOn) ArduinoOTA.handle();
  updateBrightness();
  handleButton();
  handleTouch();
  delay(10);
}
