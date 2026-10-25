"""Battery control: your own charge, hold and discharge schedules, holding
the battery in cheap hours, optionally charging it from the grid in the
cheapest ones, and handing control back to the Anker app the rest of the
time.

The battery has no schedule registers, so this is the scheduler. While a
window is active it switches the battery to third-party control (mode 3)
and writes a power setpoint: 0 W to hold (the house runs from the grid at
the cheap price instead of draining the battery), or a negative value to
charge. When the window ends, when control is switched off, or when the
dashboard stops, it writes back the mode the battery was in before.

Nothing is written unless CONTROL_LIVE=1 is set on the container. Without
it, control runs as a dry run and only logs what it would have written.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from . import usage_forecast
from .registers import OPERATING_MODES, THIRD_PARTY_MODE

log = logging.getLogger("solarbank.control")

TICK_SECONDS = 15
STALE_SECONDS = 180  # no good battery reading for this long while in control: hand back to the Anker app
REASSERT_SECONDS = 60  # rewrite the setpoint now and then in case the battery forgot it
APP = "app"
HOLD = "hold"
CHARGE = "charge"
DISCHARGE = "discharge"
ACTIONS = (CHARGE, HOLD, DISCHARGE)
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MAX_SCHEDULES = 20
MAX_HAPPY = 20
FULL_SOC = 99  # counts as full
HEADROOM_HOURS = 36  # a Happy Hour starting this soon makes the charge before it leave room
MIN_BEFORE_HAPPY = 30  # but never cut a charge below this level (or the battery reserve)
PROFILE_TTL_SECONDS = 1800  # rebuild the home usage profile this often
CHARGE_EFFICIENCY = 0.95  # grid energy in to energy stored
DISCHARGE_EFFICIENCY = 0.95  # energy stored to energy delivered to the house
OVERRIDE_MAX_MINUTES = 12 * 60
MAX_SETPOINT_W = 5000  # no setpoint beyond this either way, whatever the battery reports as its maximum
PACE_MIN_W = 300  # paced charging doesn't go below this; very low power is inefficient
PACE_SETTLE_SECONDS = 10 * 60  # after changing the paced power, leave it alone this long unless it's well off pace
PACE_STEP_W = 100  # paced power moves in steps of this, so the setpoint (and event log) stays calm
DEFAULT_PACE_MARGIN = 45  # minutes before the window ends that a paced charge aims to be done
# What the battery itself accepts for its own limits (from Anker's integration):
# it refuses a charge limit under 80% or a discharge limit over 20%.
LIMIT_RANGES = {"charging_limit_soc": (80, 100), "discharge_limit_soc": (0, 20)}
# Gentle on LFP cells: stop short of full most days, keep a small reserve.
RECOMMENDED_LIMITS = {"charging_limit_soc": 80, "discharge_limit_soc": 10}


def _env_soc(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return value if 20 <= value <= 100 else default


# Where the dashboard stops charging (cheap hours, smart-charge slots, new
# schedules) until you change it on the dashboard. CHARGE_TARGET_SOC sets it.
DEFAULT_CHARGE_TARGET = _env_soc("CHARGE_TARGET_SOC", 80)


def live_writes_allowed() -> bool:
    return os.environ.get("CONTROL_LIVE", "").strip().lower() in ("1", "true", "yes", "on")


def limits_allowed() -> bool:
    """Narrowing the battery's own SOC limits while control drives it; CONTROL_LIMITS=0 turns it off."""
    return os.environ.get("CONTROL_LIMITS", "1").strip().lower() not in ("0", "false", "no", "off")


def _hhmm(value: str) -> tuple[int, int]:
    try:
        h, m = (int(x) for x in str(value).split(":"))
    except ValueError:
        raise ValueError(f"Times look like 07:30, not {value!r}") from None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Times look like 07:30, not {value!r}")
    return h, m


@dataclass(frozen=True)
class Schedule:
    """One of your own time windows: charge, hold or discharge on the chosen days."""

    action: str = CHARGE
    start: str = "00:30"
    end: str = "05:30"
    days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)  # Monday = 0; the day the window starts
    power_w: int = 1500  # charge or discharge power
    target_soc: int = DEFAULT_CHARGE_TARGET  # charge: stop here; discharge: stop here (a floor)
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "Schedule":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in (data or {}).items() if k in known}
        if "days" in data:
            data["days"] = tuple(data["days"])
        return cls(**data)

    def validate(self) -> "Schedule":
        if self.action not in ACTIONS:
            raise ValueError("A schedule must charge, hold or discharge")
        h1, m1 = _hhmm(self.start)
        h2, m2 = _hhmm(self.end)
        if (h1, m1) == (h2, m2):
            raise ValueError("A schedule's start and end can't be the same")
        days = tuple(sorted({int(d) for d in self.days}))
        if not days or not all(0 <= d <= 6 for d in days):
            raise ValueError("Pick at least one day for each schedule")
        if self.action != HOLD and not 100 <= int(self.power_w) <= 5000:
            raise ValueError("Schedule power must be between 100 and 5000 W")
        if not 5 <= int(self.target_soc) <= 100:
            raise ValueError("Schedule battery level must be between 5% and 100%")
        return Schedule(self.action, f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}", days,
                        int(self.power_w), int(self.target_soc), bool(self.enabled))

    def week_minutes(self) -> list[tuple[int, int, int]]:
        """(day, start, end) in minutes from Monday 00:00, one per day it runs."""
        h1, m1 = _hhmm(self.start)
        h2, m2 = _hhmm(self.end)
        a, b = h1 * 60 + m1, h2 * 60 + m2
        length = b - a if b > a else b - a + 1440
        return [(d, d * 1440 + a, d * 1440 + a + length) for d in self.days]

    def describe(self) -> str:
        days = "every day" if len(self.days) == 7 else ", ".join(DAY_NAMES[d] for d in self.days)
        what = {CHARGE: f"Charge at {self.power_w} W to {self.target_soc}%",
                DISCHARGE: f"Discharge at {self.power_w} W down to {self.target_soc}%",
                HOLD: "Hold (no charging or discharging)"}[self.action]
        return f"{what}, {self.start}-{self.end} {days}"


