// Anker Solarbank overview for the Waveshare ESP32-S3 1.75" AMOLED round touch board (466x466).
// Polls the dashboard's read-only /api/widget and draws swipeable pages. Display only: it never writes to the battery.
// Library: "GFX Library for Arduino" (moononournation). UNTESTED on hardware, see README.md.
#include <Arduino_GFX_Library.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include "config.h"

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

Arduino_DataBus *bus = new Arduino_ESP32QSPI(LCD_CS, LCD_SCK, LCD_D0, LCD_D1, LCD_D2, LCD_D3);
Arduino_CO5300 *panel = new Arduino_CO5300(bus, LCD_RST, 0, 466, 466);
Arduino_Canvas *gfx = new Arduino_Canvas(466, 466, panel);  // draw off-screen, then flush: no flicker

const int W = 466, C = 233;
const uint16_t BG = 0x0000, DIM = 0x4208, WHITE = 0xFFFF, YEL = 0xFE60, GRN = 0x2FEA, RED = 0xF9A6, PUR = 0xBA1F;
enum { P_OVERVIEW, P_BATTERY, P_SOLAR, P_GRID, P_HOME, P_SAVING, P_COUNT };
int page = 0;

struct Data { float soc = NAN, solar = NAN, home = NAN, batt = NAN, grid = NAN, saved = NAN; bool online = false; long age = -1; } d;
String err = "starting";
float hist[P_COUNT][60]; int hn = 0;  // last ~5 min of each series for the sparkline

void fetch() {
  if (WiFi.status() != WL_CONNECTED) { err = "wifi"; return; }
  HTTPClient http;
  http.setTimeout(4000);
  http.begin(WIDGET_URL);
  http.addHeader("Authorization", "Bearer " WIDGET_TOKEN);
  int code = http.GET();
  if (code != 200) { err = code == 401 ? "bad token" : code == 404 ? "widget off" : "http " + String(code); http.end(); return; }
  JsonDocument j;
  if (deserializeJson(j, http.getString())) { err = "bad json"; http.end(); return; }
  http.end();
  err = "";
  d.soc = j["soc"] | NAN; d.solar = j["solar_w"] | NAN; d.home = j["home_w"] | NAN;
  d.batt = j["battery_w"] | NAN; d.grid = j["grid_w"] | NAN; d.saved = j["saved_today"] | NAN;
  d.online = j["online"] | false; d.age = j["age_s"] | -1L;
  float v[P_COUNT] = {d.solar, d.soc, d.solar, d.grid, d.home, d.saved};
  if (hn == 60) for (int p = 0; p < P_COUNT; p++) memmove(hist[p], hist[p] + 1, 59 * sizeof(float)); else hn++;
  for (int p = 0; p < P_COUNT; p++) hist[p][hn - 1] = v[p];
}

String kw(float w) { if (isnan(w)) return "--"; return fabsf(w) >= 1000 ? String(w / 1000, 2) + "kW" : String((int)w) + "W"; }
void ctext(const String &s, int y, int size, uint16_t col) {
  gfx->setTextSize(size); gfx->setTextColor(col);
  int16_t x1, y1; uint16_t w, h; gfx->getTextBounds(s, 0, 0, &x1, &y1, &w, &h);
  gfx->setCursor(C - w / 2, y); gfx->print(s);
}
void arc(float frac, uint16_t col, int r0, int r1) {  // ring from 7 o'clock, sweeping 270 degrees
  frac = isnan(frac) ? 0 : constrain(frac, 0.f, 1.f);
  gfx->fillArc(C, C, r1, r0, 135, 135 + 270, DIM);
  if (frac > 0) gfx->fillArc(C, C, r1, r0, 135, 135 + 270 * frac, col);
}
void spark(int p, uint16_t col) {
  if (hn < 2) return;
  float lo = 1e9, hi = -1e9;
  for (int i = 0; i < hn; i++) { if (isnan(hist[p][i])) continue; lo = min(lo, hist[p][i]); hi = max(hi, hist[p][i]); }
  if (hi <= lo) hi = lo + 1;
  for (int i = 1; i < hn; i++) {
    if (isnan(hist[p][i]) || isnan(hist[p][i - 1])) continue;
    int x0 = 90 + (i - 1) * 286 / 59, x1 = 90 + i * 286 / 59;
    gfx->drawLine(x0, 330 - (hist[p][i - 1] - lo) / (hi - lo) * 40, x1, 330 - (hist[p][i] - lo) / (hi - lo) * 40, col);
  }
}
void dots() {
  for (int i = 0; i < P_COUNT; i++) gfx->fillCircle(C + (i - (P_COUNT - 1) / 2.0f) * 22, 440, 4, i == page ? WHITE : DIM);
}
void big(const char *title, const String &v, const String &sub, uint16_t col, int p) {
  ctext(title, 70, 2, WHITE); ctext(v, 150, 6, col); ctext(sub, 235, 2, DIM); spark(p, col);
}

