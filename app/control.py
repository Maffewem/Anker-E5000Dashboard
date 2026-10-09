"""Battery control: hold the battery in cheap hours, optionally charge it
from the grid in the cheapest ones, and hand control back to the Anker app
the rest of the time.

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


def live_writes_allowed() -> bool:
    return os.environ.get("CONTROL_LIVE", "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ControlSettings:
    enabled: bool = False
    hold_cheap: bool = True  # don't discharge while electricity is cheap
    grid_charge: bool = False  # also charge from the grid in the cheapest hours
    charge_power_w: int = 1500
    charge_target_soc: int = 90  # stop grid charging here; easier on the cells than 100%

    @classmethod
    def from_dict(cls, data: dict | None) -> "ControlSettings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    def validate(self) -> "ControlSettings":
        if not 100 <= int(self.charge_power_w) <= 5000:
            raise ValueError("Charge power must be between 100 and 5000 W")
        if not 20 <= int(self.charge_target_soc) <= 100:
            raise ValueError("Stop charging at must be between 20% and 100%")
        return ControlSettings(bool(self.enabled), bool(self.hold_cheap), bool(self.grid_charge),
                               int(self.charge_power_w), int(self.charge_target_soc))


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    why: str
    source: str = "octopus"  # for the event log: "octopus" or "schedule" (typed off-peak hours)


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
            out.append(Window(_utc(d["start"]), _utc(d["end"]), "Intelligent Go smart-charge slot"))
        return sorted(out, key=lambda w: w.start)
    if offpeak and offpeak[0] != offpeak[1]:
        local = now.astimezone(tz)
        for day in (-1, 0, 1):
            base = (local + timedelta(days=day)).date()
            h1, m1 = map(int, offpeak[0].split(":"))
            h2, m2 = map(int, offpeak[1].split(":"))
            start = datetime(base.year, base.month, base.day, h1, m1, tzinfo=tz)
            end = datetime(base.year, base.month, base.day, h2, m2, tzinfo=tz)
            if end <= start:
                end += timedelta(days=1)
            out.append(Window(start.astimezone(timezone.utc), end.astimezone(timezone.utc), "Off-peak hours", "schedule"))
    return out


def decide(settings: ControlSettings, windows: list[Window], snapshot: dict, now: datetime) -> tuple[str, int, str, Window | None]:
    """(action, setpoint W, reason, window) for this moment."""
    if not settings.enabled:
        return APP, 0, "Control is off", None
    current = next((w for w in windows if w.start <= now < w.end), None)
    if current is None:
        nxt = next((w for w in windows if w.start > now), None)
        return APP, 0, "Normal price: the Anker app is in charge", nxt
    soc = snapshot.get("soc")
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
        self.settings = settings
        self._note(f"Settings changed: control {'on' if settings.enabled else 'off'}")
        self._wake.set()

    def _note(self, text: str) -> None:
        self.log.appendleft({"ts": time.time(), "text": text})
        log.info("Control: %s", text)

    def _event(self, message: str, field: str, old, new, source: str) -> None:
        """Add a line to the dashboard's battery event log, when it has one."""
        storage = getattr(self.collector, "storage", None)
        if not self.live or storage is None or not hasattr(storage, "record_event"):
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
                    self._event(f"Took control for a cheap window: {window.why if window else reason}", "operating_mode",
                                OPERATING_MODES.get(self.saved_mode, self.saved_mode), OPERATING_MODES[THIRD_PARTY_MODE],
                                window.source if window else "dashboard")
                due = time.monotonic() - self.last_write > REASSERT_SECONDS
                if setpoint != self.last_setpoint or due:
                    await self._write("battery_power_setpoint", setpoint)
                    if setpoint != self.last_setpoint:
                        self._note("Holding at 0 W" if action == HOLD else f"Charging at {-setpoint} W")
                        self._event("Holding the battery (no discharge)" if action == HOLD
                                    else f"Charging from the grid at {-setpoint} W", "battery_power_setpoint",
                                    self.last_setpoint, setpoint, window.source if window else "dashboard")
                    self.last_setpoint, self.last_write = setpoint, time.monotonic()
                self.action = action
            self.last_error = None
        except Exception as err:
            self.last_error = str(err)
            self._note(f"Write failed: {err}")

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
            "last_error": self.last_error,
            "log": list(self.log)[:20],
        }
