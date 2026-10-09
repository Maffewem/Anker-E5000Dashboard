"""Spots changes between polls and writes them to the event log.

Settings and the operating mode are logged as soon as they change. The
charging state is logged once it has held for STATUS_HOLD seconds, so a
battery hovering around zero doesn't fill the log; the event keeps the time
the new state began. The last value of everything watched is saved, so a
change made while the dashboard was stopped is logged when it starts again.
"""

from __future__ import annotations

from typing import Any

from .storage import Storage

STATUS_HOLD = 60

# field: (event kind, label, unit)
WATCHED: dict[str, dict[str, tuple[str, str, str]]] = {
    "battery": {
        "operating_mode": ("mode", "Mode", ""),
        "charging_limit_soc": ("setting", "Charge limit", "%"),
        "discharge_limit_soc": ("setting", "Discharge limit", "%"),
        "backup_reserve_soc": ("setting", "Backup reserve", "%"),
        "firmware": ("firmware", "Firmware", ""),
    },
    "meter": {
        "firmware": ("firmware", "Firmware", ""),
    },
}


def fmt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def status_message(old: str | None, new: str, soc: Any) -> str:
    at = f" at {fmt(soc)}%" if soc is not None else ""
    if new == "charging":
        return f"Started charging{at}"
    if new == "discharging":
        return f"Started discharging{at}"
    if old in ("charging", "discharging"):
        stopped = f"Stopped {old}{at}"
        return stopped if new == "standby" else f"{stopped}; now {new}"
    return f"Now {new}{at}"


class EventWatcher:
    def __init__(self, storage: Storage, device: str) -> None:
        self.storage = storage
        self.device = device
        self.fields = WATCHED.get(device, {})
        self.state = storage.event_state(device)
        # A new charging state not yet held long enough: (state, since, soc then).
        self._pending: tuple[str, float, Any] | None = None

    def _changed(self, field: str, value: str) -> bool:
        """Remember `value`; True if it differs from a value seen before."""
        last = self.state.get(field)
        if last == value:
            return False
        self.state[field] = value
        self.storage.save_event_state(self.device, field, value)
        return last is not None  # the first value ever seen is a baseline, not a change

    def observe(self, snapshot: dict[str, Any], now: float) -> None:
        for field, (kind, label, unit) in self.fields.items():
            value = fmt(snapshot.get(field))
            if value is None:
                continue  # not read this time; don't log a change to "nothing"
            old = self.state.get(field)
            if self._changed(field, value):
                self.storage.record_event(
                    kind, f"{label} changed from {old}{unit} to {value}{unit}",
                    device=self.device, field=field, old=old, new=value, ts=now,
                )
        if self.device == "battery":
            self._observe_status(snapshot, now)

    def _observe_status(self, snapshot: dict[str, Any], now: float) -> None:
        status = snapshot.get("battery_status")
        if status is None:
            return
        current = self.state.get("battery_status")
        if status == current:
            self._pending = None
            return
        if self._pending is None or self._pending[0] != status:
            self._pending = (status, now, snapshot.get("soc"))
            if current is not None:
                return
        _, since, soc = self._pending
        if current is not None and now - since < STATUS_HOLD:
            return
        self._pending = None
        if self._changed("battery_status", status):
            self.storage.record_event(
                "charging", status_message(current, status, soc),
                device=self.device, field="battery_status", old=current, new=status, ts=since,
            )
