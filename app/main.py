"""Web server: JSON API plus the static dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from .collector import Collector, probe
from .registers import BATTERY, METER
from .config import Connection, ConnectionStore, Settings
from .storage import Storage

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

STATIC = Path(__file__).parent / "static"


DEVICES = {"battery": BATTERY, "meter": METER}
ENV_NAMES = {"battery": "SOLARBANK_HOST", "meter": "METER_HOST"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    storage = Storage(settings.db_path, settings.retention_days, settings.timezone)
    store = ConnectionStore(settings.settings_path)
    collectors = {
        "battery": Collector(settings, storage, store.load("battery"), BATTERY),
        # The Smart Meter is optional and shown live only; history comes from the battery.
        "meter": Collector(settings, None, store.load("meter"), METER),
    }
    app.state.collectors = collectors
    app.state.collector = collectors["battery"]
    app.state.connection_store = store
    app.state.storage = storage
    tasks = [asyncio.create_task(c.run()) for c in collectors.values()]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for c in collectors.values():
            c.close()
        storage.close()


app = FastAPI(title="Solarbank Dashboard", lifespan=lifespan)


@app.get("/api/live")
def live() -> dict:
    battery: Collector = app.state.collectors["battery"]
    meter: Collector = app.state.collectors["meter"]
    return {
        "status": battery.status(),
        "data": battery.snapshot,
        "meter": {"status": meter.status(), "data": meter.snapshot},
    }


@app.get("/api/history")
def history(hours: int = Query(24, ge=1, le=24 * 366)) -> dict:
    return app.state.storage.history(hours)


@app.get("/api/energy")
def energy(days: int = Query(14, ge=1, le=366)) -> list[dict]:
    return app.state.storage.daily_energy(days)


class ConnectionIn(BaseModel):
    host: str
    port: int = 502
    unit_id: int = 1


@app.get("/api/settings")
def get_settings() -> dict:
    store: ConnectionStore = app.state.connection_store
    out = {}
    for device, collector in app.state.collectors.items():
        conn: Connection = collector.connection
        out[device] = {"host": conn.host, "port": conn.port, "unit_id": conn.unit_id, "locked": store.from_env(device)}
    return out


@app.post("/api/settings/{device}/test")
async def test_settings(device: str, body: ConnectionIn) -> dict:
    profile = _profile(device)
    conn = _validated(body)
    try:
        return {"ok": True, "device": await probe(conn, profile)}
    except Exception as err:
        raise HTTPException(status_code=422, detail=str(err)) from err


@app.post("/api/settings/{device}")
async def save_settings(device: str, body: ConnectionIn, skip_test: bool = False) -> dict:
    profile = _profile(device)
    store: ConnectionStore = app.state.connection_store
    if store.from_env(device):
        raise HTTPException(
            status_code=409,
            detail=f"This address is set by {ENV_NAMES[device]} in the container settings. Remove it there to edit it here.",
        )
    found = None
    if device == "meter" and not body.host.strip():
        conn = Connection()  # removing the optional meter
    else:
        conn = _validated(body)
        if not skip_test:
            try:
                found = await probe(conn, profile)
            except Exception as err:
                raise HTTPException(status_code=422, detail=str(err)) from err
    store.save(conn, device)
    app.state.collectors[device].reconfigure(conn)
    return {"ok": True, "device": found}


def _profile(device: str):
    if device not in DEVICES:
        raise HTTPException(status_code=404, detail="Unknown device")
    return DEVICES[device]


def _validated(body: ConnectionIn) -> Connection:
    try:
        return Connection(body.host, body.port, body.unit_id).validate()
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err


@app.get("/api/raw")
def raw() -> dict:
    """Every decoded register, for troubleshooting."""
    return {device: c.raw for device, c in app.state.collectors.items()}


@app.get("/healthz")
def healthz():
    c: Collector = app.state.collector
    body = c.status()
    return JSONResponse(body, status_code=200 if c.connected else 503)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
