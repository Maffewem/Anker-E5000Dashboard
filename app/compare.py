"""Compare tariffs by replaying recorded home use and solar against each one.

For every half hour the dashboard has recorded, it knows how much the house
used and how much solar there was. Those don't depend on the tariff, so the
same history can be priced on any tariff, with the battery run the way that
suits that tariff:

- solar the house doesn't need charges the battery, then is exported;
- in the day's cheapest half hours the battery holds (the house uses grid
  power) and, when it pays after losses, charges from the grid;
- the rest of the time the battery covers the house until it reaches its
  reserve.

Costs are scaled to a year from the days recorded, so the estimate gets
better as history builds up and covers more seasons.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .octopus import Client, OctopusError, half_hours, local_slot
from .tariff import fixed_profile, slot_time

log = logging.getLogger("solarbank.compare")

EFFICIENCY = 0.9  # round trip, applied when charging
RESERVE = 0.1  # share of capacity kept back, as the discharge limit does

# Octopus product families, matched on product code. Export pairing follows
# Octopus's own rules: Agile Outgoing and Flux export go with their import.
IMPORT_FAMILIES = (
    ("agile", "Agile Octopus", lambda c: c.startswith("AGILE-") and "OUTGOING" not in c),
    ("go", "Octopus Go", lambda c: c.startswith("GO-VAR-")),
    ("intelligent_go", "Intelligent Octopus Go", lambda c: c.startswith("INTELLI-VAR-")),
    ("cosy", "Cosy Octopus", lambda c: c.startswith("COSY-")),
    ("flux", "Octopus Flux", lambda c: c.startswith("FLUX-IMPORT-")),
    ("tracker", "Octopus Tracker", lambda c: c.startswith("SILVER-")),
    ("flexible", "Flexible Octopus", lambda c: c.startswith("VAR-")),
)
# Products Octopus doesn't always show in its public list; tried by code, newest first.
KNOWN_CODES = {"intelligent_go": ("INTELLI-VAR-24-10-29", "INTELLI-VAR-22-10-14")}
EXPORT_FAMILIES = (
    ("agile_outgoing", lambda c: c.startswith("AGILE-OUTGOING-")),
    ("flux_export", lambda c: c.startswith("FLUX-EXPORT-")),
    ("outgoing", lambda c: c.startswith("OUTGOING-VAR-") or c.startswith("OUTGOING-FIX-")),
)
EXPORT_FOR = {"agile": "agile_outgoing", "flux": "flux_export"}

# Fixed windows for suppliers without a public price list; the prices are typed in.
PRESETS = [
    {"name": "E.ON Next Drive", "offpeak_start": "00:00", "offpeak_end": "07:00"},
    {"name": "EDF GoElectric Overnight", "offpeak_start": "00:00", "offpeak_end": "05:00"},
    {"name": "British Gas Electric Driver", "offpeak_start": "00:00", "offpeak_end": "05:00"},
]


@dataclass
class Candidate:
    key: str
    name: str
    standing_p: float | None
    import_actual: dict = field(default_factory=dict)  # (day, slot) -> p
    import_profile: list = field(default_factory=lambda: [None] * 48)
    export_actual: dict = field(default_factory=dict)
    export_profile: list = field(default_factory=lambda: [None] * 48)
    export_name: str | None = None
    note: str = ""
    source: str = "octopus"

    def import_p(self, day: str, slot: int) -> float | None:
        v = self.import_actual.get((day, slot))
        return v if v is not None else self.import_profile[slot]

    def export_p(self, day: str, slot: int) -> float:
        v = self.export_actual.get((day, slot))
        v = v if v is not None else self.export_profile[slot]
        return v or 0.0


def profile(prices: dict) -> list:
    """Average price per half hour of the day, for days a price list doesn't cover."""
    sums, counts = [0.0] * 48, [0] * 48
    for (_, slot), p in prices.items():
        if p is not None:
            sums[slot] += p
            counts[slot] += 1
    return [sums[i] / counts[i] if counts[i] else None for i in range(48)]


def usage_by_day(slots: list[dict]) -> dict[str, list]:
    """kWh of house use minus solar per half hour, grouped by day.

    Half hours with only part of their minutes recorded are scaled up; ones
    with fewer than ten minutes are dropped.
    """
    days: dict[str, list] = {}
    for r in slots:
        if r["minutes"] < 10:
            continue
        scale = 30 / min(30, r["minutes"])
        days.setdefault(r["day"], [None] * 48)[r["slot"]] = (r["home_wh"] - r["solar_wh"]) * scale / 1000
    return days


