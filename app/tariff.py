"""Battery cost and electricity prices, and the payback worked out from them.

Prices live in one place (`Tariff`); fetched Octopus prices, when there
are any, override it half hour by half hour.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Days of savings needed before projecting a payback date. Fewer than this
# and one sunny or grey day swings the answer by decades.
MIN_DAYS = 7
# The projection is shown as a range: savings per day this much better or
# worse than the average so far (seasons alone move it this much).
SPREAD = 0.25
# Beyond this the answer is just "longer than the battery will last".
MAX_DAYS = 50 * 365


# Energy suppliers you can pick. Only Octopus has a price feed; the others use
# the prices typed into Edit costs, with their off-peak windows as presets.
SUPPLIERS = {
    "octopus": {"name": "Octopus Energy", "presets": []},
    "eon": {"name": "E.ON Next", "presets": [{"name": "Next Drive", "offpeak_start": "00:00", "offpeak_end": "07:00"}]},
    "edf": {"name": "EDF", "presets": [{"name": "GoElectric Overnight", "offpeak_start": "00:00", "offpeak_end": "05:00"}]},
    "british_gas": {"name": "British Gas", "presets": [{"name": "Electric Driver", "offpeak_start": "00:00", "offpeak_end": "05:00"}]},
    "other": {"name": "Other", "presets": []},
}


@dataclass(frozen=True)
class Tariff:
    battery_cost: float = 0.0  # what was paid for the battery, in pounds
    peak_rate: float = 28.0  # p/kWh outside the off-peak window
    offpeak_rate: float = 28.0  # p/kWh inside it; same as peak means a flat tariff
    offpeak_start: str = "00:30"
    offpeak_end: str = "05:30"
    export_rate: float = 15.0  # p/kWh paid for export; what solar charging gives up
    use_manual: bool = False  # use these prices even when Octopus is connected
    installed: str = ""  # ISO date the battery was installed; lets its lifetime totals count
    supplier: str = ""  # one of SUPPLIERS; empty until picked (then Octopus if connected, else other)

    @classmethod
    def from_dict(cls, data: dict | None) -> "Tariff":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    def validate(self) -> "Tariff":
        for name in ("battery_cost", "peak_rate", "offpeak_rate", "export_rate"):
            value = float(getattr(self, name))
            if not 0 <= value <= (100_000 if name == "battery_cost" else 1000):
                raise ValueError(f"{name.replace('_', ' ').capitalize()} is out of range")
        for name in ("offpeak_start", "offpeak_end"):
            if not TIME_RE.match(getattr(self, name)):
                raise ValueError("Off-peak times must look like 00:30")
        if self.supplier and self.supplier not in SUPPLIERS:
            raise ValueError("Pick an energy supplier from the list")
        if self.installed:
            try:
                installed = date.fromisoformat(self.installed)
            except ValueError:
                raise ValueError("Enter the install date as a date") from None
            if installed > date.today() or installed.year < 2015:
                raise ValueError("The install date can't be in the future")
        return Tariff(**{**asdict(self), "use_manual": bool(self.use_manual),
                         **{n: float(getattr(self, n)) for n in ("battery_cost", "peak_rate", "offpeak_rate", "export_rate")}})

    def slot_rates(self) -> list[float]:
        """Price in p/kWh for each half hour of the day (48 slots)."""
        return fixed_profile(self.peak_rate, self.offpeak_rate, self.offpeak_start, self.offpeak_end)


def slot_of(hhmm: str) -> int:
    """The half hour of the day (0-47) a "HH:MM" time falls in."""
    h, m = map(int, hhmm.split(":"))
    return (h * 60 + m) // 30


def slot_time(slot: int) -> str:
    """When a half hour of the day starts, as "HH:MM"; 48 wraps to 00:00."""
    return f"{slot % 48 // 2:02d}:{slot % 2 * 30:02d}"


def fixed_profile(peak: float, offpeak: float, start: str, end: str) -> list[float]:
    """Price per half hour of a two-rate day, off-peak from `start` to `end`."""
    rates = [peak] * 48
    i, stop = slot_of(start), slot_of(end)
    while i != stop:  # the window may run past midnight
        rates[i] = offpeak
        i = (i + 1) % 48
    return rates


def payback(tariff: Tariff, slots: list[dict], today: date,
            prices: dict[tuple[str, int], tuple] | None = None,
            lifetime: dict | None = None) -> dict:
    """Savings so far and a projection, from half-hourly battery totals.

    Each discharged kWh is valued at the price of the half hour it was used
    in, as grid electricity it replaced. Charging costs the price of the half
    hour for energy taken from the grid, and the export rate for solar energy
    that could otherwise have been sold.

    `prices` holds actual (import, export) p/kWh per (day, slot), such as
    fetched Octopus prices; half hours it doesn't cover use the tariff.

    `lifetime` is the battery's own lifetime "charged_kwh" and
    "discharged_kwh". With an install date, the energy it moved before this
    dashboard started recording is counted too, valued at the average price
    per kWh recorded so far, so the average per day covers the battery's
    whole life rather than the first few hours.
    """
    price = _pricer(tariff, prices)
    value = grid_cost = solar_cost = 0.0
    days = set()
    for r in slots:
        rate, export = price(r["day"], r["slot"])
        value += r["discharge_wh"] / 1000 * rate
        grid_cost += r["grid_charge_wh"] / 1000 * rate
        solar_cost += r["solar_charge_wh"] / 1000 * export
        days.add(r["day"])

    recorded = value - grid_cost - solar_cost
    first = min(days) if days else None
    before = _before_recording(tariff, slots, lifetime, value, grid_cost + solar_cost)
    if before is not None and tariff.installed < (first or "9999"):
        first = tariff.installed
    saved = recorded + (before or 0.0)
    span = (today - date.fromisoformat(first)).days + 1 if first else 0
    per_day = saved / span if span else 0.0
    remaining = max(0.0, tariff.battery_cost - saved)

    payback_days = low = high = None
    too_long = False
    if tariff.battery_cost > 0 and remaining == 0:
        payback_days = low = high = 0
    elif tariff.battery_cost > 0 and per_day > 0 and span >= MIN_DAYS and remaining / per_day > MAX_DAYS:
        too_long = True
    elif tariff.battery_cost > 0 and per_day > 0 and span >= MIN_DAYS:
        payback_days = round(remaining / per_day)
        low = round(remaining / (per_day * (1 + SPREAD)))
        high = round(remaining / (per_day * (1 - SPREAD)))

    def on(days_from_now: int | None) -> str | None:
        return (today + timedelta(days=days_from_now)).isoformat() if days_from_now is not None else None

    return {
        "tariff": asdict(tariff),
        "since": first,
        "days": span,
        "min_days": MIN_DAYS,
        "saved": round(saved, 2),
        "saved_before_recording": None if before is None else round(before, 2),
        "discharge_value": round(value, 2),
        "grid_charge_cost": round(grid_cost, 2),
        "solar_charge_cost": round(solar_cost, 2),
        "per_day": round(per_day, 3),
        "remaining": round(remaining, 2),
        "payback_days": payback_days,
        "payback_too_long": too_long,
        "payback_date": on(payback_days),
        "payback_days_low": low,
        "payback_days_high": high,
        "payback_date_low": on(low),
        "payback_date_high": on(high),
    }


def _pricer(tariff: Tariff, prices: dict | None):
    """(import, export) in pounds per kWh for a (day, slot): the actual price if
    there is one (e.g. from Octopus), else the tariff's."""
    rates = tariff.slot_rates()
    prices = prices or {}

    def price(day: str, slot: int) -> tuple[float, float]:
        actual_import, actual_export = prices.get((day, slot), (None, None))
        rate = rates[slot] if actual_import is None else actual_import
        export = tariff.export_rate if actual_export is None else actual_export
        return rate / 100, export / 100

    return price


