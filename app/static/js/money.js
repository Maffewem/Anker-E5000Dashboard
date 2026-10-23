"use strict";

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
    if (p.early_estimate) eta += `. Early guess from ${p.recorded_days} days; it settles after ${p.settled_days}`;
  } else if (p.payback_too_long) {
    eta = "More than 50 years at this rate";
  } else if (p.recorded_days < p.min_days) {
    eta = `Collecting data: ${p.recorded_days} of ${p.min_days} days${p.tariff.installed ? "" : ". Add the install date to count the battery's history"}`;
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
  refreshSpend();
}

// ---------- Charging spend per day ----------

const SPEND_DAYS = 14;

function renderSpend(c) {
  const shown = c.days.slice(0, SPEND_DAYS);
  const any = c.days.some((d) => d.recorded);
  $("spend-empty").hidden = any;
  $("spend-table").hidden = !any;
  const total = (t) => (t.days ? `${money(t.grid_charge_cost)} (${kwh(t.grid_charge_kwh)})` : null);
  factList($("spend-facts"), any ? [
    ["Today so far", total(c.today) || money(0)],
    ["Last 7 days", total(c.week)],
    ["Last 30 days", total(c.month)],
    ["Average per day", money(c.average_per_day)],
  ] : []);
  const most = Math.max(0.01, ...shown.map((d) => d.grid_charge_cost));
  const label = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  $("spend-table").tBodies[0].replaceChildren(...shown.map((d) => {
    const tr = document.createElement("tr");
    const cells = d.recorded
      ? [label(d.day), kwh(d.grid_charge_kwh), money(d.grid_charge_cost), "",
        d.grid_charge_p_per_kwh != null ? pence(d.grid_charge_p_per_kwh) : "–", money(d.saved)]
      : [label(d.day), "–", "–", "", "–", "–"];
    cells.forEach((text, i) => {
      const td = document.createElement("td");
      if (i === 3) {
        td.className = "spend-bar-col";
        const bar = document.createElement("span");
        bar.className = "spend-bar";
        bar.style.width = `${Math.round((d.grid_charge_cost / most) * 100)}%`;
        td.append(bar);
      } else td.textContent = text;
      tr.append(td);
    });
    return tr;
  }));
}

async function refreshSpend() {
  try { renderSpend(await (await fetch("/api/charging-costs?days=30", { cache: "no-store" })).json()); } catch (_) {}
}

$("open-costs").addEventListener("click", () => {
  const t = (payback && payback.tariff) || {};
  $("cost-battery").value = t.battery_cost || "";
  $("cost-installed").value = t.installed || "";
  $("cost-installed").max = isoDay(new Date());
  $("cost-prices-from").value = "";
  $("cost-prices-from").max = isoDay(new Date());
  $("cost-peak").value = t.peak_rate ?? "";
  $("cost-offpeak").value = t.offpeak_rate ?? "";
  $("cost-from").value = t.offpeak_start || "00:30";
  $("cost-to").value = t.offpeak_end || "05:30";
  $("cost-export").value = t.export_rate ?? "";
  $("cost-standing").value = (payback && payback.manual ? payback.manual.standing_p : t.standing_p) ?? "";
  $("cost-include-standing").checked = payback && payback.manual ? payback.manual.include_standing !== false : true;
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
  "cost-from": "offpeak_start", "cost-to": "offpeak_end", "cost-export": "export_rate", "cost-standing": "standing_p" };
function syncCostInputs() {
  const connected = Boolean(payback && payback.octopus_connected) && $("cost-supplier").value === "octopus";
  const manual = $("cost-manual").checked;
  const fromOctopus = connected && !manual && payback.source === "octopus";
  $("costs-octopus").hidden = !fromOctopus;
  $("costs-octopus-wait").hidden = !(connected && !manual && payback.source !== "octopus");
  for (const id of ["cost-peak", "cost-offpeak", "cost-from", "cost-to"]) $(id).disabled = fromOctopus;
  // Octopus has no export price on some accounts; then the typed one is used and stays editable.
  $("cost-export").disabled = fromOctopus && Boolean(payback.export_from_octopus);
  // Octopus publishes the standing charge for the tariff; show it and keep the typed one for later.
  const sc = fromOctopus && payback.tariff && payback.tariff.standing_p ? payback.tariff.standing_p : null;
  $("cost-standing").disabled = sc != null;
  if (sc != null) $("cost-standing").value = sc;
  else if (!$("cost-standing").value && payback && payback.manual) $("cost-standing").value = payback.manual.standing_p ?? "";
  $("cost-standing-hint").textContent = sc != null ? "Taken from your Octopus tariff."
    : "Optional. Counted in the cost trends and projections; it doesn't change the battery's savings.";
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
      offpeak_start: time("cost-from", "00:00"), offpeak_end: time("cost-to", "00:00"), export_rate: num("cost-export"), standing_p: num("cost-standing"), include_standing: $("cost-include-standing").checked,
      use_manual: $("cost-manual").checked, supplier: $("cost-supplier").value, prices_from: $("cost-prices-from").value,
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
  // Only when a sync fails: the error plus what's needed to look into it. The full steps are in the container log.
  const known = [o.import && o.import.tariff, o.last_sync && `last updated ${new Date(o.last_sync * 1000).toLocaleString()}`].filter(Boolean);
  $("tariff-error").textContent = o.last_error
    ? `Couldn't update from Octopus: ${o.last_error}${known.length ? ` (${known.join(", ")})` : ""}` : "";
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
