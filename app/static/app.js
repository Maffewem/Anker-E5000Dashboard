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
  const next = r.recharges_at ? ` until it charges at ${atTime(r.recharges_at)}` : "";
  if (r.soc <= r.floor_soc + 0.5 && !charging) return `At its ${floor}% discharge limit${next}`;
  const short = r.empty_at && !(r.recharges_at && new Date(r.recharges_at) - new Date(r.empty_at) < RUNTIME_MARGIN_MS);
  const low = short ? `down to ${floor}% around ${atTime(r.empty_at)}` : null;
  if (charging && r.full_at) return `Full around ${atTime(r.full_at)}${low ? `, then ${low}` : ""}`;
  if (low) return `Down to ${floor}% around ${atTime(r.empty_at)}${r.recharges_at ? `, before it charges at ${atTime(r.recharges_at)}` : ""}`;
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
    ? `Estimate from how the battery was used at each time of day over the last ${Math.round(r.pattern_days)} days, down to its ${Math.round(r.floor_soc)}% limit.`
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
    ["Discharge limit", pct(d.discharge_limit_soc)],
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

// "8 to 12 years": a range, since savings per day vary with the seasons.
function durationRange(low, high) {
  if (high >= 730) {
    const [a, b] = [Math.round(low / 365.25), Math.round(high / 365.25)];
    return a === b ? `${a} years` : `${a} to ${b} years`;
  }
  const [a, b] = [Math.max(1, Math.round(low / 30.44)), Math.max(1, Math.round(high / 30.44))];
  return a === b ? `${a} month${a === 1 ? "" : "s"}` : `${a} to ${b} months`;
}

// ---------- Energy supplier ----------
// Only Octopus has a price feed. With anyone else the Octopus parts are
// hidden and the typed prices (with that supplier's off-peak presets) are used.
const isOctopus = () => !payback || payback.supplier === "octopus";

function fillSuppliers(sel) {
  if (sel.options.length || !payback) return;
  for (const s of payback.suppliers) sel.append(new Option(s.name, s.value));
}

function applySupplier() {
  const oct = isOctopus();
  for (const sel of document.querySelectorAll(".supplier-select")) {
    fillSuppliers(sel);
    if (sel !== document.activeElement) sel.value = payback.supplier;
  }
  $("open-octopus").hidden = !oct;
  $("tariff-edit-costs").hidden = oct;
  $("control-dispatch-row").hidden = !oct;
  $("cheap-hint").textContent = oct
    ? "Uses your Octopus prices, or the off-peak hours in Edit costs. Schedules win when they overlap."
    : "Uses the off-peak hours in Edit costs. Schedules win when they overlap.";
  $("compare-region-default").textContent = oct ? "Your Octopus region" : "London (pick your region)";
  if (!oct) renderManualTariff();
  else if (octopus) renderOctopus(octopus);
}

// The Electricity prices card for suppliers without a price feed: what was typed in.
function renderManualTariff() {
  const t = payback.manual || payback.tariff;
  const name = (payback.suppliers.find((s) => s.value === payback.supplier) || {}).name;
  const flat = t.peak_rate === t.offpeak_rate;
  $("tariff-empty").hidden = true;
  $("tariff-error").hidden = true;
  $("tariff-diag").hidden = true;
  $("tariff-detail").hidden = true;
  $("tariff-manual").hidden = false;
  $("tariff-manual").textContent = `Prices you entered in Edit costs${payback.supplier === "other" ? "" : ` for ${name}`}. `
    + "They're used for the battery payback, battery control and the tariff comparison.";
  factList($("tariff-facts"), [
    ["Supplier", name],
    [flat ? "Price" : "Peak price", pence(t.peak_rate)],
    ...(flat ? [] : [["Off-peak price", `${pence(t.offpeak_rate)} (${t.offpeak_start}–${t.offpeak_end})`]]),
    ["Export price", pence(t.export_rate)],
  ]);
}

async function saveSupplier(value) {
  try {
    renderPayback(await postSettings("/api/supplier", { supplier: value }));
    refreshOctopus();
    refreshControl();
  } catch (err) { alert(err.message); }
}
$("setup-supplier").addEventListener("change", (e) => saveSupplier(e.target.value));
$("tariff-edit-costs").addEventListener("click", () => $("open-costs").click());

function syncPresets() {
  const s = payback && payback.suppliers.find((x) => x.value === $("cost-supplier").value);
  const presets = (s && s.presets) || [];
  $("cost-preset-row").hidden = !presets.length;
  $("cost-preset").replaceChildren(new Option("Choose to fill in the off-peak hours", ""),
    ...presets.map((p, i) => new Option(`${p.name} (off-peak ${p.offpeak_start}–${p.offpeak_end})`, i)));
  // Octopus prices come from the account; the "use my own prices" switch only makes sense there.
  $("costs-manual-row").hidden = !(payback && payback.octopus_connected) || $("cost-supplier").value !== "octopus";
  syncCostInputs();
}
$("cost-supplier").addEventListener("change", syncPresets);
$("cost-preset").addEventListener("change", (e) => {
  const s = payback.suppliers.find((x) => x.value === $("cost-supplier").value);
  const p = s && s.presets[Number(e.target.value)];
  if (!p || e.target.value === "") return;
  $("cost-from").value = p.offpeak_start;
  $("cost-to").value = p.offpeak_end;
});

