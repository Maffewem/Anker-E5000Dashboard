"""Optional lock for everything that changes something.

ADMIN_PASSWORD: when set, every request that changes anything (any method
other than GET/HEAD/OPTIONS) and the full database backup need a signed-in
session. The session is an HttpOnly, SameSite=Strict cookie, and every change
must also carry the session's CSRF token in an X-CSRF-Token header.

READ_ONLY=true: refuses every change outright, signed in or not. The
dashboard keeps recording, and battery control keeps running on the settings
it already has.

Sessions are signed with a key made at startup, so restarting the container
signs everyone out.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

COOKIE = "solarbank_session"
CSRF_HEADER = "x-csrf-token"
SESSION_SECONDS = 30 * 86400
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
OPEN_PATHS = {"/api/auth/login", "/api/auth/logout"}  # usable while locked
MAX_FAILURES = 5  # wrong passwords per address ...
FAILURE_WINDOW = 15 * 60  # ... within this many seconds, then refused until it passes


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class Auth:
    def __init__(self, password: str = "", read_only: bool = False) -> None:
        self.password = password
        self.read_only = read_only
        self._key = secrets.token_bytes(32)
        self._failures: dict[str, list[float]] = {}

    @classmethod
    def from_env(cls) -> "Auth":
        return cls(os.environ.get("ADMIN_PASSWORD", ""), _flag("READ_ONLY"))

    @property
    def password_set(self) -> bool:
        return bool(self.password)

    def _sign(self, text: str) -> str:
        return hmac.new(self._key, text.encode(), hashlib.sha256).hexdigest()

    def new_session(self) -> str:
        body = f"{int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(16)}"
        return f"{body}.{self._sign(body)}"

    def session_valid(self, token: str | None) -> bool:
        if not token or token.count(".") != 2:
            return False
        body, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sign(body)):
            return False
        try:
            return int(body.split(".")[0]) > time.time()
        except ValueError:
            return False

    def csrf_for(self, token: str) -> str:
        return self._sign(f"csrf:{token}")

    def signed_in(self, request: Request) -> bool:
        return self.password_set and self.session_valid(request.cookies.get(COOKIE))

    def can_edit(self, request: Request) -> bool:
        if self.read_only:
            return False
        return not self.password_set or self.signed_in(request)

    def status(self, request: Request) -> dict:
        signed_in = self.signed_in(request)
        return {
            "password_set": self.password_set,
            "read_only": self.read_only,
            "signed_in": signed_in,
            "can_edit": self.can_edit(request),
            "csrf": self.csrf_for(request.cookies[COOKIE]) if signed_in else None,
        }

    def check_password(self, client: str, password: str) -> bool:
        """Compare in constant time, refusing an address after too many misses."""
        now = time.time()
        recent = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW]
        if len(recent) >= MAX_FAILURES:
            raise HTTPException(status_code=429, detail="Too many wrong passwords. Try again in 15 minutes.")
        ok = self.password_set and hmac.compare_digest(password.encode(), self.password.encode())
        if ok:
            self._failures.pop(client, None)
        else:
            self._failures[client] = recent + [now]
            if len(self._failures) > 10000:  # don't let a flood of addresses grow this forever
                self._failures.clear()
        return ok

    def denial(self, request: Request) -> JSONResponse | None:
        """Why this request may not change anything, or None if it may."""
        if self.read_only:
            return JSONResponse({"detail": "This dashboard is read-only (READ_ONLY is set)."}, status_code=403)
        if not self.password_set:
            return None
        token = request.cookies.get(COOKIE)
        if not self.session_valid(token):
            return JSONResponse({"detail": "Sign in to change settings."}, status_code=401)
        sent = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(sent.encode(), self.csrf_for(token).encode()):
            return JSONResponse({"detail": "Reload the page and try again (security token missing)."}, status_code=403)
        return None

    def require_admin(self, request: Request) -> None:
        """For reads that hand out everything, like the database backup."""
        if self.password_set and not self.signed_in(request):
            raise HTTPException(status_code=401, detail="Sign in to download a backup.")


class LockMiddleware(BaseHTTPMiddleware):
    """Refuses changes the viewer isn't allowed to make, and adds safe headers."""

    async def dispatch(self, request: Request, call_next):
        if request.method not in SAFE_METHODS and request.url.path not in OPEN_PATHS:
            denied = request.app.state.auth.denial(request)
            if denied is not None:
                return self._harden(denied)
        return self._harden(await call_next(request))

    @staticmethod
    def _harden(response):
        headers = response.headers
        headers.setdefault("X-Frame-Options", "DENY")  # no clicking the controls through someone else's page
        headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "same-origin")
        return response
