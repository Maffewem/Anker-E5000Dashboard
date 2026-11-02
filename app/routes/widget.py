"""Web routes: a small read-only summary for a phone widget.

Off unless WIDGET_TOKEN (at least 16 characters) is set. The token goes in an
Authorization: Bearer header (or X-Widget-Token), never in the URL, so it stays
out of access logs. It unlocks only this one read-only endpoint, so it is safe
to expose on its own through a tunnel while the rest of the dashboard stays private.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from ..context import state
from ..tariff import daily_costs
from .tariff import _pricing

log = logging.getLogger("solarbank.web")
router = APIRouter()
MIN_TOKEN = 16


def _token() -> str:
    return os.environ.get("WIDGET_TOKEN", "").strip()


def _presented(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-widget-token", "").strip()


def require_token(request: Request) -> None:
    """404 while WIDGET_TOKEN is off or too short, 401 for a wrong token. Shared with the firmware download."""
    token = _token()
    if len(token) < MIN_TOKEN:
        raise HTTPException(status_code=404, detail="Not found")  # off, and looks like it isn't there
    if not hmac.compare_digest(_presented(request).encode(), token.encode()):
        raise HTTPException(status_code=401, detail="Wrong token")


def self_sufficiency(slots: list[dict], today: str) -> float | None:
    """Share of today's home use that did not come from the grid, as a percentage; None until there is data."""
    home = sum(r.get("home_wh") or 0.0 for r in slots if r.get("day") == today)
    grid = sum(r.get("import_wh") or 0.0 for r in slots if r.get("day") == today)
    if home < 50:  # under 50 Wh: too early in the day to mean anything
        return None
    return round(max(0.0, min(100.0, (1 - grid / home) * 100)), 1)


def rate_now(rates: list[float], now: datetime) -> dict:
    """The price in force now, whether that is the cheap or the peak part of the day, and when it ends.
    A flat tariff (one price all day) gives no kind and no end."""
    if len(rates) != 48:
        return {}
    idx = now.hour * 2 + (1 if now.minute >= 30 else 0)
    cur, lo, hi = rates[idx], min(rates), max(rates)
    out: dict = {"rate_p": round(cur, 2)}
    if hi - lo < 0.01:
        return out
    out["rate_kind"] = "cheap" if cur - lo < 0.01 else "peak" if hi - cur < 0.01 else "mid"
    for step in range(1, 49):
        nxt = (idx + step) % 48
        if abs(rates[nxt] - cur) >= 0.01:
            out["rate_until"] = f"{nxt // 2:02d}:{30 * (nxt % 2):02d}"
            break
    return out


@router.get("/api/widget")
def widget(request: Request) -> dict:
    """Battery, solar, home and grid power plus today's saving, for a home-screen widget."""
    require_token(request)
    battery = state.collectors["battery"]
    snap = battery.snapshot or {}
    out = {k: snap.get(k) for k in ("soc", "solar_w", "home_w", "battery_w", "grid_w")}
    meter = state.collectors["meter"]
    if meter.connected and meter.snapshot.get("grid_w") is not None:
        out["grid_w"] = meter.snapshot["grid_w"]
    out["battery_status"] = snap.get("battery_status")
    out["self_sufficiency"] = None
    out["online"] = bool(battery.connected)
    out["updated"] = battery.last_update
    out["age_s"] = round(time.time() - battery.last_update) if battery.last_update else None
    try:
        _, tariff, _, prices = _pricing()
    except Exception as err:  # the power figures are still worth showing
        log.warning("Widget couldn't load the tariff: %s", err)
        tariff = prices = None
    now = datetime.now(state.storage.tz)
    out["saved_today"] = None
    if tariff is not None:
        try:
            out["saved_today"] = daily_costs(tariff, state.storage.battery_slots(), now.date(), 1, prices)["today"]["saved"]
        except Exception as err:
            log.warning("Widget couldn't work out today's saving: %s", err)
        try:
            out.update(rate_now(tariff.slot_rates(), now))
        except Exception as err:
            log.warning("Widget couldn't work out the current rate: %s", err)
    try:
        out["self_sufficiency"] = self_sufficiency(state.storage.energy_slots(), now.date().isoformat())
    except Exception as err:
        log.warning("Widget couldn't work out self-sufficiency: %s", err)
    return out