function renderPayback(p) {
  payback = p;
  applySupplier();
  const set = p.tariff.battery_cost > 0;
  $("payback-empty").hidden = set;
  $("payback-empty").textContent = p.source === "octopus"
    ? "Enter what you paid for the battery to see how long it takes to pay for itself. Prices come from Octopus."
    : "Enter what you paid for the battery and your electricity prices to see how long it takes to pay for itself.";
  if (!set) { $("payback-facts").replaceChildren(); return; }
  const date = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
  const year = (iso) => iso.slice(0, 4);
  let eta;
  if (p.payback_days === 0) eta = "Paid back";
  else if (p.payback_days != null) {
    const [a, b] = [year(p.payback_date_low), year(p.payback_date_high)];
    eta = `About ${durationRange(p.payback_days_low, p.payback_days_high)} (${a === b ? a : `${a} to ${b}`})`;
  } else if (p.payback_too_long) {
    eta = "More than 50 years at this rate";
  } else if (p.days < p.min_days) {
    eta = `Collecting data: ${p.days} of ${p.min_days} days${p.tariff.installed ? "" : ". Add the install date to count the battery's history"}`;
  } else eta = "Not saving yet";
  $("payback-source").hidden = p.source !== "octopus";
  $("payback-source").textContent = p.source === "octopus"
    ? `Using your actual ${p.tariff_name || "Octopus"} prices for each half hour since they were fetched, and the prices you typed before that.` : "";
  if (p.use_manual) {
    $("payback-source").hidden = false;
    $("payback-source").textContent = "Using your own prices (set in Edit costs) instead of Octopus.";
  }
  factList($("payback-facts"), [
    ["Saved so far", p.since ? `${money(p.saved)} since ${date(p.since)}` : money(0)],
    ...(p.saved_before_recording != null ? [["Of which before recording", `${money(p.saved_before_recording)} (estimated)`]] : []),
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
  $("cost-installed").value = t.installed || "";
  $("cost-installed").max = isoDay(new Date());
  $("cost-peak").value = t.peak_rate ?? "";
  $("cost-offpeak").value = t.offpeak_rate ?? "";
  $("cost-from").value = t.offpeak_start || "00:30";
  $("cost-to").value = t.offpeak_end || "05:30";
  $("cost-export").value = t.export_rate ?? "";
  const connected = Boolean(payback && payback.octopus_connected);
  $("costs-manual-row").hidden = !connected;
  $("cost-manual").checked = Boolean(payback && payback.use_manual);
  if (payback && payback.use_manual && payback.manual) {
    // Show the typed prices, not the Octopus ones they override.
    const m = payback.manual;
    $("cost-peak").value = m.peak_rate; $("cost-offpeak").value = m.offpeak_rate;
    $("cost-from").value = m.offpeak_start; $("cost-to").value = m.offpeak_end; $("cost-export").value = m.export_rate;
  }
  fillSuppliers($("cost-supplier"));
  $("cost-supplier").value = payback.supplier;
  syncPresets();
  $("costs-msg").textContent = "";
  $("costs").showModal();
});
$("costs-cancel").addEventListener("click", () => $("costs").close());
const KEYS = { "cost-battery": "battery_cost", "cost-peak": "peak_rate", "cost-offpeak": "offpeak_rate",
  "cost-from": "offpeak_start", "cost-to": "offpeak_end", "cost-export": "export_rate" };
function syncCostInputs() {
  const connected = Boolean(payback && payback.octopus_connected) && $("cost-supplier").value === "octopus";
  const manual = $("cost-manual").checked;
  const fromOctopus = connected && !manual && payback.source === "octopus";
  $("costs-octopus").hidden = !fromOctopus;
  $("costs-octopus-wait").hidden = !(connected && !manual && payback.source !== "octopus");
  for (const id of ["cost-peak", "cost-offpeak", "cost-from", "cost-to", "cost-export"]) $(id).disabled = fromOctopus;
}
$("cost-manual").addEventListener("change", syncCostInputs);
$("costs-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const typed = (payback && payback.manual) || {};
  // While Octopus prices are shown (read-only), keep the typed prices as they were.
  const num = (id) => ($(id).disabled && typed[KEYS[id]] != null ? typed[KEYS[id]] : Number($(id).value) || 0);
  const time = (id, fallback) => ($(id).disabled && typed[KEYS[id]] ? typed[KEYS[id]] : $(id).value || fallback);
  try {
    const p = await postSettings("/api/tariff", {
      battery_cost: num("cost-battery"), installed: $("cost-installed").value, peak_rate: num("cost-peak"), offpeak_rate: num("cost-offpeak"),
      offpeak_start: time("cost-from", "00:00"), offpeak_end: time("cost-to", "00:00"), export_rate: num("cost-export"),
      use_manual: $("cost-manual").checked, supplier: $("cost-supplier").value,
    });
    renderPayback(p);
    $("costs").close();
  } catch (err) {
    $("costs-msg").className = "setup-msg error";
    $("costs-msg").textContent = err.message;
  }
});

// ---------- Octopus prices ----------

let octopus = null;
let priceChart = null;
const pence = (v) => (v == null ? null : `${v.toFixed(2)}p`);
const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
function when(start, end) {
  const s = new Date(start);
  const today = new Date();
  const tomorrow = new Date(today); tomorrow.setDate(today.getDate() + 1);
  const day = s.toDateString() === today.toDateString() ? "Today" : s.toDateString() === tomorrow.toDateString() ? "Tomorrow"
    : s.toLocaleDateString([], { weekday: "short" });
  return `${day} ${clock(start)}–${clock(end)}`;
}
const WINDOW_LABEL = { charge: "Charge", avoid: "Use battery", export: "Export" };

function renderOctopus(o) {
  octopus = o;
  if (!isOctopus()) return renderManualTariff();
  $("tariff-manual").hidden = true;
  $("open-octopus").textContent = o.configured ? "Octopus settings" : "Connect Octopus";
  $("tariff-empty").hidden = o.configured;
  $("tariff-error").hidden = !o.last_error;
  $("tariff-error").textContent = o.last_error ? `Couldn't update from Octopus: ${o.last_error}` : "";
  $("tariff-diag").hidden = !(o.configured || o.last_error);
  $("tariff-diag-text").textContent = JSON.stringify({ account: o.account, import: o.import && o.import.tariff,
    export: o.export && o.export.tariff, last_sync: o.last_sync && new Date(o.last_sync * 1000).toISOString(),
    error: o.last_error, ...o.diagnostics }, null, 2);
  const ready = o.configured && o.import;
  $("tariff-detail").hidden = !ready || !(o.prices && o.prices.length);
  if (!ready) { $("tariff-facts").replaceChildren(); return; }
  factList($("tariff-facts"), [
    ["Tariff", o.import.name],
    ["Price now", pence(o.current_p)],
    ["Standing charge", o.import.standing_charge_p != null ? `${pence(o.import.standing_charge_p)} a day` : null],
    ["Export tariff", o.export ? o.export.name : "None"],
    ["Export price now", pence(o.current_export_p)],
    ["Prices known until", o.prices && o.prices.length ? when(o.prices.at(-1).start, new Date(Date.parse(o.prices.at(-1).start) + 1800e3).toISOString()).replace(/–.*/, "") : null],
  ]);

  const items = [...(o.recommendations || []).map((w) => ({ ...w, label: WINDOW_LABEL[w.kind],
    why: `${w.note} · ${w.kind === "export" ? "about" : "average"} ${pence(w.avg_p)}` })),
    ...(o.dispatches || []).map((d) => ({ ...d, kind: "charge", label: "Smart charge", why: "Intelligent Go slot: the whole home pays the off-peak price" }))]
    .sort((a, b) => a.start.localeCompare(b.start));
  $("tariff-windows").replaceChildren(...(items.length ? items : [{ none: true }]).map((w) => {
    const li = document.createElement("li");
    if (w.none) { li.textContent = "Nothing worth charging from the grid in the prices published so far."; li.className = "note"; return li; }
    const tag = document.createElement("span"); tag.className = `tag ${w.kind}`; tag.textContent = w.label;
    const t = document.createElement("span"); t.textContent = when(w.start, w.end);
    const why = document.createElement("span"); why.className = "why"; why.textContent = w.why;
    li.append(tag, t, why);
    return li;
  }));
  drawPrices(o);
}

