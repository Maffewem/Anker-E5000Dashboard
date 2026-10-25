"""Weekly and monthly trends, and simple projections from recent days.

Everything comes from the half-hour `slots` table, which is never pruned,
so trends cover the whole time the dashboard has been recording. Each half
hour is priced the same way as the battery payback: Octopus prices where
they were fetched, the typed tariff otherwise. Net cost includes the typed
standing charge (zero until one is entered) for each recorded day.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from .tariff import Tariff, _pricer, discharge_value, standing_for

# Projections use the average of this many recent full days: long enough to
# smooth one sunny or grey day, short enough to follow the season.
RECENT_DAYS = 14
# Fewer recorded days than this and there is nothing to project from.
MIN_RECENT = 3

ENERGY = ("solar", "home", "import", "export", "charge", "discharge")
MONEY = ("import_cost", "export_earned", "standing_cost", "net_cost", "battery_saved")


def daily_totals(tariff: Tariff, slots: list[dict], prices: dict | None = None) -> dict[str, dict]:
    """kWh and pounds per local day from half-hour totals."""
    price = _pricer(tariff, prices)
    days: dict[str, dict] = {}
    for r in slots:
        imp, exp = price(r["day"], r["slot"])  # pounds per kWh
        d = days.setdefault(r["day"], dict.fromkeys(ENERGY + MONEY, 0.0))
        kwh = {k: (r.get(f"{k}_wh") or 0.0) / 1000 for k in ("solar", "home", "import", "export", "discharge")}
        kwh["charge"] = ((r.get("grid_charge_wh") or 0.0) + (r.get("solar_charge_wh") or 0.0)) / 1000
        for k, v in kwh.items():
            d[k] += v
        d["import_cost"] += kwh["import"] * imp
        d["export_earned"] += kwh["export"] * exp
        # As in the payback: discharge saves buying at this half hour's price;
        # grid charging costs that price, solar charging the export it gave up.
        d["battery_saved"] += (discharge_value({"discharge_wh": r.get("discharge_wh") or 0.0,
                                                "discharge_export_wh": r.get("discharge_export_wh")}, imp, exp) - (r.get("grid_charge_wh") or 0.0) / 1000 * imp
                               - (r.get("solar_charge_wh") or 0.0) / 1000 * exp)
    for day, d in days.items():
        d["standing_cost"] = standing_for(prices, day, tariff.standing_p) / 100 if tariff.include_standing else 0.0
        d["net_cost"] = d["import_cost"] + d["standing_cost"] - d["export_earned"]
    return days


def period_start(day: date, period: str) -> date:
    if period == "day":
        return day
    if period == "month":
        return day.replace(day=1)
    return day - timedelta(days=day.weekday())  # weeks start on Monday


def period_end(start: date, period: str) -> date:
    if period == "day":
        return start
    if period == "month":
        return start.replace(day=calendar.monthrange(start.year, start.month)[1])
    return start + timedelta(days=6)


def periods(days: dict[str, dict], period: str, today: date, count: int, per_day: dict | None = None) -> list[dict]:
    """The last `count` weeks or months up to today, oldest first, with totals.

    With `per_day` (recent daily averages), the current period also gets
    `projected`: its totals if the rest of it, today included, goes like that.
    """
    starts = [period_start(today, period)]
    first = period_start(date.fromisoformat(min(days)), period) if days else starts[0]
    while len(starts) < count and starts[0] > first:
        starts.insert(0, period_start(starts[0] - timedelta(days=1), period))
    out = []
    for start in starts:
        end = period_end(start, period)
        rows = [v for k, v in days.items() if start.isoformat() <= k <= end.isoformat()]
        totals = {k: sum(r[k] for r in rows) for k in ENERGY + MONEY}
        out.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": len(rows),  # days with anything recorded
            "length": (end - start).days + 1,
            "current": start <= today <= end,
            **{k: round(v, 3) for k, v in totals.items()},
        })
        if per_day and start <= today <= end:
            before = [v for k, v in days.items() if start.isoformat() <= k < today.isoformat()]
            left = (end - today).days + 1
            out[-1]["projected"] = {k: round(sum(r[k] for r in before) + per_day[k] * left, 3) for k in ENERGY + MONEY}
    return out


def recent_average(days: dict[str, dict], today: date) -> tuple[dict | None, int]:
    """Average per day over the last RECENT_DAYS recorded days before today."""
    recent = sorted(k for k in days if k < today.isoformat())[-RECENT_DAYS:]
    if len(recent) < MIN_RECENT:
        return None, len(recent)
    return {k: sum(days[d][k] for d in recent) / len(recent) for k in ENERGY + MONEY}, len(recent)


def month_projection(days: dict[str, dict], today: date) -> dict:
    """This month so far, and where it ends if the rest goes like the recent days.

    Today is still under way, so it's replaced by the recent average too.
    """
    avg, n = recent_average(days, today)
    start = today.replace(day=1)
    length = calendar.monthrange(today.year, today.month)[1]
    done = [v for k, v in days.items() if start.isoformat() <= k < today.isoformat()]
    so_far = {k: sum(r[k] for r in done) + days.get(today.isoformat(), {}).get(k, 0.0) for k in ENERGY + MONEY}
    out = {"month": start.isoformat()[:7], "days_left": length - today.day + 1, "recent_days": n,
           "so_far": {k: round(v, 2) for k, v in so_far.items()}, "projected": None}
    last_start = (start - timedelta(days=1)).replace(day=1)
    last = [v for k, v in days.items() if last_start.isoformat() <= k < start.isoformat()]
    out["last_month"] = {k: round(sum(r[k] for r in last), 2) for k in ENERGY + MONEY} if last else None
    out["last_month_days"] = len(last)
    if avg is None:
        return out
    before_today = {k: sum(r[k] for r in done) for k in ENERGY + MONEY}
    out["projected"] = {k: round(before_today[k] + avg[k] * out["days_left"], 2) for k in ENERGY + MONEY}
    out["per_day"] = {k: round(v, 3) for k, v in avg.items()}
    return out


def savings_curve(days: dict[str, dict], before: float | None) -> list[dict]:
    """Running total of battery savings at the end of each recorded day."""
    total = before or 0.0
    out = []
    for day in sorted(days):
        total += days[day]["battery_saved"]
        out.append({"date": day, "saved": round(total, 2)})
    return out


def payback_outlook(curve: list[dict], cost: float, days: dict[str, dict], today: date) -> dict:
    """When savings reach the battery's cost at the recent rate, next to the lifetime one."""
    avg, n = recent_average(days, today)
    saved = curve[-1]["saved"] if curve else 0.0
    out = {"cost": cost, "saved": saved, "recent_per_day": None, "recent_date": None, "recent_days": n}
    if avg is None:
        return out
    out["recent_per_day"] = round(avg["battery_saved"], 3)
    if cost > 0 and saved >= cost:
        out["recent_date"] = today.isoformat()
    elif cost > 0 and avg["battery_saved"] > 0:
        left = (cost - saved) / avg["battery_saved"]
        if left <= 50 * 365:
            out["recent_date"] = (today + timedelta(days=round(left))).isoformat()
    return out


