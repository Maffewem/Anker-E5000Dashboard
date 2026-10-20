"use strict";

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

const DEVICE_LABEL = { battery: "Solarbank", meter: "Smart Meter", tariff: "Tariff" };
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
