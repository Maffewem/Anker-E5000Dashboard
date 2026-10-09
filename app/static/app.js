"use strict";

const LIVE_MS = 5000;
const HISTORY_MS = 30000;
let hours = 24;
let compare = false;
try {
  hours = Number(localStorage.getItem("hours")) || 24;
  compare = localStorage.getItem("compare") === "1";
} catch (_) {}

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function watts(w) {
  if (w === null || w === undefined) return "–";
  const a = Math.abs(w);
  return a >= 1000 ? `${(a / 1000).toFixed(2)} kW` : `${Math.round(a)} W`;
}
function kwh(v) { return v === null || v === undefined ? "–" : `${v.toFixed(v >= 100 ? 0 : 1)} kWh`; }
function timeAgo(ts) {
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return new Date(ts * 1000).toLocaleString();
}

// ---------- Live tiles ----------

async function refreshLive() {
  let body;
  try {
    const res = await fetch("/api/live", { cache: "no-store" });
    body = await res.json();
  } catch (err) {
    setStatus("offline", "Dashboard unreachable");
    return;
  }
  const { status, data, meter } = body;
  renderMeter(meter);
  const stale = status.last_update && Date.now() / 1000 - status.last_update > status.poll_seconds * 4;
  const meterOn = Boolean(meter && meter.status.configured);
  // Each device works on its own; the pill follows the Solarbank when there is one.
  const shown = status.configured || !meterOn ? status : meter.status;
  const name = shown === status ? "Solarbank" : "Smart Meter";

  if (!status.configured && !meterOn) setStatus("offline", "Not set up");
  else if (shown.connected && !(shown === status && stale)) setStatus("live", `Live · ${timeAgo(shown.last_update)}`);
  else if (shown.last_update) setStatus("offline", `Offline · last data ${timeAgo(shown.last_update)}`);
  else setStatus("offline", shown.last_error ? `Can't reach ${name}` : "Connecting");
  $("status").title = shown.last_error || "";
  showBanner(status, stale, meter);
  if (!status.configured && !meterOn && !setupShownOnce) { setupShownOnce = true; openSetup(); }
  $("solarbank-card").hidden = !status.configured;

  if (!data || !Object.keys(data).length) {
    // Nothing read yet from the current address: don't leave old numbers up.
    for (const id of ["solar", "home", "soc"]) $(id).textContent = "–";
    for (const id of ["solar-detail", "home-detail", "battery-detail"]) $(id).textContent = "\u00a0";
    $("soc-bar").style.width = "0";
    $("device-name").textContent = status.configured ? `Waiting for ${status.host}`
      : meterOn ? "Smart Meter only · no Solarbank set up" : "Not set up yet";
    $("facts").replaceChildren();
    renderGrid(null, meter);
    return;
  }

  $("device-name").textContent = [data.model ? "Solarbank 4 E5000" : null, data.operating_mode ? `${data.operating_mode} mode` : null]
    .filter(Boolean).join(" · ") || status.host;

  $("solar").textContent = watts(data.solar_w);
  $("solar-detail").textContent = data.solar_total_kwh != null ? `${kwh(data.solar_total_kwh)} lifetime` : "\u00a0";

  $("home").textContent = watts(data.home_w);
  $("home-detail").textContent = data.ac_output_w != null ? `Battery AC output ${watts(data.ac_output_w)}` : "\u00a0";

  $("soc").textContent = data.soc != null ? `${Math.round(data.soc)}%` : "–";
  $("soc-bar").style.width = `${Math.max(0, Math.min(100, data.soc || 0))}%`;
  const b = data.battery_w;
  $("battery-detail").textContent =
    b == null ? "\u00a0" : b < -5 ? `Charging ${watts(b)}` : b > 5 ? `Discharging ${watts(b)}` : "Idle";

  renderGrid(data, meter);
  renderFacts(data, status);
}

function renderGrid(data, meter) {
  // Prefer the Smart Meter's reading of the grid connection when it's live.
  const meterLive = meter && meter.status.connected && meter.data && meter.data.grid_w != null;
  const g = meterLive ? meter.data.grid_w : data ? data.grid_w : null;
  $("grid").textContent = watts(g);
  const dir = g == null ? null : g > 5 ? "Importing" : g < -5 ? "Exporting" : "Balanced";
  $("grid-detail").textContent = dir == null ? "\u00a0" : meterLive ? `${dir} · Smart Meter` : dir;
}

