"""Expected household draw on the battery between two moments, from past days.

Used to estimate the battery level when a Happy Hour starts: the house keeps
using power (kettle, toaster, washing machine) between a charge ending and the
free slot, so the level then is lower than the level now.
"""
from __future__ import annotations

from datetime import datetime, timedelta

HISTORY_DAYS = 14
SAME_WEEKDAY_WEIGHT = 2.0  # weekday and weekend routines differ, so count the same weekday double
MIN_DAYS = 3  # fewer days of history than this and no forecast is made


def slot_profile(slots: list[dict], today, days: int = HISTORY_DAYS) -> dict[str, dict[int, float]]:
    """Net energy the battery had to cover (home minus solar, kWh) per half hour of each recent day.

    Returns {local day: {slot: kWh}} for the last `days` finished days, as
    `predict_kwh` takes it. Slots with under 10 minutes of data are dropped; the
    rest are scaled up to a full half hour."""
    first = today - timedelta(days=days)
    out: dict[str, dict[int, float]] = {}
    for r in slots:
        if r["minutes"] < 10 or not (first <= datetime.fromisoformat(r["day"]).date() < today):
            continue
        scale = 30 / min(30, r["minutes"])
        net = max(0.0, (r["home_wh"] - r["solar_wh"]) * scale / 1000)
        out.setdefault(r["day"], {})[r["slot"]] = net
    return out


def predict_kwh(profile: dict[str, dict[int, float]], start: datetime, end: datetime) -> tuple[float, int] | None:
    """Expected battery draw (kWh) from `start` to `end` (local times), and how many days it is based on.

    Each half hour is the weighted mean of that half hour on recent days, the
    same weekday counting extra. A half hour only partly inside the range counts
    pro rata. None if there isn't enough history."""
    if end <= start or len(profile) < MIN_DAYS:
        return None
    total = 0.0
    cur = start
    while cur < end:
        slot = (cur.hour * 60 + cur.minute) // 30
        slot_end = cur.replace(hour=slot * 30 // 60, minute=slot * 30 % 60, second=0, microsecond=0) + timedelta(minutes=30)
        part = (min(end, slot_end) - cur).total_seconds() / 1800
        num = den = 0.0
        for day, slots in profile.items():
            if slot in slots:
                w = SAME_WEEKDAY_WEIGHT if datetime.fromisoformat(day).weekday() == cur.weekday() else 1.0
                num += slots[slot] * w
                den += w
        if den:
            total += num / den * part
        cur = slot_end
    return total, len(profile)
