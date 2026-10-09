"""Web server: JSON API plus the static dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import io
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from .auth import COOKIE, SESSION_SECONDS, Auth, LockMiddleware
from .collector import DEVICE_NAMES, Collector
from .registers import BATTERY, METER
from .battery_care import care
from .compare import PRESETS, Candidate, Comparer, compare, profile as price_profile
from .config import ENV_PREFIX, Connection, ConnectionStore, Settings
from .control import ControlSettings, Controller, Schedule, cheap_windows, schedule_windows
from .octopus import ECONOMY7_NIGHT, Octopus, tariff_parts, validate as validate_octopus
from .relay import Relay
from .runtime import PATTERN_DAYS, battery_size, estimate, pattern_from_minutes
from .storage import EVENT_KINDS, Storage
from .tariff import Tariff, fixed_profile, payback

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


# Set by the image build: a readable version (1.0.57), the commit and the build date.
APP_VERSION = os.environ.get("APP_VERSION", "").strip()[:32] or "dev"
APP_COMMIT = os.environ.get("APP_COMMIT", "").strip()[:40]
APP_BUILT = os.environ.get("APP_BUILT", "").strip()[:10]
INDEX_HTML = (STATIC / "index.html").read_text().replace("{{version}}", _asset_version())


DEVICES = {"battery": BATTERY, "meter": METER}
OCTOPUS_SYNC_SECONDS = 30 * 60
OCTOPUS_DISPATCH_SECONDS = 5 * 60  # Intelligent Go slots change through the evening


def _make_octopus(store: ConnectionStore, storage: Storage, api_key: str | None = None,
                  account: str | None = None) -> Octopus:
    config_error = None
    if api_key is None:
        api_key, account = store.load_octopus()
        if api_key and store.octopus_from_env():
            try:
                api_key, account = validate_octopus(api_key.strip("'\" "), (account or "").strip("'\" "))
            except ValueError as err:
                config_error = f"Check OCTOPUS_API_KEY and OCTOPUS_ACCOUNT in the container settings. {err}"
                log.warning("Octopus: %s", config_error)
                api_key = ""

    def offpeak() -> set[int]:
        rates = Tariff.from_dict(store.load_tariff()).slot_rates()
        return {i for i, r in enumerate(rates) if r == min(rates)} if len(set(rates)) > 1 else set(ECONOMY7_NIGHT)

    octopus = Octopus(storage, api_key or "", account or "", offpeak=offpeak)
    octopus.config_error = config_error
    return octopus


def _make_controller(app: FastAPI, store: ConnectionStore, storage: Storage) -> Controller:
    def windows(now):
        octopus: Octopus = app.state.octopus
        battery = app.state.collectors["battery"]
        t = Tariff.from_dict(store.load_tariff())
        status = (octopus.status(battery.snapshot, now)
                  if octopus.configured and octopus.info and not t.use_manual else None)
        offpeak = (t.offpeak_start, t.offpeak_end) if t.peak_rate != t.offpeak_rate else None
        mine = schedule_windows(app.state.controller.settings.schedules, storage.tz, now)
        return mine + cheap_windows(status, offpeak, storage.tz, now)

    return Controller(
        app.state.collectors["battery"],
        load=lambda: {"settings": store.load_section("control"), "state": store.load_section("control_state")},
        save_state=lambda state: store.save_section("control_state", state),
        windows=windows,
    )


async def _octopus_loop(app: FastAPI) -> None:
    while True:
        octopus: Octopus = app.state.octopus
        if octopus.configured:
            await asyncio.to_thread(octopus.sync)
        intelligent = (octopus.info.get("import") or {}).get("kind") == "intelligent_go"
        await asyncio.sleep(OCTOPUS_DISPATCH_SECONDS if intelligent or octopus.last_error else OCTOPUS_SYNC_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Solarbank dashboard version %s (commit %s, built %s)", APP_VERSION, APP_COMMIT[:7] or "unknown",
             APP_BUILT or "unknown")
    app.state.auth = auth = Auth.from_env()
    if auth.read_only:
        log.info("READ_ONLY is set: the dashboard can't change anything")
    elif auth.password_set:
        log.info("ADMIN_PASSWORD is set: changes need signing in")
    settings = Settings.from_env()
    storage = Storage(settings.db_path, settings.retention_days, settings.timezone)
    store = ConnectionStore(settings.settings_path)
    collectors = {
        "battery": Collector(settings, storage, store.load("battery"), BATTERY),
        # The Smart Meter is optional; its readings are kept for export only.
        "meter": Collector(settings, storage, store.load("meter"), METER),
    }
    app.state.collectors = collectors
    app.state.connection_store = store
    app.state.storage = storage
    app.state.pattern_cache = {}
    app.state.octopus = _make_octopus(store, storage)
    app.state.comparer = Comparer()
    controller = _make_controller(app, store, storage)
    app.state.controller = controller
    tasks = [asyncio.create_task(c.run()) for c in collectors.values()]
    tasks.append(asyncio.create_task(_octopus_loop(app)))
    relay = None
    if settings.relay_meter:
        relay = Relay(collectors["meter"], settings.relay_port)
        try:
            await relay.start()
        except OSError as err:
            log.error("Can't start the Smart Meter relay on port %s: %s", settings.relay_port, err)
            relay = None
    app.state.relay = relay
    control_task = asyncio.create_task(controller.run())
    try:
        yield
    finally:
        control_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await control_task
        with contextlib.suppress(Exception):
            await asyncio.wait_for(controller.restore(), 10)  # hand the battery back before stopping
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
app.add_middleware(LockMiddleware)


class LoginIn(BaseModel):
    password: str = ""


@app.get("/api/auth")
def auth_status(request: Request) -> dict:
    return app.state.auth.status(request)


@app.post("/api/auth/login")
async def login(body: LoginIn, request: Request) -> JSONResponse:
    auth: Auth = app.state.auth
    if not auth.password_set:
        raise HTTPException(status_code=409, detail="No ADMIN_PASSWORD is set, so there's nothing to sign in to.")
    client = request.client.host if request.client else "unknown"
    if not auth.check_password(client, body.password[:1024]):
        log.warning("Wrong dashboard password from %s", client)
        await asyncio.sleep(1)  # slows guessing
        raise HTTPException(status_code=401, detail="Wrong password")
    token = auth.new_session()
    out = {**auth.status(request), "signed_in": True, "can_edit": not auth.read_only, "csrf": auth.csrf_for(token)}
    response = JSONResponse(out)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").startswith("https")
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", secure=secure)
    log.info("Signed in from %s", client)
    return response


@app.post("/api/auth/logout")
def logout(request: Request) -> JSONResponse:
    response = JSONResponse({**app.state.auth.status(request), "signed_in": False, "csrf": None,
                             "can_edit": not app.state.auth.read_only and not app.state.auth.password_set})
    response.delete_cookie(COOKIE, httponly=True, samesite="strict")
    return response


@app.get("/api/live")
def live() -> dict:
    battery: Collector = app.state.collectors["battery"]
    meter: Collector = app.state.collectors["meter"]
    return {
        "version": {"name": APP_VERSION, "commit": APP_COMMIT, "built": APP_BUILT},
        "status": battery.status(),
        "data": battery.snapshot,
        "meter": {
            "status": meter.status(),
            "data": meter.snapshot,
            "relay_port": app.state.relay.port if app.state.relay else None,
        },
    }


@app.get("/api/runtime")
def runtime() -> dict:
    """When the battery is expected to run empty or be full, from its usage pattern."""
    storage: Storage = app.state.storage
    now = time.time()
    cache = app.state.pattern_cache
    if now - cache.get("at", 0) > 600:  # the pattern moves slowly; rebuild every 10 minutes
        rows = storage.battery_power_since(int(now) - PATTERN_DAYS * 86400)
        cache.update(zip(("pattern", "hours"), pattern_from_minutes(rows, storage.tz)), at=now)
    snap = app.state.collectors["battery"].snapshot
    return estimate(snap, cache["pattern"], cache["hours"], datetime.now(storage.tz))


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
    use_manual: bool = False
    installed: str = ""


@app.get("/api/payback")
def get_payback() -> dict:
    store: ConnectionStore = app.state.connection_store
    storage: Storage = app.state.storage
    octopus: Octopus = app.state.octopus
    typed = Tariff.from_dict(store.load_tariff())
    tariff = typed
    use_octopus = octopus.configured and not typed.use_manual
    profile = octopus.profile() if use_octopus else None
    if profile:
        # Octopus prices stand in for the typed ones; only the battery cost is kept.
        tariff = Tariff.from_dict({**{k: v for k, v in profile.items() if v is not None},
                                   "battery_cost": typed.battery_cost, "installed": typed.installed})
    prices = storage.rates() if use_octopus else None
    out = payback(tariff, storage.battery_slots(), datetime.now(storage.tz).date(), prices, _lifetime())
    out["source"] = "octopus" if profile else "manual"
    out["octopus_connected"] = octopus.configured
    out["use_manual"] = typed.use_manual
    out["manual"] = asdict(typed)  # what was typed, even while Octopus prices are shown
    out["tariff_name"] = (octopus.info.get("import") or {}).get("name") if profile else None
    return out


def _lifetime() -> dict | None:
    """The battery's lifetime charge/discharge totals, remembered while it's offline."""
    store: ConnectionStore = app.state.connection_store
    snap = app.state.collectors["battery"].snapshot
    now = {"charged_kwh": snap.get("charged_total_kwh"), "discharged_kwh": snap.get("discharged_total_kwh")}
    if None in now.values():
        return store.load_lifetime()
    if now != store.load_lifetime():
        store.save_lifetime(now)
    return now


@app.post("/api/tariff")
def save_tariff(body: TariffIn) -> dict:
    try:
        tariff = Tariff(**body.model_dump()).validate()
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    app.state.connection_store.save_tariff(asdict(tariff))
    return get_payback()


class OctopusIn(BaseModel):
    api_key: str = ""
    account: str = ""


@app.get("/api/octopus")
def get_octopus(request: Request) -> dict:
    octopus: Octopus = app.state.octopus
    out = octopus.status(app.state.collectors["battery"].snapshot)
    out["locked"] = app.state.connection_store.octopus_from_env()
    if out.get("account") and not app.state.auth.can_edit(request):
        out["account"] = out["account"][:4] + "…"  # the API key is never sent; hide the account number from viewers too
    return out


@app.post("/api/octopus")
async def save_octopus(body: OctopusIn, request: Request) -> dict:
    store: ConnectionStore = app.state.connection_store
    storage: Storage = app.state.storage
    if store.octopus_from_env():
        raise HTTPException(status_code=409, detail="Octopus is set by OCTOPUS_API_KEY in the container settings.")
    if not body.api_key.strip():
        store.save_octopus("", "")
        storage.clear_rates()
        app.state.octopus = _make_octopus(store, storage, "", "")
        log.info("Disconnected Octopus")
        return get_octopus(request)
    try:
        api_key, account = validate_octopus(body.api_key, body.account)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    octopus = _make_octopus(store, storage, api_key, account)
    await asyncio.to_thread(octopus.sync)
    if octopus.last_error:
        raise HTTPException(status_code=422, detail=octopus.last_error)
    store.save_octopus(api_key, account)
    app.state.octopus = octopus
    log.info("Connected Octopus account %s (%s)", account, octopus.info["import"]["name"])
    return get_octopus(request)


class ScheduleIn(BaseModel):
    action: str = "charge"
    start: str = "00:30"
    end: str = "05:30"
    days: list[int] = [0, 1, 2, 3, 4, 5, 6]
    power_w: int = 1500
    target_soc: int = 90
    enabled: bool = True


class ControlIn(BaseModel):
    enabled: bool = False
    hold_cheap: bool = True
    grid_charge: bool = False
    charge_power_w: int = 1500
    charge_target_soc: int = 90
    schedules: list[ScheduleIn] = []


class ModeIn(BaseModel):
    mode: int


@app.get("/api/control")
def get_control() -> dict:
    return app.state.controller.status()


@app.post("/api/control")
async def save_control(body: ControlIn) -> dict:
    try:
        data = body.model_dump()
        data["schedules"] = tuple(Schedule.from_dict(x) for x in data["schedules"])
        settings = ControlSettings(**data).validate()
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    app.state.connection_store.save_section("control", asdict(settings))
    controller: Controller = app.state.controller
    controller.update_settings(settings)
    await controller.tick()
    return controller.status()


@app.post("/api/control/mode")
async def set_battery_mode(body: ModeIn) -> dict:
    """Switch the battery to one of the Anker app's modes now."""
    controller: Controller = app.state.controller
    battery = app.state.collectors["battery"]
    if not battery.connected:
        raise HTTPException(status_code=409, detail="The battery isn't connected")
    try:
        await controller.set_mode(body.mode)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    except Exception as err:
        raise HTTPException(status_code=502, detail=f"The battery didn't accept the change: {err}") from err
    return controller.status()