function drawPrices(o) {
  if (!o || !o.prices || !o.prices.length || $("tariff-detail").hidden) return;
  const cheap = (o.recommendations || []).filter((w) => w.kind === "charge");
  const inCheap = (iso) => cheap.some((w) => iso >= w.start && iso < w.end);
  const opts = baseOptions();
  opts.scales.y.ticks.callback = (v) => `${v}p`;
  opts.scales.x.ticks.maxTicksLimit = window.innerWidth < 600 ? 4 : 8;
  opts.plugins.tooltip.callbacks = { label: (c) => `${c.dataset.label}: ${c.parsed.y.toFixed(2)}p/kWh` };
  const sets = [{ type: "bar", label: "Import price", data: o.prices.map((p) => p.import_p),
    backgroundColor: o.prices.map((p) => (inCheap(p.start) ? css("--battery") : css("--gridpower"))),
    borderRadius: 2, barPercentage: 1, categoryPercentage: 0.9 }];
  if (o.prices.some((p) => p.export_p != null)) {
    sets.push({ type: "line", label: "Export price", data: o.prices.map((p) => p.export_p), borderColor: css("--export"),
      backgroundColor: css("--export"), pointRadius: 0, borderWidth: 2, stepped: true });
  }
  priceChart?.destroy();
  priceChart = new Chart($("price-chart"), { type: "bar", data: { labels: o.prices.map((p) => clock(p.start)), datasets: sets }, options: opts });
}

async function refreshOctopus() {
  try { renderOctopus(await (await fetch("/api/octopus", { cache: "no-store" })).json()); } catch (_) {}
}

$("open-octopus").addEventListener("click", () => {
  const o = octopus || {};
  $("octopus-account").value = o.account || "";
  $("octopus-key").value = "";
  $("octopus-key").placeholder = o.configured ? "Saved; enter a new key to change it" : "sk_live_…";
  $("octopus-locked").hidden = !o.locked;
  for (const id of ["octopus-account", "octopus-key", "octopus-save"]) $(id).disabled = Boolean(o.locked);
  $("octopus-remove").hidden = !o.configured || o.locked;
  $("octopus-msg").textContent = "";
  $("octopus").showModal();
});
$("octopus-cancel").addEventListener("click", () => $("octopus").close());
async function sendOctopus(body, busyText) {
  $("octopus-msg").className = "setup-msg";
  $("octopus-msg").textContent = busyText;
  $("octopus-save").disabled = true;
  try {
    renderOctopus(await postSettings("/api/octopus", body));
    refreshPayback();
    $("octopus").close();
  } catch (err) {
    $("octopus-msg").className = "setup-msg error";
    $("octopus-msg").textContent = err.message;
  } finally { $("octopus-save").disabled = false; }
}
$("octopus-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const key = $("octopus-key").value.trim();
  if (!key) {
    $("octopus-msg").className = "setup-msg error";
    $("octopus-msg").textContent = "Enter your API key.";
    return;
  }
  sendOctopus({ api_key: key, account: $("octopus-account").value.trim() }, "Checking with Octopus and fetching prices…");
});
$("octopus-remove").addEventListener("click", () => sendOctopus({ api_key: "" }, "Disconnecting…"));

// ---------- Battery control ----------

let controlLoaded = false;
let controlDirty = false; // unsaved cheap-hours edits: don't overwrite them on refresh
let control = null; // last /api/control response
let schedules = [];

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const ACTION_LABEL = { charge: "Charge", hold: "Hold", discharge: "Discharge" };

function modeName(c, mode) {
  if (mode == null) return "unknown";
  return (c.modes || []).find((m) => m.value === mode)?.name || (mode === 3 ? "Third-party control" : `mode ${mode}`);
}

// One line that says plainly whether the dashboard is in charge, and of what.
function controlState(c) {
  // While the dashboard drives the battery its mode reads "third-party", so name the one it'll go back to.
  const app = c.in_control ? (c.saved_mode || "the Anker app mode") : modeName(c, c.battery_mode);
  if (!c.settings.enabled) return ["off", "Control off", `The battery runs in its Anker app mode (${app}).`];
  if (c.reason === "Waiting for the battery") return ["off", "Waiting for the battery", "Control is on but the battery isn't connected."];
  const until = c.window && c.action !== "app" ? ` until ${clock(c.window.end)}` : "";
  const next = c.window && c.action === "app" ? ` Next: ${when(c.window.start, c.window.end)}.` : "";
  if (c.action === "app") {
    return ["idle", c.reason.startsWith("Standing back") ? "Standing back" : "On, using the Anker app mode",
      `${c.reason.startsWith("Standing back") ? c.reason + "." : `Nothing scheduled right now, so the battery runs in ${app}.`}${next}`];
  }
  const doing = { charge: "Charging", hold: "Holding", discharge: "Discharging" }[c.action];
  if (!c.live) return ["dry", `Dry run: would be ${doing.toLowerCase()}${until}`, `${c.reason}. Nothing is written to the battery until CONTROL_LIVE=1 is set.`];
  return ["active", `${doing}${until}`, `${c.reason}.`];
}

function renderControl(c) {
  control = c;
  const live = c.live;
  $("control-mode").textContent = live ? "Live" : "Dry run";
  $("control-mode").className = `pill${live ? " live" : ""}`;
  $("control-mode").title = live ? "Writes to the battery are allowed (CONTROL_LIVE=1)"
    : "Nothing is written to the battery. Set CONTROL_LIVE=1 on the container to allow it.";
  const [kind, title, detail] = controlState(c);
  $("control-state").className = `control-state ${kind}`;
  $("control-state-title").textContent = title;
  $("control-now").textContent = detail;
  $("control-error").hidden = !c.last_error;
  $("control-error").textContent = c.last_error || "";
  const t = c.settings;
  $("control-enabled").checked = t.enabled;
  $("control-enabled-text").textContent = t.enabled ? "On" : "Off";
  if (!controlLoaded || (!controlDirty && !$("control-form").contains(document.activeElement))) {
    $("control-hold").checked = t.hold_cheap;
    $("control-charge").checked = t.grid_charge;
    $("control-dispatch").checked = t.charge_dispatch;
    $("control-power").value = t.charge_power_w;
    $("control-target").value = t.charge_target_soc;
    controlLoaded = true;
  }
  schedules = (t.schedules || []).map((s) => ({ ...s }));
  renderSchedules();
  renderModes(c);
  syncControlInputs();
  $("control-log").replaceChildren(...(c.log.length ? c.log : [{ text: "Nothing yet." }]).map((e) => {
    const li = document.createElement("li");
    if (e.ts) { const t = document.createElement("time"); t.textContent = new Date(e.ts * 1000).toLocaleString(); li.append(t); }
    li.append(e.text);
    return li;
  }));
}