class PriceHistory:
    """Prices per (day, slot) for `_pricer`: fetched prices where there are
    any, else the typed prices that were in force that day.

    `changes` is Storage.tariff_history(): (local date from, prices), oldest
    first, the first applying to everything before the second.
    """

    def __init__(self, changes: list[tuple[str, dict]], actual: dict | None = None) -> None:
        self.since = [day for day, _ in changes]
        self.tariffs = [Tariff.from_dict(prices) for _, prices in changes]
        self.rates = [t.slot_rates() for t in self.tariffs]
        self.actual = actual or {}

    def get(self, key: tuple[str, int], default=(None, None)) -> tuple:
        actual_import, actual_export = self.actual.get(key, (None, None))
        if not self.since or (actual_import is not None and actual_export is not None):
            return (actual_import, actual_export) if key in self.actual else default
        i = max(0, bisect_right(self.since, key[0]) - 1)
        return (self.rates[i][key[1]] if actual_import is None else actual_import,
                self.tariffs[i].export_rate if actual_export is None else actual_export)


def prices_changed(old: Tariff, new: Tariff) -> list[str]:
    """Readable list of the typed prices that differ, e.g. "peak 28p to 30p"."""
    out = []
    for name, label in (("peak_rate", "peak"), ("offpeak_rate", "off-peak"), ("export_rate", "export")):
        a, b = getattr(old, name), getattr(new, name)
        if a != b:
            out.append(f"{label} {a:g}p to {b:g}p")
    if (old.offpeak_start, old.offpeak_end) != (new.offpeak_start, new.offpeak_end):
        out.append(f"off-peak hours {old.offpeak_start}–{old.offpeak_end} to {new.offpeak_start}–{new.offpeak_end}")
    return out