function showBanner(status, stale, meter) {
  const lines = [];
  if (status.configured && (!status.connected || stale) && status.last_error) lines.push(`Solarbank: ${status.last_error}`);
  const m = meter && meter.status;
  if (m && m.configured && !m.connected && m.last_error) lines.push(`Smart Meter: ${m.last_error}`);
  $("banner").hidden = !lines.length;
  $("banner-text").replaceChildren(...lines.map((t) => { const p = document.createElement("p"); p.textContent = t; return p; }));
}

function factList(el, rows) {
  el.replaceChildren(...rows.filter(([, v]) => v != null && v !== "").map(([k, v]) => {
    const div = document.createElement("div");
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    div.append(dt, dd);
    return div;
  }));
}

function renderMeter(meter) {
  const card = $("meter-card");
  if (!meter || !meter.status.configured) { card.hidden = true; return; }
  card.hidden = false;
  const s = meter.status, d = meter.data || {};
  $("meter-status").textContent = s.connected ? `Live · ${s.host}` : s.last_update ? `Offline · last data ${timeAgo(s.last_update)}` : `Waiting for ${s.host}`;
  const fixed = (v, n, unit) => (v == null ? null : `${v.toFixed(n)} ${unit}`);
  const rows = [
    ["Grid power", d.grid_w == null ? null : `${watts(d.grid_w)} ${d.grid_w > 5 ? "importing" : d.grid_w < -5 ? "exporting" : ""}`.trim()],
  ];
  for (const p of d.phases || []) {
    const label = d.phases.length > 1 ? `Phase ${p.phase}` : "Line";
    rows.push([label, [fixed(p.voltage, 1, "V"), fixed(p.current, 2, "A"), p.power_w == null ? null : watts(p.power_w)].filter(Boolean).join(" · ")]);
  }
  rows.push(
    ["Power factor", d.power_factor == null ? null : d.power_factor.toFixed(2)],
    ["Lifetime import", d.import_total_kwh == null ? null : kwh(d.import_total_kwh)],
    ["Lifetime export", d.export_total_kwh == null ? null : kwh(d.export_total_kwh)],
  );
  if (d.secondary_w) rows.push(["Second CT power", watts(d.secondary_w)]);
  rows.push(["Model", d.model], ["Serial number", d.serial], ["Firmware", d.firmware], ["Type", d.meter_type]);
  factList($("meter-facts"), rows);
}

function setStatus(kind, text) {
  $("status").className = `status ${kind}`;
  $("status-text").textContent = text;
}

function renderFacts(d, status) {
  const pct = (v) => (v == null ? null : `${v}%`);
  const rows = [
    ["Model", d.model],
    ["Serial number", d.serial],
    ["Firmware", d.firmware],
    ["Address", `${status.host}`],
    ["Operating mode", d.operating_mode],
    ["Battery state", d.battery_status && d.battery_status[0].toUpperCase() + d.battery_status.slice(1)],
    ["Capacity", d.rated_kwh != null ? kwh(d.rated_kwh) : null],
    ["Charge limit", pct(d.charging_limit_soc)],
    ["Discharge limit", pct(d.discharge_limit_soc)],
    ["Backup reserve", pct(d.backup_reserve_soc)],
    ["Max charge / discharge", d.max_charge_w != null ? `${watts(d.max_charge_w)} / ${watts(d.max_discharge_w)}` : null],
    ["Lifetime charged", d.charged_total_kwh != null ? kwh(d.charged_total_kwh) : null],
    ["Lifetime discharged", d.discharged_total_kwh != null ? kwh(d.discharged_total_kwh) : null],
  ].filter(([, v]) => v);
  const dl = $("facts");
  dl.replaceChildren(...rows.map(([k, v]) => {
    const div = document.createElement("div");
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    div.append(dt, dd);
    return div;
  }));
}

// ---------- Charts ----------

let powerChart, socChart, energyChart;