function renderModes(c) {
  const sel = $("mode-select");
  if (!sel.options.length && c.modes) {
    for (const m of c.modes) sel.append(new Option(m.name, m.value));
  }
  const cur = c.battery_mode;
  $("mode-current").textContent = cur === 3
    ? `the dashboard is driving it, and it goes back to ${c.saved_mode || "the Anker app mode"} afterwards` : modeName(c, cur);
  if (sel !== document.activeElement && cur != null && cur !== 3) sel.value = String(cur);
  $("mode-set").disabled = cur == null;
}

$("mode-set").addEventListener("click", async () => {
  const mode = Number($("mode-select").value);
  const name = $("mode-select").selectedOptions[0]?.text || mode;
  if (!confirm(`Switch the battery to ${name} now?`)) return;
  $("mode-set").disabled = true;
  try {
    renderControl(await postSettings("/api/control/mode", { mode }));
  } catch (err) {
    $("control-error").hidden = false;
    $("control-error").textContent = err.message;
  } finally {
    $("mode-set").disabled = false;
  }
});

function dayText(days) {
  const d = [...days].sort();
  if (d.length === 7) return "Every day";
  if (d.join() === "0,1,2,3,4") return "Weekdays";
  if (d.join() === "5,6") return "Weekends";
  return d.map((i) => DAYS[i]).join(", ") || "No days";
}

function scheduleText(s) {
  if (s.action === "hold") return "Hold, no charging or discharging";
  if (s.action === "charge") return `Charge at ${s.power_w} W to ${s.target_soc}%`;
  return `Discharge at ${s.power_w} W down to ${s.target_soc}%`;
}

// ----- schedule timeline: one day at a time, 24 hours across -----
let tlDay = (new Date().getDay() + 6) % 7; // Monday = 0, like the server
const minutes = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return h * 60 + m; };
const hhmm = (min) => `${String(Math.floor(min / 60) % 24).padStart(2, "0")}:${String(min % 60).padStart(2, "0")}`;

// The parts of each schedule that fall on `day`: its own start that day, and
// the tail of yesterday's window if it ran past midnight.
function daySegments(day) {
  const out = [];
  schedules.forEach((s, i) => {
    const a = minutes(s.start), b = minutes(s.end);
    const wraps = b <= a;
    if (s.days.includes(day)) out.push({ i, s, from: a, to: wraps ? 1440 : b });
    if (wraps && b > 0 && s.days.includes((day + 6) % 7)) out.push({ i, s, from: 0, to: b });
  });
  return out;
}

function renderTimeline() {
  const chips = $("tl-days");
  chips.replaceChildren(...DAYS.map((d, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = d;
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(i === tlDay));
    b.addEventListener("click", () => { tlDay = i; renderTimeline(); });
    return b;
  }));
  const track = $("tl-track");
  track.querySelectorAll(".tl-block").forEach((el) => el.remove());
  for (const seg of daySegments(tlDay)) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = `tl-block ${seg.s.action}${seg.s.enabled ? "" : " off"}`;
    el.style.left = `${(seg.from / 1440) * 100}%`;
    el.style.width = `${((seg.to - seg.from) / 1440) * 100}%`;
    el.title = `${ACTION_LABEL[seg.s.action]} ${seg.s.start}–${seg.s.end}${seg.s.enabled ? "" : " (off)"}`;
    el.setAttribute("aria-label", el.title);
    el.textContent = ACTION_LABEL[seg.s.action];
    el.addEventListener("click", () => openSchedule(seg.i));
    el.addEventListener("pointerdown", (e) => e.stopPropagation()); // a tap edits, it doesn't start a new window
    track.append(el);
    if (el.scrollWidth > el.clientWidth) el.textContent = ""; // too narrow to label; the colour says it
  }
}

// Drag across the empty bar to paint a new window, snapped to half hours.
(() => {
  const track = $("tl-track");
  let anchor = null;
  let ghost = null;
  const slotAt = (e) => {
    const r = track.getBoundingClientRect();
    return Math.max(0, Math.min(47, Math.floor(((e.clientX - r.left) / r.width) * 48)));
  };
  const span = (e) => {
    const s = slotAt(e);
    return [Math.min(anchor, s) * 30, (Math.max(anchor, s) + 1) * 30];
  };
  track.addEventListener("pointerdown", (e) => {
    if (document.body.classList.contains("locked") || $("control-fields").disabled) return;
    anchor = slotAt(e);
    track.setPointerCapture(e.pointerId);
    ghost = document.createElement("div");
    ghost.className = "tl-ghost";
    track.append(ghost);
    const [a, b] = span(e);
    ghost.style.left = `${(a / 1440) * 100}%`;
    ghost.style.width = `${((b - a) / 1440) * 100}%`;
  });
  track.addEventListener("pointermove", (e) => {
    if (anchor == null) return;
    const [a, b] = span(e);
    ghost.style.left = `${(a / 1440) * 100}%`;
    ghost.style.width = `${((b - a) / 1440) * 100}%`;
    ghost.textContent = `${hhmm(a)}–${hhmm(b)}`;
  });
  const finish = (e, open) => {
    if (anchor == null) return;
    const [a, b] = span(e);
    anchor = null;
    ghost?.remove();
    if (!open) return;
    // A tap (one half hour) makes an hour-long window to start from.
    openSchedule(-1, { start: hhmm(a), end: hhmm(b - a <= 30 ? Math.min(a + 60, 1440) : b), days: [tlDay] });
  };
  track.addEventListener("pointerup", (e) => finish(e, true));
  track.addEventListener("pointercancel", (e) => finish(e, false));
})();

function renderSchedules() {
  renderTimeline();
  const list = $("schedule-list");
  if (!schedules.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "No schedules yet. Add one to charge, hold or discharge at set times.";
    list.replaceChildren(li);
    return;
  }
  list.replaceChildren(...schedules.map((s, i) => {
    const li = document.createElement("li");
    li.className = s.enabled ? "" : "off";
    li.innerHTML = `
      <label class="switch edit" title="Turn this schedule on or off"><input type="checkbox" role="switch" data-toggle><span></span></label>
      <div class="sched-main">
        <div class="sched-time"><b></b><span class="tag ${s.action}"></span><span class="state"></span></div>
        <div class="sched-what"></div>
      </div>
      <button type="button" class="secondary edit" data-edit>Edit</button>`;
    li.querySelector("[data-toggle]").checked = s.enabled;
    li.querySelector("b").textContent = `${s.start}–${s.end}`;
    li.querySelector(".tag").textContent = ACTION_LABEL[s.action];
    li.querySelector(".state").textContent = s.enabled ? "Enabled" : "Disabled";
    li.querySelector(".sched-what").textContent = `${dayText(s.days)} · ${scheduleText(s)}`;
    li.querySelector("[data-toggle]").addEventListener("change", (e) => {
      schedules[i].enabled = e.target.checked;
      saveControl();
    });
    li.querySelector("[data-edit]").addEventListener("click", () => openSchedule(i));
    return li;
  }));
}

