"""Web routes: settings."""

from __future__ import annotations

import logging
from ..registers import BATTERY, METER
from ..context import state


from fastapi import APIRouter, HTTPException

from pydantic import BaseModel

from ..collector import DEVICE_NAMES
from ..config import ENV_PREFIX, Connection, ConnectionStore

log = logging.getLogger("solarbank.web")
DEVICES = {"battery": BATTERY, "meter": METER}
router = APIRouter()


class ConnectionIn(BaseModel):
    host: str
    port: int = 502
    unit_id: int = 1


@router.get("/api/settings")
def get_settings() -> dict:
    store: ConnectionStore = state.connection_store
    out = {}
    for device, collector in state.collectors.items():
        conn: Connection = collector.connection
        out[device] = {"host": conn.host, "port": conn.port, "unit_id": conn.unit_id, "locked": store.from_env(device)}
    return out


@router.post("/api/settings/{device}/test")
async def test_settings(device: str, body: ConnectionIn) -> dict:
    _profile(device)
    conn = _validated(body)
    _check_not_other_device(device, conn)
    try:
        return {"ok": True, "device": await state.collectors[device].test(conn)}
    except Exception as err:
        raise HTTPException(status_code=422, detail=str(err)) from err


@router.post("/api/settings/{device}")
async def save_settings(device: str, body: ConnectionIn, skip_test: bool = False) -> dict:
    _profile(device)
    store: ConnectionStore = state.connection_store
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
                found = await state.collectors[device].test(conn)
            except Exception as err:
                raise HTTPException(status_code=422, detail=str(err)) from err
    store.save(conn, device)
    log.info("Saved %s address %r (port %s, unit id %s)%s", device, conn.host, conn.port, conn.unit_id,
             " without a test" if skip_test else "")
    state.collectors[device].reconfigure(conn)
    return {"ok": True, "device": found}


def _profile(device: str):
    if device not in DEVICES:
        raise HTTPException(status_code=404, detail="Unknown device")
    return DEVICES[device]


def _check_not_other_device(device: str, conn: Connection) -> None:
    """Catch the meter's address typed into the Solarbank tab, or the reverse."""
    for other, collector in state.collectors.items():
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