@dataclass(frozen=True)
class ControlSettings:
    enabled: bool = False
    hold_cheap: bool = True  # don't discharge while electricity is cheap
    grid_charge: bool = False  # also charge from the grid in the cheapest hours
    charge_power_w: int = 1500
    charge_target_soc: int = DEFAULT_CHARGE_TARGET  # stop grid charging here; easier on the cells than 100%
    charge_dispatch: bool = True  # charge (and never discharge) in Intelligent Go smart-charge slots
    auto_balance: bool = False  # now and then charge to 100% so the battery's level estimate stays accurate
    balance_days: int = 28  # how long after the last full charge it counts as due
    charge_happy: bool = True  # charge (and never discharge) in booked Octopus Happy Hours
    happy_hours: tuple[dict, ...] = ()  # booked Happy Hours: {"date": "YYYY-MM-DD", "start": "HH:MM", "end": "HH:MM"}, local time
    schedules: tuple[Schedule, ...] = ()  # your own windows; they win over cheap hours
    pace_charge: bool = False  # spread a charge over its window instead of charging flat out and then sitting full
    pace_margin_min: int = DEFAULT_PACE_MARGIN  # a paced charge aims to be done this long before the window ends

    @classmethod
    def from_dict(cls, data: dict | None) -> "ControlSettings":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in (data or {}).items() if k in known}
        data["happy_hours"] = tuple(dict(h) for h in data.get("happy_hours") or ())
        data["schedules"] = tuple(s if isinstance(s, Schedule) else Schedule.from_dict(s)
                                  for s in data.get("schedules") or ())
        return cls(**data)

    def validate(self) -> "ControlSettings":
        if not 100 <= int(self.charge_power_w) <= 5000:
            raise ValueError("Charge power must be between 100 and 5000 W")
        if not 20 <= int(self.charge_target_soc) <= 100:
            raise ValueError("Stop charging at must be between 20% and 100%")
        if len(self.schedules) > MAX_SCHEDULES:
            raise ValueError(f"Up to {MAX_SCHEDULES} schedules")
        schedules = tuple(s.validate() for s in self.schedules)
        _check_overlaps(schedules)
        if not 7 <= int(self.balance_days) <= 120:
            raise ValueError("Balance interval must be between 7 and 120 days")
        if not 15 <= int(self.pace_margin_min) <= 240:
            raise ValueError("Finish paced charging early by between 15 and 240 minutes")
        happy = tuple(_check_happy(h) for h in self.happy_hours)
        if len(happy) > MAX_HAPPY:
            raise ValueError(f"Up to {MAX_HAPPY} Happy Hours")
        return ControlSettings(enabled=bool(self.enabled), hold_cheap=bool(self.hold_cheap),
                               grid_charge=bool(self.grid_charge), charge_power_w=int(self.charge_power_w),
                               charge_target_soc=int(self.charge_target_soc), charge_dispatch=bool(self.charge_dispatch),
                               charge_happy=bool(self.charge_happy),
                               auto_balance=bool(self.auto_balance), balance_days=int(self.balance_days), happy_hours=happy, schedules=schedules,
                               pace_charge=bool(self.pace_charge), pace_margin_min=int(self.pace_margin_min))


def _check_happy(h: dict) -> dict:
    try:
        day = date.fromisoformat(str(h.get("date")))
    except ValueError:
        raise ValueError("A Happy Hour needs a date") from None
    start, end = str(h.get("start")), str(h.get("end"))
    _hhmm(start), _hhmm(end)
    if start == end:
        raise ValueError("A Happy Hour can't start and end at the same time")
    return {"date": day.isoformat(), "start": start, "end": end}


def happy_windows(happy_hours, tz, now: datetime) -> list[Window]:
    """Booked Happy Hours as concrete windows, dropping ones that ended over a day ago."""
    out = []
    for h in happy_hours:
        day = date.fromisoformat(h["date"])
        a, b = _hhmm(h["start"]), _hhmm(h["end"])
        start = datetime(day.year, day.month, day.day, *a, tzinfo=tz).astimezone(timezone.utc)
        end = datetime(day.year, day.month, day.day, *b, tzinfo=tz).astimezone(timezone.utc)
        if end <= start:
            end += timedelta(days=1)
        if end > now - timedelta(days=1):
            out.append(Window(start, end, f"Octopus Happy Hour {h['start']}-{h['end']}", "octopus", happy=True))
    return sorted(out, key=lambda w: w.start)