REGIONS = "ABCDEFGHJKLMNP"


def _current_candidate() -> Candidate:
    """What you pay now: Octopus's stored prices, or the typed ones."""
    store: ConnectionStore = app.state.connection_store
    octopus: Octopus = app.state.octopus
    typed = Tariff.from_dict(store.load_tariff())
    if octopus.configured and octopus.info and not typed.use_manual:
        rates = app.state.storage.rates()
        imp = {k: v[0] for k, v in rates.items() if v[0] is not None}
        exp = {k: v[1] for k, v in rates.items() if v[1] is not None}
        info = octopus.info
        c = Candidate("current", f"Your tariff: {info['import']['name']}", info["import"].get("standing_charge_p"),
                      imp, price_profile(imp), exp, price_profile(exp), source="current")
        c.export_name = info["export"]["name"] if info.get("export") else None
        if not exp:
            c.export_profile = [0.0] * 48
        return c
    c = Candidate("current", "Your tariff (prices from Edit costs)", None,
                  import_profile=fixed_profile(typed.peak_rate, typed.offpeak_rate, typed.offpeak_start, typed.offpeak_end),
                  export_profile=[typed.export_rate] * 48, source="current",
                  note="No standing charge included: it isn't in Edit costs.")
    c.export_name = f"{typed.export_rate:g}p export"
    return c