function baseOptions() {
  const muted = css("--text-muted");
  const grid = css("--grid");
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { position: "top", align: "start", labels: { color: css("--text-secondary"), boxWidth: 10, boxHeight: 10, useBorderRadius: true, borderRadius: 3 } },
      tooltip: { backgroundColor: css("--surface"), titleColor: css("--text-primary"), bodyColor: css("--text-secondary"),
        borderColor: css("--axis"), borderWidth: 1, padding: 10, boxPadding: 4, usePointStyle: true },
    },
    scales: {
      x: { ticks: { color: muted, maxRotation: 0, autoSkipPadding: 16 }, grid: { display: false }, border: { color: css("--axis") } },
      y: { ticks: { color: muted }, grid: { color: grid }, border: { display: false } },
    },
  };
}

function timeAxis(opts) {
  const span = hours * 3600 * 1000;
  opts.scales.x.type = "linear";
  opts.scales.x.min = Date.now() - span;
  opts.scales.x.max = Date.now();
  // Put ticks on round local times: the smallest step from this ladder (in
  // minutes) that keeps the tick count readable, aligned to the clock.
  const STEPS = [10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 7200, 10080];
  const maxTicks = window.innerWidth < 600 ? 4 : 9;
  opts.scales.x.afterBuildTicks = (axis) => {
    const spanMin = (axis.max - axis.min) / 60e3;
    const step = STEPS.find((m) => spanMin / m <= maxTicks) || STEPS[STEPS.length - 1];
    const t = new Date(axis.min);
    t.setSeconds(0, 0);
    if (step >= 1440) t.setHours(0, 0);
    else {
      const minuteOfDay = t.getHours() * 60 + t.getMinutes();
      const aligned = Math.ceil(minuteOfDay / step) * step;
      t.setHours(0, aligned);
    }
    const ticks = [];
    for (; t.getTime() <= axis.max; t.setMinutes(t.getMinutes() + step)) {
      if (t.getTime() >= axis.min) ticks.push({ value: t.getTime() });
    }
    axis.ticks = ticks;
  };
  opts.scales.x.ticks.callback = (v) => {
    const d = new Date(v);
    return hours <= 24 ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString([], { day: "numeric", month: "short" });
  };
  opts.plugins.tooltip.callbacks = {
    title: (items) => items.length ? new Date(items[0].parsed.x).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" }) : "",
  };
  return opts;
}

function line(label, color, data) {
  return { label, data, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0,
    pointHoverRadius: 4, pointHoverBorderWidth: 2, pointHoverBorderColor: css("--surface"), tension: 0.25, spanGaps: false };
}

// One value per bucket across the whole window, null where nothing was
// recorded: outages show as gaps, and every series (including the previous
// period, moved forward by `shiftMs`) lines up for the tooltip.
function series(points, key, bucketMs, shiftMs = 0) {
  const byX = new Map(points.map((p) => [p.t * 1000 + shiftMs, p[key]]));
  const end = Date.now();
  const out = [];
  for (let x = Math.floor((end - hours * 3600e3) / bucketMs) * bucketMs; x <= end; x += bucketMs) {
    const v = byX.get(x);
    out.push({ x, y: v == null ? null : Math.round(v) });
  }
  return out;
}

// The previous period: same colour, dashed and lighter.
function previousLine(label, color, data) {
  return { ...line(`${label} · previous`, `${color}80`, data), borderDash: [5, 4], borderWidth: 1.5 };
}

const PERIOD_NAMES = { 1: "hour", 3: "3 hours", 6: "6 hours", 24: "day", 168: "7 days", 720: "30 days" };

async function getHistory(offsetHours) {
  const res = await fetch(`/api/history?hours=${hours}&offset_hours=${offsetHours}`, { cache: "no-store" });
  return res.json();
}

