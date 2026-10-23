"""When the battery will run empty (or be full), from how it is usually used.

The battery's own average power for each half hour of the day over the last
couple of weeks is its usage pattern: it captures evening discharge,
overnight off-peak charging and daytime solar charging alike. Starting from
the current charge, the next half hour uses the current power (it says the
most about the next few minutes) and every half hour after that the
pattern, up to the next time it starts charging. Running down to the floor
before then is what's worth a warning; if the charge lasts until it next
charges, that's all there is to say.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

HORIZON_HOURS = 48
PATTERN_DAYS = 14
MIN_PATTERN_HOURS = 24  # less history than this and only the current power is used
CHARGING_W = 50  # below -this an average half hour counts as charging
CURRENT_EMPTY_HOURS = 24  # without a pattern, only report running empty this soon


def battery_size(snap: dict[str, Any]) -> tuple[float, float]:
    """(capacity kWh, charging power kW), with an E5000's when not read yet."""
    return snap.get("rated_kwh") or 5.0, (snap.get("max_charge_w") or 2400) / 1000


def floor_soc(snap: dict[str, Any]) -> float:
    """The level the battery stops discharging at: its discharge limit or backup reserve."""
    limits = [v for v in (snap.get("discharge_limit_soc"), snap.get("backup_reserve_soc")) if v is not None]
    return float(max(limits)) if limits else 0.0


def ceiling_soc(snap: dict[str, Any]) -> float:
    limit = snap.get("charging_limit_soc")
    return float(limit) if limit else 100.0


def estimate(snap: dict[str, Any], pattern: list[float | None], hours_of_history: float,
             now: datetime, charges: list[tuple[datetime, str]] | None = None,
             charging_now: tuple[datetime, str] | None = None, charge_target: int | None = None) -> dict[str, Any]:
    """Project the charge forward from `now` (timezone-aware, local).

    `charges` are the charges the dashboard has planned (start, description):
    your schedules, Happy Hours, smart-charge slots. The first one ends the
    projection, ahead of any charging the usual pattern suggests.

    `charging_now` is the end and description of a planned charge in progress,
    `charge_target` the level it stops at. While charging, the time it is full
    (or reaches that level) comes from the live power, not the usual pattern.

    `pattern` is the average battery power in W for each of the 48 local
    half hours (+ discharging, - charging; None where there's no history).
    """
    # Step through real time (UTC): adding to a local datetime moves the wall
    # clock, which is wrong across a clock change (a 25 or 23 hour day). Local
    # time is only for the half hour of the day and the answers given back.
    tz = now.tzinfo
    now = now.astimezone(timezone.utc)

    def iso(d: datetime) -> str:
        return d.astimezone(tz).isoformat()

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
        "recharges_at": None,  # when it next starts charging (after empty_at, if it runs out first)
        "charging": power < -CHARGING_W,  # it is charging right now
        "charging_until": iso(charging_now[0]) if charging_now and power < -CHARGING_W else None,
        "charging_by": charging_now[1] if charging_now and power < -CHARGING_W else None,
        "charge_target": None,  # the level the charge in progress is heading for
        "recharges_by": None,  # what starts it: a scheduled charge's description, or None for the usual pattern
    }
    if power < -CHARGING_W:
        goal = min(ceiling, charge_target) if charge_target else ceiling
        out["charge_target"] = goal
        if soc < goal:
            out["full_at"] = iso(now + timedelta(hours=(goal - soc) / 100 * capacity_wh / -power))
    planned = sorted((a, why) for a, why in (charges or []) if a > now)
    planned = planned[0] if planned else None
    if not use_pattern:
        # Only the current power to go on: straight-line at that rate. With no
        # pattern there's no telling when it next charges, so running empty more
        # than a day away isn't worth a warning.
        if power > 5 and 0 < stored_wh / power <= CURRENT_EMPTY_HOURS:
            out["empty_at"] = iso(now + timedelta(hours=stored_wh / power))
        elif power < -5 and room_wh > 0 and out["full_at"] is None:
            out["full_at"] = iso(now + timedelta(hours=room_wh / -power))
        if planned and (out["empty_at"] is None or planned[0] < datetime.fromisoformat(out["empty_at"])):
            out["recharges_at"], out["recharges_by"] = iso(planned[0]), planned[1]
        return out

    t = now
    level = stored_wh  # Wh above the floor
    top = stored_wh + room_wh
    end = now + timedelta(hours=HORIZON_HOURS)
    first = True
    was_charging = power < -CHARGING_W
    while t < end:
        slot_end = t.replace(minute=0 if t.minute < 30 else 30, second=0, microsecond=0) + timedelta(minutes=30)
        planned_now = bool(planned) and t <= planned[0] < slot_end
        if planned_now:
            slot_end = max(planned[0], t)
        hours = (slot_end - t).total_seconds() / 3600
        lt = t.astimezone(tz)
        watts = power if first else pattern[(lt.hour * 60 + lt.minute) // 30]
        first = False
        if out["empty_at"] is None and watts and watts > 0 and level - watts * hours <= 0:
            out["empty_at"] = iso(t + timedelta(hours=max(0.0, level) / watts))
            level = 0.0  # it sits at the floor until it next charges
        if planned_now:
            out["recharges_at"], out["recharges_by"] = iso(planned[0]), planned[1]
            break
        charging = bool(watts and watts < -CHARGING_W)
        if charging and not was_charging:
            # It starts charging again (after running out, if empty_at is
            # set); what happens after that is the next cycle's story.
            out["recharges_at"] = iso(t)
            break
        was_charging = charging
        if charging:
            if out["full_at"] is None and level < top <= level - watts * hours:
                out["full_at"] = iso(t + timedelta(hours=(top - level) / -watts))
        if out["empty_at"] is None:
            level = min(top, level - (watts or 0) * hours)
        t = slot_end
    return out


def pattern_from_minutes(rows: list[tuple[int, float | None]], tz) -> tuple[list[float | None], float]:
    """Typical battery power per local half hour from (ts, battery_w) minute rows.

    Each day's average for a half hour is taken first and the median across days
    after, so one odd day (a test charge, a one-off schedule) doesn't read as usual.
    """
    sums: dict[tuple, list[float]] = {}
    hours = 0
    for ts, watts in rows:
        if watts is None:
            continue
        local = datetime.fromtimestamp(ts, tz)
        acc = sums.setdefault((local.date(), (local.hour * 60 + local.minute) // 30), [0.0, 0])
        acc[0] += watts
        acc[1] += 1
        hours += 1
    by_slot: list[list[float]] = [[] for _ in range(48)]
    for (_, slot), (total, n) in sums.items():
        by_slot[slot].append(total / n)
    pattern = [statistics.median(v) if v else None for v in by_slot]
    return pattern, hours / 60