void draw() {
  gfx->fillScreen(BG);
  switch (page) {
    case P_OVERVIEW: {
      arc(d.soc / 100, GRN, 205, 225);
      ctext(String(isnan(d.soc) ? 0 : (int)d.soc) + "%", 190, 6, WHITE);
      gfx->setTextSize(3);
      ctext("sun " + kw(d.solar), 110, 3, YEL);
      ctext("home " + kw(d.home), 270, 3, RED);
      ctext("grid " + kw(d.grid), 320, 3, PUR);
      break; }
    case P_BATTERY: {
      String state = isnan(d.batt) ? "" : d.batt > 20 ? "discharging" : d.batt < -20 ? "charging" : "idle";
      big("Battery", String(isnan(d.soc) ? 0 : (int)d.soc) + "%", kw(d.batt) + " " + state, GRN, P_BATTERY); break; }
    case P_SOLAR: big("Solar", kw(d.solar), "generating now", YEL, P_SOLAR); break;
    case P_GRID: big("Grid", kw(d.grid), isnan(d.grid) ? "" : d.grid > 0 ? "importing" : "exporting", PUR, P_GRID); break;
    case P_HOME: big("Home", kw(d.home), "using now", RED, P_HOME); break;
    case P_SAVING: big("Saved today", isnan(d.saved) ? "--" : String(d.saved, 2), "from the dashboard tariff", GRN, P_SAVING); break;
  }
  if (err.length()) ctext(err, 380, 2, RED);
  else if (!d.online || d.age > 120) ctext("battery offline", 380, 2, RED);
  dots();
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
int downX = -1, lastX = -1; unsigned long lastTouch = 0;
void handleTouch() {
  int x, y;
  if (touchRead(x, y)) { if (downX < 0) downX = x; lastX = x; lastTouch = millis(); return; }
  if (downX >= 0 && millis() - lastTouch > 80) {
    int dx = lastX - downX;
    if (dx < -60) page = (page + 1) % P_COUNT;
    else if (dx > 60) page = (page + P_COUNT - 1) % P_COUNT;
    else page = (page + 1) % P_COUNT;
    downX = -1; draw();
  }
}

void setup() {
  Serial.begin(115200);
  Wire.begin(TP_SDA, TP_SCL); pinMode(TP_INT, INPUT_PULLUP);
  gfx->begin(); panel->setBrightness(180);
  WiFi.mode(WIFI_STA); WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  for (int i = 0; i < 6; i++) for (int p = 0; p < 60; p++) hist[i][p] = NAN;
  draw();
}

void loop() {
  static unsigned long lastPoll = 0, lastPage = 0;
  unsigned long now = millis();
  if (now - lastPoll >= POLL_MS || lastPoll == 0) { lastPoll = now; fetch(); draw(); }
  if (AUTO_PAGE_MS > 0 && now - lastPage >= AUTO_PAGE_MS) { lastPage = now; page = (page + 1) % P_COUNT; draw(); }
  handleTouch();
  delay(10);
}