async function refreshHistory() {
  let body, prev = null;
  try {
    [body, prev] = await Promise.all([getHistory(0), compare ? getHistory(hours) : null]);
  } catch (_) { return; }
  const bucketMs = body.bucket_seconds * 1000;
  const pts = body.points;
  const shiftMs = hours * 3600e3;

  const SERIES = [["Solar", "--solar", "solar_w"], ["Home", "--home", "home_w"],
    ["Battery", "--battery", "battery_w"], ["Grid", "--gridpower", "grid_w"]];
  const powerSets = SERIES.map(([label, color, key]) => line(label, css(color), series(pts, key, bucketMs)));
  if (prev) {
    for (const [label, color, key] of SERIES) powerSets.push(previousLine(label, css(color), series(prev.points, key, bucketMs, shiftMs)));
  }
  const powerOpts = timeAxis(baseOptions());
  powerOpts.scales.y.ticks.callback = (v) => (Math.abs(v) >= 1000 ? `${v / 1000} kW` : `${v} W`);
  powerOpts.plugins.tooltip.callbacks.label = (c) => `${c.dataset.label}: ${c.parsed.y < 0 ? "−" : ""}${watts(c.parsed.y)}`;

  powerChart?.destroy();
  powerChart = new Chart($("power-chart"), { type: "line", data: { datasets: powerSets }, options: powerOpts });

  const socOpts = timeAxis(baseOptions());
  socOpts.plugins.legend.display = false;
  socOpts.scales.y.min = 0;
  socOpts.scales.y.max = 100;
  socOpts.scales.y.ticks.stepSize = 50;
  socOpts.scales.y.ticks.callback = (v) => `${v}%`;
  socOpts.plugins.tooltip.callbacks.label = (c) => `${c.datasetIndex ? "Previous" : "Battery"}: ${c.parsed.y}%`;
  socChart?.destroy();
  socChart = new Chart($("soc-chart"), { type: "line", data: { datasets: [line("Battery level", css("--battery"), series(pts, "soc", bucketMs)),
    ...(prev ? [previousLine("Battery level", css("--battery"), series(prev.points, "soc", bucketMs, shiftMs))] : [])] }, options: socOpts });
}

