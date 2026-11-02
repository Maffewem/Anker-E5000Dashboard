// Anker Solarbank overview for the Waveshare ESP32-S3 1.75" AMOLED round touch board (466x466).
// Polls the dashboard's read-only /api/widget and draws swipeable pages. Display only: it never writes to the battery.
// Libraries: "GFX Library for Arduino" (moononournation) and "U8g2" (for the smooth fonts). UNTESTED on hardware, see README.md.
#include <U8g2lib.h>  // included here so the build finds the library; GFX Library for Arduino then enables its smooth fonts
#include <Arduino_GFX_Library.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <ArduinoOTA.h>
#include <Preferences.h>
#include <WebServer.h>
#include <Update.h>
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
#define C565(r, g, b) ((((r) & 0xF8) << 8) | (((g) & 0xFC) << 3) | ((b) >> 3))
// Same colours as the preview page (sun #f6cc2c, battery #2fd47a, home #f2564b, grid #b985f5, orange #ff9a2e).
const uint16_t BG = 0x0000, DIM = 0x18E3, GREY = C565(125, 125, 133), WHITE = C565(232, 232, 234), YEL = C565(246, 204, 44), GRN = C565(47, 212, 122),
               RED = C565(242, 86, 75), ORG = C565(255, 154, 46), PUR = C565(185, 133, 245);
enum { P_OVERVIEW, P_BATTERY, P_SOLAR, P_GRID, P_HOME, P_SAVING, P_COUNT };
int page = 0;
#define BTN_PIN 0  // BOOT button on the board
bool autoPage = false;  // toggled by the BOOT button or a long press; remembered across restarts
unsigned long lastPage = 0;
Preferences prefs;

// Settings you can change from the web page (http://DEVICE_NAME.local/). config.h values are only the defaults.
struct Settings { int dayBr, nightBr, nStart, nEnd, green, red, updHours, autoMs; bool solar; } S;
void loadSettings() {
  S.dayBr = prefs.getInt("dayBr", DAY_BRIGHTNESS); S.nightBr = prefs.getInt("nightBr", NIGHT_BRIGHTNESS);
  S.nStart = prefs.getInt("nStart", NIGHT_START_HOUR); S.nEnd = prefs.getInt("nEnd", NIGHT_END_HOUR);
  S.green = prefs.getInt("green", LOAD_GREEN_BELOW_W); S.red = prefs.getInt("red", LOAD_RED_ABOVE_W);
  S.updHours = prefs.getInt("updHours", UPDATE_CHECK_HOURS); S.autoMs = prefs.getInt("autoMs", AUTO_PAGE_MS > 0 ? AUTO_PAGE_MS : 8000);
  S.solar = prefs.getBool("solar", true);
}
void saveSettings() {
  prefs.putInt("dayBr", S.dayBr); prefs.putInt("nightBr", S.nightBr); prefs.putInt("nStart", S.nStart); prefs.putInt("nEnd", S.nEnd);
  prefs.putInt("green", S.green); prefs.putInt("red", S.red); prefs.putInt("updHours", S.updHours); prefs.putInt("autoMs", S.autoMs); prefs.putBool("solar", S.solar);
}

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
uint16_t battColour() { return isnan(d.batt) || d.batt < -20 ? GRN : d.batt > 20 ? ORG : GREY; }  // charging green, discharging orange

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
  gfx->setFont(size >= 6 ? u8g2_font_logisoso58_tf : size >= 3 ? u8g2_font_helvB24_tf : u8g2_font_helvB14_tf);
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
bool pageOn(int p) { return p != P_SOLAR || S.solar; }  // the Solar page can be switched off on the settings page
int stepPage(int from, int dir) { int p = from; do p = (p + dir + P_COUNT) % P_COUNT; while (!pageOn(p)); return p; }
void dots() {
  int n = 0, at = 0;
  for (int i = 0; i < P_COUNT; i++) if (pageOn(i)) { if (i == page) at = n; n++; }
  for (int i = 0; i < n; i++) gfx->fillCircle(C + (i - (n - 1) / 2.0f) * 22, 440, 4, i == at ? WHITE : DIM);
}
void big(const char *title, const String &v, const String &sub, uint16_t col, int p) {
  ctext(title, 70, 2, WHITE); ctext(v, 150, 6, col); ctext(sub, 235, 2, GREY); spark(p, col);
}

