"use strict";

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
