"""Web server: JSON API plus the static dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .collector import Collector
from .config import Settings
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
    collector = Collector(settings, storage)
    app.state.collector = collector
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
