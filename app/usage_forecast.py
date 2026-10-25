"""Expected household draw on the battery between two moments, from past days.

Used to estimate the battery level when a Happy Hour starts: the house keeps
using power (kettle, toaster, washing machine) between a charge ending and the
free slot, so the level then is lower than the level now.
"""
from __future__ import annotations

from datetime import datetime, timedelta

HISTORY_DAYS = 28  # four weeks, so there are a few of any given weekday to go on
# Weekday and weekend routines differ (a Sunday morning isn't a Tuesday one), so the days
# being predicted for count most: the same weekday, then the same kind of day, then the rest.
SAME_WEEKDAY_WEIGHT = 4.0
SAME_KIND_WEIGHT = 1.0  # weekday for weekday, weekend for weekend
OTHER_KIND_WEIGHT = 0.2
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


def _weight(past: int, target: int) -> float:
    if past == target:
        return SAME_WEEKDAY_WEIGHT
    return SAME_KIND_WEIGHT if (past >= 5) == (target >= 5) else OTHER_KIND_WEIGHT


def predict_kwh(profile: dict[str, dict[int, float]], start: datetime, end: datetime) -> tuple[float, int] | None:
    """Expected battery draw (kWh) from `start` to `end` (local times), and how many days it is based on.

    Each half hour is the weighted mean of that half hour on recent days, the
    same weekday counting most and the same kind of day (weekday or weekend) next. A half hour only partly inside the range counts
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
                w = _weight(datetime.fromisoformat(day).weekday(), cur.weekday())
                num += slots[slot] * w
                den += w
        if den:
            total += num / den * part
        cur = slot_end
    return total, len(profile)


def basis(profile: dict[str, dict[int, float]], when: datetime) -> dict:
    """How many recorded days are the same weekday, or the same kind of day, as `when` (and how many in all)."""
    days = [datetime.fromisoformat(d).weekday() for d in profile]
    return {"days": len(days), "same_weekday": sum(d == when.weekday() for d in days),
            "same_kind": sum((d >= 5) == (when.weekday() >= 5) for d in days)}
