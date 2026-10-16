"""Web routes: compare."""

from __future__ import annotations

from ..context import state
from ..services import octopus_prices

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from pydantic import BaseModel

from ..compare import PRESETS, Candidate, Comparer, compare, profile as price_profile
from ..config import ConnectionStore
from ..octopus import Octopus, tariff_parts
from ..runtime import battery_size
from ..storage import Storage
from ..tariff import Tariff, fixed_profile

router = APIRouter()


REGIONS = "ABCDEFGHJKLMNP"


def _current_candidate() -> Candidate:
    """What you pay now: Octopus's stored prices, or the typed ones."""
    store: ConnectionStore = state.connection_store
    octopus: Octopus = state.octopus
    typed = Tariff.from_dict(store.load_tariff())
    if octopus_prices(typed) and octopus.info:
        rates = state.storage.rates()
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


@router.get("/api/compare")
async def get_compare(region: str = Query("", max_length=1), days: int = Query(365, ge=1, le=366)) -> dict:
    octopus: Octopus = state.octopus
    store: ConnectionStore = state.connection_store
    storage: Storage = state.storage
    if not region:
        imp = (octopus.info.get("import") or {}).get("tariff") if octopus.info else None
        region = (tariff_parts(imp) or {}).get("region") or "C"
    region = region.upper()
    if region not in REGIONS:
        raise HTTPException(status_code=422, detail="Unknown region")
    custom = store.load_section("compare").get("custom") or []
    snap = state.collectors["battery"].snapshot
    cap, kw = battery_size(snap)
    usage = storage.usage_slots(days)
    if not usage:
        return {"days": 0, "rows": [], "region": region, "custom": custom, "presets": PRESETS,
                "message": "No home-use history yet. Come back after a day or two of recording."}
    first = min(u["day"] for u in usage)
    start = datetime.fromisoformat(first).replace(tzinfo=storage.tz).astimezone(timezone.utc)
    end = datetime.now(timezone.utc)
    comparer: Comparer = state.comparer

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


@router.get("/api/compare/custom")
def get_custom() -> dict:
    return {"custom": state.connection_store.load_section("compare").get("custom") or [], "presets": PRESETS}


@router.post("/api/compare/custom")
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
    state.connection_store.save_section("compare", {"custom": rows})
    return {"custom": rows}
