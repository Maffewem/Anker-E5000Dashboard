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
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Callable

from .registers import OPERATING_MODES, THIRD_PARTY_MODE

log = logging.getLogger("solarbank.control")

TICK_SECONDS = 15
REASSERT_SECONDS = 60  # rewrite the setpoint now and then in case the battery forgot it
APP = "app"
HOLD = "hold"
CHARGE = "charge"
DISCHARGE = "discharge"
ACTIONS = (CHARGE, HOLD, DISCHARGE)
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MAX_SCHEDULES = 20


def live_writes_allowed() -> bool:
    return os.environ.get("CONTROL_LIVE", "").strip().lower() in ("1", "true", "yes", "on")


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
    target_soc: int = 90  # charge: stop here; discharge: stop here (a floor)
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
    charge_target_soc: int = 90  # stop grid charging here; easier on the cells than 100%
    charge_dispatch: bool = True  # charge (and never discharge) in Intelligent Go smart-charge slots
    schedules: tuple[Schedule, ...] = ()  # your own windows; they win over cheap hours

    @classmethod
    def from_dict(cls, data: dict | None) -> "ControlSettings":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in (data or {}).items() if k in known}
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
        return ControlSettings(enabled=bool(self.enabled), hold_cheap=bool(self.hold_cheap),
                               grid_charge=bool(self.grid_charge), charge_power_w=int(self.charge_power_w),
                               charge_target_soc=int(self.charge_target_soc), charge_dispatch=bool(self.charge_dispatch),
                               schedules=tuple(s.validate() for s in self.schedules))


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    why: str
    source: str = "octopus"  # for the event log: "octopus" or "schedule" (typed off-peak hours, your schedules)
    schedule: Schedule | None = None  # set for your own schedules
    dispatch: bool = False  # an Intelligent Go smart-charge slot: the whole home pays the off-peak price


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
                out.append(Window(_utc(w["start"]), _utc(w["end"]), f"{w['note']} ({w['avg_p']:.2f}p)"))
        for d in octopus_status.get("dispatches") or []:
            a, b = _utc(d["start"]), _utc(d["end"])
            out.append(Window(a, b, f"Intelligent Go smart-charge slot {a.astimezone(tz):%H:%M}-{b.astimezone(tz):%H:%M}",
                              dispatch=True))
        return sorted(out, key=lambda w: w.start)
    if offpeak and offpeak[0] != offpeak[1]:
        out = [Window(a, b, "Off-peak hours", "schedule") for a, b in _daily(*offpeak, tz, now)]
    return out


