"""When the battery will run empty (or be full), from how it is usually used.

The battery's own average power for each half hour of the day over the last
couple of weeks is its usage pattern: it captures evening discharge,
overnight off-peak charging and daytime solar charging alike. Starting from
the current charge, the next half hour uses the current power (it says the
most about the next few minutes) and every half hour after that the
pattern, until the charge reaches the discharge floor or the top.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

HORIZON_HOURS = 48
PATTERN_DAYS = 14
MIN_PATTERN_HOURS = 24  # less history than this and only the current power is used
CHARGING_W = 50  # below -this an average half hour counts as charging


def floor_soc(snap: dict[str, Any]) -> float:
    """The level the battery stops discharging at: its discharge limit or backup reserve."""
    limits = [v for v in (snap.get("discharge_limit_soc"), snap.get("backup_reserve_soc")) if v is not None]
    return float(max(limits)) if limits else 0.0


def ceiling_soc(snap: dict[str, Any]) -> float:
    limit = snap.get("charging_limit_soc")
    return float(limit) if limit else 100.0


def estimate(snap: dict[str, Any], pattern: list[float | None], hours_of_history: float,
             now: datetime) -> dict[str, Any]:
    """Project the charge forward from `now` (timezone-aware, local).

    `pattern` is the average battery power in W for each of the 48 local
    half hours (+ discharging, - charging; None where there's no history).
    """
    soc, rated, power = snap.get("soc"), snap.get("rated_kwh"), snap.get("battery_w")
    if soc is None or not rated or power is None:
        return {"available": False}
    floor, ceiling = floor_soc(snap), ceiling_soc(snap)
    capacity_wh = rated * 1000
    stored_wh = (soc - floor) / 100 * capacity_wh  # usable energy above the floor
    room_wh = (ceiling - soc) / 100 * capacity_wh
    use_pattern = hours_of_history >= MIN_PATTERN_HOURS and any(p is not None for p in pattern)
    out = {
        "available": True,
        "soc": soc,
        "floor_soc": floor,
        "ceiling_soc": ceiling,
        "usable_kwh": round(max(0.0, stored_wh) / 1000, 2),
        "method": "pattern" if use_pattern else "current",
        "pattern_days": round(hours_of_history / 24, 1),
        "empty_at": None,
        "full_at": None,
        "recharges_at": None,  # when it next starts charging, if that comes before running out
    }
    if not use_pattern:
        # Only the current power to go on: straight-line at that rate.
        if power > 5 and stored_wh > 0:
            out["empty_at"] = (now + timedelta(hours=stored_wh / power)).isoformat()
        elif power < -5 and room_wh > 0:
            out["full_at"] = (now + timedelta(hours=room_wh / -power)).isoformat()
        return out

    t = now
    level = stored_wh  # Wh above the floor
    top = stored_wh + room_wh
    end = now + timedelta(hours=HORIZON_HOURS)
    first = True
    was_charging = power < -CHARGING_W
    while t < end:
        slot_end = t.replace(minute=0 if t.minute < 30 else 30, second=0, microsecond=0) + timedelta(minutes=30)
        hours = (slot_end - t).total_seconds() / 3600
        watts = power if first else pattern[(t.hour * 60 + t.minute) // 30]
        first = False
        if watts and watts > 0 and level - watts * hours <= 0:
            out["empty_at"] = (t + timedelta(hours=max(0.0, level) / watts)).isoformat()
            break
        charging = bool(watts and watts < -CHARGING_W)
        if charging and not was_charging:
            # It starts charging again before running out; what happens
            # after that is the next cycle's story.
            out["recharges_at"] = t.isoformat()
            break
        was_charging = charging
        if charging:
            if out["full_at"] is None and level < top <= level - watts * hours:
                out["full_at"] = (t + timedelta(hours=(top - level) / -watts)).isoformat()
        level = min(top, level - (watts or 0) * hours)
        t = slot_end
    return out


def pattern_from_minutes(rows: list[tuple[int, float | None]], tz) -> tuple[list[float | None], float]:
    """Average battery power per local half hour from (ts, battery_w) minute rows."""
    sums, counts = [0.0] * 48, [0] * 48
    for ts, watts in rows:
        if watts is None:
            continue
        local = datetime.fromtimestamp(ts, tz)
        slot = (local.hour * 60 + local.minute) // 30
        sums[slot] += watts
        counts[slot] += 1
    pattern = [sums[i] / counts[i] if counts[i] else None for i in range(48)]
    return pattern, sum(counts) / 60