// Overview ring colour follows how much the home is using: green when low, orange in between, red when high.
uint16_t loadColour() { return isnan(d.home) ? DIM : d.home < S.green ? GRN : d.home > S.red ? RED : ORG; }

// Outer ring colour follows the battery level: green from 67%, orange from 34%, red below.
#define USAGE_FULL_W 4000.f  // inner ring is full at this home load
uint16_t ringColour() { return isnan(d.soc) ? DIM : d.soc >= 67 ? GRN : d.soc >= 34 ? ORG : RED; }

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
  ctext("retrying every few seconds", 300, 2, GREY);
  wifiBars();
  gfx->flush();
}

void draw() {
  if (err.length() && !haveData) { drawUnreachable(); return; }
  gfx->fillScreen(BG);
  switch (page) {
    case P_OVERVIEW: {
      arc(d.soc / 100, ringColour(), 188, 208);                          // outer: battery %, coloured by battery power
      arc(isnan(d.home) ? 0 : d.home / USAGE_FULL_W, loadColour(), 166, 180);  // inner: live home use
      ctext(String(isnan(d.soc) ? 0 : (int)d.soc) + "%", 190, 6, WHITE);
      if (S.solar) ctext("sun " + kw(d.solar), 112, 3, YEL);
      ctext("home " + kw(d.home), 296, 3, RED);
      ctext("grid " + kw(d.grid), 342, 3, PUR);
      break; }
    case P_BATTERY: {
      String state = isnan(d.batt) ? "" : d.batt > 20 ? "discharging" : d.batt < -20 ? "charging" : "idle";
      big("Battery", String(isnan(d.soc) ? 0 : (int)d.soc) + "%", kw(d.batt) + " " + state, battColour(), P_BATTERY); break; }
    case P_SOLAR: big("Solar", kw(d.solar), "generating now", YEL, P_SOLAR); break;
    case P_GRID: big("Grid", kw(d.grid), isnan(d.grid) ? "" : d.grid > 0 ? "importing" : "exporting", PUR, P_GRID); break;
    case P_HOME: big("Home", kw(d.home), "using now", RED, P_HOME); break;
    case P_SAVING: big("Saved today", isnan(d.saved) ? "--" : String("\xC2\xA3") + String(d.saved, 2), "from the dashboard tariff", GRN, P_SAVING); break;
  }
  if (autoPage) ctext("auto", 30, 2, GREY);
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
      if (dx > 60) page = stepPage(page, -1);
      else page = stepPage(page, 1);
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
    arc((float)done / total, GRN, 188, 208); gfx->flush();
  });
  ArduinoOTA.begin();
  otaOn = true;
}

// ---- Automatic updates: the board checks every UPDATE_CHECK_HOURS and installs a newer build by itself. ----
// "dashboard": asks your dashboard (/api/firmware/esp32-round, same token). "github": reads firmware/dist/ from the repo
// with a read-only token. Either way the CI build's MD5 is compared with the running firmware's.
// Security note: GitHub mode talks https without checking the server certificate (setInsecure); the token is read-only.
void updateProgress(int done, int total) { arc((float)done / total, GRN, 188, 208); gfx->flush(); }

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

