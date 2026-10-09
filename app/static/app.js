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

function runtimeText(r) {
  const charging = $("battery-detail").textContent.startsWith("Charging");
  if (r.soc <= r.floor_soc + 0.5 && !charging) return `At its ${Math.round(r.floor_soc)}% discharge limit`;
  const low = r.empty_at ? `down to ${Math.round(r.floor_soc)}% around ${atTime(r.empty_at)}` : null;
  if (charging && r.full_at) return `Full around ${atTime(r.full_at)}${low ? `, then ${low}` : ""}`;
  if (low) return low[0].toUpperCase() + low.slice(1);
  if (r.recharges_at) return `Lasts until it charges at ${atTime(r.recharges_at)}`;
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

function renderPayback(p) {
  payback = p;
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
  syncCostInputs();
  $("costs-msg").textContent = "";
  $("costs").showModal();
});
$("costs-cancel").addEventListener("click", () => $("costs").close());
const KEYS = { "cost-battery": "battery_cost", "cost-peak": "peak_rate", "cost-offpeak": "offpeak_rate",
  "cost-from": "offpeak_start", "cost-to": "offpeak_end", "cost-export": "export_rate" };
function syncCostInputs() {
  const connected = Boolean(payback && payback.octopus_connected);
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
      use_manual: $("cost-manual").checked,
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
let controlDirty = false; // unsaved edits: don't overwrite them on refresh

function renderControl(c) {
  const live = c.live;
  $("control-mode").textContent = live ? "Live" : "Dry run";
  $("control-mode").className = `pill${live ? " live" : ""}`;
  $("control-mode").title = live ? "Writes to the battery are allowed (CONTROL_LIVE=1)"
    : "Nothing is written to the battery. Set CONTROL_LIVE=1 on the container to allow it.";
  let now = c.reason;
  if (c.window && c.action === "app") now += `. Next window: ${when(c.window.start, c.window.end)}.`;
  else if (c.window) now += `. Until ${clock(c.window.end)}.`;
  $("control-now").textContent = now;
  $("control-error").hidden = !c.last_error;
  $("control-error").textContent = c.last_error || "";
  if (!controlLoaded || (!controlDirty && !$("control-card").contains(document.activeElement))) {
    const t = c.settings;
    $("control-enabled").checked = t.enabled;
    $("control-hold").checked = t.hold_cheap;
    $("control-charge").checked = t.grid_charge;
    $("control-power").value = t.charge_power_w;
    $("control-target").value = t.charge_target_soc;
    renderSchedules(t.schedules || []);
    controlLoaded = true;
  }
  renderModes(c);
  syncControlInputs();
  $("control-log").replaceChildren(...(c.log.length ? c.log : [{ text: "Nothing yet." }]).map((e) => {
    const li = document.createElement("li");
    if (e.ts) { const t = document.createElement("time"); t.textContent = new Date(e.ts * 1000).toLocaleString(); li.append(t); }
    li.append(e.text);
    return li;
  }));
}

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const ACTION_LABEL = { charge: "Charge", hold: "Hold", discharge: "Discharge" };

function renderModes(c) {
  const sel = $("mode-select");
  if (!sel.options.length && c.modes) {
    for (const m of c.modes) sel.append(new Option(m.name, m.value));
  }
  const cur = c.battery_mode;
  const name = cur == null ? "unknown" : (c.modes || []).find((m) => m.value === cur)?.name
    || (cur === 3 ? "Third-party control, run by the dashboard" : `mode ${cur}`);
  $("mode-current").textContent = `(now: ${name})`;
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

function scheduleRow(s) {
  const li = document.createElement("li");
  const n = Math.random().toString(36).slice(2, 8);
  li.innerHTML = `
    <div class="sched-grid">
      <div><label for="sa-${n}">Action</label><select id="sa-${n}" data-k="action">
        ${Object.entries(ACTION_LABEL).map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select></div>
      <div><label for="ss-${n}">From</label><input id="ss-${n}" type="time" data-k="start" required></div>
      <div><label for="se-${n}">To</label><input id="se-${n}" type="time" data-k="end" required></div>
      <div data-power><label for="sp-${n}">Power (W)</label><input id="sp-${n}" type="number" min="100" max="5000" step="100" data-k="power_w"></div>
      <div data-target><label for="st-${n}"></label><input id="st-${n}" type="number" min="5" max="100" step="5" data-k="target_soc"></div>
    </div>
    <div class="days">${DAYS.map((d, i) => `<label><input type="checkbox" data-day="${i}">${d}</label>`).join("")}</div>
    <div class="sched-foot">
      <label class="compare"><input type="checkbox" data-k="enabled"> On</label>
      <button type="button" class="link" data-remove>Remove</button>
    </div>`;
  const q = (k) => li.querySelector(`[data-k="${k}"]`);
  q("action").value = s.action;
  q("start").value = s.start;
  q("end").value = s.end;
  q("power_w").value = s.power_w;
  q("target_soc").value = s.target_soc;
  q("enabled").checked = s.enabled !== false;
  for (const box of li.querySelectorAll("[data-day]")) box.checked = s.days.includes(Number(box.dataset.day));
  const sync = () => {
    const a = q("action").value;
    li.querySelector("[data-power]").hidden = a === "hold";
    li.querySelector("[data-target]").hidden = a === "hold";
    li.querySelector("[data-target] label").textContent = a === "discharge" ? "Stop at (%)" : "Charge to (%)";
  };
  q("action").addEventListener("change", () => {
    if (q("action").value === "discharge" && Number(q("target_soc").value) > 50) q("target_soc").value = 20;
    sync();
  });
  li.querySelector("[data-remove]").addEventListener("click", () => { li.remove(); emptyNote(); });
  sync();
  return li;
}

function emptyNote() {
  const list = $("schedule-list");
  list.querySelector(".empty")?.remove();
  if (!list.children.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "No schedules yet. Add one to charge, hold or discharge at set times.";
    list.append(li);
  }
}

function renderSchedules(list) {
  $("schedule-list").replaceChildren(...list.map(scheduleRow));
  emptyNote();
}

function readSchedules() {
  return [...$("schedule-list").querySelectorAll("li:not(.empty)")].map((li) => {
    const q = (k) => li.querySelector(`[data-k="${k}"]`);
    return {
      action: q("action").value, start: q("start").value, end: q("end").value,
      days: [...li.querySelectorAll("[data-day]")].filter((b) => b.checked).map((b) => Number(b.dataset.day)),
      power_w: Number(q("power_w").value) || 1500, target_soc: Number(q("target_soc").value) || 90,
      enabled: q("enabled").checked,
    };
  });
}

for (const ev of ["input", "change"]) $("control-form").addEventListener(ev, () => { controlDirty = true; });
$("schedule-list").addEventListener("click", (e) => { if (e.target.closest("[data-remove]")) controlDirty = true; });

$("schedule-add").addEventListener("click", () => {
  controlDirty = true;
  $("schedule-list").querySelector(".empty")?.remove();
  $("schedule-list").append(scheduleRow({ action: "charge", start: "00:30", end: "05:30", days: [0, 1, 2, 3, 4, 5, 6],
    power_w: 1500, target_soc: 90, enabled: true }));
});

function syncControlInputs() {
  const on = $("control-enabled").checked;
  $("control-hold").disabled = !on;
  $("control-charge").disabled = !on;
  const charging = on && $("control-charge").checked;
  $("control-power").disabled = !charging;
  $("control-target").disabled = !charging;
}
for (const id of ["control-enabled", "control-charge"]) $(id).addEventListener("change", syncControlInputs);

async function refreshControl() {
  try { renderControl(await (await fetch("/api/control", { cache: "no-store" })).json()); } catch (_) {}
}

$("control-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    renderControl(await postSettings("/api/control", {
      enabled: $("control-enabled").checked, hold_cheap: $("control-hold").checked, grid_charge: $("control-charge").checked,
      charge_power_w: Number($("control-power").value) || 1500, charge_target_soc: Number($("control-target").value) || 90,
      schedules: readSchedules(),
    }));
    controlDirty = false;
    renderSchedules((await (await fetch("/api/control", { cache: "no-store" })).json()).settings.schedules);
    document.activeElement?.blur();
  } catch (err) {
    $("control-error").hidden = false;
    $("control-error").textContent = err.message;
  }
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
  $("compare-result").replaceChildren(...r.rows.map((row, i) => {
    const li = document.createElement("li");
    if (i === 0) li.className = "best";
    if (row.key === "current") li.classList.add("current");
    const info = document.createElement("div");
    const strong = document.createElement("b"); strong.textContent = row.name;
    const extra = [
      batteryPlan(row),
      row.export ? `Export: ${row.export}` : "", row.note || "",
    ].filter(Boolean);
    info.append(strong, ...extra.map((t) => { const sm = document.createElement("small"); sm.textContent = t; return sm; }));
    const money = document.createElement("div");
    money.className = "money";
    const total = document.createElement("b"); total.textContent = `${pounds(row.annual)} a year`;
    money.append(total);
    if (row.vs_current != null && row.key !== "current") {
      const vs = document.createElement("small");
      vs.className = row.vs_current < 0 ? "cheaper" : "dearer";
      vs.textContent = `${pounds(Math.abs(row.vs_current))} ${row.vs_current < 0 ? "less" : "more"} than now`;
      money.append(vs);
    }
    const b = row.breakdown || {};
    const parts = [["House", b.home], ["Battery charging", b.battery_charging], ["Standing charge", b.standing], ["Export credit", b.export, true]]
      .filter(([label, v]) => v != null && (v || label === "House"))
      .map(([label, v, minus]) => `${label} ${minus ? "−" : ""}${pounds(v)}`);
    const split = document.createElement("small"); split.textContent = parts.join(" · ");
    li.append(info, money, split);
    return li;
  }));
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
// With ADMIN_PASSWORD set, changes need signing in; with READ_ONLY they're off.
// The server enforces both; this only hides the controls that would fail.

let auth = { password_set: false, read_only: false, signed_in: false, can_edit: true, csrf: null };

function applyAuth(a) {
  auth = a;
  document.body.classList.toggle("locked", !a.can_edit);
  $("control-fields").disabled = !a.can_edit;
  const btn = $("lock-btn");
  btn.hidden = !a.password_set && !a.read_only;
  btn.classList.toggle("open", a.can_edit);
  const label = a.read_only ? "Read-only: changes are turned off in the container settings"
    : a.signed_in ? "Signed in. Click to sign out" : "Locked. Click to sign in and make changes";
  btn.title = label;
  btn.setAttribute("aria-label", label);
}

async function refreshAuth() {
  try { applyAuth(await (await fetch("/api/auth", { cache: "no-store" })).json()); } catch (_) {}
}

$("lock-btn").addEventListener("click", async () => {
  if (auth.read_only) return;
  if (auth.signed_in) {
    try { applyAuth(await (await fetch("/api/auth/logout", { method: "POST" })).json()); } catch (_) {}
    refreshOctopus();
    return;
  }
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
