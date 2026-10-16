"""Web routes: tariff."""

from __future__ import annotations

import logging
from ..context import state
from ..services import make_octopus, octopus_prices, supplier

import asyncio
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from pydantic import BaseModel

from ..config import ConnectionStore
from ..octopus import Octopus, validate as validate_octopus
from ..storage import Storage
from ..tariff import SUPPLIERS, Tariff, payback

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


@router.get("/api/payback")
def get_payback() -> dict:
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
    prices = storage.rates() if use_octopus else None
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
    data = body.model_dump()
    data["supplier"] = data["supplier"] or state.connection_store.load_tariff().get("supplier", "")
    try:
        tariff = Tariff(**data).validate()
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    state.connection_store.save_tariff(asdict(tariff))
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
