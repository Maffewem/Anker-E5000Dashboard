"use strict";

// ---------- Trends ----------
// Weekly or monthly totals from the half-hour history, with the rest of the
// current period projected from recent days, plus this month's outlook, the
// payback over time and the measured battery capacity.

let trends = null, trendsChart, paybackChart, tempChart;
let trendView = "energy", trendPeriod = "day";
try {
  trendView = localStorage.getItem("trend-view") || trendView;
  trendPeriod = localStorage.getItem("trend-period") || trendPeriod;
} catch (_) {}

const TREND_SERIES = {
  energy: [["Solar", "--solar", "solar"], ["Home", "--home", "home"], ["Grid import", "--gridpower", "import"], ["Grid export", "--export", "export"]],
  money: [["Grid cost", "--gridpower", "import_cost"], ["Export earned", "--export", "export_earned"], ["Battery saved", "--battery", "battery_saved"]],
};

const shortDate = (iso, opts = { day: "numeric", month: "short" }) => new Date(`${iso}T12:00:00`).toLocaleDateString([], opts);
const periodLabel = (p) => (trends.period === "month" ? shortDate(p.start, { month: "short", year: "2-digit" }) : shortDate(p.start));

function trendBars() {
  const ps = trends.periods;
  const bar = (label, color, data, extra = {}) => ({ label, data, backgroundColor: color, stack: extra.stack || label,
    borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", barPercentage: 0.85, categoryPercentage: 0.8, ...extra });
  const sets = [];
  for (const [label, color, key] of TREND_SERIES[trendView]) {
    sets.push(bar(label, css(color), ps.map((p) => p[key])));
    // The rest of the current period, stacked on top in a paler shade.
    const rest = ps.map((p) => (p.projected ? Math.max(0, p.projected[key] - p[key]) : null));
    if (rest.some((v) => v)) sets.push(bar(`${label} · projected`, `${css(color)}55`, rest, { projection: true, stack: label }));
  }
  const unit = trendView === "money" ? (v) => money(v) : (v) => `${v.toFixed(v >= 100 ? 0 : 1)} kWh`;
  const opts = baseOptions();
  opts.scales.x.stacked = true;
  opts.scales.y.stacked = true;
  opts.scales.y.ticks.callback = trendView === "money" ? (v) => `£${v}` : (v) => `${v} kWh`;
  opts.plugins.legend.labels.filter = (item, data) => !data.datasets[item.datasetIndex].projection;
  opts.plugins.tooltip.filter = (item) => item.raw != null;
  opts.plugins.tooltip.callbacks = { label: (c) => `${c.dataset.label}: ${unit(c.parsed.y)}` };
  return { type: "bar", data: { labels: ps.map(periodLabel), datasets: sets }, options: opts };
}

function trendBattery() {
  const ps = trends.periods, h = trends.health;
  const opts = baseOptions();
  opts.scales.y.ticks.callback = (v) => `${v} kWh`;
  // Capacity changes by a few percent over years, so don't start the axis at zero.
  if (h.rated_kwh) { opts.scales.y.min = Math.floor(h.rated_kwh * 0.7); opts.scales.y.max = Math.ceil(h.rated_kwh * 1.05 * 2) / 2; }
  opts.scales.y2 = { position: "right", beginAtZero: true, ticks: { color: css("--text-muted") }, grid: { display: false }, border: { display: false } };
  opts.plugins.tooltip.filter = (item) => item.raw != null;
  opts.plugins.tooltip.callbacks = {
    label: (c) => (c.dataset.yAxisID === "y2" ? `Cycles: ${c.parsed.y.toFixed(1)}` : `${c.dataset.label}: ${c.parsed.y.toFixed(2)} kWh`),
  };
  const sets = [
    { type: "line", label: "Measured capacity", data: h.periods.map((p) => p.capacity_kwh), borderColor: css("--battery"),
      backgroundColor: css("--battery"), borderWidth: 2, pointRadius: 3, spanGaps: true, order: 0 },
    { type: "bar", label: "Cycles", yAxisID: "y2", data: h.periods.map((p) => p.cycles), backgroundColor: `${css("--battery")}40`,
      borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", order: 2 },
  ];
  if (h.rated_kwh) {
    sets.push({ type: "line", label: "Rated capacity", data: ps.map(() => h.rated_kwh), borderColor: css("--text-muted"),
      borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, order: 1 });
  }
  return { type: "bar", data: { labels: ps.map(periodLabel), datasets: sets }, options: opts };
}

// Solar output against the sunshine it had, for the last weeks of days.
function weatherBars() {
  const rows = trends.conditions.slice(-(window.innerWidth < 600 ? 14 : 30));
  const opts = baseOptions();
  opts.scales.y.ticks.callback = (v) => `${v} kWh`;
  opts.scales.y2 = { position: "right", beginAtZero: true, ticks: { color: css("--text-muted"), callback: (v) => `${v}` }, grid: { display: false }, border: { display: false },
    title: { display: true, text: "Sunshine kWh/m²", color: css("--text-muted") } };
  opts.plugins.tooltip.filter = (item) => item.raw != null;
  opts.plugins.tooltip.callbacks = {
    label: (c) => (c.dataset.yAxisID === "y2" ? `Sunshine: ${c.parsed.y.toFixed(1)} kWh/m²` : `Solar: ${c.parsed.y.toFixed(1)} kWh`),
  };
  return { type: "bar", data: { labels: rows.map((r) => shortDate(r.day)), datasets: [
    { type: "bar", label: "Solar", data: rows.map((r) => r.solar_kwh), backgroundColor: css("--solar"), borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", order: 2 },
    { type: "line", label: "Sunshine", yAxisID: "y2", data: rows.map((r) => r.sun_kwh_m2), borderColor: css("--text-muted"), backgroundColor: css("--text-muted"),
      borderWidth: 2, pointRadius: 2, spanGaps: true, order: 0 },
  ] }, options: opts };
}

function tempLines() {
  const rows = trends.conditions;
  const opts = baseOptions();
  const h = trends.health;
  opts.scales.y.ticks.callback = (v) => `${v} kWh`;
  if (h.rated_kwh) { opts.scales.y.min = Math.floor(h.rated_kwh * 0.7); opts.scales.y.max = Math.ceil(h.rated_kwh * 1.05 * 2) / 2; }
  opts.scales.y2 = { position: "right", ticks: { color: css("--text-muted"), callback: (v) => `${v}°` }, grid: { display: false }, border: { display: false } };
  opts.plugins.tooltip.filter = (item) => item.raw != null;
  opts.plugins.tooltip.callbacks = {
    label: (c) => (c.dataset.yAxisID === "y2" ? `Temperature: ${c.parsed.y.toFixed(1)}°C` : `Capacity: ${c.parsed.y.toFixed(2)} kWh`),
  };
  return { type: "line", data: { labels: rows.map((r) => shortDate(r.day)), datasets: [
    { label: "Measured capacity", data: rows.map((r) => r.capacity_kwh), borderColor: css("--battery"), backgroundColor: css("--battery"),
      showLine: false, pointRadius: 4, order: 0 },
    { label: "Temperature", yAxisID: "y2", data: rows.map((r) => r.temp_avg), borderColor: css("--gridpower"), backgroundColor: css("--gridpower"),
      borderWidth: 2, pointRadius: 0, tension: 0.3, spanGaps: true, order: 1 },
  ] }, options: opts };
}

function renderInsights() {
  const i = trends.insights;
  const pct = (v) => (v == null ? null : `${v}%`);
  factList($("trends-insights"), i.days ? [
    ["Home use covered without the grid", pct(i.self_sufficiency)],
    ["Solar used or stored (not exported)", pct(i.solar_used)],
    ["Battery saved", money(i.battery_saved)],
    ["Net grid cost", money(i.net_cost)],
    ...(i.standing_cost ? [["…of which standing charge", money(i.standing_cost)]] : []),
    ["Over", `the last ${i.days} recorded day${i.days === 1 ? "" : "s"}`],
  ] : []);
}

const fmtVal = (v, unit, digits = 0) => (v == null ? null : `${v.toFixed(digits)}${unit}`);

function renderConditions() {
  const rows = trends.conditions;
  const recent = rows.filter((r) => r.solar_kwh != null).slice(-30);
  const max = (key) => recent.reduce((m, r) => (r[key] != null && (m == null || r[key] > m.v) ? { v: r[key], day: r.day } : m), null);
  const peak = (key, label) => { const m = max(key); return [label, m ? `${Math.round(m.v)} W on ${shortDate(m.day)}` : null]; };
  const volts = recent.filter((r) => r.voltage_min != null);
  const yields = recent.filter((r) => r.solar_yield != null);
  const effect = trends.temperature_effect;
  factList($("trends-conditions"), [
    ["Solar yield", yields.length ? `${(yields.reduce((a, r) => a + r.solar_yield, 0) / yields.length).toFixed(1)} kWh per kWh/m² of sunshine (last ${yields.length} days; a fall over time can mean dirty or shaded panels)` : "Needs a location and a few days of weather"],
    ["Capacity when cold and mild", effect ? `${effect.cold_kwh} kWh at about ${effect.cold_temp}°C, ${effect.warm_kwh} kWh at about ${effect.warm_temp}°C` : "Needs a wider range of temperatures over a few weeks"],
    peak("solar_peak_w", "Highest solar"), peak("home_peak_w", "Highest home use"), peak("import_peak_w", "Highest grid import"),
    peak("charge_peak_w", "Fastest charge"), peak("discharge_peak_w", "Fastest discharge"),
    ["Grid voltage (Smart Meter)", volts.length ? `${Math.min(...volts.map((r) => r.voltage_min)).toFixed(0)}–${Math.max(...volts.map((r) => r.voltage_max)).toFixed(0)} V` : null],
  ]);
}

function drawTrends() {
  if (!trends) return;
  const isWeather = trendView === "weather";
  $("trends-weather").hidden = !isWeather;
  for (const b of document.querySelectorAll("[data-trend-view]")) b.setAttribute("aria-pressed", String(b.dataset.trendView === trendView));
  for (const b of document.querySelectorAll("[data-trend-period]")) b.setAttribute("aria-pressed", String(b.dataset.trendPeriod === trendPeriod));
  const since = trends.first_day ? `Recorded since ${shortDate(trends.first_day, { day: "numeric", month: "short", year: "numeric" })}.` : "Nothing recorded yet.";
  $("trends-note").textContent = trendView === "battery"
    ? `${since} Measured usable capacity, and full cycles (energy discharged ÷ rated capacity) per ${trends.period}.`
    : `${since} The paler part of the latest bar is the rest of this ${trends.period}, projected from the last ${trends.month.recent_days} days.`;
  if (isWeather) {
    $("trends-note").textContent = `${since} Daily solar against the sunshine the day had.`;
    $("trends-weather-note").textContent = trends.has_weather
      ? "Measured capacity needs a long discharge, so only some days have a dot. The Solarbank 4 reports neither battery temperature nor cell voltages over Modbus, so outdoor temperature is shown instead."
      : "Enter your location to fetch weather. Only the rounded latitude and longitude are sent to Open-Meteo.";
    tempChart?.destroy();
    tempChart = trends.has_weather ? new Chart($("temp-chart"), tempLines()) : null;
    $("temp-chart").parentElement.hidden = !trends.has_weather;
    renderConditions();
  }
  trendsChart?.destroy();
  trendsChart = new Chart($("trends-chart"), isWeather ? weatherBars() : trendView === "battery" ? trendBattery() : trendBars());
  renderInsights();
  drawPaybackCurve();
  renderTrendFacts();
}

function drawPaybackCurve() {
  const p = trends.payback;
  $("trends-payback").hidden = !(p.cost > 0 && p.curve.length);
  if ($("trends-payback").hidden) return;
  const at = (iso) => new Date(`${iso}T12:00:00`).getTime();
  const last = p.curve[p.curve.length - 1];
  const sets = [{ label: "Saved so far", data: p.curve.map((c) => ({ x: at(c.date), y: c.saved })), borderColor: css("--battery"),
    backgroundColor: css("--battery"), borderWidth: 2, pointRadius: 0, tension: 0.2 }];
  let end = at(last.date);
  if (p.recent_date && p.recent_date > last.date) {
    sets.push({ label: "Projected", data: [{ x: at(last.date), y: last.saved }, { x: at(p.recent_date), y: p.cost }],
      borderColor: css("--battery"), borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0 });
    end = at(p.recent_date);
  }
  const start = at(p.curve[0].date);
  sets.push({ label: "Battery cost", data: [{ x: start, y: p.cost }, { x: end, y: p.cost }], borderColor: css("--text-muted"),
    borderDash: [2, 3], borderWidth: 1.5, pointRadius: 0 });
  const opts = baseOptions();
  opts.scales.x.type = "linear";
  opts.scales.x.min = start;
  opts.scales.x.max = end;
  const long = end - start > 400 * 86400e3;
  opts.scales.x.ticks.callback = (v) => new Date(v).toLocaleDateString([], long ? { month: "short", year: "numeric" } : { day: "numeric", month: "short" });
  opts.scales.y.beginAtZero = true;
  opts.scales.y.ticks.callback = (v) => `£${v}`;
  opts.plugins.tooltip.callbacks = {
    title: (items) => (items.length ? new Date(items[0].parsed.x).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" }) : ""),
    label: (c) => `${c.dataset.label}: ${money(c.parsed.y)}`,
  };
  paybackChart?.destroy();
  paybackChart = new Chart($("payback-chart"), { type: "line", data: { datasets: sets }, options: opts });
}

function renderTrendFacts() {
  const m = trends.month;
  const monthName = new Date(`${m.month}-15T12:00:00`).toLocaleDateString([], { month: "long" });
  $("trends-month-title").textContent = `This month (${monthName})`;
  const fmt = { kwh: (v) => kwh(v), gbp: (v) => (v == null ? "–" : money(v)) };
  const row = (label, key, kind) => {
    let text = `${fmt[kind](m.so_far[key])} so far`;
    if (m.projected) text += ` · about ${fmt[kind](m.projected[key])} by month end`;
    if (m.last_month) text += ` · ${fmt[kind](m.last_month[key])} last month${m.last_month_days < 28 ? ` (${m.last_month_days} days recorded)` : ""}`;
    return [label, text];
  };
  factList($("trends-month"), [
    row("Grid cost", "import_cost", "gbp"),
    row("Export earned", "export_earned", "gbp"),
    row("Battery saved", "battery_saved", "gbp"),
    row("Solar", "solar", "kwh"),
    row("Grid import", "import", "kwh"),
    ...(m.projected ? [] : [["Projection", `Needs at least 3 recorded days before today (${m.recent_days} so far)`]]),
  ]);

  const p = trends.payback;
  const when = (iso) => (iso ? shortDate(iso, { month: "short", year: "numeric" }) : null);
  factList($("trends-payback-facts"), [
    ["At the last 14 days' rate", p.recent_per_day == null ? `Needs at least 3 recorded days (${p.recent_days} so far)`
      : `${money(p.recent_per_day)} a day${p.recent_date ? `, paid back around ${when(p.recent_date)}` : p.recent_per_day <= 0 ? ", not paying back at this rate" : ", more than 50 years"}`],
    ["Over everything recorded", p.lifetime_days ? `${money(p.lifetime_per_day)} a day${p.lifetime_date ? `, paid back around ${when(p.lifetime_date)}` : ""}` : null],
  ]);

  const h = trends.health;
  const change = h.first_kwh && h.latest_kwh ? (h.latest_kwh / h.first_kwh - 1) * 100 : null;
  factList($("trends-health"), [
    ["Measured capacity", h.measured_days ? `${(h.latest_kwh || h.first_kwh).toFixed(2)} kWh${h.rated_kwh ? ` (rated ${h.rated_kwh} kWh)` : ""}`
      : "Not measured yet: needs a discharge of at least 20% without charging"],
    ["Change since the first month measured", change == null ? (h.measured_days ? `Needs 60 days measured (${h.measured_days} so far)` : null)
      : `${change >= 0 ? "+" : "−"}${Math.abs(change).toFixed(1)}%`],
    ["Cycles this " + trends.period, (() => { const c = h.periods[h.periods.length - 1].cycles; return c == null ? null : c.toFixed(1); })()],
  ]);

  const caps = new Map(h.periods.map((x) => [x.start, x.capacity_kwh]));
  $("trends-table").tBodies[0].replaceChildren(...trends.periods.slice().reverse().map((x) => {
    const tr = document.createElement("tr");
    for (const v of [x.start, x.solar.toFixed(1), x.home.toFixed(1), x.import.toFixed(1), x.export.toFixed(1),
      x.import_cost.toFixed(2), x.export_earned.toFixed(2), x.battery_saved.toFixed(2), caps.get(x.start)?.toFixed(2) ?? "–"]) {
      const td = document.createElement("td"); td.textContent = v; tr.append(td);
    }
    return tr;
  }));
}

async function refreshTrends() {
  const phone = window.innerWidth < 600;
  const count = trendPeriod === "day" ? (phone ? 14 : 30) : phone ? (trendPeriod === "month" ? 6 : 8) : 12;
  try {
    trends = await (await fetch(`/api/trends?period=${trendPeriod}&count=${count}`, { cache: "no-store" })).json();
  } catch (_) { return; }
  drawTrends();
}

for (const btn of document.querySelectorAll("[data-trend-view]")) {
  btn.addEventListener("click", () => {
    trendView = btn.dataset.trendView;
    try { localStorage.setItem("trend-view", trendView); } catch (_) {}
    drawTrends();
  });
}
for (const btn of document.querySelectorAll("[data-trend-period]")) {
  btn.addEventListener("click", () => {
    trendPeriod = btn.dataset.trendPeriod;
    try { localStorage.setItem("trend-period", trendPeriod); } catch (_) {}
    refreshTrends();
  });
}

$("location-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("location-message");
  msg.className = "note";
  msg.textContent = "Fetching weather…";
  try {
    const clear = !$("loc-lat").value.trim() && !$("loc-lon").value.trim();
    await postSettings("/api/location", clear ? {} : { lat: Number($("loc-lat").value), lon: Number($("loc-lon").value) });
    msg.textContent = clear ? "Location removed. Weather already stored is kept." : "Saved. Weather is fetched every few hours.";
    refreshTrends();
  } catch (err) {
    msg.className = "note error";
    msg.textContent = err.message;
  }
});
(async () => {
  try {
    const place = await (await fetch("/api/location", { cache: "no-store" })).json();
    if (place.lat != null) { $("loc-lat").value = place.lat; $("loc-lon").value = place.lon; }
  } catch (_) {}
})();
