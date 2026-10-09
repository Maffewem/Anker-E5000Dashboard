"""Battery cost and electricity prices, and the payback worked out from them.

Prices live in one place (`Tariff`) so a future source, such as fetched
Octopus rates, can replace the manual ones without touching the maths.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class Tariff:
    battery_cost: float = 0.0  # what was paid for the battery, in pounds
    peak_rate: float = 28.0  # p/kWh outside the off-peak window
    offpeak_rate: float = 28.0  # p/kWh inside it; same as peak means a flat tariff
    offpeak_start: str = "00:30"
    offpeak_end: str = "05:30"
    export_rate: float = 15.0  # p/kWh paid for export; what solar charging gives up

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
        return Tariff(**{**asdict(self), **{n: float(getattr(self, n)) for n in
                                            ("battery_cost", "peak_rate", "offpeak_rate", "export_rate")}})

    def slot_rates(self) -> list[float]:
        """Price in p/kWh for each half hour of the day (48 slots)."""

        def slot(hhmm: str) -> int:
            h, m = map(int, hhmm.split(":"))
            return (h * 60 + m) // 30

        start, end = slot(self.offpeak_start), slot(self.offpeak_end)
        rates = [self.peak_rate] * 48
        i = start
        while i != end:  # the window may run past midnight
            rates[i] = self.offpeak_rate
            i = (i + 1) % 48
        return rates


def payback(tariff: Tariff, slots: list[dict], today: date) -> dict:
    """Savings so far and a projection, from half-hourly battery totals.

    Each discharged kWh is valued at the price of the half hour it was used
    in, as grid electricity it replaced. Charging costs the price of the half
    hour for energy taken from the grid, and the export rate for solar energy
    that could otherwise have been sold.
    """
    rates = tariff.slot_rates()
    value = grid_cost = solar_cost = 0.0
    days = set()
    for r in slots:
        rate = rates[r["slot"]] / 100  # pounds per kWh
        value += r["discharge_wh"] / 1000 * rate
        grid_cost += r["grid_charge_wh"] / 1000 * rate
        solar_cost += r["solar_charge_wh"] / 1000 * tariff.export_rate / 100
        days.add(r["day"])

    saved = value - grid_cost - solar_cost
    first = min(days) if days else None
    span = (today - date.fromisoformat(first)).days + 1 if first else 0
    per_day = saved / span if span else 0.0
    remaining = max(0.0, tariff.battery_cost - saved)
    payback_days = None
    if tariff.battery_cost > 0 and remaining == 0:
        payback_days = 0
    elif tariff.battery_cost > 0 and per_day > 0 and span >= 1:
        payback_days = round(remaining / per_day)
    return {
        "tariff": asdict(tariff),
        "since": first,
        "days": span,
        "saved": round(saved, 2),
        "discharge_value": round(value, 2),
        "grid_charge_cost": round(grid_cost, 2),
        "solar_charge_cost": round(solar_cost, 2),
        "per_day": round(per_day, 3),
        "remaining": round(remaining, 2),
        "payback_days": payback_days,
        "payback_date": (today + timedelta(days=payback_days)).isoformat() if payback_days is not None else None,
    }
