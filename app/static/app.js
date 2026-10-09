"use strict";

const LIVE_MS = 5000;
const HISTORY_MS = 60000;
let hours = 24;
try { hours = Number(localStorage.getItem("hours")) || 24; } catch (_) {}

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
  const { status, data } = body;
  const stale = status.last_update && Date.now() / 1000 - status.last_update > status.poll_seconds * 4;

  if (!status.configured) setStatus("offline", "SOLARBANK_HOST not set");
  else if (status.connected && !stale) setStatus("live", `Live · ${timeAgo(status.last_update)}`);
  else if (status.last_update) setStatus("offline", `Offline · last data ${timeAgo(status.last_update)}`);
  else setStatus("offline", status.last_error ? "Can't reach battery" : "Connecting");
  $("status").title = status.last_error || "";

  if (!data || !Object.keys(data).length) return;

  $("device-name").textContent = [data.model ? "Solarbank 4 E5000" : null, data.operating_mode ? `${data.operating_mode} mode` : null]
    .filter(Boolean).join(" · ") || status.host;

  $("solar").textContent = watts(data.solar_w);
  $("solar-detail").textContent = data.solar_total_kwh != null ? `${kwh(data.solar_total_kwh)} lifetime` : " ";

  $("home").textContent = watts(data.home_w);
  $("home-detail").textContent = data.ac_output_w != null ? `Battery AC output ${watts(data.ac_output_w)}` : " ";

  $("soc").textContent = data.soc != null ? `${Math.round(data.soc)}%` : "–";
  $("soc-bar").style.width = `${Math.max(0, Math.min(100, data.soc || 0))}%`;
  const b = data.battery_w;
  $("battery-detail").textContent =
    b == null ? " " : b < -5 ? `Charging ${watts(b)}` : b > 5 ? `Discharging ${watts(b)}` : "Idle";

  const g = data.grid_w;
  $("grid").textContent = watts(g);
  $("grid-detail").textContent = g == null ? " " : g > 5 ? "Importing" : g < -5 ? "Exporting" : "Balanced";

  renderFacts(data, status);
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
  // Put ticks on round local times (every N hours, or midnights).
  const stepHours = hours <= 6 ? 1 : hours <= 24 ? 3 : hours <= 168 ? 24 : 120;
  const maxTicks = window.innerWidth < 600 ? 4 : 9;
  opts.scales.x.afterBuildTicks = (axis) => {
    const ticks = [];
    const t = new Date(axis.min);
    t.setMinutes(0, 0, 0);
    if (stepHours >= 24) t.setHours(0);
    else t.setHours(Math.ceil(t.getHours() / stepHours) * stepHours);
    let step = stepHours;
    while ((axis.max - axis.min) / (step * 3600e3) > maxTicks) step *= 2;
    for (; t.getTime() <= axis.max; t.setHours(t.getHours() + step)) {
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

// Insert nulls where buckets are missing so outages show as gaps, not ramps.
function withGaps(points, key, bucketMs) {
  const out = [];
  let prev = null;
  for (const p of points) {
    const x = p.t * 1000;
    if (prev !== null && x - prev > bucketMs * 1.5) out.push({ x: prev + bucketMs, y: null });
    out.push({ x, y: p[key] == null ? null : Math.round(p[key]) });
    prev = x;
  }
  return out;
}

async function refreshHistory() {
  let body;
  try {
    body = await (await fetch(`/api/history?hours=${hours}`, { cache: "no-store" })).json();
  } catch (_) { return; }
  const bucketMs = body.bucket_seconds * 1000;
  const pts = body.points;

  const powerSets = [
    line("Solar", css("--solar"), withGaps(pts, "solar_w", bucketMs)),
    line("Home", css("--home"), withGaps(pts, "home_w", bucketMs)),
    line("Battery", css("--battery"), withGaps(pts, "battery_w", bucketMs)),
    line("Grid", css("--gridpower"), withGaps(pts, "grid_w", bucketMs)),
  ];
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
  socOpts.plugins.tooltip.callbacks.label = (c) => `Battery: ${c.parsed.y}%`;
  socChart?.destroy();
  socChart = new Chart($("soc-chart"), { type: "line", data: { datasets: [line("Battery level", css("--battery"), withGaps(pts, "soc", bucketMs))] }, options: socOpts });
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

// ---------- Wiring ----------

for (const btn of document.querySelectorAll(".range button")) {
  btn.setAttribute("aria-pressed", String(Number(btn.dataset.hours) === hours));
  btn.addEventListener("click", () => {
    hours = Number(btn.dataset.hours);
    try { localStorage.setItem("hours", String(hours)); } catch (_) {}
    for (const b of document.querySelectorAll(".range button")) b.setAttribute("aria-pressed", String(b === btn));
    refreshHistory();
  });
}

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { refreshHistory(); refreshEnergy(); });

refreshLive();
refreshHistory();
refreshEnergy();
setInterval(refreshLive, LIVE_MS);
setInterval(() => { refreshHistory(); refreshEnergy(); }, HISTORY_MS);
