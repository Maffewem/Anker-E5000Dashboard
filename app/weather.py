"""Weather history and forecast from Open-Meteo (free, no account), for the
location typed into the dashboard.

Only a latitude and longitude are sent, rounded to two decimals (about a
kilometre). The hourly temperature, cloud cover and sunshine are kept for
good, so solar output can be set against the sunshine it had, and battery
capacity against temperature.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("solarbank.weather")

URL = "https://api.open-meteo.com/v1/forecast"
SYNC_SECONDS = 3 * 3600
Fetch = Callable[[str], dict]


def _fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 - fixed https host
        return json.load(resp)


def parse_location(lat: object, lon: object) -> tuple[float, float]:
    try:
        lat, lon = round(float(lat), 2), round(float(lon), 2)
    except (TypeError, ValueError):
        raise ValueError("Enter the latitude and longitude as numbers, e.g. 51.50 and -0.12") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Latitude must be between -90 and 90 and longitude between -180 and 180")
    return lat, lon


def fetch_hours(lat: float, lon: float, fetch: Fetch = _fetch, past_days: int = 3) -> list[tuple]:
    """(hour start unix seconds, temp °C, cloud %, sunshine W/m²) for the last few days and the forecast."""
    query = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": "UTC", "past_days": past_days, "forecast_days": 3,
        "hourly": "temperature_2m,cloud_cover,shortwave_radiation",
    })
    hourly = fetch(f"{URL}?{query}").get("hourly") or {}
    out = []
    for i, t in enumerate(hourly.get("time") or []):
        ts = int(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp())
        pick = lambda k: (hourly.get(k) or [None] * (i + 1))[i]  # noqa: E731
        out.append((ts, pick("temperature_2m"), pick("cloud_cover"), pick("shortwave_radiation")))
    return out


def sync(storage, lat: float, lon: float, fetch: Fetch = _fetch, first_run: bool = False) -> int:
    """Store the latest hours; the first sync goes back a month. Blocking."""
    rows = fetch_hours(lat, lon, fetch, past_days=31 if first_run else 3)
    storage.save_weather(rows)
    return len(rows)
