// Optional phone layout: a bottom tab bar that shows one group of cards at a
// time. Opt in with ?layout=tabs (remembered), back out with ?layout=classic.
// Only active on narrow screens; the classic single-column page is untouched.
(function () {
  const GROUPS = [
    ["now", "Now", ["tiles", "power-card", "care-card", "meter-card", "solarbank-card"]],
    ["energy", "Energy", ["energy-card", "trends-card"]],
    ["money", "Money", ["tariff-card", "compare-card", "payback-card", "spend-card"]],
    ["control", "Control", ["control-card"]],
    ["log", "Log", ["events-card", "export-card"]],
  ];
  try {
    const q = new URLSearchParams(location.search).get("layout");
    if (q === "tabs" || q === "classic") localStorage.setItem("layout", q);
    if (localStorage.getItem("layout") !== "tabs") return;
  } catch (_) { return; }

  const mq = matchMedia("(max-width: 700px)");
  const main = document.querySelector("main");
  const nav = document.createElement("nav");
  nav.className = "tabbar";
  nav.setAttribute("aria-label", "Sections");
  const place = {};
  GROUPS.forEach(([key, label, ids]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.dataset.tab = key;
    b.addEventListener("click", () => select(key));
    nav.append(b);
    ids.forEach((id) => { place[id] = key; });
  });
  document.body.append(nav);

  function select(key) {
    document.documentElement.dataset.tab = key;
    nav.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tab === key)));
    main.querySelectorAll(":scope > *").forEach((el) => {
      const sel = el.id ? place[el.id] : el.classList.contains("tiles") ? "now" : "now";
      el.classList.toggle("tab-off", sel !== key);
    });
    try { sessionStorage.setItem("tab", key); } catch (_) {}
    window.scrollTo(0, 0);
    window.dispatchEvent(new Event("resize")); // charts re-measure once visible
  }
  function apply() {
    document.documentElement.classList.toggle("tabs", mq.matches);
    if (!mq.matches) main.querySelectorAll(".tab-off").forEach((el) => el.classList.remove("tab-off"));
    else {
      let k = "now";
      try { k = sessionStorage.getItem("tab") || "now"; } catch (_) {}
      select(k);
    }
  }
  mq.addEventListener("change", apply);
  apply();
})();
