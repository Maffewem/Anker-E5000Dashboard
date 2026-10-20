"""Web routes: trends and projections."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Query

from ..context import state
from ..storage import Storage
from ..trends import conditions, daily_totals, health, insights, temperature_effect, month_projection, payback_outlook, periods, recent_average, savings_curve
from .tariff import _pricing, get_payback

router = APIRouter()


@router.get("/api/trends")
def trends(period: str = Query("week", pattern="^(day|week|month)$"), count: int = Query(12, ge=2, le=400)) -> dict:
    """Weekly or monthly totals, this month's projection, the payback outlook and battery health."""
    storage: Storage = state.storage
    today = datetime.now(storage.tz).date()
    # Priced like the payback, which also says what the battery saved before recording began.
    _, tariff, _, prices = _pricing()
    pb = get_payback()
    days = daily_totals(tariff, storage.energy_slots(), prices)
    curve = savings_curve(days, pb.get("saved_before_recording"))
    rated = state.collectors["battery"].snapshot.get("rated_kwh")
    capacity = storage.capacity_days()
    first = today - timedelta(days=89)
    if days:
        first = max(first, datetime.fromisoformat(min(days)).date())
    rows = conditions(days, storage.day_summaries(include_today=True), storage.weather_days(first), capacity, first, today)
    return {
        "insights": insights(days, today),
        "conditions": rows,
        "temperature_effect": temperature_effect(rows),
        "has_weather": any(r["temp_avg"] is not None for r in rows),
        "period": period,
        "first_day": min(days) if days else None,
        "periods": periods(days, period, today, count, recent_average(days, today)[0]),
        "month": month_projection(days, today),
        "payback": {**payback_outlook(curve, tariff.battery_cost, days, today),
                    "curve": curve, "lifetime_per_day": pb["per_day"], "lifetime_date": pb["payback_date"],
                    "lifetime_days": pb["days"]},
        "health": health(capacity, period, today, count, rated, days),
    }