String updMsg = "";  // result of the last update check, shown on the settings page
void checkUpdate() {
  updMsg = "";
  if (cfg.updSrc == "off") { updMsg = "Updates are off (update source is off)."; return; }
  if (WiFi.status() != WL_CONNECTED) { updMsg = "Not connected to Wi-Fi."; return; }
  bool gh = cfg.updSrc == "github";
  String base, md5, body;
  if (gh) {
    if (!cfg.ghToken.length()) { updMsg = "No GitHub token set."; return; }
    base = "https://api.github.com/repos/" + cfg.ghRepo + "/contents/firmware/dist/esp32-round";
    if (!httpGet(base + ".md5?ref=" + cfg.ghBranch, cfg.ghToken.c_str(), true, body)) { updMsg = "Could not reach GitHub, or no build is published yet (check the token and branch)."; return; }
    md5 = body;
  } else {
    base = cfg.url; base.replace("/api/widget", "/api/firmware/esp32-round");
    if (!httpGet(base, cfg.token.c_str(), false, body)) { updMsg = "The dashboard has no firmware to offer yet (it needs redeploying once a build is published), or the token is wrong."; return; }
    JsonDocument j; if (deserializeJson(j, body)) { updMsg = "Unreadable reply from the dashboard."; return; }
    md5 = String((const char *)(j["md5"] | ""));
  }
  md5.trim(); md5.toLowerCase();
  if (md5.length() != 32) { updMsg = "Bad checksum from the update source."; return; }
  // Skip if we already run it, or already installed it once (stops an install loop if the two checksums ever differ).
  if (md5 == ESP.getSketchMD5() || md5 == prefs.getString("lastmd5")) { updMsg = "Already up to date."; return; }

  gfx->fillScreen(BG); ctext("Updating...", 200, 3, WHITE); gfx->flush();
  // Download and flash by hand with HTTPClient + Update, so it needs no HTTPUpdate extras and works on any arduino-esp32 core.
  String binUrl = base + ".bin" + (gh ? "?ref=" + cfg.ghBranch : "");
  WiFiClientSecure sec; WiFiClient plain; if (gh) sec.setInsecure();
  HTTPClient h; h.setTimeout(15000); h.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  if (!(gh ? h.begin(sec, binUrl) : h.begin(plain, binUrl))) { err = "update failed"; updMsg = "Update failed to start."; draw(); return; }
  if (gh) { h.addHeader("Authorization", "Bearer " + cfg.ghToken); h.addHeader("Accept", "application/vnd.github.raw+json"); h.addHeader("User-Agent", DEVICE_NAME); }
  else h.addHeader("Authorization", "Bearer " + cfg.token);
  bool ok = false;
  if (h.GET() == 200) {
    int len = h.getSize();
    if (Update.begin(len > 0 ? len : UPDATE_SIZE_UNKNOWN)) {
      Update.setMD5(md5.c_str());  // refuse the image unless it matches the build's checksum
      Update.onProgress([](size_t done, size_t total) { if (total) updateProgress(done, total); });
      size_t written = Update.writeStream(*h.getStreamPtr());
      ok = (len <= 0 || written == (size_t)len) && Update.end(true);
      if (!ok) Update.abort();
    }
  }
  h.end();
  if (ok) { prefs.putString("lastmd5", md5); ESP.restart(); }
  err = "update failed"; updMsg = "Update download failed."; draw();
}

// ---- Web settings page: http://DEVICE_NAME.local/ (or the board's IP). Sign in as "admin" with OTA_PASSWORD. ----
WebServer web(80);
bool updateNow = false;
int curBrightReset = 0;

