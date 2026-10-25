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


@router.get("/api/widget")
def widget(request: Request) -> dict:
    """Battery, solar, home and grid power plus today's saving, for a home-screen widget."""
    token = _token()
    if len(token) < MIN_TOKEN:
        raise HTTPException(status_code=404, detail="Not found")  # off, and looks like it isn't there
    if not hmac.compare_digest(_presented(request).encode(), token.encode()):
        raise HTTPException(status_code=401, detail="Wrong token")
    battery = state.collectors["battery"]
    snap = battery.snapshot or {}
    out = {k: snap.get(k) for k in ("soc", "solar_w", "home_w", "battery_w", "grid_w")}
    meter = state.collectors["meter"]
    if meter.connected and meter.snapshot.get("grid_w") is not None:
        out["grid_w"] = meter.snapshot["grid_w"]
    out["battery_status"] = snap.get("battery_status")
    out["online"] = bool(battery.connected)
    out["updated"] = battery.last_update
    out["age_s"] = round(time.time() - battery.last_update) if battery.last_update else None
    try:
        _, tariff, _, prices = _pricing()
        today = datetime.now(state.storage.tz).date()
        out["saved_today"] = daily_costs(tariff, state.storage.battery_slots(), today, 1, prices)["today"]["saved"]
    except Exception as err:  # the power figures are still worth showing
        log.warning("Widget couldn't work out today's saving: %s", err)
        out["saved_today"] = None
    return out