@app.get("/api/compare")
async def get_compare(region: str = Query("", max_length=1), days: int = Query(365, ge=1, le=366)) -> dict:
    octopus: Octopus = app.state.octopus
    store: ConnectionStore = app.state.connection_store
    storage: Storage = app.state.storage
    if not region:
        imp = (octopus.info.get("import") or {}).get("tariff") if octopus.info else None
        region = (tariff_parts(imp) or {}).get("region") or "C"
    region = region.upper()
    if region not in REGIONS:
        raise HTTPException(status_code=422, detail="Unknown region")
    custom = store.load_section("compare").get("custom") or []
    snap = app.state.collectors["battery"].snapshot
    cap, kw = battery_size(snap)
    usage = storage.usage_slots(days)
    if not usage:
        return {"days": 0, "rows": [], "region": region, "custom": custom, "presets": PRESETS,
                "message": "No home-use history yet. Come back after a day or two of recording."}
    first = min(u["day"] for u in usage)
    start = datetime.fromisoformat(first).replace(tzinfo=storage.tz).astimezone(timezone.utc)
    end = datetime.now(timezone.utc)
    comparer: Comparer = app.state.comparer

    def run():
        candidates, problems = comparer.candidates(region, start, end, storage.tz, _current_candidate(), custom)
        out = compare(candidates, usage, cap, kw)
        out["problems"] = problems
        return out

    out = await asyncio.to_thread(run)
    out.update({"region": region, "custom": custom, "presets": PRESETS})
    return out


