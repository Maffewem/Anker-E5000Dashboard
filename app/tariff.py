"""Battery cost and electricity prices, and the payback worked out from them.

Prices live in one place (`Tariff`); fetched Octopus prices, when there
are any, override it half hour by half hour.
"""

from __future__ import annotations

import re
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
    rates = tariff.slot_rates()
    prices = prices or {}
    value = grid_cost = solar_cost = 0.0
    days = set()
    for r in slots:
        actual_import, actual_export = prices.get((r["day"], r["slot"]), (None, None))
        rate = (rates[r["slot"]] if actual_import is None else actual_import) / 100  # pounds per kWh
        export = (tariff.export_rate if actual_export is None else actual_export) / 100
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