def health(capacity: list[dict], period: str, today: date, count: int, rated: float | None,
           days: dict[str, dict]) -> dict:
    """Measured usable capacity and cycles per week or month.

    The battery doesn't report its state of health, so capacity is worked
    out from long discharges: energy out divided by the share of the battery
    level it took. It reads a little under the rated capacity (the battery's
    own losses), so watch the trend rather than the number.
    """
    rows = []
    for p in periods(days, period, today, count):
        caps = [c for c in capacity if p["start"] <= c["day"] <= p["end"] and c["soc_drop"] > 0]
        wh, drop = sum(c["discharge_wh"] for c in caps), sum(c["soc_drop"] for c in caps)
        discharged = sum(v["discharge"] for k, v in days.items() if p["start"] <= k <= p["end"])
        rows.append({
            "start": p["start"],
            "capacity_kwh": round(wh / 1000 / (drop / 100), 2) if drop else None,
            "soc_drop": round(drop),
            "cycles": round(discharged / rated, 2) if rated else None,
        })
    measured = [c for c in capacity if c["soc_drop"] > 0]
    first = _capacity(measured[:30])
    last = _capacity(measured[-30:]) if len(measured) >= 60 else None
    return {"rated_kwh": rated, "periods": rows, "first_kwh": first, "latest_kwh": last,
            "measured_days": len(measured)}