def daily_costs(tariff: Tariff, slots: list[dict], today: date, days: int = 30,
                prices: dict[tuple[str, int], tuple] | None = None) -> dict:
    """What charging the battery cost each day, and what its discharge saved.

    Priced the same way as the payback: grid charging at the price of the half
    hour it happened in, solar charging at the export price it gave up, and
    discharge at the import price it saved. Days with nothing recorded are
    left in as zeros so the list has no gaps.
    """
    price = _pricer(tariff, prices)
    first = (today - timedelta(days=days - 1)).isoformat()
    by_day: dict[str, dict] = {}
    for r in slots:
        if r["day"] < first or r["day"] > today.isoformat():
            continue
        d = by_day.setdefault(r["day"], {"grid_wh": 0.0, "grid_cost": 0.0, "solar_wh": 0.0, "solar_cost": 0.0,
                                         "discharge_wh": 0.0, "discharge_value": 0.0})
        rate, export = price(r["day"], r["slot"])
        d["grid_wh"] += r["grid_charge_wh"]
        d["grid_cost"] += r["grid_charge_wh"] / 1000 * rate
        d["solar_wh"] += r["solar_charge_wh"]
        d["solar_cost"] += r["solar_charge_wh"] / 1000 * export
        d["discharge_wh"] += r["discharge_wh"]
        d["discharge_value"] += r["discharge_wh"] / 1000 * rate

    rows = []
    for i in range(days):
        day = (today - timedelta(days=i)).isoformat()
        d = by_day.get(day)
        if d is None:
            rows.append({"day": day, "recorded": False, "grid_charge_kwh": 0.0, "grid_charge_cost": 0.0,
                         "grid_charge_p_per_kwh": None, "solar_charge_kwh": 0.0, "solar_charge_cost": 0.0,
                         "discharge_kwh": 0.0, "discharge_value": 0.0, "saved": 0.0})
            continue
        grid_kwh = d["grid_wh"] / 1000
        rows.append({
            "day": day,
            "recorded": True,
            "grid_charge_kwh": round(grid_kwh, 2),
            "grid_charge_cost": round(d["grid_cost"], 2),
            "grid_charge_p_per_kwh": round(d["grid_cost"] / grid_kwh * 100, 2) if grid_kwh >= 0.05 else None,
            "solar_charge_kwh": round(d["solar_wh"] / 1000, 2),
            "solar_charge_cost": round(d["solar_cost"], 2),
            "discharge_kwh": round(d["discharge_wh"] / 1000, 2),
            "discharge_value": round(d["discharge_value"], 2),
            "saved": round(d["discharge_value"] - d["grid_cost"] - d["solar_cost"], 2),
        })

    def total(n: int) -> dict:
        part = [r for r in rows[:n] if r["recorded"]]
        kwh = sum(r["grid_charge_kwh"] for r in part)
        cost = sum(r["grid_charge_cost"] for r in part)
        return {"days": len(part), "grid_charge_kwh": round(kwh, 2), "grid_charge_cost": round(cost, 2),
                "saved": round(sum(r["saved"] for r in part), 2)}

    return {"days": rows, "today": total(1), "week": total(7), "month": total(min(30, days)),
            "average_per_day": round(total(days)["grid_charge_cost"] / max(1, total(days)["days"]), 2)}


def _before_recording(tariff: Tariff, slots: list[dict], lifetime: dict | None,
                      value: float, charge_cost: float) -> float | None:
    """Estimated savings from before the dashboard recorded, in pounds.

    None unless there is an install date and the battery's lifetime totals.
    """
    if not tariff.installed or not lifetime:
        return None
    charged_total, discharged_total = lifetime.get("charged_kwh"), lifetime.get("discharged_kwh")
    if charged_total is None or discharged_total is None:
        return None
    discharged = sum(r["discharge_wh"] for r in slots) / 1000
    charged = sum(r["grid_charge_wh"] + r["solar_charge_wh"] for r in slots) / 1000
    # Price per kWh from what was recorded; until there's enough of it,
    # discharge saves peak price and charging costs the cheaper of off-peak
    # and lost export.
    worth = value / discharged if discharged >= 1 else tariff.peak_rate / 100
    cost = charge_cost / charged if charged >= 1 else min(tariff.offpeak_rate, tariff.export_rate) / 100
    return max(0.0, discharged_total - discharged) * worth - max(0.0, charged_total - charged) * cost
