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
from .config import Connection, ConnectionStore, Settings
from .storage import Storage

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    storage = Storage(settings.db_path, settings.retention_days, settings.timezone)
    store = ConnectionStore(settings.settings_path)
    collector = Collector(settings, storage, store.load())
    app.state.collector = collector
    app.state.connection_store = store
    app.state.storage = storage
    task = asyncio.create_task(collector.run())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        collector.close()
        storage.close()


app = FastAPI(title="Solarbank Dashboard", lifespan=lifespan)


@app.get("/api/live")
def live() -> dict:
    c: Collector = app.state.collector
    return {"status": c.status(), "data": c.snapshot}


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
    conn: Connection = app.state.collector.connection
    return {"host": conn.host, "port": conn.port, "unit_id": conn.unit_id, "locked": store.from_env}


@app.post("/api/settings/test")
async def test_settings(body: ConnectionIn) -> dict:
    conn = _validated(body)
    try:
        return {"ok": True, "device": await probe(conn)}
    except Exception as err:
        raise HTTPException(status_code=422, detail=str(err)) from err


@app.post("/api/settings")
async def save_settings(body: ConnectionIn, skip_test: bool = False) -> dict:
    store: ConnectionStore = app.state.connection_store
    if store.from_env:
        raise HTTPException(
            status_code=409,
            detail="The address is set by SOLARBANK_HOST in the container settings. Remove it there to edit it here.",
        )
    conn = _validated(body)
    device = None
    if not skip_test:
        try:
            device = await probe(conn)
        except Exception as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
    store.save(conn)
    app.state.collector.reconfigure(conn)
    return {"ok": True, "device": device}


def _validated(body: ConnectionIn) -> Connection:
    try:
        return Connection(body.host, body.port, body.unit_id).validate()
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err


@app.get("/api/raw")
def raw() -> dict:
    """Every decoded register, for troubleshooting."""
    return app.state.collector.raw


@app.get("/healthz")
def healthz():
    c: Collector = app.state.collector
    body = c.status()
    return JSONResponse(body, status_code=200 if c.connected else 503)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