async function refreshEnergy() {
  let days;
  try {
    days = await (await fetch(`/api/energy?days=${window.innerWidth < 600 ? 7 : 14}`, { cache: "no-store" })).json();
  } catch (_) { return; }

  const label = (d) => new Date(`${d}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric" });
  const bar = (name, color, key) => ({ label: name, data: days.map((d) => d[key]), backgroundColor: color,
    borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", barPercentage: 0.85, categoryPercentage: 0.8,
    borderColor: css("--surface"), borderWidth: { left: 1, right: 1 } });
  const opts = baseOptions();
  opts.scales.y.ticks.callback = (v) => `${v} kWh`;
  opts.plugins.tooltip.callbacks = { label: (c) => `${c.dataset.label}: ${c.parsed.y.toFixed(2)} kWh` };

  energyChart?.destroy();
  energyChart = new Chart($("energy-chart"), {
    type: "bar",
    data: {
      labels: days.map((d) => label(d.date)),
      datasets: [
        bar("Solar", css("--solar"), "solar_kwh"),
        bar("Home", css("--home"), "home_kwh"),
        bar("Grid export", css("--export"), "export_kwh"),
        bar("Grid import", css("--gridpower"), "import_kwh"),
      ],
    },
    options: opts,
  });

  const t = days[days.length - 1];
  if (t) $("today").textContent = `Today: ${t.solar_kwh.toFixed(1)} kWh solar, ${t.home_kwh.toFixed(1)} kWh used, ${t.import_kwh.toFixed(1)} kWh from grid`;

  const fmt = (v) => v.toFixed(2);
  $("energy-table").tBodies[0].replaceChildren(...days.slice().reverse().map((d) => {
    const tr = document.createElement("tr");
    for (const v of [d.date, fmt(d.solar_kwh), fmt(d.home_kwh), fmt(d.export_kwh), fmt(d.import_kwh), fmt(d.charge_kwh), fmt(d.discharge_kwh)]) {
      const td = document.createElement("td"); td.textContent = v; tr.append(td);
    }
    return tr;
  }));
}

// ---------- Battery payback ----------

let payback = null;
const money = (v) => (v == null ? null : `${v < 0 ? "−" : ""}£${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);

function duration(days) {
  if (days < 60) return `${days} days`;
  const months = Math.round(days / 30.44);
  if (months < 24) return `${months} months`;
  const y = Math.floor(months / 12), m = months % 12;
  return m ? `${y} years ${m} months` : `${y} years`;
}

function renderPayback(p) {
  payback = p;
  const set = p.tariff.battery_cost > 0;
  $("payback-empty").hidden = set;
  if (!set) { $("payback-facts").replaceChildren(); return; }
  const date = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
  let eta;
  if (p.payback_days === 0) eta = "Paid back";
  else if (p.payback_days != null) eta = `About ${duration(p.payback_days)} (${date(p.payback_date)})`;
  else eta = p.days ? "Not saving yet" : "Waiting for data";
  factList($("payback-facts"), [
    ["Saved so far", p.since ? `${money(p.saved)} over ${p.days} day${p.days === 1 ? "" : "s"}` : money(0)],
    ["Average per day", money(p.per_day)],
    ["Battery cost", money(p.tariff.battery_cost)],
    ["Left to pay back", money(p.remaining)],
    ["Payback", eta],
    ["Discharge worth", money(p.discharge_value)],
    ["Grid charging cost", money(p.grid_charge_cost)],
    ["Solar export given up", money(p.solar_charge_cost)],
  ]);
}

async function refreshPayback() {
  try { renderPayback(await (await fetch("/api/payback", { cache: "no-store" })).json()); } catch (_) {}
}

$("open-costs").addEventListener("click", () => {
  const t = (payback && payback.tariff) || {};
  $("cost-battery").value = t.battery_cost || "";
  $("cost-peak").value = t.peak_rate ?? "";
  $("cost-offpeak").value = t.offpeak_rate ?? "";
  $("cost-from").value = t.offpeak_start || "00:30";
  $("cost-to").value = t.offpeak_end || "05:30";
  $("cost-export").value = t.export_rate ?? "";
  $("costs-msg").textContent = "";
  $("costs").showModal();
});
$("costs-cancel").addEventListener("click", () => $("costs").close());
$("costs-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const num = (id) => Number($(id).value) || 0;
  try {
    const p = await postSettings("/api/tariff", {
      battery_cost: num("cost-battery"), peak_rate: num("cost-peak"), offpeak_rate: num("cost-offpeak"),
      offpeak_start: $("cost-from").value || "00:00", offpeak_end: $("cost-to").value || "00:00", export_rate: num("cost-export"),
    });
    renderPayback(p);
    $("costs").close();
  } catch (err) {
    $("costs-msg").className = "setup-msg error";
    $("costs-msg").textContent = err.message;
  }
});

// ---------- Setup ----------

let setupShownOnce = false;
let setupDevice = "battery";
let meterPromptShown = false;
let savedSettings = {};
// ---------- Event log ----------

const EVENT_PAGE = 50;
let eventsShown = [];

function eventTime(ts) {
  const d = new Date(ts * 1000);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function renderEvents() {
  const body = $("events-table").querySelector("tbody");
  body.replaceChildren(...eventsShown.map((e) => {
    const tr = document.createElement("tr");
    for (const text of [eventTime(e.ts), DEVICE_LABEL[e.device] || e.device, e.message]) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.append(td);
    }
    return tr;
  }));
  $("events-empty").hidden = eventsShown.length > 0;
  $("events-table").hidden = eventsShown.length === 0;
}

async function getEvents(before) {
  const params = new URLSearchParams({ limit: EVENT_PAGE, kind: $("events-filter").value });
  if (before) params.set("before", before);
  const r = await fetch(`/api/events?${params}`);
  return r.ok ? r.json() : [];
}

async function refreshEvents() {
  // Reload as many as are showing, so "Show older" pages aren't lost on refresh.
  const params = new URLSearchParams({ limit: Math.max(EVENT_PAGE, eventsShown.length), kind: $("events-filter").value });
  const r = await fetch(`/api/events?${params}`);
  if (!r.ok) return;
  eventsShown = await r.json();
  $("events-more").hidden = eventsShown.length < Number(params.get("limit"));
  renderEvents();
}

$("events-more").addEventListener("click", async () => {
  const older = await getEvents(eventsShown.at(-1)?.id);
  eventsShown = eventsShown.concat(older);
  $("events-more").hidden = older.length < EVENT_PAGE;
  renderEvents();
});
$("events-filter").addEventListener("change", () => { eventsShown = []; refreshEvents(); });

// Export: dates default to the last 7 days, in the browser's local time.
function isoDay(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
{
  const today = new Date();
  $("export-end").value = isoDay(today);
  $("export-start").value = isoDay(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 6));
}
$("export-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const [start, end] = [$("export-start").value, $("export-end").value].sort();
  const params = new URLSearchParams({
    data: $("export-data").value,
    start,
    end,
    format: $("export-format").value,
  });
  window.location.href = `/api/export?${params}`;
});

