"""Web routes: tariff."""

from __future__ import annotations

import logging
from ..context import state
from ..services import make_octopus, octopus_prices, supplier

import asyncio
import csv
import io
from dataclasses import asdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from pydantic import BaseModel

from ..config import ConnectionStore
from ..octopus import Octopus, validate as validate_octopus
from ..storage import Storage
from ..tariff import SUPPLIERS, PriceHistory, Tariff, daily_costs, payback, prices_changed

log = logging.getLogger("solarbank.web")
router = APIRouter()


class TariffIn(BaseModel):
    battery_cost: float = 0.0
    peak_rate: float = 28.0
    offpeak_rate: float = 28.0
    offpeak_start: str = "00:30"
    offpeak_end: str = "05:30"
    export_rate: float = 15.0
    use_manual: bool = False
    installed: str = ""
    supplier: str = ""
    prices_from: str = ""  # local date the typed prices start; today if empty


def _pricing() -> tuple[Tariff, Tariff, dict | None, dict | None]:
    """(typed tariff, tariff in use, Octopus profile, actual half-hour prices)."""
    store: ConnectionStore = state.connection_store
    storage: Storage = state.storage
    octopus: Octopus = state.octopus
    typed = Tariff.from_dict(store.load_tariff())
    tariff = typed
    use_octopus = octopus_prices(typed)
    profile = octopus.profile() if use_octopus else None
    if profile:
        # Octopus prices stand in for the typed ones; only the battery cost is kept.
        tariff = Tariff.from_dict({**{k: v for k, v in profile.items() if v is not None},
                                   "battery_cost": typed.battery_cost, "installed": typed.installed})
    if use_octopus:
        prices = storage.rates()
    else:
        # Typed prices that have changed keep pricing each past day at what it cost then.
        history = storage.tariff_history()
        prices = PriceHistory(history) if history else None
    return typed, tariff, profile, prices


@router.get("/api/charging-costs")
def get_charging_costs(days: int = Query(30, ge=1, le=366), format: str = Query("json", pattern="^(csv|json)$")):
    """What charging the battery cost each day (newest first), with today, 7 and 30 day totals."""
    storage: Storage = state.storage
    _, tariff, profile, prices = _pricing()
    today = datetime.now(storage.tz).date()
    out = daily_costs(tariff, storage.battery_slots(), today, days, prices)
    out["source"] = "octopus" if profile else "manual"
    if format == "json":
        return out
    columns = [k for k in out["days"][0] if k != "recorded"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(r for r in reversed(out["days"]) if r["recorded"])
    name = f"solarbank-charging-costs-{today - timedelta(days=days - 1)}-to-{today}.csv"
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/payback")
def get_payback() -> dict:
    storage: Storage = state.storage
    octopus: Octopus = state.octopus
    typed, tariff, profile, prices = _pricing()
    out = payback(tariff, storage.battery_slots(), datetime.now(storage.tz).date(), prices, _lifetime())
    out["source"] = "octopus" if profile else "manual"
    out["octopus_connected"] = octopus.configured and supplier(typed) == "octopus"
    out["supplier"] = supplier(typed)
    out["supplier_picked"] = bool(typed.supplier)
    out["suppliers"] = [{"value": k, **v} for k, v in SUPPLIERS.items()]
    out["use_manual"] = typed.use_manual
    out["manual"] = asdict(typed)  # what was typed, even while Octopus prices are shown
    out["tariff_name"] = (octopus.info.get("import") or {}).get("name") if profile else None
    return out


class SupplierIn(BaseModel):
    supplier: str


@router.post("/api/supplier")
def save_supplier(body: SupplierIn) -> dict:
    store: ConnectionStore = state.connection_store
    try:
        tariff = Tariff.from_dict({**store.load_tariff(), "supplier": body.supplier}).validate()
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    store.save_tariff(asdict(tariff))
    return get_payback()


def _lifetime() -> dict | None:
    """The battery's lifetime charge/discharge totals, remembered while it's offline."""
    store: ConnectionStore = state.connection_store
    snap = state.collectors["battery"].snapshot
    now = {"charged_kwh": snap.get("charged_total_kwh"), "discharged_kwh": snap.get("discharged_total_kwh")}
    if None in now.values():
        return store.load_lifetime()
    if now != store.load_lifetime():
        store.save_lifetime(now)
    return now


@router.post("/api/tariff")
def save_tariff(body: TariffIn) -> dict:
    store: ConnectionStore = state.connection_store
    storage: Storage = state.storage
    data = body.model_dump()
    prices_from = data.pop("prices_from") or ""
    saved = store.load_tariff()
    data["supplier"] = data["supplier"] or saved.get("supplier", "")
    today = datetime.now(storage.tz).date()
    try:
        tariff = Tariff(**data).validate()
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    try:
        since = date.fromisoformat(prices_from) if prices_from else today
    except ValueError:
        raise HTTPException(status_code=422, detail="Enter when the new prices start as a date") from None
    if since > today:
        raise HTTPException(status_code=422, detail="The new prices can't start in the future; save them on the day they start")
    old = Tariff.from_dict(saved)
    changes = prices_changed(old, tariff) if saved else []
    if changes:
        storage.save_tariff_change(asdict(old), asdict(tariff), since.isoformat())
        storage.record_event("tariff", f"Prices changed from {since:%-d %b %Y}: " + ", ".join(changes),
                             device="tariff", source="dashboard")
    store.save_tariff(asdict(tariff))
    return get_payback()


class OctopusIn(BaseModel):
    api_key: str = ""
    account: str = ""


@router.get("/api/octopus")
def get_octopus(request: Request) -> dict:
    octopus: Octopus = state.octopus
    out = octopus.status(state.collectors["battery"].snapshot)
    out["locked"] = state.connection_store.octopus_from_env()
    out["supplier"] = supplier()
    if out.get("account") and not state.auth.can_edit(request):
        out["account"] = out["account"][:4] + "…"  # the API key is never sent; hide the account number from viewers too
    return out


@router.post("/api/octopus")
async def save_octopus(body: OctopusIn, request: Request) -> dict:
    store: ConnectionStore = state.connection_store
    storage: Storage = state.storage
    if store.octopus_from_env():
        raise HTTPException(status_code=409, detail="Octopus is set by OCTOPUS_API_KEY in the container settings.")
    if not body.api_key.strip():
        store.save_octopus("", "")
        storage.clear_rates()
        state.octopus = make_octopus(store, storage, "", "")
        log.info("Disconnected Octopus")
        return get_octopus(request)
    try:
        api_key, account = validate_octopus(body.api_key, body.account)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    octopus = make_octopus(store, storage, api_key, account)
    await asyncio.to_thread(octopus.sync)
    if octopus.last_error:
        raise HTTPException(status_code=422, detail=octopus.last_error)
    store.save_octopus(api_key, account)
    state.octopus = octopus
    typed = store.load_tariff()
    if typed.get("supplier") not in (None, "", "octopus"):  # connecting Octopus means you're with Octopus
        store.save_tariff({**typed, "supplier": "octopus"})
    log.info("Connected Octopus account %s (%s)", account, octopus.info["import"]["name"])
    return get_octopus(request)
