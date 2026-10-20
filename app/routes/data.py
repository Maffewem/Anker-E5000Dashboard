"""Web routes: data."""

from __future__ import annotations

import logging
from ..context import state

import csv
import io
import os
import tempfile
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, JSONResponse, Response


from ..battery_care import care
from ..control import planned_charges
from ..runtime import PATTERN_DAYS, estimate, pattern_from_minutes
from ..storage import EVENT_KINDS, Storage

log = logging.getLogger("solarbank.web")
router = APIRouter()


@router.get("/api/runtime")
def runtime() -> dict:
    """When the battery is expected to run empty or be full, from its usage pattern."""
    storage: Storage = state.storage
    now = time.time()
    cache = state.pattern_cache
    if now - cache.get("at", 0) > 600:  # the pattern moves slowly; rebuild every 10 minutes
        rows = storage.battery_power_since(int(now) - PATTERN_DAYS * 86400)
        cache.update(zip(("pattern", "hours"), pattern_from_minutes(rows, storage.tz)), at=now)
    snap = state.collectors["battery"].snapshot
    local = datetime.now(storage.tz)
    charges = planned_charges(state.controller.settings, state.controller._windows(local.astimezone(timezone.utc)), local)
    return estimate(snap, cache["pattern"], cache["hours"], local, charges)


@router.get("/api/history")
def history(
    hours: int = Query(24, ge=1, le=24 * 366),
    offset_hours: int = Query(0, ge=0, le=24 * 366),
) -> dict:
    return state.storage.history(hours, offset_hours=offset_hours)


@router.get("/api/battery-care")
def battery_care() -> dict:
    snapshot = state.collectors["battery"].snapshot
    return care(snapshot, state.storage.soc_stats(), asdict(state.controller.settings))


@router.get("/api/energy")
def energy(days: int = Query(14, ge=1, le=366)) -> list[dict]:
    return state.storage.daily_energy(days)


@router.get("/api/events")
def events(
    limit: int = Query(100, ge=1, le=1000),
    before: int | None = Query(None, description="an event id; returns older events"),
    kind: str = Query("", description="comma-separated kinds, e.g. charging,mode"),
    offset: int = Query(0, ge=0, description="skip this many of the newest; for numbered pages"),
) -> JSONResponse:
    """The event log, newest first. X-Total-Count says how many match the filter."""
    kinds = [k for k in kind.split(",") if k]
    unknown = set(kinds) - set(EVENT_KINDS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown kind: {', '.join(sorted(unknown))}")
    storage = state.storage
    return JSONResponse(storage.events(limit, before, kinds, offset),
                        headers={"X-Total-Count": str(storage.count_events(kinds))})


@router.get("/api/export")
def export(
    data: str = Query("minutes", pattern="^(minutes|daily|meter|slots|events|days|weather|samples)$"),
    start: date | None = None,
    end: date | None = None,
    format: str = Query("csv", pattern="^(csv|json)$"),
):
    """Recorded data for local dates `start` to `end` (default: the last 7 days).

    minutes: Solarbank power and energy per minute. daily: daily kWh totals.
    meter: Smart Meter readings per minute. slots: battery energy per half hour.
    events: the event log.
    """
    storage: Storage = state.storage
    today = datetime.now(storage.tz).date()
    end = end or today
    start = start or end - timedelta(days=6)
    if start > end:
        raise HTTPException(status_code=422, detail="The start date is after the end date")
    if (end - start).days > 3660:
        raise HTTPException(status_code=422, detail="Choose a range of ten years or less")
    columns, rows = storage.export_rows(data, start, end)
    name = f"solarbank-{data}-{start}-to-{end}.{format}"
    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    if format == "json":
        return JSONResponse(rows, headers=headers)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return Response(out.getvalue(), media_type="text/csv", headers=headers)


def _admin(request: Request) -> None:
    state.auth.require_admin(request)


@router.get("/api/export/backup", dependencies=[Depends(_admin)])
def export_backup() -> FileResponse:
    """The whole database as a SQLite file, everything recorded so far."""
    storage: Storage = state.storage
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    storage.backup(path)
    name = f"solarbank-backup-{datetime.now(storage.tz).date()}.db"
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=name,
                        background=BackgroundTask(os.remove, path))
