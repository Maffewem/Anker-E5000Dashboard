"use strict";

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
    for (const [id, k] of [["custom-peak", "peak_rate"], ["custom-offpeak", "offpeak_rate"], ["custom-standing", "standing_p"], ["custom-export", "export_rate"]]) {
      if (p[k] != null) $(id).value = p[k];
    }
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

let compareRows = [];
const SHOWN = 5;

// Cheapest first, optionally for one supplier. The tariff you pay now stays so there's something to compare against.
function renderRows(showAll = false) {
  const want = $("compare-provider").value;
  const rows = compareRows.filter((row) => !want || row.provider === want || row.key === "current");
  $("compare-result").replaceChildren(...rows.map((row, i) => {
    const li = rowItem(row, i);
    li.hidden = !showAll && i >= SHOWN && row.key !== "current";
    return li;
  }));
  const extra = rows.length - [...$("compare-result").children].filter((li) => !li.hidden).length;
  $("compare-more").hidden = extra <= 0;
  $("compare-more").textContent = `Show ${extra} more`;
}

$("compare-more").addEventListener("click", () => renderRows(true));
$("compare-provider").addEventListener("change", () => renderRows());

function rowItem(row, i) {
  const li = document.createElement("li");
  if (i === 0) li.className = "best";
  if (row.key === "current") li.classList.add("current");
  if (row.source === "indicative") li.classList.add("estimate");
  const det = document.createElement("details");
  const sum = document.createElement("summary");
  const name = document.createElement("span"); name.className = "name"; name.textContent = row.source === "indicative" ? `${row.name} (est.)` : row.name;
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
  compareRows = r.rows;
  const providers = [...new Set(r.rows.filter((x) => x.key !== "current").map((x) => x.provider))];
  const keep = $("compare-provider").value;
  $("compare-provider").replaceChildren(new Option("All suppliers", ""), ...providers.map((p) => new Option(p, p)));
  $("compare-provider").value = providers.includes(keep) ? keep : "";
  $("compare-provider-row").hidden = providers.length < 2;
  renderRows();
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