def _ranges(slots: set[int]) -> str:
    """Half-hour slots as readable time ranges, e.g. "00:30-05:30"."""
    out, run = [], []
    for s in sorted(slots):
        if run and s != run[-1] + 1:
            out.append(run)
            run = []
        run.append(s)
    if run:
        out.append(run)
    if len(out) > 1 and out[0][0] == 0 and out[-1][-1] == 47:  # wraps past midnight
        out[0] = out.pop() + out[0]
    return ", ".join(f"{slot_time(r[0])}-{slot_time(r[-1] + 1)}" for r in out)


def cheap_slots(known: list[tuple[float, int]], need: int) -> tuple[set[int], float]:
    """The day's cheap half hours and the dearest price among them.

    Prices are taken a level at a time (within 0.5p counts as one level), so a
    whole off-peak window on Go or Cosy comes in together. Levels are added
    until there are enough half hours to fill the battery, but never the top
    level, and never one that would make more than half the day cheap: a big
    battery or a slow charger can't make Go's day rate or Cosy's mid rate
    "cheap" just because the off-peak window is too short to fill it.
    With one price all day there's nothing cheap to hold for.
    """
    levels: list[list[tuple[float, int]]] = []
    for p, s in sorted(known):
        if levels and p <= levels[-1][0][0] + 0.5:
            levels[-1].append((p, s))
        else:
            levels.append([(p, s)])
    cheap: set[int] = set()
    cut = 0.0
    for level in levels[:-1]:
        if cheap and len(cheap) + len(level) > len(known) / 2:
            break
        cheap |= {s for _, s in level}
        cut = level[-1][0]
        if len(cheap) >= need:
            break
    return cheap, cut


def simulate(c: Candidate, days: dict[str, list], cap_kwh: float, power_kw: float, battery: bool = True) -> dict:
    """Import, export and cost over the recorded days on one tariff, with a
    breakdown of where the money goes."""
    floor = cap_kwh * RESERVE
    soc = floor  # start empty, so the battery never brings free energy into the comparison
    step = power_kw * 0.5  # kWh per half hour
    imp_kwh = exp_kwh = 0.0
    home_p = charge_p = export_p = 0.0  # pence: house from the grid, grid charging, export credit
    charge_kwh = from_battery_kwh = 0.0
    counted = 0
    windows: dict[str, int] = {}  # how often each charging window was used
    modes: dict[str, int] = {}  # per day: "grid" charging pays, "flat" one price, "small_gap" not worth it
    solar_kwh = 0.0
    for day in sorted(days):
        net = days[day]
        prices = [c.import_p(day, s) for s in range(48)]
        known = [(p, s) for s, p in enumerate(prices) if p is not None and net[s] is not None]
        if not known:
            continue
        counted += 1
        need = max(1, math.ceil((cap_kwh - floor) / max(step, 0.01)))
        cheap, cut = cheap_slots(known, need)
        # Grid charging pays when a stored kWh, after losses, costs less than
        # the grid price it replaces: the dearest half hours the house uses,
        # up to one battery's worth. With no solar this is all the battery does.
        offset, left = 0.0, cap_kwh - floor
        for p, s in sorted(((p, s) for p, s in known if s not in cheap), reverse=True):
            take = min(max(net[s], 0.0), left)
            offset += take * p
            left -= take
            if left <= 0:
                break
        used = (cap_kwh - floor) - left
        grid_charge_pays = bool(cheap) and used > 0 and cut / EFFICIENCY < offset / used
        mode = "grid" if grid_charge_pays else ("flat" if not cheap else "small_gap")
        modes[mode] = modes.get(mode, 0) + 1
        if battery and grid_charge_pays:
            key = _ranges(cheap)
            windows[key] = windows.get(key, 0) + 1
        for p, s in sorted(known, key=lambda x: x[1]):
            load = net[s]
            house = charged = 0.0  # grid kWh for the house and for the battery
            exported = 0.0
            if load < 0:  # spare solar
                spare = -load
                stored = min(spare, step, (cap_kwh - soc) / EFFICIENCY) if battery else 0.0
                soc += stored * EFFICIENCY
                solar_kwh += stored
                exported = spare - stored
            elif battery and s in cheap:
                house = load  # hold: the house uses cheap grid power
                if grid_charge_pays:
                    charged = max(0.0, min(step, (cap_kwh - soc) / EFFICIENCY))
                    soc += charged * EFFICIENCY
            else:
                use = max(0.0, min(load, step, soc - floor)) if battery else 0.0
                soc -= use
                from_battery_kwh += use
                house = load - use
            imp_kwh += house + charged
            charge_kwh += charged
            exp_kwh += exported
            home_p += house * p
            charge_p += charged * p
            export_p += exported * c.export_p(day, s)
    standing_p = (c.standing_p or 0.0) * counted
    total = (home_p + charge_p - export_p + standing_p) / 100
    year = 365 / counted if counted else 0
    yearly = lambda pence: round(pence / 100 * year, 0) if counted else None  # noqa: E731
    return {"days": counted, "import_kwh": round(imp_kwh, 1), "export_kwh": round(exp_kwh, 1),
            "cost": round(total, 2), "annual": round(total * year, 0) if counted else None,
            "breakdown": {"home": yearly(home_p), "battery_charging": yearly(charge_p),
                          "export": yearly(export_p), "standing": yearly(standing_p)},
            "daily": {"grid_charge_kwh": round(charge_kwh / counted, 1) if counted else 0,
                      "from_battery_kwh": round(from_battery_kwh / counted, 1) if counted else 0,
                      "solar_stored_kwh": round(solar_kwh / counted, 1) if counted else 0},
            "avg_import_p": round((home_p + charge_p) / imp_kwh, 1) if imp_kwh > 0 else None,
            "charge_window": max(windows, key=windows.get) if windows else None,
            "battery_mode": max(modes, key=modes.get) if modes else None}