def happy_slots(happy_hours, tz) -> set[tuple[str, int]]:
    """The local (day, half hour) pairs the booked Happy Hours cover."""
    out = set()
    for w in happy_windows(happy_hours, tz, datetime.min.replace(tzinfo=timezone.utc) + timedelta(days=2)):
        t = w.start.astimezone(tz)
        t = t.replace(minute=t.minute // 30 * 30, second=0, microsecond=0)
        while t < w.end.astimezone(tz):
            out.add((t.date().isoformat(), t.hour * 2 + t.minute // 30))
            t += timedelta(minutes=30)
    return out


def _check_overlaps(schedules) -> None:
    """Two enabled schedules can't cover the same minute: which should win would be a guess."""
    week = 7 * 1440
    spans = [(sch, d, a, b) for sch in schedules if sch.enabled for d, a, b in sch.week_minutes()]
    for i, (s1, d1, a1, b1) in enumerate(spans):
        for s2, d2, a2, b2 in spans[i + 1:]:
            if s1 is s2:
                continue
            # Sunday night runs into Monday morning, so compare a week either side too.
            if any(a1 < b2 + k and a2 + k < b1 for k in (-week, 0, week)):
                what = {CHARGE: "Charge", HOLD: "Hold", DISCHARGE: "Discharge"}
                raise ValueError(f"Schedules overlap on {DAY_NAMES[d1]}: {what[s1.action]} {s1.start}-{s1.end} "
                                 f"and {what[s2.action]} {s2.start}-{s2.end}. Change the times or turn one off.")


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    why: str
    source: str = "octopus"  # for the event log: "octopus" or "schedule" (typed off-peak hours, your schedules)
    schedule: Schedule | None = None  # set for your own schedules
    happy: bool = False  # a booked Octopus Happy Hour: free electricity, so fill the battery
    dispatch: bool = False  # an Intelligent Go smart-charge slot: the whole home pays the off-peak price
    prices: tuple[tuple[datetime, float], ...] = ()  # (half-hour start, import p/kWh) when the rate varies in the window


def _daily(start: str, end: str, tz, now: datetime, days=range(7)) -> list[tuple[datetime, datetime]]:
    """A daily "HH:MM" to "HH:MM" window as UTC (start, end) pairs starting
    yesterday, today and tomorrow, on the given weekdays. An end at or
    before the start runs past midnight."""
    h1, m1 = _hhmm(start)
    h2, m2 = _hhmm(end)
    local = now.astimezone(tz)
    out = []
    for day in (-1, 0, 1):
        base = (local + timedelta(days=day)).date()
        if base.weekday() not in days:
            continue
        a = datetime(base.year, base.month, base.day, h1, m1, tzinfo=tz)
        b = datetime(base.year, base.month, base.day, h2, m2, tzinfo=tz)
        if b <= a:
            b += timedelta(days=1)
        out.append((a.astimezone(timezone.utc), b.astimezone(timezone.utc)))
    return out


def schedule_windows(schedules, tz, now: datetime) -> list[Window]:
    """Your enabled schedules as concrete windows around now (yesterday to tomorrow)."""
    out = [Window(a, b, f"your schedule {sch.start}-{sch.end}", "schedule", sch)
           for sch in schedules if sch.enabled
           for a, b in _daily(sch.start, sch.end, tz, now, sch.days)]
    return sorted(out, key=lambda w: w.start)


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def cheap_windows(octopus_status: dict | None, offpeak: tuple[str, str] | None, tz, now: datetime) -> list[Window]:
    """When electricity is cheap: Octopus's suggested charge windows and
    Intelligent Go slots if Octopus is connected, else the off-peak hours
    typed into the payback settings."""
    out: list[Window] = []
    if octopus_status and octopus_status.get("prices"):
        for w in octopus_status.get("recommendations") or []:
            if w["kind"] == "charge":
                a, b = _utc(w["start"]), _utc(w["end"])
                prices = tuple((t, p["import_p"]) for p in octopus_status["prices"]
                               if p.get("start") and p.get("import_p") is not None and a <= (t := _utc(p["start"])) < b)
                out.append(Window(a, b, f"{w['note']} ({w['avg_p']:.2f}p)", prices=prices))
        for d in octopus_status.get("dispatches") or []:
            a, b = _utc(d["start"]), _utc(d["end"])
            out.append(Window(a, b, f"Intelligent Go smart-charge slot {a.astimezone(tz):%H:%M}-{b.astimezone(tz):%H:%M}",
                              dispatch=True))
        return sorted(out, key=lambda w: w.start)
    if offpeak and offpeak[0] != offpeak[1]:
        out = [Window(a, b, "Off-peak hours", "schedule") for a, b in _daily(*offpeak, tz, now)]
    return out


def _charges(settings: ControlSettings, w: Window) -> bool:
    """Whether the controller charges the battery in this window."""
    if w.schedule:
        return w.schedule.action == CHARGE
    if w.happy:
        return settings.charge_happy
    if w.dispatch:
        return settings.charge_dispatch
    return settings.grid_charge


def planned_charges(settings: ControlSettings, windows: list[Window], now: datetime) -> list[tuple[datetime, str]]:
    """When the controller will next start charging the battery, and why."""
    if not settings.enabled:
        return []
    return sorted((w.start, w.why) for w in windows if w.start > now and _charges(settings, w))


def active_charge(settings: ControlSettings, windows: list[Window], now: datetime) -> tuple[datetime, str] | None:
    """The end and description of the charging window in progress, if there is one."""
    if not settings.enabled:
        return None
    ends = sorted((w.end, w.why) for w in windows if w.start <= now < w.end and _charges(settings, w))
    return ends[-1] if ends else None


def active_target(settings: ControlSettings, windows: list[Window], now: datetime, balance: bool = False) -> int | None:
    """The level the charging window in progress stops at, as `decide` would choose it."""
    if not settings.enabled:
        return None
    mine = [w for w in windows if w.start <= now < w.end and _charges(settings, w)]
    if not mine:
        return None
    w = max(mine, key=lambda w: w.end)
    if w.schedule and not (w.happy and settings.charge_happy):
        return 100 if balance and w.source == "schedule" else w.schedule.target_soc
    return 100 if balance else settings.charge_target_soc


def happy_headroom(settings: ControlSettings, windows: list[Window], snapshot: dict, now: datetime,
                   use_kwh: Callable[[datetime, datetime], float | None] | None = None,
                   accepted: str | None = None) -> dict | None:
    """Room to leave for the next booked Happy Hour, if one starts within a day and a half.

    The free session can take the battery's max charge power for as long as it
    runs, so a charge before it stops short by that much energy: 2 hours at
    2.5 kW leaves about 5 kWh. The target is never cut below the battery
    reserve or MIN_BEFORE_HAPPY. `use_kwh(a, b)` predicts the household draw
    between two moments from past days; what the house uses between the end of
    the charge in progress (or now) and the slot starting comes off the room,
    since the battery will have emptied that much by then anyway.

    The target is only lowered once you've said yes to this slot (`accepted` is
    the start time you approved); until then the result is a suggestion, with
    "ok" False, for the card to ask about."""
    rated = snapshot.get("rated_kwh")
    if not settings.charge_happy or not rated:
        return None
    nxt = next((w for w in windows if w.happy and w.start > now and w.start - now <= timedelta(hours=HEADROOM_HOURS)), None)
    if not nxt:
        return None
    power = int(snapshot.get("max_charge_w") or settings.charge_power_w)
    kwh = power / 1000 * (nxt.end - nxt.start).total_seconds() / 3600 * CHARGE_EFFICIENCY
    floor = max(int(snapshot.get("reserve_soc") or 0), MIN_BEFORE_HAPPY)
    since = max([w.end for w in windows if not w.happy and w.start <= now < w.end] or [now])
    used = use_kwh(min(since, nxt.start), nxt.start) if use_kwh else None
    used = (used or 0.0) / DISCHARGE_EFFICIENCY
    room = max(0.0, kwh - used)
    return {"window": nxt, "kwh": round(room, 1), "slot_kwh": round(kwh, 1), "use_kwh": round(used, 1),
            "pct": room / rated * 100, "floor": floor, "ok": nxt.start.isoformat() == accepted}


def lowered(target: int, room: dict | None) -> int:
    """`target` with the Happy Hour room taken off (unchanged when there is none)."""
    if not room:
        return target
    return min(target, max(room["floor"], round(target - room["pct"])))


def pace_charge(now: datetime, window: Window, soc: float, target: int, rated_kwh: float | None, cap_w: int,
                margin_min: int, delivery: float | None = None, current_w: int | None = None,
                settled: bool = False) -> dict | None:
    """How to spread a charge over its window so it reaches `target` with time to spare.

    The aim is to be done `margin_min` before the window ends (less for a short
    window), at the lowest steady power that gets there, never above `cap_w`.
    If the rate varies through the window, the cheapest half hours are used
    first, at `cap_w`, and the battery is held in between. Called again every
    tick with the real level, so a slow or interrupted charge speeds up; if it
    can no longer make it, the answer is full power.

    `settled` means the power was changed recently: it is kept unless it is more
    than 20% short of what's needed.

    `delivery` is the share of the commanded power the battery really took.
    Returns None when there's nothing to pace; else power (W, 0 = wait), kind
    ("even", "cheapest" or "full") and when it expects to be done."""
    if not rated_kwh or soc is None or soc >= target or cap_w <= 0:
        return None
    length = window.end - window.start
    deadline = window.end - min(timedelta(minutes=margin_min), length / 2)
    eff = min(1.0, max(0.5, delivery)) if delivery else CHARGE_EFFICIENCY
    need_wh = (target - soc) / 100 * rated_kwh * 1000 / eff
    if target >= 98:
        need_wh *= 1.1  # the last few percent go in slower
    left_h = (deadline - now).total_seconds() / 3600

    def full() -> dict:
        return {"power": cap_w, "kind": "full", "finish": now + timedelta(hours=need_wh / cap_w), "deadline": deadline}

    if left_h <= 0 or need_wh >= cap_w * left_h:
        return full()
    slots = [(max(t, now), min(t + timedelta(minutes=30), deadline), p) for t, p in window.prices]
    slots = [s for s in slots if s[1] > s[0]]
    if slots and max(p for *_, p in slots) - min(p for *_, p in slots) > 0.01:
        chosen, got = [], 0.0
        for lo, hi, p in sorted(slots, key=lambda s: (s[2], s[0])):
            chosen.append((lo, hi, p))
            got += cap_w * (hi - lo).total_seconds() / 3600
            if got >= need_wh:
                break
        if got < need_wh:
            return full()
        now_slot = next((s for s in chosen if s[0] <= now < s[1]), None)
        return {"power": cap_w if now_slot else 0, "kind": "cheapest", "finish": max(hi for _, hi, _ in chosen),
                "deadline": deadline, "price": now_slot[2] if now_slot else None,
                "next": None if now_slot else min((lo for lo, _, _ in chosen if lo > now), default=None)}
    power = max(need_wh / left_h, PACE_MIN_W)
    power = min(cap_w, -(-int(power) // PACE_STEP_W) * PACE_STEP_W)
    if current_w and (power <= current_w <= power + PACE_STEP_W or (settled and current_w >= power * 0.8)):
        power = min(current_w, cap_w)  # close enough: don't fiddle with the setpoint
    return {"power": power, "kind": "even", "finish": now + timedelta(hours=need_wh / power), "deadline": deadline}


def _charge(settings: ControlSettings, w: Window, snapshot: dict, now: datetime, soc, target: int, cap: int,
            text: str, pace: dict | None, grid: bool = False) -> tuple[str, int, str, Window]:
    """The charge step for a window: flat out at `cap`, or paced if that's switched on.

    `pace` is scratch space shared with the caller: it can hold "delivery",
    "current_w" and "tz" going in, and gets the plan under "plan" coming out."""
    plan = None
    if settings.pace_charge and pace is not None and w.source != "override" and not w.happy:
        plan = pace_charge(now, w, soc, target, snapshot.get("rated_kwh"), cap, settings.pace_margin_min,
                           pace.get("delivery"), pace.get("current_w"), bool(pace.get("settled")))
    if pace is not None:
        pace["plan"] = plan
    if plan is None or plan["kind"] == "full":
        extra = ""
        if plan is not None and plan["kind"] == "full":
            extra = "; no time to spare, so flat out"
        lead = f"Charging from the grid to {target}%" if grid else f"Charging at {cap} W to {target}%"
        return CHARGE, -cap, f"{lead} ({text}{extra})", w
    tz = pace.get("tz") or timezone.utc
    done = f"{plan['finish'].astimezone(tz):%H:%M}"
    if plan["power"] <= 0:
        when = f", next at {plan['next'].astimezone(tz):%H:%M}" if plan.get("next") else ""
        return HOLD, 0, f"Waiting for a cheaper half hour; paced to reach {target}% by about {done}{when} ({text})", w
    how = f"paced to reach {target}% by about {done}"
    if plan["kind"] == "cheapest":
        how = f"cheapest half hours first, done by about {done}"
    return CHARGE, -plan["power"], f"Charging at {plan['power']} W, {how} ({text})", w


def decide(settings: ControlSettings, windows: list[Window], snapshot: dict, now: datetime,
           balance: bool = False, pace: dict | None = None) -> tuple[str, int, str, Window | None]:
    """(action, setpoint W, reason, window) for this moment.

    With `balance`, any charging in a cheap, free or scheduled window goes to
    100% instead of the usual target, even if grid charging is otherwise off.

    Your own schedules come first; cheap-hour windows only apply when the
    cheap-hour options are ticked.

    With paced charging on, charges in schedules, cheap hours and smart-charge
    slots are spread out (see `pace_charge`); `pace` carries what it needs in
    and returns the plan. Happy Hours always charge flat out."""
    if not settings.enabled:
        return APP, 0, "Control is off", None
    soc = snapshot.get("soc")
    happy = next((w for w in windows if w.happy and w.start <= now < w.end), None) if settings.charge_happy else None
    room = None if happy else happy_headroom(settings, windows, snapshot, now, (pace or {}).get("use_kwh"), (pace or {}).get("room_ok"))
    waiting = room is not None
    if room and not room["ok"]:
        room = None  # only a suggestion until you say yes
    if balance and waiting:
        balance = False  # the free session is the place for the full charge; don't pay for one tonight
    if balance:
        settings = replace(settings, charge_target_soc=100, grid_charge=True)
    note = f"; leaving room for the {room['window'].why}" if room else ""
    cheap_target = lowered(settings.charge_target_soc, room)
    if happy:
        # Free power, so charge as fast as the battery allows and never discharge.
        # Beats every schedule; the normal plan resumes when the hour ends.
        if soc is not None and soc < settings.charge_target_soc:
            power = int(snapshot.get("max_charge_w") or settings.charge_power_w)
            return CHARGE, -power, f"Charging at {power} W to {settings.charge_target_soc}% ({happy.why})", happy
        return HOLD, 0, f"Holding the battery (target reached; {happy.why})", happy
    mine = next((w for w in windows if w.schedule and w.start <= now < w.end), None)
    slot = next((w for w in windows if w.dispatch and w.start <= now < w.end), None) if settings.charge_dispatch else None
    if slot and not (mine and mine.schedule.action == CHARGE):
        # Smart-charge slots come at short notice, at any time of day, and
        # price the whole home at off-peak: top the battery up and let the
        # house run on the grid rather than spend stored energy now.
        if soc is not None and soc < cheap_target:
            power = settings.charge_power_w
            if snapshot.get("max_charge_w"):
                power = min(power, int(snapshot["max_charge_w"]))
            return _charge(settings, slot, snapshot, now, soc, cheap_target, power, f"{slot.why}{note}", pace)
        return HOLD, 0, f"Holding the battery (target reached; {slot.why})", slot
    if mine:
        sch = mine.schedule
        target = 100 if balance and mine.source == "schedule" else sch.target_soc
        if mine.source == "schedule":  # not a quick override
            target = lowered(target, room)
        if sch.action == CHARGE and soc is not None and soc < target:
            power = min(sch.power_w, int(snapshot["max_charge_w"])) if snapshot.get("max_charge_w") else sch.power_w
            return _charge(settings, mine, snapshot, now, soc, target, power, f"{mine.why}{note}", pace)
        floor = max(sch.target_soc, snapshot.get("reserve_soc") or 0)
        if sch.action == DISCHARGE and soc is not None and soc > floor:
            power = min(sch.power_w, int(snapshot["max_discharge_w"])) if snapshot.get("max_discharge_w") else sch.power_w
            return DISCHARGE, power, f"Discharging at {power} W down to {floor}% ({mine.why})", mine
        reached = {CHARGE: " (target reached)", DISCHARGE: " (floor reached)"}.get(sch.action, "")
        return HOLD, 0, f"Holding the battery{reached} ({mine.why})", mine
    if not (settings.hold_cheap or settings.grid_charge):
        windows = [w for w in windows if w.schedule or (w.dispatch and settings.charge_dispatch)]
    current = next((w for w in windows if not w.schedule and w.start <= now < w.end), None)
    if current is None:
        nxt = next((w for w in windows if w.start > now), None)
        return APP, 0, "No schedule or cheap window now: the Anker app is in charge", nxt
    if settings.grid_charge and soc is not None and soc < cheap_target:
        power = settings.charge_power_w
        if snapshot.get("max_charge_w"):
            power = min(power, int(snapshot["max_charge_w"]))
        return _charge(settings, current, snapshot, now, soc, cheap_target, power, f"{current.why}{note}", pace, grid=True)
    if settings.hold_cheap or settings.grid_charge:
        return HOLD, 0, f"Holding the battery so the house uses cheap grid power ({current.why})", current
    return APP, 0, "Normal price: the Anker app is in charge", None


class Controller:
    def __init__(self, collector, load: Callable[[], dict], save_state: Callable[[dict], None],
                 windows: Callable[[datetime], list[Window]],
                 last_full: Callable[[], float | None] = lambda: None) -> None:
        self.collector = collector
        self._load = load  # -> {"settings": {...}, "state": {...}}
        self._save_state = save_state
        self._windows = windows
        self._history_full = last_full  # unix time the battery was last seen full, from the recorded history
        stored = load()
        self.settings = ControlSettings.from_dict(stored.get("settings"))
        state = stored.get("state") or {}
        # The mode to give back. Kept on disk so a restart mid-window still restores it.
        self.saved_mode: int | None = state.get("saved_mode")
        self.in_control = bool(state.get("in_control"))
        # The battery's own SOC limits as they were before we narrowed them; kept on disk so a restart puts them back.
        self.saved_limits: dict[str, int] = dict(state.get("saved_limits") or {})
        self._last_cap: dict[str, int] = {}
        self._lost_since: float | None = None  # when the battery readings went missing or stale (unix time)
        self.override: dict | None = state.get("override") or None
        self.full_at: float | None = state.get("full_at")  # last time we saw it full, kept across restarts
        self.balancing = False
        self.pace: dict | None = None  # the paced charge in progress, for the card
        self._pace_logged: tuple | None = None
        self._setpoint_since = 0.0  # when the setpoint last changed (monotonic)
        self.room: dict | None = None  # room being left for a booked Happy Hour, for the card
        self._room_raw: dict | None = None
        self._room_logged: tuple | None = None
        self.room_ok: str | None = state.get("room_ok")  # start of the Happy Hour you said yes to leaving room for
        self.room_declined: str | None = state.get("room_declined")
        self.suggestion: dict | None = None  # lower-the-target offer waiting for a yes or no
        self.forecast: dict | None = None  # predicted level when the next Happy Hour starts, for the card
        self._profile: tuple[float, dict] | None = None  # (when built, usage_forecast.slot_profile)
        self.action = APP
        self.reason = "Starting"
        self.window: Window | None = None
        self.last_write = 0.0
        self.last_setpoint: int | None = None
        self.last_error: str | None = None
        self.took_control_at = 0.0
        self.stand_back_until: datetime | None = None
        self._source = "dashboard"
        self.log: deque = deque(maxlen=50)
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()  # one tick at a time, whether from the loop or a settings change

    @property
    def live(self) -> bool:
        return live_writes_allowed()

    def update_settings(self, settings: ControlSettings) -> None:
        old, self.settings = self.settings, settings
        self._note(f"Settings changed: control {'on' if settings.enabled else 'off'}")
        bits = [f"control {'on' if settings.enabled else 'off'}" + ("" if self.live else " (dry run)")]
        if settings.hold_cheap or settings.grid_charge:
            bits.append("hold in cheap hours" + (f", grid charge to {settings.charge_target_soc}%" if settings.grid_charge else ""))
        on = [s for s in settings.schedules if s.enabled]
        bits.append(f"{len(on)} schedule{'s' if len(on) != 1 else ''}" + (": " + "; ".join(s.describe() for s in on) if on else ""))
        self._event("Control settings saved: " + ", ".join(bits), "control_enabled", old.enabled, settings.enabled,
                    "dashboard", always=True)
        self._wake.set()

    def _note(self, text: str) -> None:
        self.log.appendleft({"ts": time.time(), "text": text})
        log.info("Control: %s", text)

    def _event(self, message: str, field: str, old, new, source: str, always: bool = False) -> None:
        """Add a line to the battery event log.
        Battery writes are only logged when they really happen (live)."""
        storage = getattr(self.collector, "storage", None)
        if not (self.live or always) or storage is None:
            return
        try:
            storage.record_event("control", message, field=field, old=old, new=new, source=source)
        except Exception as err:  # the log must never stop control
            log.warning("Couldn't record control event: %s", err)

    def _persist(self) -> None:
        self._save_state({"saved_mode": self.saved_mode, "in_control": self.in_control,
                          "saved_limits": self.saved_limits, "override": self.override, "full_at": self.full_at,
                          "room_ok": self.room_ok, "room_declined": self.room_declined})

    def answer_room(self, start: str, accept: bool) -> None:
        """Yes or no to lowering the charge target for the Happy Hour starting at `start`."""
        sug = self.suggestion
        if not sug or sug["start"] != start:
            raise ValueError("That suggestion is no longer current")
        if accept:
            self.room_ok, self.room_declined = start, None
            self._event(f"You chose to charge to {sug['to']}% instead of {sug['from']}% for the {sug['slot']}",
                        "happy_headroom", sug["from"], sug["to"], "dashboard", always=True)
        else:
            self.room_declined = start
            self._event(f"You kept the {sug['from']}% charge target for the {sug['slot']}", "happy_headroom",
                        sug["from"], sug["from"], "dashboard", always=True)
        self.suggestion = None
        self._persist()

    def _last_full(self) -> float | None:
        seen = [t for t in (self.full_at, self._history_full()) if t]
        return max(seen) if seen else None

    def balance_due(self, now: datetime) -> bool:
        """Auto balance is on, the battery can take a 100% limit, and it's been too long since it was full."""
        if not self.settings.auto_balance or not limits_allowed():
            return False
        last = self._last_full()
        return last is None or now.timestamp() - last >= self.settings.balance_days * 86400

    async def _write(self, key: str, value: int) -> None:
        # Last line of defence before the battery: whatever a bug or a bad setting asks for,
        # nothing outside what the hardware and this dashboard allow reaches a register.
        if key == "battery_power_setpoint":
            if not -MAX_SETPOINT_W <= value <= MAX_SETPOINT_W:
                raise ValueError(f"Refusing a {value} W setpoint (limit {MAX_SETPOINT_W} W)")
        elif key == "operating_mode":
            if value not in OPERATING_MODES:
                raise ValueError(f"Refusing unknown operating mode {value}")
        elif key in LIMIT_RANGES:
            if not 0 <= value <= 100:  # not LIMIT_RANGES: putting back a limit the battery already had must work
                raise ValueError(f"Refusing {key} = {value}")
        else:
            raise ValueError(f"Refusing to write {key}")
        if key == "battery_power_setpoint":
            value = self._clamp_setpoint(value)  # then within the battery's own power limits
        if not self.live:
            self._note(f"Dry run: would write {key} = {value}")
            return
        await self.collector.write(key, value)

    def _use_kwh(self, tz, now: datetime) -> Callable[[datetime, datetime], float | None] | None:
        """Predict home draw between two moments from the last two weeks of stored half hours (None without history)."""
        storage = getattr(self.collector, "storage", None)
        if storage is None or not hasattr(storage, "usage_slots"):
            return None
        if not self._profile or time.monotonic() - self._profile[0] > PROFILE_TTL_SECONDS:
            try:
                prof = usage_forecast.slot_profile(storage.usage_slots(usage_forecast.HISTORY_DAYS + 1),
                                                   now.astimezone(tz).date())
            except Exception:
                log.exception("Couldn't build the home usage profile")
                prof = {}
            self._profile = (time.monotonic(), prof)
        prof = self._profile[1]

        def use(a: datetime, b: datetime) -> float | None:
            got = usage_forecast.predict_kwh(prof, a.astimezone(tz), b.astimezone(tz))
            return got[0] if got else None
        return use

    def _update_forecast(self, snap: dict, windows: list[Window], now: datetime, use_kwh, room: dict | None,
                         target: int | None, tz=timezone.utc) -> None:
        """What the battery level is likely to be when the next Happy Hour starts, for the card."""
        self.forecast = None
        soc, rated = snap.get("soc"), snap.get("rated_kwh")
        nxt = next((w for w in windows if w.happy and w.start > now and w.start - now <= timedelta(hours=HEADROOM_HOURS)), None)
        if not (self.settings.charge_happy and nxt and rated and isinstance(soc, (int, float)) and use_kwh):
            return
        since = max([w.end for w in windows if not w.happy and w.start <= now < w.end] or [now])
        since = min(since, nxt.start)
        used = use_kwh(since, nxt.start)
        if used is None:
            return
        # While a charge runs the level at its end is the target (or the level now if it is already past it).
        start_soc = max(soc, target) if since > now and target is not None else soc
        drop = used / DISCHARGE_EFFICIENCY / rated * 100
        floor = int(snap.get("reserve_soc") or 0)
        at_start = max(floor, start_soc - drop)
        power = int(snap.get("max_charge_w") or self.settings.charge_power_w)
        slot_pct = power / 1000 * (nxt.end - nxt.start).total_seconds() / 3600 * CHARGE_EFFICIENCY / rated * 100
        self.forecast = {"slot": nxt.why, "start": nxt.start.isoformat(), "soc_now": round(soc), "use_kwh": round(used, 1),
                         "soc_at_start": round(at_start), "slot_pct": round(slot_pct),
                         "soc_after": round(min(100, at_start + slot_pct)), "from": since.isoformat(),
                         "basis": usage_forecast.basis(self._profile[1], nxt.start.astimezone(tz)) if self._profile else None}
    def _clamp_setpoint(self, value: int) -> int:
        """Last line of defence: never ask for more than the battery's own power limits (or a fixed cap)."""
        snap = getattr(self.collector, "snapshot", None) or {}
        charge = min(int(snap.get("max_charge_w") or MAX_SETPOINT_W), MAX_SETPOINT_W)
        discharge = min(int(snap.get("max_discharge_w") or MAX_SETPOINT_W), MAX_SETPOINT_W)
        clamped = max(-charge, min(discharge, int(value)))
        if clamped != value:
            self._note(f"Setpoint {value} W is beyond the battery's limits; using {clamped} W")
        return clamped

    def _lost_contact(self, now: datetime) -> bool:
        """True once the battery has gone quiet for STALE_SECONDS."""
        updated = getattr(self.collector, "last_update", None)
        stale = isinstance(updated, (int, float)) and now.timestamp() - updated > STALE_SECONDS
        if self.collector.connected and (self.collector.snapshot or {}) and not stale:
            self._lost_since = None
            return False
        if self._lost_since is None:
            self._lost_since = now.timestamp()
        return now.timestamp() - self._lost_since >= STALE_SECONDS

    async def _failsafe(self) -> None:
        """Contact with the battery was lost while the dashboard was driving it: give control back if we can."""
        mode = self.saved_mode if self.saved_mode is not None else 0
        self.reason = "Lost contact with the battery: handing control back to the Anker app"
        try:
            await self._write("operating_mode", mode)
        except Exception as err:  # still unreachable; try again next tick
            self.last_error = f"Failsafe couldn't reach the battery yet: {err}"
            self._note(self.last_error)
            return
        self._note(f"Lost contact with the battery; handed control back ({OPERATING_MODES.get(mode, mode)})")
        self._event("Failsafe: lost contact with the battery, handed control back to the Anker app", "operating_mode",
                    OPERATING_MODES[THIRD_PARTY_MODE], OPERATING_MODES.get(mode, mode), "failsafe", always=True)
        self.in_control, self.saved_mode, self.last_setpoint, self.action = False, None, None, APP
        self._persist()
        self.last_error = None


    async def tick(self, now: datetime | None = None) -> None:
        async with self._lock:
            await self._tick(now or datetime.now(timezone.utc))

    async def _tick(self, now: datetime) -> None:
        snap = self.collector.snapshot or {}
        lost = self._lost_contact(now)
        if lost and self.in_control:
            await self._failsafe()
            return
        if not self.collector.connected or not snap:
            self.reason = "Waiting for the battery"
            return
        if self.saved_limits and not self.in_control:
            await self._restore_limits()  # e.g. a retry after a failed write, or the mode was changed in the app
        quick = self._override_window(now)
        settings = replace(self.settings, enabled=True) if quick and not self.settings.enabled else self.settings
        windows = ([quick] if quick else []) + self._windows(now)
        # The battery reserve (its discharge limit) is a floor no dashboard discharge goes under.
        reserve = self.saved_limits.get("discharge_limit_soc", self.collector.raw.get("discharge_limit_soc"))
        if isinstance(reserve, int):
            snap = {**snap, "reserve_soc": reserve}
        soc = snap.get("soc")
        if isinstance(soc, (int, float)) and soc >= FULL_SOC and (not self.full_at or now.timestamp() - self.full_at > 3600):
            was = self.balancing
            self.full_at = now.timestamp()
            self._persist()
            if was:
                self._event("Auto balance finished: the battery reached 100%", "auto_balance", "charging", "done", "dashboard", always=True)
        balance = self.balance_due(now) and not quick
        tz = getattr(getattr(self.collector, "storage", None), "tz", None) or timezone.utc
        use_kwh = self._use_kwh(tz, now)
        pace_io = {"tz": tz, "use_kwh": use_kwh, "room_ok": self.room_ok}
        if self.action == CHARGE and self.last_setpoint and self.last_setpoint < 0:
            pace_io["current_w"] = -self.last_setpoint
            pace_io["settled"] = time.monotonic() - self._setpoint_since < PACE_SETTLE_SECONDS
            power = snap.get("battery_w")
            if isinstance(power, (int, float)) and power < 0 and time.monotonic() - self._setpoint_since > 90:
                pace_io["delivery"] = -power / -self.last_setpoint  # how much of the commanded power it really took
        action, setpoint, reason, window = decide(settings, windows, snap, now, balance, pace_io)
        self._update_pace(pace_io.get("plan") if action in (CHARGE, HOLD) else None, window, now, tz, settings)
        balancing = balance and action == CHARGE and (soc is None or soc < FULL_SOC)
        if balancing and not self.balancing:
            self._event(f"Auto balance: charging to 100% ({window.why if window else reason})", "auto_balance", "due",
                        "charging", window.source if window else "dashboard", always=True)
        self.balancing = balancing
        happy_now = any(w.happy and w.start <= now < w.end for w in windows)
        room = None if happy_now or balancing else happy_headroom(settings, windows, snap, now, use_kwh, self.room_ok)
        self.suggestion = None
        if room and not room["ok"]:
            base = window.schedule.target_soc if window and window.schedule else settings.charge_target_soc
            lower = lowered(base, room)
            if (settings.enabled and action == CHARGE and window is not None and window.source != "override"
                    and lower < base and room["window"].start.isoformat() != self.room_declined):
                self.suggestion = {"start": room["window"].start.isoformat(), "slot": room["window"].why,
                                   "from": base, "to": lower, "use_kwh": room["use_kwh"], "kwh": room["kwh"]}
            room = None
        if settings.enabled and room and action == CHARGE and window is not None and window.source != "override":
            base = window.schedule.target_soc if window.schedule else settings.charge_target_soc
            target = lowered(base, room)
            self._room_raw = room
            self.room = {"kwh": room["kwh"], "target": target, "slot": room["window"].why,
                         "start": room["window"].start.isoformat(), "slot_kwh": room["slot_kwh"], "use_kwh": room["use_kwh"]}
            key = (self.room["start"], target)
            if key != self._room_logged:
                self._room_logged = key
                used = f", after about {room['use_kwh']:g} kWh of expected home use before it" if room["use_kwh"] else ""
                self._event(f"Leaving room for the {room['window'].why}: charging to {target}% instead of "
                            f"{base}% so it can take about {room['kwh']:g} kWh free{used}",
                            "happy_headroom", None, target, window.source, always=True)
        else:
            self.room = self._room_raw = None
        self._update_forecast(snap, windows, now, use_kwh, room, self.room["target"] if self.room else None, tz)
        if balancing:
            reason = f"Auto balance: {reason}"
        mode = self.collector.raw.get("operating_mode")
        if (self.live and self.in_control and isinstance(mode, int) and mode != THIRD_PARTY_MODE
                and time.monotonic() - self.took_control_at > 3 * REASSERT_SECONDS):
            # Someone picked a mode in the Anker app while we were in control: respect it.
            self._note(f"Mode changed to {OPERATING_MODES.get(mode, mode)} outside the dashboard; standing back until the next cheap window")
            self.in_control, self.saved_mode, self.last_setpoint = False, None, None
            self.stand_back_until = window.end if window else None
            self._persist()
        if self.stand_back_until and now < self.stand_back_until and action != APP:
            action, setpoint, reason = APP, 0, "Standing back: the mode was changed in the Anker app"
        self.reason, self.window = reason, window
        self._source = window.source if window else "dashboard"
        try:
            if action == APP:
                if self.in_control:
                    mode = self.saved_mode if self.saved_mode is not None else 0
                    await self._write("operating_mode", mode)
                    self._note(f"Gave control back to the Anker app ({OPERATING_MODES.get(mode, mode)})")
                    self._event(f"Gave control back to the Anker app: {reason}", "operating_mode",
                                OPERATING_MODES[THIRD_PARTY_MODE], OPERATING_MODES.get(mode, mode),
                                "dashboard" if not self.settings.enabled else self._source)
                    self.in_control, self.saved_mode, self.last_setpoint = False, None, None
                    self._persist()
                    await self._restore_limits()
                self.action = APP
            else:
                if not self.in_control:
                    mode = self.collector.raw.get("operating_mode")
                    self.saved_mode = mode if isinstance(mode, int) and mode != THIRD_PARTY_MODE else (self.saved_mode or 0)
                    self.in_control = True
                    self.took_control_at = time.monotonic()
                    self._persist()  # before writing, so a crash can still restore it
                    try:
                        await self._write("operating_mode", THIRD_PARTY_MODE)
                    except Exception:
                        self.in_control = False  # try again next tick
                        self._persist()
                        raise
                    self._note(f"Took control from the Anker app (was {OPERATING_MODES.get(self.saved_mode, self.saved_mode)})")
                    self._event(f"Took control: {window.why if window else reason}", "operating_mode",
                                OPERATING_MODES.get(self.saved_mode, self.saved_mode), OPERATING_MODES[THIRD_PARTY_MODE],
                                window.source if window else "dashboard")
                due = time.monotonic() - self.last_write > REASSERT_SECONDS
                if setpoint != self.last_setpoint or due:
                    await self._write("battery_power_setpoint", setpoint)
                    if setpoint != self.last_setpoint:
                        self._setpoint_since = time.monotonic()
                        what = {HOLD: "Holding the battery at 0 W", CHARGE: f"Charging at {-setpoint} W",
                                DISCHARGE: f"Discharging at {setpoint} W"}[action]
                        self._note(what)
                        self._event(what, "battery_power_setpoint", self.last_setpoint, setpoint,
                                    window.source if window else "dashboard")
                    self.last_setpoint, self.last_write = setpoint, time.monotonic()
                await self._cap_limits(action, window)
                self.action = action
            self.last_error = None
        except Exception as err:
            self.last_error = str(err)
            self._note(f"Write failed: {err}")

    def _update_pace(self, plan: dict | None, window: Window | None, now: datetime, tz, settings: ControlSettings) -> None:
        if plan is None or window is None:
            self.pace = self._pace_logged = None
            return
        self.pace = {"kind": plan["kind"], "power_w": plan["power"], "finish": plan["finish"].isoformat(),
                     "deadline": plan["deadline"].isoformat(), "margin_min": settings.pace_margin_min}
        key = (window.start, plan["kind"])
        if key == self._pace_logged:
            return
        behind = self._pace_logged is not None and self._pace_logged[0] == window.start and plan["kind"] == "full"
        self._pace_logged = key
        done = f"{plan['finish'].astimezone(tz):%H:%M}"
        end = f"{window.end.astimezone(tz):%H:%M}"
        if plan["kind"] == "full":
            what = ("Charge is behind schedule: back to full power" if behind else
                    "Not enough time to pace the charge: charging at full power")
            what += f" (window ends {end})"
        elif plan["kind"] == "cheapest":
            what = f"Pacing the charge: cheapest half hours first, done by about {done} (window ends {end})"
        else:
            what = f"Pacing the charge at about {plan['power']} W, done by about {done} (window ends {end}, {settings.pace_margin_min} min to spare)"
        self._note(what)
        self._event(what, "pace_charge", None, plan["kind"], window.source, always=True)

    def _override_window(self, now: datetime) -> Window | None:
        o = self.override
        if not o:
            return None
        until = datetime.fromisoformat(o["until"])
        if now >= until:
            self.override = None
            self._persist()
            self._note("Quick override finished")
            return None
        sch = Schedule(o["action"], "00:00", "23:59", tuple(range(7)), o["power_w"], o["target_soc"])
        return Window(datetime.fromisoformat(o["started"]), until, "quick override", "override", sch)

    async def set_override(self, action: str, minutes: int, power_w: int | None = None,
                           target_soc: int | None = None, now: datetime | None = None) -> None:
        """Charge, hold or discharge for the next few minutes, whatever the schedules say.

        It works even when schedules are switched off, and it ends by itself."""
        if action not in ACTIONS:
            raise ValueError("A quick override must charge, hold or discharge")
        if not 5 <= int(minutes) <= OVERRIDE_MAX_MINUTES:
            raise ValueError(f"Pick between 5 minutes and {OVERRIDE_MAX_MINUTES // 60} hours")
        power = int(power_w if power_w is not None else self.settings.charge_power_w)
        target = int(target_soc if target_soc is not None
                     else self.settings.charge_target_soc if action == CHARGE else 10)
        Schedule(action, "00:00", "23:59", tuple(range(7)), power, target).validate()
        now = now or datetime.now(timezone.utc)
        self.override = {"action": action, "started": now.isoformat(), "until": (now + timedelta(minutes=int(minutes))).isoformat(),
                         "power_w": power, "target_soc": target}
        self.stand_back_until = None
        self._persist()
        what = {HOLD: "Hold", CHARGE: f"Charge at {power} W to {target}%", DISCHARGE: f"Discharge at {power} W down to {target}%"}[action]
        self._note(f"Quick override: {what} for {int(minutes)} minutes")
        self._event(f"Quick override started: {what} for {int(minutes)} minutes", "override", None, action, "dashboard", always=True)
        self._wake.set()
        await self.tick(now)

    async def clear_override(self) -> None:
        if self.override:
            self.override = None
            self._persist()
            self._note("Quick override cancelled")
            self._event("Quick override cancelled", "override", None, None, "dashboard", always=True)
        await self.tick()

    async def _cap_limits(self, action: str, window: Window | None) -> None:
        """While charging or discharging, narrow the battery's own SOC limit to the target.

        If the dashboard (or the Pi) stops mid-window the battery then still stops at the
        target instead of carrying on. The limit that was there is put back afterwards."""
        want = None
        sch = window.schedule if window else None
        if limits_allowed():
            if action == CHARGE:
                base = sch.target_soc if sch else self.settings.charge_target_soc
                want = ("charging_limit_soc", 100 if self.balancing else
                        lowered(base, self._room_raw) if not (window and window.source == "override") else base)
            elif action == DISCHARGE and sch:
                want = ("discharge_limit_soc", max(sch.target_soc, self.saved_limits.get("discharge_limit_soc", 0)))
        for key in [k for k in self.saved_limits if not want or k != want[0]]:
            await self._restore_limit(key)
        if not want:
            return
        key, value = want
        current = self.collector.raw.get(key)
        if key not in self.saved_limits:
            if not isinstance(current, int):
                return  # can't put it back afterwards, so leave it alone
            self.saved_limits[key] = current
            self._persist()  # before writing, so a crash can still restore it
        saved = self.saved_limits[key]
        lo, hi = LIMIT_RANGES[key]
        # Never widen your own limit, and stay inside what the battery accepts.
        if self.balancing and key == "charging_limit_soc":
            value = max(value, saved)  # a balance run is the one time we raise your own limit; it goes back afterwards
        else:
            value = min(max(value, lo), saved) if key == "charging_limit_soc" else max(min(value, hi), saved)
        if self._last_cap.get(key) == value:
            return
        await self._write(key, value)
        self._last_cap[key] = value
        name = "charge" if key == "charging_limit_soc" else "discharge"
        self._note(f"Set the battery's own {name} limit to {value}% while it {name}s")
        self._event(f"Set the battery's own {name} limit to {value}% while it {name}s (was {saved}%)", key, saved, value,
                    window.source if window else "dashboard")

    async def _restore_limit(self, key: str) -> None:
        saved = self.saved_limits[key]
        await self._write(key, saved)
        del self.saved_limits[key]
        self._last_cap.pop(key, None)
        self._persist()
        name = "charge" if key == "charging_limit_soc" else "discharge"
        self._note(f"Put the battery's own {name} limit back to {saved}%")
        self._event(f"Put the battery's own {name} limit back to {saved}%", key, None, saved, "dashboard")

    async def _restore_limits(self) -> None:
        for key in list(self.saved_limits):
            try:
                await self._restore_limit(key)
            except Exception as err:  # retried at the start of the next tick
                self.last_error = str(err)
                self._note(f"Couldn't put the battery's {key} back yet: {err}")
                return

    async def set_limits(self, limits: dict[str, int]) -> None:
        """Set the battery's own charge and/or discharge limit, as in the Anker app.

        These hold all the time, solar charging included. If control is
        narrowing a limit right now, the new value is what gets put back
        afterwards (and it is written now if it is stricter)."""
        for key, value in limits.items():
            lo, hi = LIMIT_RANGES.get(key, (None, None))
            if lo is None:
                raise ValueError(f"{key} isn't a battery limit")
            if not lo <= int(value) <= hi:
                name = "Charge limit" if key == "charging_limit_soc" else "Discharge limit"
                raise ValueError(f"{name} must be between {lo}% and {hi}% (the battery won't accept other values)")
        async with self._lock:
            for key, value in limits.items():
                value = int(value)
                old = self.collector.raw.get(key)
                name = "charge" if key == "charging_limit_soc" else "discharge"
                if key in self.saved_limits:
                    self.saved_limits[key] = value
                    self._last_cap.pop(key, None)  # the next tick writes the stricter of the cap and this
                    self._persist()
                    self._note(f"Battery {name} limit will be {value}% once control stops narrowing it")
                    self._event(f"Battery {name} limit set to {value}% from the dashboard (applies after the current window)",
                                key, old, value, "dashboard")
                    continue
                if old == value:
                    continue
                await self._write(key, value)
                if self.live:
                    self.collector.raw[key] = value  # show it now, before the next poll reads it back
                self._note(f"Battery {name} limit set to {value}% from the dashboard")
                self._event(f"Battery {name} limit set to {value}% from the dashboard", key, old, value, "dashboard")
            self.last_error = None
        self._wake.set()

    async def set_mode(self, mode: int) -> None:
        """Switch the battery to one of the Anker app's modes now.

        If the dashboard was in control, it stands back until the current
        window ends, the same as when the mode is changed in the app."""
        if mode not in OPERATING_MODES or mode == THIRD_PARTY_MODE:
            raise ValueError("Pick one of the Anker app's modes")
        async with self._lock:
            old = self.collector.raw.get("operating_mode")
            await self._write("operating_mode", mode)
            if self.live:
                self.collector.raw["operating_mode"] = mode  # show it now, before the next poll reads it back
            name = OPERATING_MODES[mode]
            self._note(f"Mode set to {name} from the dashboard")
            self._event(f"Mode set to {name} from the dashboard", "operating_mode",
                        OPERATING_MODES.get(old, old), name, "dashboard")
            if self.in_control:
                self.in_control, self.saved_mode, self.last_setpoint = False, None, None
                self.stand_back_until = self.window.end if self.window else None
                self.action, self.reason = APP, f"Standing back: you picked {name}"
                self._persist()
            if self.override:
                self.override = None
                self._persist()
            await self._restore_limits()
            self.last_error = None

    async def stop(self) -> None:
        """Emergency stop: cancel any override, switch control off and hand the battery back now."""
        async with self._lock:
            self.override = None
            self.settings = replace(self.settings, enabled=False)
            if self.in_control:
                mode = self.saved_mode if self.saved_mode is not None else 0
                await self._write("operating_mode", mode)
                self._note(f"Stopped from the dashboard; gave control back ({OPERATING_MODES.get(mode, mode)})")
                self._event("Control stopped from the dashboard: gave control back to the Anker app", "operating_mode",
                            OPERATING_MODES[THIRD_PARTY_MODE], OPERATING_MODES.get(mode, mode), "dashboard", always=True)
                self.in_control, self.saved_mode, self.last_setpoint, self.action = False, None, None, APP
            self._persist()
            await self._restore_limits()
            self.reason, self.last_error = "Stopped from the dashboard", None
        self._wake.set()

    async def restore(self) -> None:
        """Give control back now, e.g. when the dashboard shuts down."""
        self.override = None
        if self.in_control or self.saved_limits:
            self.settings = ControlSettings(**{**asdict(self.settings), "enabled": False})
            await self.tick()
            self.settings = ControlSettings.from_dict(self._load().get("settings"))

    async def run(self) -> None:
        while True:
            await self.tick()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    def status(self) -> dict:
        w = self.window
        return {
            "settings": asdict(self.settings),
            "live": self.live,
            "action": self.action,
            "reason": self.reason,
            "in_control": self.in_control,
            "saved_mode": OPERATING_MODES.get(self.saved_mode) if self.saved_mode is not None else None,
            "override": self.override,
            "happy_room": self.room,
            "happy_forecast": self.forecast,
            "happy_suggestion": self.suggestion,
            "pace": self.pace,
            "balance": {"due": self.balance_due(datetime.now(timezone.utc)), "active": self.balancing,
                        "last_full": self._last_full()},
            "window": {"start": w.start.isoformat(), "end": w.end.isoformat(), "why": w.why} if w else None,
            "modes": [{"value": k, "name": v} for k, v in OPERATING_MODES.items() if k != THIRD_PARTY_MODE],
            "limits": {k: self.saved_limits.get(k, self.collector.raw.get(k)) if self.collector.connected else None
                       for k in LIMIT_RANGES},
            "limit_ranges": LIMIT_RANGES,
            "recommended_limits": RECOMMENDED_LIMITS,
            "battery_mode": self.collector.raw.get("operating_mode") if self.collector.connected else None,
            "last_error": self.last_error,
            "log": list(self.log)[:20],
        }
