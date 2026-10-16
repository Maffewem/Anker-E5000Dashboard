"""Web routes: auth."""

from __future__ import annotations

import logging
from ..context import state

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from pydantic import BaseModel

from ..auth import COOKIE, SESSION_SECONDS, Auth

log = logging.getLogger("solarbank.web")
router = APIRouter()


class LoginIn(BaseModel):
    password: str = ""


@router.get("/api/auth")
def auth_status(request: Request) -> dict:
    return state.auth.status(request)


@router.post("/api/auth/login")
async def login(body: LoginIn, request: Request) -> JSONResponse:
    auth: Auth = state.auth
    if not auth.password_set:
        raise HTTPException(status_code=409, detail="No ADMIN_PASSWORD is set, so there's nothing to sign in to.")
    client = request.client.host if request.client else "unknown"
    if not auth.check_password(client, body.password[:1024]):
        log.warning("Wrong dashboard password from %s", client)
        await asyncio.sleep(1)  # slows guessing
        raise HTTPException(status_code=401, detail="Wrong password")
    log.info("Signed in from %s", client)
    return _session_response(auth.status(request), request)


class PasswordIn(BaseModel):
    current: str = ""
    new: str = ""


def _session_response(out: dict, request: Request) -> JSONResponse:
    """A response that signs this browser in with a new session."""
    auth: Auth = state.auth
    token = auth.new_session()
    response = JSONResponse({**out, "signed_in": True, "can_edit": not auth.read_only, "csrf": auth.csrf_for(token)})
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").startswith("https")
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", secure=secure)
    return response


@router.post("/api/auth/password")
async def set_password(body: PasswordIn, request: Request) -> JSONResponse:
    """Set, change or remove the dashboard password (not ADMIN_PASSWORD).

    Setting the first one works while the dashboard is unlocked, which is
    already anyone on the network. Changing or removing one needs signing
    in (the lock middleware checks that) and the current password too.
    """
    auth: Auth = state.auth
    if auth.source == "env":
        raise HTTPException(status_code=409, detail="The password is set by ADMIN_PASSWORD in the container settings.")
    client = request.client.host if request.client else "unknown"
    if auth.password_set and not auth.check_password(client, body.current[:1024]):
        await asyncio.sleep(1)
        raise HTTPException(status_code=401, detail="The current password is wrong")
    try:
        auth.set_password(body.new)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    log.info("Dashboard password %s from %s", "set" if body.new else "removed", client)
    if not body.new:
        return JSONResponse(auth.status(request))
    return _session_response(auth.status(request), request)


@router.post("/api/auth/logout")
def logout(request: Request) -> JSONResponse:
    response = JSONResponse({**state.auth.status(request), "signed_in": False, "csrf": None,
                             "can_edit": not state.auth.read_only and not state.auth.password_set})
    response.delete_cookie(COOKIE, httponly=True, samesite="strict")
    return response