// Everything in the card saves the whole settings object; this builds it from
// the last saved state plus whatever the caller changes.
async function saveControl(changes = {}) {
  const t = control.settings;
  try {
    renderControl(await postSettings("/api/control", {
      enabled: t.enabled, hold_cheap: t.hold_cheap, grid_charge: t.grid_charge, charge_dispatch: t.charge_dispatch,
      charge_power_w: t.charge_power_w, charge_target_soc: t.charge_target_soc,
      schedules, ...changes,
    }));
    return true;
  } catch (err) {
    $("control-error").hidden = false;
    $("control-error").textContent = err.message;
    refreshControl();
    return false;
  }
}

$("control-enabled").addEventListener("change", (e) => {
  const on = e.target.checked;
  if (on && control.live && !confirm("Let the dashboard take control of the battery for your schedules and cheap hours?")) {
    e.target.checked = false;
    return;
  }
  saveControl({ enabled: on });
});

// ----- schedule dialog -----
let editing = -1; // index into schedules, or -1 for a new one

$("sd-days").innerHTML = DAYS.map((d, i) => `<label><input type="checkbox" data-day="${i}">${d}</label>`).join("");

function syncScheduleDialog() {
  const a = $("sd-action").value;
  $("sd-amounts").hidden = a === "hold";
  $("sd-target-label").textContent = a === "discharge" ? "Stop at (%)" : "Charge to (%)";
}
$("sd-action").addEventListener("change", () => {
  if ($("sd-action").value === "discharge" && Number($("sd-target").value) > 50) $("sd-target").value = 20;
  syncScheduleDialog();
});

function openSchedule(i, preset = {}) {
  editing = i;
  const s = i >= 0 ? schedules[i] : { action: "charge", start: "00:30", end: "05:30", days: [0, 1, 2, 3, 4, 5, 6],
    power_w: 1500, target_soc: 90, enabled: true, ...preset };
  $("schedule-title").textContent = i >= 0 ? "Edit schedule" : "New schedule";
  $("sd-action").value = s.action;
  $("sd-start").value = s.start;
  $("sd-end").value = s.end;
  $("sd-power").value = s.power_w;
  $("sd-target").value = s.target_soc;
  for (const box of $("sd-days").querySelectorAll("[data-day]")) box.checked = s.days.includes(Number(box.dataset.day));
  $("schedule-delete").hidden = i < 0;
  $("schedule-error").textContent = "";
  syncScheduleDialog();
  $("schedule-dialog").showModal();
}

$("schedule-add").addEventListener("click", () => openSchedule(-1));
$("schedule-cancel").addEventListener("click", () => $("schedule-dialog").close());

$("schedule-delete").addEventListener("click", async () => {
  if (!confirm("Delete this schedule?")) return;
  schedules.splice(editing, 1);
  if (await saveControl()) $("schedule-dialog").close();
});

$("schedule-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const s = {
    action: $("sd-action").value, start: $("sd-start").value, end: $("sd-end").value,
    days: [...$("sd-days").querySelectorAll("[data-day]")].filter((b) => b.checked).map((b) => Number(b.dataset.day)),
    power_w: Number($("sd-power").value) || 1500, target_soc: Number($("sd-target").value) || 90,
    enabled: editing >= 0 ? schedules[editing].enabled : true,
  };
  if (!s.start || !s.end) { $("schedule-error").textContent = "Pick a start and end time."; return; }
  if (!s.days.length) { $("schedule-error").textContent = "Pick at least one day."; return; }
  const before = schedules.map((x) => ({ ...x }));
  if (editing >= 0) schedules[editing] = s; else schedules.push(s);
  try {
    renderControl(await postSettings("/api/control", { ...control.settings, schedules }));
    $("schedule-dialog").close();
  } catch (err) {
    schedules = before;
    $("schedule-error").textContent = err.message;
  }
});

// ----- cheap hours -----
for (const ev of ["input", "change"]) $("control-form").addEventListener(ev, () => { controlDirty = true; $("control-saved").textContent = ""; });

function syncControlInputs() {
  const charging = $("control-charge").checked || $("control-dispatch").checked;
  $("control-power").disabled = !charging;
  $("control-target").disabled = !charging;
}
for (const id of ["control-charge", "control-dispatch"]) $(id).addEventListener("change", syncControlInputs);

async function refreshControl() {
  try { renderControl(await (await fetch("/api/control", { cache: "no-store" })).json()); } catch (_) {}
}

$("control-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  controlDirty = false;
  if (await saveControl({
    hold_cheap: $("control-hold").checked, grid_charge: $("control-charge").checked, charge_dispatch: $("control-dispatch").checked,
    charge_power_w: Number($("control-power").value) || 1500, charge_target_soc: Number($("control-target").value) || 90,
  })) {
    $("control-saved").textContent = "Saved";
    document.activeElement?.blur();
  } else controlDirty = true;
});

// Battery care is a notice: only tips worth acting on, and once dismissed it
// stays hidden until a different tip comes up.
const CARE_KEY = "care-dismissed";
let careKey = "";

async function refreshCare() {
  let c;
  try { c = await (await fetch("/api/battery-care", { cache: "no-store" })).json(); } catch (_) { return; }
  const tips = c.tips.filter((t) => t.level === "suggest");
  careKey = tips.map((t) => t.title).join("|");
  let dismissed = "";
  try { dismissed = localStorage.getItem(CARE_KEY) || ""; } catch (_) {}
  $("care-card").hidden = !tips.length || dismissed === careKey;
  $("care-tips").replaceChildren(...tips.map((t) => {
    const li = document.createElement("li"); li.className = t.level;
    const b = document.createElement("b"); b.textContent = t.title;
    const span = document.createElement("span"); span.textContent = t.text;
    li.append(b, span);
    return li;
  }));
}

$("care-dismiss").addEventListener("click", () => {
  try { localStorage.setItem(CARE_KEY, careKey); } catch (_) {}
  $("care-card").hidden = true;
});

// ---------- Tariff comparison ----------

let customTariffs = [];
const pounds = (v) => (v == null ? "–" : `£${Math.round(v).toLocaleString()}`);

function renderCustom(list) {
  customTariffs = list || [];
  $("custom-list").replaceChildren(...customTariffs.map((t, i) => {
    const li = document.createElement("li");
    const text = document.createElement("span");
    text.textContent = `${t.name}: ${t.peak_rate}p, ${t.offpeak_rate}p ${t.offpeak_start}–${t.offpeak_end}, export ${t.export_rate}p, standing ${t.standing_p}p`;
    const del = document.createElement("button"); del.type = "button"; del.className = "link edit"; del.textContent = "Remove";
    del.addEventListener("click", () => saveCustom(customTariffs.filter((_, j) => j !== i)));
    li.append(text, del);
    return li;
  }));
}