def decide(settings: ControlSettings, windows: list[Window], snapshot: dict, now: datetime) -> tuple[str, int, str, Window | None]:
    """(action, setpoint W, reason, window) for this moment.

    Your own schedules come first; cheap-hour windows only apply when the
    cheap-hour options are ticked."""
    if not settings.enabled:
        return APP, 0, "Control is off", None
    soc = snapshot.get("soc")
    mine = next((w for w in windows if w.schedule and w.start <= now < w.end), None)
    slot = next((w for w in windows if w.dispatch and w.start <= now < w.end), None) if settings.charge_dispatch else None
    if slot and not (mine and mine.schedule.action == CHARGE):
        # Smart-charge slots come at short notice, at any time of day, and
        # price the whole home at off-peak: top the battery up and let the
        # house run on the grid rather than spend stored energy now.
        if soc is not None and soc < settings.charge_target_soc:
            power = settings.charge_power_w
            if snapshot.get("max_charge_w"):
                power = min(power, int(snapshot["max_charge_w"]))
            return CHARGE, -power, f"Charging at {power} W to {settings.charge_target_soc}% ({slot.why})", slot
        return HOLD, 0, f"Holding the battery (target reached; {slot.why})", slot
    if mine:
        sch = mine.schedule
        if sch.action == CHARGE and soc is not None and soc < sch.target_soc:
            power = min(sch.power_w, int(snapshot["max_charge_w"])) if snapshot.get("max_charge_w") else sch.power_w
            return CHARGE, -power, f"Charging at {power} W to {sch.target_soc}% ({mine.why})", mine
        if sch.action == DISCHARGE and soc is not None and soc > sch.target_soc:
            power = min(sch.power_w, int(snapshot["max_discharge_w"])) if snapshot.get("max_discharge_w") else sch.power_w
            return DISCHARGE, power, f"Discharging at {power} W down to {sch.target_soc}% ({mine.why})", mine
        reached = {CHARGE: " (target reached)", DISCHARGE: " (floor reached)"}.get(sch.action, "")
        return HOLD, 0, f"Holding the battery{reached} ({mine.why})", mine
    if not (settings.hold_cheap or settings.grid_charge):
        windows = [w for w in windows if w.schedule or (w.dispatch and settings.charge_dispatch)]
    current = next((w for w in windows if not w.schedule and w.start <= now < w.end), None)
    if current is None:
        nxt = next((w for w in windows if w.start > now), None)
        return APP, 0, "No schedule or cheap window now: the Anker app is in charge", nxt
    if settings.grid_charge and soc is not None and soc < settings.charge_target_soc:
        power = settings.charge_power_w
        if snapshot.get("max_charge_w"):
            power = min(power, int(snapshot["max_charge_w"]))
        return CHARGE, -power, f"Charging from the grid to {settings.charge_target_soc}% ({current.why})", current
    if settings.hold_cheap or settings.grid_charge:
        return HOLD, 0, f"Holding the battery so the house uses cheap grid power ({current.why})", current
    return APP, 0, "Normal price: the Anker app is in charge", None


class Controller:
    def __init__(self, collector, load: Callable[[], dict], save_state: Callable[[dict], None],
                 windows: Callable[[datetime], list[Window]]) -> None:
        self.collector = collector
        self._load = load  # -> {"settings": {...}, "state": {...}}
        self._save_state = save_state
        self._windows = windows
        stored = load()
        self.settings = ControlSettings.from_dict(stored.get("settings"))
        state = stored.get("state") or {}
        # The mode to give back. Kept on disk so a restart mid-window still restores it.
        self.saved_mode: int | None = state.get("saved_mode")
        self.in_control = bool(state.get("in_control"))
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
        self._save_state({"saved_mode": self.saved_mode, "in_control": self.in_control})

    async def _write(self, key: str, value: int) -> None:
        if not self.live:
            self._note(f"Dry run: would write {key} = {value}")
            return
        await self.collector.write(key, value)

    async def tick(self, now: datetime | None = None) -> None:
        async with self._lock:
            await self._tick(now or datetime.now(timezone.utc))

    async def _tick(self, now: datetime) -> None:
        snap = self.collector.snapshot or {}
        if not self.collector.connected or not snap:
            self.reason = "Waiting for the battery"
            return
        action, setpoint, reason, window = decide(self.settings, self._windows(now), snap, now)
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
                        what = {HOLD: "Holding the battery at 0 W", CHARGE: f"Charging at {-setpoint} W",
                                DISCHARGE: f"Discharging at {setpoint} W"}[action]
                        self._note(what)
                        self._event(what, "battery_power_setpoint", self.last_setpoint, setpoint,
                                    window.source if window else "dashboard")
                    self.last_setpoint, self.last_write = setpoint, time.monotonic()
                self.action = action
            self.last_error = None
        except Exception as err:
            self.last_error = str(err)
            self._note(f"Write failed: {err}")

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
            self.last_error = None

    async def restore(self) -> None:
        """Give control back now, e.g. when the dashboard shuts down."""
        if self.in_control:
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
            "window": {"start": w.start.isoformat(), "end": w.end.isoformat(), "why": w.why} if w else None,
            "modes": [{"value": k, "name": v} for k, v in OPERATING_MODES.items() if k != THIRD_PARTY_MODE],
            "battery_mode": self.collector.raw.get("operating_mode") if self.collector.connected else None,
            "last_error": self.last_error,
            "log": list(self.log)[:20],
        }