class CustomTariff(BaseModel):
    name: str
    peak_rate: float
    offpeak_rate: float
    offpeak_start: str = "00:00"
    offpeak_end: str = "07:00"
    export_rate: float = 0.0
    standing_p: float = 0.0


@app.get("/api/compare/custom")
def get_custom() -> dict:
    return {"custom": app.state.connection_store.load_section("compare").get("custom") or [], "presets": PRESETS}


@app.post("/api/compare/custom")
def save_custom(body: list[CustomTariff]) -> dict:
    rows = []
    for t in body[:10]:
        try:
            Tariff(peak_rate=t.peak_rate, offpeak_rate=t.offpeak_rate, offpeak_start=t.offpeak_start,
                   offpeak_end=t.offpeak_end, export_rate=t.export_rate).validate()
        except ValueError as err:
            raise HTTPException(status_code=422, detail=f"{t.name}: {err}") from err
        if not t.name.strip() or len(t.name) > 60:
            raise HTTPException(status_code=422, detail="Give each tariff a name")
        rows.append({**t.model_dump(), "name": t.name.strip()})
    app.state.connection_store.save_section("compare", {"custom": rows})
    return {"custom": rows}


@app.get("/api/battery-care")
def battery_care() -> dict:
    snapshot = app.state.collectors["battery"].snapshot
    return care(snapshot, app.state.storage.soc_stats(), asdict(app.state.controller.settings))


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
            detail=f"This address is set by {ENV_PREFIX[device]}_HOST in the container settings. Remove it there to edit it here.",
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


@app.get("/api/events")
def events(
    limit: int = Query(100, ge=1, le=1000),
    before: int | None = Query(None, description="an event id; returns older events"),
    kind: str = Query("", description="comma-separated kinds, e.g. charging,mode"),
) -> list[dict]:
    """The event log, newest first."""
    kinds = [k for k in kind.split(",") if k]
    unknown = set(kinds) - set(EVENT_KINDS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown kind: {', '.join(sorted(unknown))}")
    return app.state.storage.events(limit, before, kinds)


@app.get("/api/export")
def export(
    data: str = Query("minutes", pattern="^(minutes|daily|meter|slots|events)$"),
    start: date | None = None,
    end: date | None = None,
    format: str = Query("csv", pattern="^(csv|json)$"),
):
    """Recorded data for local dates `start` to `end` (default: the last 7 days).

    minutes: Solarbank power and energy per minute. daily: daily kWh totals.
    meter: Smart Meter readings per minute. slots: battery energy per half hour.
    events: the event log.
    """
    storage: Storage = app.state.storage
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
    app.state.auth.require_admin(request)


@app.get("/api/export/backup", dependencies=[Depends(_admin)])
def export_backup() -> FileResponse:
    """The whole database as a SQLite file, everything recorded so far."""
    storage: Storage = app.state.storage
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    storage.backup(path)
    name = f"solarbank-backup-{datetime.now(storage.tz).date()}.db"
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=name,
                        background=BackgroundTask(os.remove, path))


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
