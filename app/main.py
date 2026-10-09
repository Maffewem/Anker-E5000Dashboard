"""Web server: JSON API plus the static dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from .collector import DEVICE_NAMES, Collector
from .registers import BATTERY, METER
from .config import Connection, ConnectionStore, Settings
from .storage import Storage
from .tariff import Tariff, payback

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger("solarbank.web")

STATIC = Path(__file__).parent / "static"


def _asset_version() -> str:
    """A hash of the front-end files, so each new image gets fresh URLs.

    Without it a browser can keep running a cached app.js from an older
    image against the new API.
    """
    digest = hashlib.sha256()
    for path in sorted(STATIC.rglob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


INDEX_HTML = (STATIC / "index.html").read_text().replace("{{version}}", _asset_version())


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
def history(
    hours: int = Query(24, ge=1, le=24 * 366),
    offset_hours: int = Query(0, ge=0, le=24 * 366),
) -> dict:
    return app.state.storage.history(hours, offset_hours=offset_hours)


class TariffIn(BaseModel):
    battery_cost: float = 0.0
    peak_rate: float = 28.0
    offpeak_rate: float = 28.0
    offpeak_start: str = "00:30"
    offpeak_end: str = "05:30"
    export_rate: float = 15.0


@app.get("/api/payback")
def get_payback() -> dict:
    store: ConnectionStore = app.state.connection_store
    storage: Storage = app.state.storage
    tariff = Tariff.from_dict(store.load_tariff())
    return payback(tariff, storage.battery_slots(), datetime.now(storage.tz).date())


@app.post("/api/tariff")
def save_tariff(body: TariffIn) -> dict:
    try:
        tariff = Tariff(**body.model_dump()).validate()
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    app.state.connection_store.save_tariff(asdict(tariff))
    return get_payback()


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
    _profile(device)
    conn = _validated(body)
    _check_not_other_device(device, conn)
    try:
        return {"ok": True, "device": await app.state.collectors[device].test(conn)}
    except Exception as err:
        raise HTTPException(status_code=422, detail=str(err)) from err


@app.post("/api/settings/{device}")
async def save_settings(device: str, body: ConnectionIn, skip_test: bool = False) -> dict:
    _profile(device)
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
        _check_not_other_device(device, conn)
        if not skip_test:
            try:
                found = await app.state.collectors[device].test(conn)
            except Exception as err:
                raise HTTPException(status_code=422, detail=str(err)) from err
    store.save(conn, device)
    log.info("Saved %s address %r (port %s, unit id %s)%s", device, conn.host, conn.port, conn.unit_id,
             " without a test" if skip_test else "")
    app.state.collectors[device].reconfigure(conn)
    return {"ok": True, "device": found}


def _profile(device: str):
    if device not in DEVICES:
        raise HTTPException(status_code=404, detail="Unknown device")
    return DEVICES[device]


def _check_not_other_device(device: str, conn: Connection) -> None:
    """Catch the meter's address typed into the Solarbank tab, or the reverse."""
    for other, collector in app.state.collectors.items():
        theirs = collector.connection
        if other != device and theirs.host and (theirs.host, theirs.port) == (conn.host, conn.port):
            raise HTTPException(
                status_code=422,
                detail=f"{conn.host} is already set as the {DEVICE_NAMES[other]}'s address. "
                f"Each device has its own IP in the Anker app.",
            )


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
    # Healthy when every device that has an address is connected.
    collectors = app.state.collectors
    body = {device: c.status() for device, c in collectors.items()}
    configured = [c for c in collectors.values() if c.connection.host]
    healthy = bool(configured) and all(c.connected for c in configured)
    return JSONResponse(body, status_code=200 if healthy else 503)


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