class Comparer:
    """Builds the candidate tariffs (fetching Octopus price lists) and runs them."""

    CACHE_SECONDS = 12 * 3600

    def __init__(self, fetch=None) -> None:
        self.fetch = fetch
        self._cache: dict = {}

    def _cached(self, key, make):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.CACHE_SECONDS:
            return hit[1]
        value = make()
        self._cache[key] = (time.time(), value)
        return value

    def _products(self, client: Client) -> list[dict]:
        def load():
            out, page = [], 1
            while page <= 10:
                body = client._get("/v1/products/", {"is_business": "false", "page": page})
                out += body.get("results") or []
                if not body.get("next"):
                    break
                page += 1
            return out
        return self._cached("products", load)

    @staticmethod
    def _latest(products: list[dict], match) -> dict | None:
        found = [p for p in products if match(p.get("code", "")) and not p.get("is_business")]
        found.sort(key=lambda p: p.get("available_from") or "")
        return found[-1] if found else None

    def _prices(self, client: Client, code: str, region: str, start: datetime, end: datetime, tz) -> tuple[dict, float | None]:
        tariff = f"E-1R-{code}-{region}"

        def load():
            rows = client.unit_rates(code, tariff, start, end)
            if not rows:  # a new product version: use what it charges now
                now = datetime.now(timezone.utc)
                rows = client.unit_rates(code, tariff, now - timedelta(days=1), now + timedelta(days=1))
                span = (now - timedelta(days=1), now + timedelta(days=1))
            else:
                span = (start, end)
            prices = {local_slot(ts, tz): p for ts, p in half_hours(rows, *span).items()}
            try:
                standing = client.standing_charge(code, tariff)
            except OctopusError:
                standing = None
            return prices, standing
        return self._cached(("prices", tariff, start.date(), end.date()), load)

    def _by_code(self, client: Client, codes, name: str, region: str, start: datetime, end: datetime, tz):
        """The first of `codes` with prices, as (product, prices, standing charge)."""
        for code in codes:
            try:
                prices, standing = self._prices(client, code, region, start, end, tz)
            except OctopusError:
                continue
            if prices:
                return {"code": code, "display_name": name}, prices, standing
        return None, {}, None

    def candidates(self, region: str, start: datetime, end: datetime, tz, current: Candidate | None,
                   custom: list[dict]) -> tuple[list[Candidate], list[str]]:
        problems: list[str] = []
        out: list[Candidate] = [current] if current else []
        client = Client("", "", self.fetch)
        try:
            products = self._products(client)
        except OctopusError as err:
            products = []
            problems.append(f"Couldn't fetch Octopus tariffs: {err}")
        exports: dict[str, tuple[str, dict, float | None]] = {}
        for key, match in EXPORT_FAMILIES:
            p = self._latest(products, match)
            if p:
                try:
                    exports[key] = (p.get("display_name") or p["code"], *self._prices(client, p["code"], region, start, end, tz))
                except OctopusError as err:
                    problems.append(f"{p['code']}: {err}")
        default_export = current if current and (current.export_actual or any(current.export_profile)) else None
        for key, name, match in IMPORT_FAMILIES:
            p = self._latest(products, match)
            if not p and key in KNOWN_CODES:
                p, prices, standing = self._by_code(client, KNOWN_CODES[key], name, region, start, end, tz)
                if not p:
                    problems.append(f"{name}: Octopus didn't publish its prices for region {region}")
                    continue
            elif not p:
                continue
            else:
                try:
                    prices, standing = self._prices(client, p["code"], region, start, end, tz)
                except OctopusError as err:
                    problems.append(f"{name}: {err}")
                    continue
            if not prices:
                problems.append(f"{name}: no prices for region {region}")
                continue
            c = Candidate(key, p.get("display_name") or name, standing, prices, profile(prices))
            pair = EXPORT_FOR.get(key)
            if pair and pair in exports:
                c.export_name, c.export_actual, _ = exports[pair]
                c.export_profile = profile(c.export_actual)
            elif default_export:
                c.export_name = default_export.export_name or "Your export tariff"
                c.export_actual, c.export_profile = default_export.export_actual, default_export.export_profile
            elif "outgoing" in exports:
                c.export_name, c.export_actual, _ = exports["outgoing"]
                c.export_profile = profile(c.export_actual)
            if key == "intelligent_go":
                c.note = "Core off-peak hours only; extra smart-charge slots would make it cheaper. Needs a compatible EV or charger."
            elif key in ("go",):
                c.note = "Needs an EV or charger."
            elif key == "cosy":
                c.note = "Needs a heat pump."
            elif key == "flux":
                c.note = "Needs solar and a battery; doesn't model exporting from the battery in the evening peak."
            out.append(c)
        for t in custom:
            try:
                c = Candidate("custom", t["name"], float(t.get("standing_p") or 0),
                              import_profile=fixed_profile(float(t["peak_rate"]), float(t["offpeak_rate"]),
                                                           t["offpeak_start"], t["offpeak_end"]),
                              export_profile=[float(t.get("export_rate") or 0)] * 48, source="custom",
                              note="Your prices")
                c.export_name = f"{float(t.get('export_rate') or 0):g}p export"
                out.append(c)
            except (KeyError, ValueError, TypeError):
                problems.append(f"Skipped {t.get('name', 'a custom tariff')}: incomplete prices")
        return out, problems


