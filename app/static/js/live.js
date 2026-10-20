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
    showInTab(null, false);
    return;
  }
  const { status, data, meter } = body;
  if (body.version) {
    const v = body.version;
    const built = v.built ? new Date(`${v.built}T12:00:00Z`).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" }) : "";
    $("version").textContent = [v.name === "dev" ? "Development build" : `v${v.name}`, built].filter(Boolean).join(" · ");
    $("version").title = v.commit ? `Commit ${v.commit.slice(0, 7)}` : "";
  }
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
  if (!status.configured && !meterOn && !setupShownOnce && auth.can_edit) { setupShownOnce = true; openSetup(); }
  $("solarbank-card").hidden = !status.configured;
  if (status.configured) showDeviceState($("solarbank-status"), status, stale);

  if (!data || !Object.keys(data).length) {
    // Nothing read yet from the current address: don't leave old numbers up.
    for (const id of ["solar", "home", "soc"]) $(id).textContent = "–";
    for (const id of ["solar-detail", "home-detail", "battery-detail"]) $(id).textContent = "\u00a0";
    $("soc-bar").style.width = "0";
    $("device-name").textContent = status.configured ? `Waiting for ${status.host}`
      : meterOn ? "Smart Meter only · no Solarbank set up" : "Not set up yet";
    $("facts").replaceChildren();
    renderGrid(null, meter);
    showInTab(null, false);
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
  showInTab(data, status.connected && !stale);
}

// ---------- Browser tab ----------

// Keep the battery level and what it's doing visible on the tab itself, so it
// can be followed from the tab strip while another tab or window is in front.
const TAB_NAME = document.title;
let tabShown = "";

function showInTab(d, live) {
  const soc = d && d.soc != null ? Math.max(0, Math.min(100, Math.round(d.soc))) : null;
  const b = d ? d.battery_w : null;
  const flow = b == null ? "" : b < -5 ? `▲ ${watts(b)}` : b > 5 ? `▼ ${watts(b)}` : "Idle";
  const title = soc == null ? TAB_NAME
    : [`${soc}%`, flow, live ? null : "offline", TAB_NAME].filter(Boolean).join(" · ");
  if (document.title !== title) document.title = title;

  const key = `${soc}|${live}`;
  if (key === tabShown) return;
  tabShown = key;
  // The icon is a battery filled to the current level, grey when not live.
  const colour = soc == null || !live ? "#8a9099" : soc <= 15 ? "#d9534f" : "#1baf7a";
  const fill = soc == null ? 0 : Math.max(soc ? 1.5 : 0, (18 * soc) / 100);
  const svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    + `<rect x='3' y='7' width='24' height='18' rx='3' fill='none' stroke='${colour}' stroke-width='2.5'/>`
    + `<rect x='27' y='12' width='3' height='8' rx='1' fill='${colour}'/>`
    + `<rect x='6' y='10' width='${fill.toFixed(1)}' height='12' rx='1' fill='${colour}'/></svg>`;
  let icon = document.querySelector("link[rel='icon']");
  if (!icon) { icon = document.createElement("link"); icon.rel = "icon"; document.head.append(icon); }
  icon.href = `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

// ---------- Battery runtime ----------

function atTime(iso) {
  const d = new Date(iso);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const days = Math.round((new Date(d).setHours(0, 0, 0, 0) - new Date().setHours(0, 0, 0, 0)) / 864e5);
  return days === 0 ? time : days === 1 ? `${time} tomorrow` : `${d.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

// Running down to the floor only counts if it happens a while before the next
// charge; reaching it just as off-peak charging starts is lasting, not a warning.
const RUNTIME_MARGIN_MS = 60 * 60 * 1000;

function runtimeText(r) {
  const charging = $("battery-detail").textContent.startsWith("Charging");
  const floor = Math.round(r.floor_soc);
  // Say what ends the projection: a charge the dashboard has planned, or the usual pattern.
  const how = r.recharges_by ? `a scheduled charge at ${atTime(r.recharges_at)}` : `it usually charges, from about ${atTime(r.recharges_at)}`;
  const next = r.recharges_at ? ` until ${how}` : "";
  if (r.soc < r.floor_soc - 0.5 && !charging) return `Below its ${floor}% reserve (now ${Math.round(r.soc)}%), holding${next}`;
  if (r.soc <= r.floor_soc + 0.5 && !charging) return `At its ${floor}% reserve${next}`;
  const short = r.empty_at && !(r.recharges_at && new Date(r.recharges_at) - new Date(r.empty_at) < RUNTIME_MARGIN_MS);
  const low = short ? `down to ${floor}% around ${atTime(r.empty_at)}` : null;
  if (charging && r.full_at) return `Full around ${atTime(r.full_at)}${low ? `, then ${low}` : ""}`;
  if (low) return `Down to ${floor}% around ${atTime(r.empty_at)}${r.recharges_at ? `, before ${how}` : ""}`;
  if (r.recharges_at) return `Lasts${next}`;
  if (r.full_at) return `Full around ${atTime(r.full_at)}`;
  return r.method === "pattern" ? "Lasts beyond the next 2 days" : "";
}

async function refreshRuntime() {
  let r;
  try { r = await (await fetch("/api/runtime", { cache: "no-store" })).json(); } catch (_) { return; }
  const el = $("battery-runtime");
  const text = r.available ? runtimeText(r) : "";
  el.hidden = !text;
  el.textContent = text;
  el.title = !r.available ? "" : r.method === "pattern"
    ? `Estimate from how the battery was used at each time of day over the last ${Math.round(r.pattern_days)} days, down to its ${Math.round(r.floor_soc)}% reserve.`
    : `Estimate at the current power, down to its ${Math.round(r.floor_soc)}% limit. After a day of history it follows your usual daily pattern instead.`;
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

// "Live · 192.168.0.40" with a green dot, or why it isn't live, in a device card's header.
function showDeviceState(el, s, stale = false) {
  const live = s.connected && !stale;
  el.className = `device-state ${live ? "live" : s.last_update || s.last_error ? "offline" : ""}`;
  const dot = document.createElement("span");
  dot.className = "dot";
  dot.setAttribute("aria-hidden", "true");
  el.replaceChildren(dot, live ? `Live · ${s.host}` : s.last_update ? `Offline · ${s.host} · last data ${timeAgo(s.last_update)}`
    : s.last_error ? `Can't reach ${s.host}` : `Connecting to ${s.host}`);
  el.title = live ? "" : s.last_error || "";
}

function renderMeter(meter) {
  const card = $("meter-card");
  if (!meter || !meter.status.configured) { card.hidden = true; return; }
  card.hidden = false;
  const s = meter.status, d = meter.data || {};
  showDeviceState($("meter-status"), s);
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
    ["Battery reserve", pct(d.discharge_limit_soc)],
    ["Backup reserve", pct(d.backup_reserve_soc)],
    ["Max charge / discharge", d.max_charge_w != null ? `${watts(d.max_charge_w)} / ${watts(d.max_discharge_w)}` : null],
    ["Lifetime charged", d.charged_total_kwh != null ? kwh(d.charged_total_kwh) : null],
    ["Lifetime discharged", d.discharged_total_kwh != null ? kwh(d.discharged_total_kwh) : null],
    ["Charge cycles", d.rated_kwh && d.discharged_total_kwh != null
      ? `About ${(Math.round(d.discharged_total_kwh / d.rated_kwh * 10) / 10).toLocaleString()}` : null],
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
