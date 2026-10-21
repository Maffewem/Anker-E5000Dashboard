"use strict";

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
  if (!c.settings.enabled && !c.override) return ["off", "Control off", `The battery runs in its Anker app mode (${app}).`];
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
    $("control-happy").checked = t.charge_happy;
    $("control-balance").checked = t.auto_balance;
    $("control-balance-days").value = t.balance_days;
    $("control-power").value = t.charge_power_w;
    $("control-target").value = t.charge_target_soc;
    controlLoaded = true;
  }
  const room = c.happy_room;
  $("happy-status").hidden = !room;
  $("happy-status").textContent = room
    ? `Charging to ${room.target}% now, leaving room for about ${room.kwh} kWh in the ${room.slot}.` : "";
  const b = c.balance || {};
  const last = b.last_full ? `Last full ${new Date(b.last_full * 1000).toLocaleDateString()}.` : "No full charge recorded yet.";
  $("balance-status").textContent = !t.auto_balance ? "" : b.active ? "Balancing now: charging to 100%."
    : b.due ? `${last} A balance charge is due and will run in the next cheap, free or scheduled charge window.` : `${last} Not due yet.`;
  happyHours = (t.happy_hours || []).map((h) => ({ ...h }));
  renderHappy();
  schedules = (t.schedules || []).map((s) => ({ ...s }));
  renderSchedules();
  renderModes(c);
  renderLimits(c);
  renderOverride(c);
  syncControlInputs();
  $("control-log").replaceChildren(...(c.log.length ? c.log : [{ text: "Nothing yet." }]).map((e) => {
    const li = document.createElement("li");
    if (e.ts) { const t = document.createElement("time"); t.textContent = new Date(e.ts * 1000).toLocaleString(); li.append(t); }
    li.append(e.text);
    return li;
  }));
}

function renderOverride(c) {
  const o = c.override;
  $("override-cancel").hidden = !o;
  $("override-state").textContent = o
    ? `${ACTION_LABEL[o.action]} until ${clock(o.until)}${o.action === "hold" ? "" : ` (${o.power_w} W, ${o.action === "charge" ? "up to" : "down to"} ${o.target_soc}%)`}, then back to normal.`
    : "Charge, hold or discharge for a while without making a schedule. It ends by itself and works even when control is off.";
}

document.querySelectorAll("[data-override]").forEach((btn) => btn.addEventListener("click", async () => {
  const action = btn.dataset.override;
  const minutes = Number($("override-minutes").value);
  if (action !== "off" && !confirm(`${ACTION_LABEL[action]} the battery now for ${$("override-minutes").selectedOptions[0].text}?`)) return;
  btn.disabled = true;
  try {
    renderControl(await postSettings("/api/control/override", { action, minutes }));
  } catch (err) {
    $("control-error").hidden = false;
    $("control-error").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}));

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

// ----- the battery's own charge and discharge limits -----
let limitsDirty = false;
const DEFAULT_TARGET = 80; // if the server hasn't said otherwise

function renderLimits(c) {
  const l = c.limits || {};
  const known = l.charging_limit_soc != null;
  $("limits-current").textContent = known
    ? `Now: charge to ${l.charging_limit_soc}%, reserve ${l.discharge_limit_soc ?? "?"}% (it won't discharge below that).` : "";
  if (!limitsDirty && !$("limits-form").contains(document.activeElement)) {
    $("limit-charge").value = l.charging_limit_soc ?? "";
    $("limit-discharge").value = l.discharge_limit_soc ?? "";
  }
  for (const el of $("limits-form").querySelectorAll("input, button")) el.disabled = !known;
}

for (const ev of ["input", "change"]) $("limits-form").addEventListener(ev, () => { limitsDirty = true; $("limits-saved").textContent = ""; });

$("limits-recommended").addEventListener("click", () => {
  const r = (control && control.recommended_limits) || { charging_limit_soc: 80, discharge_limit_soc: 10 };
  $("limit-charge").value = r.charging_limit_soc;
  $("limit-discharge").value = r.discharge_limit_soc;
  limitsDirty = true;
  $("limits-saved").textContent = "Press Set limits to send them to the battery";
});

$("limits-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { charging_limit_soc: Number($("limit-charge").value), discharge_limit_soc: Number($("limit-discharge").value) };
  if (!confirm(`Set the battery to charge to ${body.charging_limit_soc}% with a ${body.discharge_limit_soc}% reserve?`)) return;
  try {
    renderControl(await postSettings("/api/control/limits", body));
    limitsDirty = false;
    renderLimits(control);
    $("limits-saved").textContent = control.live ? "Set" : "Dry run: nothing written (CONTROL_LIVE isn't set)";
    document.activeElement?.blur();
  } catch (err) {
    $("control-error").hidden = false;
    $("control-error").textContent = err.message;
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

let happyHours = [];

function renderHappy() {
  const list = $("happy-list");
  list.replaceChildren(...happyHours.map((h, i) => {
    const li = document.createElement("li");
    li.textContent = `${h.date} ${h.start}-${h.end} `;
    const del = document.createElement("button");
    del.type = "button";
    del.textContent = "Remove";
    del.className = "secondary";
    del.addEventListener("click", () => saveControl({ happy_hours: happyHours.filter((_, j) => j !== i) }));
    li.append(del);
    return li;
  }));
}

$("happy-add").addEventListener("click", () => {
  const h = { date: $("happy-date").value, start: $("happy-start").value, end: $("happy-end").value };
  if (!h.date || !h.start || !h.end) return;
  saveControl({ happy_hours: [...happyHours, h] });
});

// Everything in the card saves the whole settings object; this builds it from
// the last saved state plus whatever the caller changes.
async function saveControl(changes = {}) {
  const t = control.settings;
  try {
    renderControl(await postSettings("/api/control", {
      enabled: t.enabled, hold_cheap: t.hold_cheap, grid_charge: t.grid_charge, charge_dispatch: t.charge_dispatch,
      charge_happy: t.charge_happy, happy_hours: happyHours,
      auto_balance: t.auto_balance, balance_days: t.balance_days,
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
    power_w: 1500, target_soc: control?.settings.charge_target_soc ?? DEFAULT_TARGET, enabled: true, ...preset };
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
    power_w: Number($("sd-power").value) || 1500, target_soc: Number($("sd-target").value) || DEFAULT_TARGET,
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
    charge_happy: $("control-happy").checked,
    auto_balance: $("control-balance").checked, balance_days: Number($("control-balance-days").value) || 28,
    charge_power_w: Number($("control-power").value) || 1500, charge_target_soc: Number($("control-target").value) || DEFAULT_TARGET,
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
