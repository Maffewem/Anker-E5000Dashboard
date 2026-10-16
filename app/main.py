"""Web server: JSON API plus the static dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles


from .context import APP_BUILT, APP_COMMIT, APP_VERSION, state
from .routes import ROUTERS
from .services import make_octopus, octopus_prices, supplier
from .auth import Auth, LockMiddleware
from .collector import Collector
from .registers import BATTERY, METER
from .compare import Comparer
from .config import ConnectionStore, Settings
from .control import Controller, cheap_windows, schedule_windows
from .octopus import Octopus
from .relay import Relay
from .storage import Storage
from .updates import CHECK_SECONDS, RETRY_SECONDS, Updates
from .tariff import Tariff

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


OCTOPUS_SYNC_SECONDS = 30 * 60
OCTOPUS_RETRY_SECONDS = 5 * 60  # after an error
# Intelligent Go slots can be added or cancelled minutes before they start
# (a 13:20-13:40 top-up), so they're re-read far more often than prices.
OCTOPUS_DISPATCH_SECONDS = 2 * 60


def _make_controller(app: FastAPI, store: ConnectionStore, storage: Storage) -> Controller:
    def windows(now):
        octopus: Octopus = state.octopus
        battery = state.collectors["battery"]
        t = Tariff.from_dict(store.load_tariff())
        status = octopus.status(battery.snapshot, now) if octopus_prices(t) and octopus.info else None
        offpeak = (t.offpeak_start, t.offpeak_end) if t.peak_rate != t.offpeak_rate else None
        mine = schedule_windows(state.controller.settings.schedules, storage.tz, now)
        return mine + cheap_windows(status, offpeak, storage.tz, now)

    return Controller(
        state.collectors["battery"],
        load=lambda: {"settings": store.load_section("control"), "state": store.load_section("control_state")},
        save_state=lambda state: store.save_section("control_state", state),
        windows=windows,
    )


async def _octopus_loop(app: FastAPI) -> None:
    last_full = -float("inf")
    while True:
        octopus: Octopus = state.octopus
        if octopus.configured and supplier() == "octopus":
            every = OCTOPUS_RETRY_SECONDS if octopus.last_error or not octopus.info else OCTOPUS_SYNC_SECONDS
            if time.monotonic() - last_full >= every:
                await asyncio.to_thread(octopus.sync)
                last_full = time.monotonic()
            else:
                await asyncio.to_thread(octopus.refresh_dispatches)
        intelligent = (octopus.info.get("import") or {}).get("kind") == "intelligent_go"
        await asyncio.sleep(OCTOPUS_DISPATCH_SECONDS if intelligent
                            else OCTOPUS_RETRY_SECONDS if octopus.last_error else OCTOPUS_SYNC_SECONDS)


async def _update_loop(app: FastAPI) -> None:
    await asyncio.sleep(60)  # let the dashboard settle first
    while True:
        updates: Updates = state.updates
        await asyncio.to_thread(updates.check)
        await asyncio.sleep(RETRY_SECONDS if updates.error else CHECK_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Solarbank dashboard version %s (commit %s, built %s)", APP_VERSION, APP_COMMIT[:7] or "unknown",
             APP_BUILT or "unknown")
    settings = Settings.from_env()
    state.auth = auth = Auth.from_env(str(Path(settings.settings_path).parent / "auth.json"))
    if auth.read_only:
        log.info("READ_ONLY is set: the dashboard can't change anything")
    elif auth.source == "env":
        log.info("ADMIN_PASSWORD is set: changes need signing in")
    elif auth.source == "dashboard":
        log.info("A dashboard password is set: changes need signing in")
    storage = Storage(settings.db_path, settings.retention_days, settings.timezone)
    store = ConnectionStore(settings.settings_path)
    collectors = {
        "battery": Collector(settings, storage, store.load("battery"), BATTERY),
        # The Smart Meter is optional; its readings are kept for export only.
        "meter": Collector(settings, storage, store.load("meter"), METER),
    }
    state.collectors = collectors
    state.connection_store = store
    state.storage = storage
    state.pattern_cache = {}
    state.octopus = make_octopus(store, storage)
    state.comparer = Comparer()
    controller = _make_controller(app, store, storage)
    state.controller = controller
    tasks = [asyncio.create_task(c.run()) for c in collectors.values()]
    tasks.append(asyncio.create_task(_octopus_loop(app)))
    state.updates = Updates(APP_COMMIT)
    if APP_COMMIT and os.environ.get("UPDATE_CHECK", "true").strip().lower() not in ("0", "false", "no", "off"):
        tasks.append(asyncio.create_task(_update_loop(app)))
    relay = None
    if settings.relay_meter:
        relay = Relay(collectors["meter"], settings.relay_port)
        try:
            await relay.start()
        except OSError as err:
            log.error("Can't start the Smart Meter relay on port %s: %s", settings.relay_port, err)
            relay = None
    state.relay = relay
    control_task = asyncio.create_task(controller.run())
    try:
        yield
    finally:
        control_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await control_task
        with contextlib.suppress(Exception):
            await asyncio.wait_for(controller.restore(), 20)  # hand the battery back before stopping
        if relay is not None:
            await relay.close()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for c in collectors.values():
            c.close()
        storage.close()


app = FastAPI(title="Solarbank Dashboard", lifespan=lifespan)
app.state = state  # the routes in app/routes read the same object
app.add_middleware(LockMiddleware)
for _router in ROUTERS:
    app.include_router(_router)


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