def compare(candidates: list[Candidate], usage: list[dict], cap_kwh: float, power_kw: float) -> dict:
    days = usage_by_day(usage)
    rows = []
    for c in candidates:
        r = simulate(c, days, cap_kwh, power_kw)
        rows.append({"key": c.key, "name": c.name, "export": c.export_name, "standing_p": c.standing_p,
                     "note": c.note, "source": c.source, **r})
    current = next((c for c in candidates if c.key == "current"), None)
    without = simulate(current, days, cap_kwh, power_kw, battery=False) if current else None
    ranked = sorted([r for r in rows if r["annual"] is not None], key=lambda r: r["annual"])
    base = next((r for r in rows if r["key"] == "current"), None)
    for r in ranked:
        r["vs_current"] = round(r["annual"] - base["annual"], 0) if base and base["annual"] is not None else None
    home: dict[str, float] = {}
    for r in usage:
        if r["minutes"] >= 10:
            home[r["day"]] = home.get(r["day"], 0.0) + r["home_wh"] * 30 / min(30, r["minutes"]) / 1000
    used = list(home.values())
    return {
        "days": len(days),
        "daily_use_kwh": round(sum(used) / len(used), 1) if used else None,
        "rows": ranked,
        "best": ranked[0]["name"] if ranked else None,
        "without_battery": without,
        "battery": {"kwh": cap_kwh, "kw": power_kw},
        "assumptions": [
            f"Uses the {len(days)} day(s) of home use and solar recorded so far, scaled to a year. "
            "More history, especially across seasons, makes this more reliable.",
            f"Battery: {cap_kwh:g} kWh, {power_kw:g} kW, {EFFICIENCY:.0%} round-trip efficiency, {RESERVE:.0%} kept in reserve.",
            "On each tariff the battery stores any spare solar, then charges from the grid in that tariff's cheapest "
            "hours each day (up to full) whenever that costs less, after losses, than the peak electricity it "
            "replaces, holding so the house runs on the cheap price. Without solar, grid charging is all it does. "
            "The rest of the day it covers the house's half-hour by half-hour use until it reaches its reserve. "
            "It isn't used to export to the grid.",
            "Each tariff's yearly cost is split into the house's own grid use, grid electricity used to charge "
            "the battery, the standing charge, and export credit (taken off).",
            "Where a price list doesn't go back far enough, its average price for each half hour is used. "
            "Prices include VAT. The standing charge is included where Octopus publishes it.",
            "Eligibility (EV, heat pump, smart meter) isn't checked: see the notes.",
        ],
    }