int argInt(const char *name, int lo, int hi, int def) {
  if (!web.hasArg(name)) return def;
  return constrain(web.arg(name).toInt(), lo, hi);
}
String field(const char *label, const char *name, int val, const char *hint) {
  return String("<label>") + label + "<input type=number name=" + name + " value=" + val + "><small>" + hint + "</small></label>";
}
// Live round preview on the settings page: draws the same layout as the screen from /state, in the browser.
const char PREVIEW_JS[] = R"JS(
<div id=pv style="width:min(340px,100%);aspect-ratio:1;margin:1rem auto;border-radius:50%;background:#000;border:4px solid #222;overflow:hidden;cursor:pointer"></div>
<p style="text-align:center"><small>Live preview. It redraws as you change the settings below (solar, ring colour levels, day brightness) before you save. Tap it to change page.</small></p>
<script>
var st=null,C={sun:'#f6cc2c',bat:'#2fd47a',home:'#f2564b',grid:'#b985f5',org:'#ff9a2e',dim:'#1c1c21'};
function kw(w){return w==null?'--':Math.abs(w)>=1000?(w/1000).toFixed(2)+'kW':Math.round(w)+'W'}
function arc(r,a0,a1){var p=function(a){a=a*Math.PI/180;return[233+r*Math.cos(a),233+r*Math.sin(a)]},s=p(a0),e=p(a1);return'M'+s+' A'+r+' '+r+' 0 '+((a1-a0)>180?1:0)+' 1 '+e}
function T(x,y,t,sz,c){return'<text x="'+x+'" y="'+y+'" font-size="'+sz+'" font-family="system-ui" text-anchor="middle" fill="'+c+'">'+t+'</text>'}
function big(t,v,sub,c){return T(233,95,t,26,'#e8e8ea')+T(233,205,v,96,c)+T(233,255,sub,26,'#6b6b73')}
function fv(n,def){var e=document.querySelector('[name='+n+']');return e&&e.value!==''?+e.value:def}
function draw(){var d=st,b='';if(!d)return;var gr=fv('green',d.green),rd=fv('red',d.red),so=document.querySelector('[name=solar]');so=so?so.checked:d.solarOn;
var p=d.page,sc=d.soc==null?0:d.soc;
if(!d.online){b=T(233,200,'Dashboard',44,C.home)+T(233,260,'unreachable',44,C.home)}
else if(p==0){var bc=sc>=67?C.bat:sc>=34?C.org:C.home,lc=d.home==null?'#555':d.home<gr?C.bat:d.home>rd?C.home:C.org;
b='<path d="'+arc(198,135,405)+'" stroke="'+C.dim+'" stroke-width="20" fill="none"/><path d="'+arc(198,135,135+270*sc/100)+'" stroke="'+bc+'" stroke-width="20" fill="none" stroke-linecap="round"/><path d="'+arc(173,135,405)+'" stroke="'+C.dim+'" stroke-width="14" fill="none"/>'+(d.home>0?'<path d="'+arc(173,135,135+270*Math.min(d.home/4000,1))+'" stroke="'+lc+'" stroke-width="14" fill="none" stroke-linecap="round"/>':'')+T(233,235,Math.round(sc)+'%',96,'#e8e8ea')+(so?T(233,130,'sun '+kw(d.solar),34,C.sun):'')+T(233,320,'home '+kw(d.home),34,C.home)+T(233,362,'grid '+kw(d.grid),34,C.grid)}
else if(p==1)b=big('Battery',Math.round(sc)+'%',kw(d.batt)+' '+(d.batt<-20?'charging':d.batt>20?'discharging':'idle'),C.bat);
else if(p==2)b=big('Solar',kw(d.solar),'generating now',C.sun);
else if(p==3)b=big('Grid',kw(d.grid),d.grid>0?'importing':'exporting',C.grid);
else if(p==4)b=big('Home',kw(d.home),'using now',C.home);
else b=big('Saved today',d.saved==null?'--':String.fromCharCode(163)+d.saved.toFixed(2),'from the dashboard tariff',C.bat);
var br=fv('dayBr',255);document.getElementById('pv').innerHTML='<svg viewBox="0 0 466 466" width="100%" height="100%" style="opacity:'+Math.max(.15,br/255)+'">'+b+'</svg>'}
function poll(){fetch('/state').then(function(r){return r.json()}).then(function(j){st=j;draw()}).catch(function(){})}
document.getElementById('pv').onclick=function(){if(!st)return;var n=st.page;do{n=(n+1)%6}while(n==2&&!st.solarOn);fetch('/page?n='+n).then(poll)};
document.addEventListener('input',draw);
poll();setInterval(poll,3000);
</script>
)JS";
void webState() {
  if (!web.authenticate("admin", cfg.otaPw.c_str())) return web.requestAuthentication();
  JsonDocument j;
  j["page"] = page; j["online"] = !(err.length() && !haveData); j["solarOn"] = S.solar; j["green"] = S.green; j["red"] = S.red;
  if (!isnan(d.soc)) j["soc"] = d.soc; if (!isnan(d.solar)) j["solar"] = d.solar; if (!isnan(d.home)) j["home"] = d.home;
  if (!isnan(d.batt)) j["batt"] = d.batt; if (!isnan(d.grid)) j["grid"] = d.grid; if (!isnan(d.saved)) j["saved"] = d.saved;
  String out; serializeJson(j, out); web.send(200, "application/json", out);
}
void webRoot() {
  if (!web.authenticate("admin", cfg.otaPw.c_str())) return web.requestAuthentication();
  String h = F("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Anker display</title>"
               "<style>body{font:16px system-ui;background:#0b0b0d;color:#e8e8ea;max-width:30rem;margin:1rem auto;padding:0 1rem}"
               "label{display:block;margin:.7rem 0}input{display:block;width:100%;padding:.5rem;font:inherit;margin-top:.2rem;background:#1c1c21;color:#e8e8ea;border:1px solid #333;border-radius:.3rem;box-sizing:border-box}"
               "input[type=checkbox]{width:auto;display:inline}small{color:#999}button,a.b{display:inline-block;padding:.6rem 1rem;margin:.3rem .3rem .3rem 0;"
               "background:#2fd47a;color:#04210f;border:0;font:inherit;font-weight:700;border-radius:.3rem;text-decoration:none}</style>");
  h += "<h2>Anker display</h2>"; h += PREVIEW_JS; h += "<p>Battery " + String(isnan(d.soc) ? 0 : (int)d.soc) + "%, home " + kw(d.home) + ", solar " + kw(d.solar) +
       ", grid " + kw(d.grid) + "<br><small>Wi-Fi " + String(WiFi.RSSI()) + " dBm, up " + String(millis() / 60000) + " min, firmware " + ESP.getSketchMD5().substring(0, 8) + "</small></p>";
  h += "<p>Show page: ";
  const char *names[] = {"Overview", "Battery", "Solar", "Grid", "Home", "Saved"};
  for (int i = 0; i < P_COUNT; i++) if (pageOn(i)) h += String("<a class=b href='/page?n=") + i + "'>" + names[i] + "</a>";
  h += "</p><form method=post action=/save>";
  h += String("<label><input type=checkbox name=solar value=1") + (S.solar ? " checked" : "") + "> Show solar (the sun line and Solar page)</label>";
  h += String("<label><input type=checkbox name=auto value=1") + (autoPage ? " checked" : "") + "> Auto page</label>";
  h += field("Seconds per page", "autoS", S.autoMs / 1000, "");
  h += field("Day brightness", "dayBr", S.dayBr, "0 to 255");
  h += field("Night brightness", "nightBr", S.nightBr, "0 to 255, 0 = off");
  h += field("Night starts (hour)", "nStart", S.nStart, "0 to 23");
  h += field("Night ends (hour)", "nEnd", S.nEnd, "0 to 23; same as start = no dimming");
  h += field("Ring green below (W)", "green", S.green, "home use");
  h += field("Ring red above (W)", "red", S.red, "orange in between");
  h += field("Check for update every (hours)", "updHours", S.updHours, "1 to 168");
  h += "<button>Save</button></form><form method=post action=/update><button>Check for update now</button></form>";
  if (updMsg.length()) h += "<p><b>Last check:</b> " + updMsg + "</p>";
  web.send(200, "text/html", h);
}
void webSave() {
  if (!web.authenticate("admin", cfg.otaPw.c_str())) return web.requestAuthentication();
  S.dayBr = argInt("dayBr", 0, 255, S.dayBr); S.nightBr = argInt("nightBr", 0, 255, S.nightBr);
  S.nStart = argInt("nStart", 0, 23, S.nStart); S.nEnd = argInt("nEnd", 0, 23, S.nEnd);
  S.green = argInt("green", 0, 20000, S.green); S.red = argInt("red", 0, 20000, S.red);
  S.updHours = argInt("updHours", 1, 168, S.updHours); S.autoMs = argInt("autoS", 2, 600, S.autoMs / 1000) * 1000;
  S.solar = web.hasArg("solar"); if (!pageOn(page)) page = P_OVERVIEW;
  saveSettings(); setAuto(web.hasArg("auto")); curBrightReset = 1; draw();
  web.sendHeader("Location", "/"); web.send(303);
}
void webPage() {
  if (!web.authenticate("admin", cfg.otaPw.c_str())) return web.requestAuthentication();
  page = argInt("n", 0, P_COUNT - 1, page); lastPage = millis(); draw();
  web.sendHeader("Location", "/"); web.send(303);
}
void webUpdate() {
  if (!web.authenticate("admin", cfg.otaPw.c_str())) return web.requestAuthentication();
  updateNow = true;
  web.sendHeader("Location", "/"); web.send(303);
}
void webStart() {
  if (!cfg.otaPw.length()) return;  // no password set: no page
  web.on("/", webRoot); web.on("/save", HTTP_POST, webSave); web.on("/page", webPage); web.on("/state", webState); web.on("/update", HTTP_POST, webUpdate);
  web.begin();
}