const DEVICE_LABEL = { battery: "Solarbank", meter: "Smart Meter" };
const DEVICE_ENV = { battery: "SOLARBANK_HOST", meter: "METER_HOST" };

function setupValues() {
  return {
    host: $("setup-host").value.trim(),
    port: Number($("setup-port").value) || 502,
    unit_id: Number($("setup-unit").value) || 1,
  };
}

function setupMessage(kind, text) {
  const el = $("setup-msg");
  el.className = `setup-msg ${kind}`;
  el.textContent = text;
}

function describeDevice(d) {
  if (!d) return "Connected.";
  const bits = [d.model && `model ${d.model}`, d.serial && `serial ${d.serial}`,
    d.soc != null && `battery at ${Math.round(d.soc)}%`,
    setupDevice === "meter" && d.grid_w != null && `grid ${watts(d.grid_w)}`].filter(Boolean);
  return bits.length ? `Found your ${DEVICE_LABEL[setupDevice]}: ${bits.join(", ")}.` : "Connected.";
}

async function postSettings(path, body) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? "Check the values you entered." : data.detail;
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return data;
}

function setBusy(busy) {
  const locked = $("setup").dataset.locked === "1";
  for (const id of ["setup-test", "setup-save", "setup-force", "setup-remove"]) $(id).disabled = busy || locked;
}

function showDevice(device) {
  setupDevice = device;
  const s = savedSettings[device] || { host: "", port: 502, unit_id: 1, locked: false };
  for (const b of document.querySelectorAll(".device-switch button")) b.setAttribute("aria-pressed", String(b.dataset.device === device));
  for (const el of document.querySelectorAll(".device-label")) el.textContent = DEVICE_LABEL[device];
  $("setup-host").value = s.host || "";
  $("setup-port").value = s.port;
  $("setup-unit").value = s.unit_id;
  $("setup").dataset.locked = s.locked ? "1" : "0";
  $("setup-env").textContent = DEVICE_ENV[device];
  $("setup-locked").hidden = !s.locked;
  for (const id of ["setup-host", "setup-port", "setup-unit"]) $(id).disabled = s.locked;
  $("setup-remove").hidden = device !== "meter" || !s.host || s.locked;
  $("setup-force").hidden = true;
  setupMessage("", "");
  setBusy(false);
}

async function openSetup(device = "battery") {
  try {
    savedSettings = await (await fetch("/api/settings", { cache: "no-store" })).json();
  } catch (_) {}
  // The dialog can't be dismissed until at least one device has an address.
  const hasHost = (d) => Boolean(savedSettings[d] && savedSettings[d].host);
  $("setup-cancel").hidden = !(hasHost("battery") || hasHost("meter"));
  $("setup-cancel").textContent = "Cancel";
  showDevice(device);
  if (!hasHost("battery") && !hasHost("meter")) {
    setupMessage("", "Only have a Smart Meter? Choose Smart Meter above to set it up on its own.");
  }
  const dlg = $("setup");
  if (!dlg.open) dlg.showModal();
  if (!$("setup-host").disabled) $("setup-host").focus();
}

$("setup-test").addEventListener("click", async () => {
  const body = setupValues();
  if (!body.host) return setupMessage("error", `Enter the ${DEVICE_LABEL[setupDevice]}'s IP address.`);
  setBusy(true);
  setupMessage("", `Trying ${body.host}…`);
  try {
    const r = await postSettings(`/api/settings/${setupDevice}/test`, body);
    setupMessage("ok", describeDevice(r.device));
  } catch (err) {
    setupMessage("error", err.message);
  } finally { setBusy(false); }
});