async function saveCustom(list) {
  try {
    renderCustom((await postSettings("/api/compare/custom", list)).custom);
    $("compare-msg").className = "setup-msg ok";
    $("compare-msg").textContent = "Saved. Press Compare to include it.";
  } catch (err) {
    $("compare-msg").className = "setup-msg error";
    $("compare-msg").textContent = err.message;
  }
}

$("custom-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const n = (id) => Number($(id).value) || 0;
  saveCustom([...customTariffs, { name: $("custom-name").value.trim(), peak_rate: n("custom-peak"), offpeak_rate: n("custom-offpeak"),
    offpeak_start: $("custom-from").value || "00:00", offpeak_end: $("custom-to").value || "00:00",
    export_rate: n("custom-export"), standing_p: n("custom-standing") }]);
});

let presetList = [];

function renderPresets(presets) {
  presetList = presets || [];
  const sel = $("custom-preset");
  const keep = sel.value;
  sel.replaceChildren(...presetList.map((p, i) => new Option(p.name, String(i))), new Option("Other (enter your own)", "other"));
  sel.value = [...sel.options].some((o) => o.value === keep) ? keep : "other";
}

$("custom-preset").addEventListener("change", () => {
  const p = presetList[Number($("custom-preset").value)];
  if (p) {
    $("custom-name").value = p.name;
    $("custom-from").value = p.offpeak_start;
    $("custom-to").value = p.offpeak_end;
    $("custom-peak").focus();
  } else {
    $("custom-name").value = "";
    $("custom-name").focus();
  }
});

// What the battery does on this tariff in the replay, in plain words.
function batteryPlan(row) {
  const d = row.daily || {};
  const solar = d.solar_stored_kwh > 0.1 ? ` It also stores about ${d.solar_stored_kwh} kWh of spare solar a day.` : "";
  if (row.charge_window) {
    return `Battery charges from the grid ${row.charge_window} (about ${d.grid_charge_kwh} kWh a day) and covers the house in dearer hours.${solar}`;
  }
  if (row.battery_mode === "flat") {
    return solar ? `One price all day, so grid charging saves nothing.${solar}`
      : "One price all day, so the battery can't save anything here without solar.";
  }
  return `Cheap and peak prices are too close for grid charging to pay after about 10% charging losses.${solar}`;
}

$("compare-more").addEventListener("click", () => {
  for (const li of $("compare-result").children) li.hidden = false;
  $("compare-more").hidden = true;
});

$("compare-run").addEventListener("click", async () => {
  $("compare-msg").className = "setup-msg";
  $("compare-msg").textContent = "Fetching tariffs and replaying your history…";
  $("compare-run").disabled = true;
  let r;
  try {
    const res = await fetch(`/api/compare?region=${encodeURIComponent($("compare-region").value)}`, { cache: "no-store" });
    r = await res.json();
    if (!res.ok) throw new Error(r.detail || `Request failed (${res.status})`);
  } catch (err) {
    $("compare-msg").className = "setup-msg error";
    $("compare-msg").textContent = err.message;
    return;
  } finally { $("compare-run").disabled = false; }
  renderCustom(r.custom);
  renderPresets(r.presets);
  $("compare-msg").textContent = r.message || (r.problems && r.problems.length ? `Some tariffs were skipped: ${r.problems.join("; ")}` : "");
  $("compare-result").hidden = !r.rows.length;
  $("compare-assumptions").hidden = !r.rows.length;
  // One compact line per tariff (cheapest first); the details open on a tap.
  const SHOWN = 5;
  $("compare-result").replaceChildren(...r.rows.map((row, i) => {
    const li = document.createElement("li");
    if (i === 0) li.className = "best";
    if (row.key === "current") li.classList.add("current");
    li.hidden = i >= SHOWN && row.key !== "current";
    const det = document.createElement("details");
    const sum = document.createElement("summary");
    const name = document.createElement("span"); name.className = "name"; name.textContent = row.name;
    const total = document.createElement("span"); total.className = "total"; total.textContent = pounds(row.annual);
    const vs = document.createElement("span"); vs.className = "vs";
    if (row.key === "current") vs.textContent = "now";
    else if (row.vs_current != null) {
      vs.className += row.vs_current < 0 ? " cheaper" : " dearer";
      vs.textContent = `${row.vs_current < 0 ? "−" : "+"}${pounds(Math.abs(row.vs_current))}`;
    }
    sum.append(name, total, vs);
    const b = row.breakdown || {};
    const parts = [["House", b.home], ["Battery charging", b.battery_charging], ["Standing charge", b.standing], ["Export credit", b.export, true]]
      .filter(([label, v]) => v != null && (v || label === "House"))
      .map(([label, v, minus]) => `${label} ${minus ? "−" : ""}${pounds(v)}`);
    if (row.avg_import_p != null) parts.push(`average ${pence(row.avg_import_p)}/kWh from the grid`);
    const more = document.createElement("div"); more.className = "more";
    for (const t of [batteryPlan(row), parts.join(" · "), row.export ? `Export: ${row.export}` : "", row.note || ""].filter(Boolean)) {
      const p = document.createElement("p"); p.textContent = t; more.append(p);
    }
    det.append(sum, more);
    li.append(det);
    return li;
  }));
  const extra = r.rows.length - [...$("compare-result").children].filter((li) => !li.hidden).length;
  $("compare-more").hidden = extra <= 0;
  $("compare-more").textContent = `Show ${extra} more`;
  const best = r.rows[0];
  const cur = r.rows.find((x) => x.key === "current");
  let summary = "";
  if (best && cur && best.key !== "current" && best.vs_current < 0) summary = `${best.name} looks about ${pounds(-best.vs_current)} a year cheaper than what you pay now. `;
  else if (best && cur && best.key === "current") summary = "Your current tariff already looks the cheapest. ";
  if (r.without_battery && cur) summary += `On your tariff the battery saves about ${pounds(r.without_battery.annual - cur.annual)} a year. `;
  if (r.days) summary += `Based on ${r.days} day${r.days === 1 ? "" : "s"} of history`
    + (r.daily_use_kwh != null ? `: your home uses about ${r.daily_use_kwh} kWh a day` : "")
    + (r.battery ? `, with a ${r.battery.kwh} kWh battery charging at up to ${r.battery.kw} kW.` : ".");
  $("compare-summary").textContent = summary;
  $("compare-assumption-list").replaceChildren(...(r.assumptions || []).map((a) => { const li = document.createElement("li"); li.textContent = a; return li; }));
});

// ---------- Setup ----------

let setupShownOnce = false;
let setupDevice = "battery";
let meterPromptShown = false;
let savedSettings = {};
// ---------- Event log ----------