// Dim between the night start and end hours using NTP time. Until the clock is set it stays at day brightness.
int curBright = -1;
void updateBrightness() {
  static unsigned long last = 0;
  if (curBrightReset) { curBrightReset = 0; last = 0; curBright = -1; }
  if (millis() - last < 30000 && last) return;
  last = millis();
  struct tm t;
  int target = S.dayBr;
  if (getLocalTime(&t, 0) && S.nStart != S.nEnd) {
    int h = t.tm_hour;
    bool night = S.nStart < S.nEnd ? (h >= S.nStart && h < S.nEnd)
                                                   : (h >= S.nStart || h < S.nEnd);
    if (night) target = S.nightBr;
  }
  if (target != curBright) { curBright = target; panel->setBrightness(target); }
}

void setup() {
  Serial.begin(115200);
  pinMode(BTN_PIN, INPUT_PULLUP);
  prefs.begin("display", false); loadConfig(); loadSettings(); autoPage = prefs.getBool("auto", AUTO_PAGE_MS > 0);
  Wire.begin(TP_SDA, TP_SCL); pinMode(TP_INT, INPUT_PULLUP);
  gfx->begin(); gfx->setUTF8Print(true);  // lets the fonts print characters like the pound sign
  panel->setBrightness(S.dayBr);
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
  if (autoPage && now - lastPage >= S.autoMs) { lastPage = now; page = stepPage(page, 1); draw(); }
  static unsigned long lastCheck = 0;
  if (millis() > 60000 && (!lastCheck || now - lastCheck >= S.updHours * 3600000UL)) { lastCheck = now; checkUpdate(); }
  if (!otaOn && WiFi.status() == WL_CONNECTED) { otaStart(); webStart(); }
  if (otaOn) web.handleClient();
  if (updateNow) { updateNow = false; checkUpdate(); }
  if (otaOn) ArduinoOTA.handle();
  updateBrightness();
  handleButton();
  handleTouch();
  delay(10);
}
