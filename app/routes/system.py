"""Web routes: system."""

from __future__ import annotations

import logging
from ..context import APP_BUILT, APP_COMMIT, APP_VERSION, state

import asyncio
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from pydantic import BaseModel

from ..collector import Collector
from ..updates import call_webhook, validate_webhook

log = logging.getLogger("solarbank.web")
router = APIRouter()


@router.get("/api/live")
def live() -> dict:
    battery: Collector = state.collectors["battery"]
    meter: Collector = state.collectors["meter"]
    return {
        "version": {"name": APP_VERSION, "commit": APP_COMMIT, "built": APP_BUILT},
        "status": battery.status(),
        "data": battery.snapshot,
        "meter": {
            "status": meter.status(),
            "data": meter.snapshot,
            "relay_port": state.relay.port if state.relay else None,
        },
    }


def _webhook() -> tuple[str, bool]:
    """(Portainer webhook URL, whether it comes from UPDATE_WEBHOOK)."""
    env = os.environ.get("UPDATE_WEBHOOK", "").strip()
    if env:
        return env, True
    return str(state.connection_store.load_section("update").get("webhook") or ""), False


@router.get("/api/version")
def version() -> dict:
    """This build, the newest published one, and whether one-click updates are set up.

    The webhook URL itself is never sent: anyone holding it can redeploy the stack.
    """
    url, from_env = _webhook()
    return {"running": {"name": APP_VERSION, "commit": APP_COMMIT, "built": APP_BUILT},
            **state.updates.status(), "webhook_set": bool(url), "webhook_locked": from_env}


class WebhookIn(BaseModel):
    url: str = ""


@router.post("/api/update/webhook")
def save_webhook(body: WebhookIn) -> dict:
    if _webhook()[1]:
        raise HTTPException(status_code=409, detail="Set by UPDATE_WEBHOOK in the container settings.")
    url = ""
    if body.url.strip():
        try:
            url = validate_webhook(body.url[:2048])
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
    state.connection_store.save_section("update", {"webhook": url} if url else {})
    log.info("Update webhook %s", "saved" if url else "removed")
    return version()


@router.post("/api/update")
async def update_now() -> dict:
    """Ask Portainer to re-pull the image and recreate this container."""
    url, _ = _webhook()
    if not url:
        raise HTTPException(status_code=409, detail="Add your Portainer webhook in Update settings first.")
    log.info("Asking Portainer to update the dashboard")
    call = asyncio.ensure_future(asyncio.to_thread(call_webhook, url))
    done, _ = await asyncio.wait({call}, timeout=15)
    # Portainer often answers only once the new container is up, by which time
    # this one is gone; no answer within 15 s means it's under way.
    error = call.result() if done else None
    if error:
        log.warning("Update failed: %s", error)
        raise HTTPException(status_code=502, detail=error)
    return {"started": True}


@router.get("/api/raw")
def raw() -> dict:
    """Every decoded register, for troubleshooting."""
    return {device: c.raw for device, c in state.collectors.items()}


@router.get("/healthz")
def healthz():
    # Healthy when every device that has an address is connected.
    collectors = state.collectors
    body = {device: c.status() for device, c in collectors.items()}
    configured = [c for c in collectors.values() if c.connection.host]
    healthy = bool(configured) and all(c.connected for c in configured)
    return JSONResponse(body, status_code=200 if healthy else 503)