const EVENT_PAGE = 10;
let eventsPage = 1;
let eventsHeight = 0;

function eventTime(ts) {
  const d = new Date(ts * 1000);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function renderEvents(events) {
  const body = $("events-table").querySelector("tbody");
  body.replaceChildren(...events.map((e) => {
    const tr = document.createElement("tr");
    for (const text of [eventTime(e.ts), DEVICE_LABEL[e.device] || e.device, e.message]) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.append(td);
    }
    return tr;
  }));
  $("events-empty").hidden = events.length > 0;
  $("events-table").hidden = events.length === 0;
  // Hold the tallest page seen (a short last page, or messages wrapping on a
  // phone) so the card and its page buttons don't jump about.
  const wrap = $("events-table").parentElement;
  wrap.style.minHeight = "";
  eventsHeight = Math.max(eventsHeight, wrap.offsetHeight);
  wrap.style.minHeight = `${eventsHeight}px`;
}

// Page numbers to show: the first, the last, and two either side of the
// current one (one on a phone, so the buttons fit on one line), with null
// where a run is left out.
function pageList(current, pages) {
  const near = window.matchMedia("(max-width: 520px)").matches ? 1 : 2;
  const keep = new Set([1, pages]);
  for (let p = current - near; p <= current + near; p++) if (p >= 1 && p <= pages) keep.add(p);
  const out = [];
  [...keep].sort((a, b) => a - b).forEach((p, i, all) => {
    if (i && p - all[i - 1] > 1) out.push(p - all[i - 1] === 2 ? p - 1 : null);
    out.push(p);
  });
  return out;
}

function renderPager(pages) {
  const nav = $("events-pager");
  nav.hidden = pages <= 1;
  if (pages <= 1) return nav.replaceChildren();
  const button = (label, page, opts = {}) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    if (opts.aria) b.setAttribute("aria-label", opts.aria);
    if (page === eventsPage && !opts.step) b.setAttribute("aria-current", "page");
    b.disabled = opts.step ? page < 1 || page > pages : false;
    b.addEventListener("click", () => { eventsPage = page; refreshEvents(); });
    return b;
  };
  nav.replaceChildren(
    button("‹", eventsPage - 1, { step: true, aria: "Newer" }),
    ...pageList(eventsPage, pages).map((p) => {
      if (p !== null) return button(String(p), p, { aria: `Page ${p}` });
      const gap = document.createElement("span");
      gap.className = "gap";
      gap.textContent = "…";
      return gap;
    }),
    button("›", eventsPage + 1, { step: true, aria: "Older" }),
  );
}

async function refreshEvents() {
  const params = new URLSearchParams({
    limit: EVENT_PAGE, offset: (eventsPage - 1) * EVENT_PAGE, kind: $("events-filter").value,
  });
  const r = await fetch(`/api/events?${params}`);
  if (!r.ok) return;
  const events = await r.json();
  const pages = Math.max(1, Math.ceil(Number(r.headers.get("X-Total-Count") || 0) / EVENT_PAGE));
  if (eventsPage > pages) { eventsPage = pages; return refreshEvents(); }
  renderEvents(events);
  renderPager(pages);
}

$("events-filter").addEventListener("change", () => { eventsPage = 1; eventsHeight = 0; refreshEvents(); });
window.addEventListener("resize", () => { eventsHeight = 0; });

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
  const headers = { "Content-Type": "application/json" };
  if (auth.csrf) headers["X-CSRF-Token"] = auth.csrf;
  const res = await fetch(path, { method: "POST", headers, body: JSON.stringify(body) });
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (res.status === 401 || res.status === 403) refreshAuth();
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
  // First run only: ask who the supplier is alongside the battery's address.
  $("setup-supplier-row").hidden = device !== "battery" || !payback || payback.supplier_picked;
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

// ---------- Lock ----------
// With a password (ADMIN_PASSWORD or one set here), changes need signing in;
// with READ_ONLY they're off.
// The server enforces both; this only hides the controls that would fail.

let auth = { password_set: false, read_only: false, signed_in: false, can_edit: true, csrf: null };

function applyAuth(a) {
  auth = a;
  document.body.classList.toggle("locked", !a.can_edit);
  $("control-fields").disabled = !a.can_edit;
  const btn = $("lock-btn");
  btn.hidden = false;
  btn.classList.toggle("open", a.can_edit);
  const label = a.read_only ? "Read-only: changes are turned off in the container settings"
    : !a.password_set ? "Unlocked: anyone can make changes. Click to set a password"
    : a.signed_in ? "Signed in. Click to sign out or change the password" : "Locked. Click to sign in and make changes";
  btn.title = label;
  btn.setAttribute("aria-label", label);
}

async function refreshAuth() {
  try { applyAuth(await (await fetch("/api/auth", { cache: "no-store" })).json()); } catch (_) {}
}

const FORGOT = " If you forget it, delete auth.json from the data volume and restart the container.";

function openPassword() {
  const env = auth.password_source === "env";
  $("pw-intro").textContent = env ? "The password is set by ADMIN_PASSWORD in the container settings. Change it there."
    : auth.password_set ? "Changes are locked behind this password. Change it here, or remove it to unlock the dashboard." + FORGOT
    : "Anyone who can open the dashboard can change its settings and the battery. Set a password to lock changes; viewing stays open." + FORGOT;
  $("pw-current-row").hidden = env || !auth.password_set;
  $("pw-new-rows").hidden = env;
  $("pw-save").hidden = env;
  $("pw-save").textContent = auth.password_set ? "Change password" : "Set password";
  $("pw-remove").hidden = env || !auth.password_set;
  $("pw-signout").hidden = !auth.signed_in;
  for (const id of ["pw-current", "pw-new", "pw-confirm"]) $(id).value = "";
  $("pw-msg").textContent = "";
  $("pw").showModal();
}

async function sendPassword(body) {
  try {
    applyAuth(await postSettings("/api/auth/password", body));
    $("pw").close();
  } catch (err) {
    $("pw-msg").className = "setup-msg error";
    $("pw-msg").textContent = err.message;
  }
}

$("pw-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if ($("pw-new").value !== $("pw-confirm").value) {
    $("pw-msg").className = "setup-msg error";
    $("pw-msg").textContent = "The two new passwords don't match.";
    return;
  }
  sendPassword({ current: $("pw-current").value, new: $("pw-new").value });
});
$("pw-remove").addEventListener("click", () => {
  if (!$("pw-current").value) {
    $("pw-msg").className = "setup-msg error";
    $("pw-msg").textContent = "Enter the current password to remove it.";
    return;
  }
  sendPassword({ current: $("pw-current").value, new: "" });
});
$("pw-signout").addEventListener("click", async () => {
  try { applyAuth(await (await fetch("/api/auth/logout", { method: "POST" })).json()); } catch (_) {}
  $("pw").close();
  refreshOctopus();
});
$("pw-cancel").addEventListener("click", () => $("pw").close());