async function save(skipTest) {
  const body = setupValues();
  if (!body.host) return setupMessage("error", `Enter the ${DEVICE_LABEL[setupDevice]}'s IP address.`);
  setBusy(true);
  setupMessage("", skipTest ? "Saving…" : `Connecting to ${body.host}…`);
  try {
    const r = await postSettings(`/api/settings/${setupDevice}${skipTest ? "?skip_test=true" : ""}`, body);
    const device = setupDevice;
    // First-time setup only: the battery had no address before this save.
    const firstMeterPrompt = device === "battery" && !(savedSettings.battery && savedSettings.battery.host)
      && !(savedSettings.meter && savedSettings.meter.host) && !meterPromptShown;
    savedSettings[device] = { ...(savedSettings[device] || {}), ...body };
    refreshLive();
    if (skipTest) {
      // Nothing was checked, so don't claim a connection; the status pill and
      // banner show the real result once the dashboard tries to connect.
      setupMessage("", `Saved ${body.host}. The dashboard will keep trying to connect; check the status at the top.`);
    } else {
      setupMessage("ok", `${describeDevice(r.device)} Saved.`);
    }
    if (firstMeterPrompt) {
      // Offer the optional Smart Meter straight after the battery, once.
      meterPromptShown = true;
      setTimeout(() => {
        $("setup-cancel").hidden = false;
        showDevice("meter");
        setupMessage("", "Solarbank saved. Got an Anker Smart Meter? Enter its IP to add it, or press Skip.");
        $("setup-cancel").textContent = "Skip";
      }, 1200);
    } else {
      setTimeout(() => { $("setup").close(); }, skipTest ? 2500 : 900);
    }
  } catch (err) {
    setupMessage("error", err.message);
    // Let people save an address that is temporarily offline.
    $("setup-force").hidden = false;
  } finally { setBusy(false); }
}

$("setup-remove").addEventListener("click", async () => {
  setBusy(true);
  try {
    await postSettings("/api/settings/meter", { host: "" });
    setupMessage("ok", "Smart Meter removed.");
    setTimeout(() => { $("setup").close(); refreshLive(); }, 700);
  } catch (err) {
    setupMessage("error", err.message);
  } finally { setBusy(false); }
});

for (const b of document.querySelectorAll(".device-switch button")) b.addEventListener("click", () => showDevice(b.dataset.device));
$("setup-form").addEventListener("submit", (e) => { e.preventDefault(); save(false); });
$("setup-force").addEventListener("click", () => save(true));
$("setup-cancel").addEventListener("click", () => $("setup").close());
$("open-setup").addEventListener("click", () => openSetup("battery"));
$("banner-setup").addEventListener("click", () => openSetup("battery"));

// ---------- Wiring ----------

for (const btn of document.querySelectorAll(".range button[data-hours]")) {
  btn.setAttribute("aria-pressed", String(Number(btn.dataset.hours) === hours));
  btn.addEventListener("click", () => {
    hours = Number(btn.dataset.hours);
    try { localStorage.setItem("hours", String(hours)); } catch (_) {}
    for (const b of document.querySelectorAll(".range button[data-hours]")) b.setAttribute("aria-pressed", String(b === btn));
    $("compare-span").textContent = PERIOD_NAMES[hours] || `${hours} hours`;
    refreshHistory();
  });
}

$("compare").checked = compare;
$("compare-span").textContent = PERIOD_NAMES[hours] || `${hours} hours`;
$("compare").addEventListener("change", () => {
  compare = $("compare").checked;
  try { localStorage.setItem("compare", compare ? "1" : "0"); } catch (_) {}
  refreshHistory();
});

// ---------- Theme ----------

const darkQuery = matchMedia("(prefers-color-scheme: dark)");
const isDark = () => document.documentElement.dataset.theme === "dark"
  || (document.documentElement.dataset.theme !== "light" && darkQuery.matches);

function showTheme() {
  const dark = isDark();
  const label = dark ? "Switch to light mode" : "Switch to dark mode";
  const btn = $("theme-toggle");
  btn.classList.toggle("is-dark", dark);
  btn.setAttribute("aria-label", label);
  btn.title = label;
}

$("theme-toggle").addEventListener("click", () => {
  const theme = isDark() ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("theme", theme); } catch (_) {}
  showTheme();
  refreshHistory();
  refreshEnergy();
});

darkQuery.addEventListener("change", () => { showTheme(); refreshHistory(); refreshEnergy(); });
showTheme();

refreshLive();
refreshHistory();
refreshEnergy();
refreshPayback();
refreshEvents();
setInterval(refreshPayback, 5 * 60 * 1000);
setInterval(refreshLive, LIVE_MS);
setInterval(() => { refreshHistory(); refreshEnergy(); refreshEvents(); }, HISTORY_MS);
// Browsers slow timers down in background tabs, so catch up as soon as the
// page is looked at again rather than showing old numbers.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { refreshLive(); refreshHistory(); refreshEnergy(); refreshEvents(); }
});
