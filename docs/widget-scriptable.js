// iPhone home-screen / lock-screen widget for the Anker Solarbank dashboard.
// Needs the free Scriptable app. Set URL and TOKEN, then add a Scriptable widget
// to the home screen and choose this script.
const URL = "https://anker.example.com/api/widget"; // your tunnel address
const TOKEN = "paste-the-WIDGET_TOKEN-here";

async function load() {
  const req = new Request(URL);
  req.headers = { Authorization: `Bearer ${TOKEN}` };
  req.timeoutInterval = 15;
  return await req.loadJSON();
}

const kw = (w) => (w == null ? "–" : `${(w / 1000).toFixed(2)} kW`);
const widget = new ListWidget();
widget.backgroundColor = new Color("#14181f");
try {
  const d = await load();
  const title = widget.addText(`🔋 ${d.soc == null ? "–" : Math.round(d.soc)}%`);
  title.font = Font.boldSystemFont(26);
  title.textColor = Color.white();
  const line = (label, value) => {
    const t = widget.addText(`${label}  ${value}`);
    t.font = Font.systemFont(13);
    t.textColor = new Color("#c9d1d9");
  };
  line("☀️ Solar", kw(d.solar_w));
  line("🏠 Home", kw(d.home_w));
  line("⚡ Grid", kw(d.grid_w));
  if (d.saved_today != null) line("💷 Saved today", `£${d.saved_today.toFixed(2)}`);
  if (!d.online || (d.age_s != null && d.age_s > 600)) line("⚠️", "data is stale");
} catch (e) {
  widget.addText("Can't reach the dashboard").textColor = Color.red();
}
widget.refreshAfterDate = new Date(Date.now() + 5 * 60 * 1000);
if (config.runsInWidget) Script.setWidget(widget);
else await widget.presentSmall();
Script.complete();