$("lock-btn").addEventListener("click", async () => {
  if (auth.read_only) return;
  if (!auth.password_set || auth.signed_in) { openPassword(); return; }
  $("login-msg").textContent = "";
  $("login-password").value = "";
  $("login").showModal();
  $("login-password").focus();
});

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    applyAuth(await postSettings("/api/auth/login", { password: $("login-password").value }));
    $("login").close();
    refreshOctopus();
    refreshControl();
  } catch (err) {
    $("login-msg").className = "setup-msg error";
    $("login-msg").textContent = err.message;
  }
});
$("login-cancel").addEventListener("click", () => $("login").close());

// ---------- Updates ----------
// The server compares its build with the newest published image. "Update now"
// asks Portainer (through the stack's webhook) to re-pull and recreate it.

let updates = null;
const buildDate = (d) => (d ? new Date(`${d}T12:00:00Z`).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" }) : "");
const versionLabel = (v) => (v && v.name ? `v${v.name}` : "a newer version");

function renderUpdates(u) {
  updates = u;
  $("update-note").hidden = !u.update_available;
  if (u.update_available) {
    const built = buildDate(u.latest.built);
    $("update-text").textContent = `Update available: ${versionLabel(u.latest)}${built ? `, built ${built}` : ""}.`
      + (u.webhook_set ? "" : " Re-pull the stack in Portainer to install it.");
  }
  $("update-now").hidden = !u.webhook_set;
}

async function refreshUpdates() {
  try { renderUpdates(await (await fetch("/api/version", { cache: "no-store" })).json()); } catch (_) {}
}

function openUpdates() {
  const u = updates || {};
  const running = u.running && u.running.name !== "dev" ? `v${u.running.name}` : "a development build";
  $("updates-status").textContent = `Running ${running}. `
    + (u.error ? u.error : !u.checked_at ? "Not checked for updates yet."
      : u.update_available ? `${versionLabel(u.latest)} is available.` : "This is the newest version.");
  $("updates-url").value = "";
  $("updates-url").placeholder = u.webhook_set ? "Saved (paste a new one to replace it)" : "https://192.168.0.10:9443/api/stacks/webhooks/…";
  for (const id of ["updates-url", "updates-save"]) $(id).disabled = Boolean(u.webhook_locked);
  $("updates-locked").hidden = !u.webhook_locked;
  $("updates-remove").hidden = !u.webhook_set || u.webhook_locked;
  $("updates-msg").textContent = "";
  $("updates").showModal();
}

async function saveWebhook(url) {
  try {
    renderUpdates(await postSettings("/api/update/webhook", { url }));
    $("updates").close();
  } catch (err) {
    $("updates-msg").className = "setup-msg error";
    $("updates-msg").textContent = err.message;
  }
}

async function waitForNewVersion(from) {
  // The container is replaced while this runs, so failed requests are expected.
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 5000));
    try {
      const v = (await (await fetch("/api/live", { cache: "no-store" })).json()).version;
      if (v && v.commit !== from) { location.reload(); return; }
    } catch (_) {}
  }
  $("update-text").textContent = "The update hasn't come through yet. Check the stack in Portainer.";
  $("update-now").disabled = false;
}

$("update-now").addEventListener("click", async () => {
  const from = updates && updates.running && updates.running.commit;
  $("update-now").disabled = true;
  $("update-note").hidden = false;
  $("update-text").textContent = "Updating. The dashboard reloads when the new version is running.";
  try {
    await postSettings("/api/update", {});
  } catch (err) {
    if (!(err instanceof TypeError)) {  // a TypeError is the connection closing as the container restarts
      $("update-text").textContent = err.message;
      $("update-now").disabled = false;
      return;
    }
  }
  waitForNewVersion(from);
});
$("update-open").addEventListener("click", openUpdates);
$("footer-updates").addEventListener("click", openUpdates);
$("updates-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if ($("updates-url").value.trim()) saveWebhook($("updates-url").value.trim());
  else $("updates").close();
});
$("updates-remove").addEventListener("click", () => saveWebhook(""));
$("updates-cancel").addEventListener("click", () => $("updates").close());

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

// ---------- Collapsible cards ----------
// Any <section class="card" id="..." data-collapsible> gets its title turned
// into a toggle; collapsed cards keep just their title, and the choice is
// remembered per card id. Cards added later can call makeCollapsible(card).

const COLLAPSED_KEY = "collapsed-cards";
function collapsedCards() {
  try { return new Set(JSON.parse(localStorage.getItem(COLLAPSED_KEY)) || []); } catch (_) { return new Set(); }
}

function makeCollapsible(card) {
  const h2 = card.querySelector("h2");
  if (!h2 || !card.id || h2.querySelector(".collapse-toggle")) return;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "collapse-toggle";
  btn.innerHTML = '<svg class="chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  btn.append(...h2.childNodes);
  h2.append(btn);
  const show = (collapsed) => {
    card.classList.toggle("collapsed", collapsed);
    btn.setAttribute("aria-expanded", String(!collapsed));
    btn.title = collapsed ? "Show" : "Hide";
  };
  show(collapsedCards().has(card.id));
  btn.addEventListener("click", () => {
    const set = collapsedCards();
    const collapsed = !card.classList.contains("collapsed");
    collapsed ? set.add(card.id) : set.delete(card.id);
    try { localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...set])); } catch (_) {}
    show(collapsed);
  });
}
document.querySelectorAll(".card[data-collapsible]").forEach(makeCollapsible);

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
  drawPrices(octopus);
});

darkQuery.addEventListener("change", () => { showTheme(); refreshHistory(); refreshEnergy(); drawPrices(octopus); });
showTheme();

refreshAuth().then(refreshLive);
refreshUpdates();
setInterval(refreshUpdates, 30 * 60 * 1000);
refreshHistory();
refreshEnergy();
refreshPayback();
refreshEvents();
refreshOctopus();
refreshRuntime();
setInterval(refreshRuntime, HISTORY_MS);
refreshControl();
refreshCare();
fetch("/api/compare/custom", { cache: "no-store" }).then((r) => r.json()).then((r) => { renderCustom(r.custom); renderPresets(r.presets); }).catch(() => {});
setInterval(refreshControl, 15000);
setInterval(refreshCare, 10 * 60 * 1000);
setInterval(refreshPayback, 5 * 60 * 1000);
setInterval(refreshOctopus, 5 * 60 * 1000);
setInterval(refreshLive, LIVE_MS);
setInterval(() => { refreshHistory(); refreshEnergy(); refreshEvents(); }, HISTORY_MS);
// Browsers slow timers down in background tabs, so catch up as soon as the
// page is looked at again rather than showing old numbers.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { refreshLive(); refreshHistory(); refreshEnergy(); refreshEvents(); }
});