def _capacity(caps: list[dict]) -> float | None:
    drop = sum(c["soc_drop"] for c in caps)
    return round(sum(c["discharge_wh"] for c in caps) / 1000 / (drop / 100), 2) if drop else None


def insights(days: dict[str, dict], today: date, span: int = 30) -> dict:
    """Headline figures over the last `span` recorded days before today (or all there are).

    Self-sufficiency is the share of home use not bought from the grid;
    solar used is the share of solar output that wasn't exported.
    """
    recent = sorted(k for k in days if k < today.isoformat())[-span:]
    if not recent:
        return {"days": 0}
    t = {k: sum(days[d][k] for d in recent) for k in ENERGY + MONEY}
    return {
        "days": len(recent),
        "self_sufficiency": round(max(0.0, 1 - t["import"] / t["home"]) * 100) if t["home"] > 0 else None,
        "solar_used": round(max(0.0, 1 - t["export"] / t["solar"]) * 100) if t["solar"] > 0.5 else None,
        "battery_saved": round(t["battery_saved"], 2),
        "net_cost": round(t["net_cost"], 2),
        "standing_cost": round(t["standing_cost"], 2),
        "discharge": round(t["discharge"], 1),
    }


def conditions(days: dict[str, dict], summaries: list[dict], weather: dict[str, dict], capacity: list[dict],
               first: date, last: date) -> list[dict]:
    """One row per day from `first` to `last`: weather, solar yield, battery range, peaks and voltage.

    `solar_yield` is solar kWh per kWh/m² of sunshine, so a grey day and a
    sunny one can be compared; it falls if panels get dirty or shaded.
    """
    by_summary = {r["day"]: r for r in summaries}
    caps = {c["day"]: c for c in capacity if c["soc_drop"] > 0}
    rows = []
    day = first
    while day <= last:
        key = day.isoformat()
        w, d, sm, c = weather.get(key, {}), days.get(key), by_summary.get(key, {}), caps.get(key)
        sun = w.get("sun_kwh_m2")
        solar = d["solar"] if d else None
        rows.append({
            "day": key,
            "temp_avg": w.get("temp_avg"), "temp_min": w.get("temp_min"), "temp_max": w.get("temp_max"),
            "cloud_avg": w.get("cloud_avg"), "sun_kwh_m2": sun,
            "solar_kwh": round(solar, 2) if solar is not None else None,
            "solar_yield": round(solar / sun, 2) if solar and sun and w.get("hours", 0) >= 20 and sun >= 0.5 else None,
            "capacity_kwh": round(c["discharge_wh"] / 1000 / (c["soc_drop"] / 100), 2) if c else None,
            **{k: sm.get(k) for k in ("soc_min", "soc_max", "solar_peak_w", "home_peak_w", "import_peak_w",
                                      "export_peak_w", "charge_peak_w", "discharge_peak_w",
                                      "voltage_min", "voltage_avg", "voltage_max")},
        })
        day += timedelta(days=1)
    return rows


def temperature_effect(rows: list[dict]) -> dict | None:
    """Measured capacity on cold days against mild ones, if both have been seen.

    Needs 5 measured days in each of the coldest and warmest thirds of the
    temperatures; lithium batteries give a little less when cold, so a
    difference here is expected and is not a fault.
    """
    pts = sorted((r["temp_avg"], r["capacity_kwh"]) for r in rows if r["temp_avg"] is not None and r["capacity_kwh"])
    third = len(pts) // 3
    if third < 5 or pts[-1][0] - pts[0][0] < 6:
        return None
    cold, warm = pts[:third], pts[-third:]
    avg = lambda part, i: sum(p[i] for p in part) / len(part)  # noqa: E731
    return {"cold_temp": round(avg(cold, 0), 1), "cold_kwh": round(avg(cold, 1), 2),
            "warm_temp": round(avg(warm, 0), 1), "warm_kwh": round(avg(warm, 1), 2), "days": len(pts)}
