"""Optional lock for everything that changes something.

ADMIN_PASSWORD: when set, every request that changes anything (any method
other than GET/HEAD/OPTIONS) and the full database backup need a signed-in
session. The session is an HttpOnly, SameSite=Strict cookie, and every change
must also carry the session's CSRF token in an X-CSRF-Token header.

Without ADMIN_PASSWORD, a password can instead be set on the dashboard. Only
a salted scrypt hash of it is kept, in auth.json on the data volume; deleting
that file (and restarting) removes it. ADMIN_PASSWORD always wins, and then
the dashboard can't change the password.

READ_ONLY=true: refuses every change outright, signed in or not. The
dashboard keeps recording, and battery control keeps running on the settings
it already has.

Sessions are signed with a key made at startup, so restarting the container
signs everyone out.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

COOKIE = "solarbank_session"
CSRF_HEADER = "x-csrf-token"
SESSION_SECONDS = 30 * 86400
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
OPEN_PATHS = {"/api/auth/login", "/api/auth/logout"}  # usable while locked
MIN_LENGTH = 8
SCRYPT = {"n": 2**15, "r": 8, "p": 1}  # about 32 MB and a tenth of a second per guess on a Pi

log = logging.getLogger("solarbank.auth")
MAX_FAILURES = 5  # wrong passwords per address ...
FAILURE_WINDOW = 15 * 60  # ... within this many seconds, then refused until it passes


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def hash_password(password: str) -> dict:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, maxmem=64 * 1024 * 1024, **SCRYPT)
    return {"scrypt": {**SCRYPT, "salt": salt.hex(), "hash": digest.hex()}}


def verify_password(password: str, stored: dict) -> bool:
    try:
        s = stored["scrypt"]
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(s["salt"]), n=int(s["n"]), r=int(s["r"]),
                                p=int(s["p"]), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(digest.hex(), s["hash"])
    except (KeyError, TypeError, ValueError):
        return False


class Auth:
    def __init__(self, password: str = "", read_only: bool = False, path: str | None = None) -> None:
        self.password = password  # from ADMIN_PASSWORD
        self.read_only = read_only
        self.path = Path(path) if path else None
        self.stored = self._load() if not password else None  # a hash set on the dashboard
        self._key = secrets.token_bytes(32)
        self._failures: dict[str, list[float]] = {}

    @classmethod
    def from_env(cls, path: str | None = None) -> "Auth":
        return cls(os.environ.get("ADMIN_PASSWORD", ""), _flag("READ_ONLY"), path)

    def _load(self) -> dict | None:
        if not self.path:
            return None
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (ValueError, OSError) as err:
            # Refuse to start unlocked because of a damaged file: nobody could sign in either way,
            # so say how to reset it.
            log.error("Can't read %s (%s). Delete it to remove the dashboard password.", self.path, err)
            return {"scrypt": {}}
        return data if isinstance(data, dict) and data.get("scrypt") is not None else None

    @property
    def source(self) -> str | None:
        """Where the password comes from: "env", "dashboard" or None."""
        return "env" if self.password else "dashboard" if self.stored else None

    @property
    def password_set(self) -> bool:
        return self.source is not None

    def set_password(self, new: str) -> None:
        """Set, change or (with "") remove the dashboard password. Signs everyone out."""
        if self.password:
            raise ValueError("The password is set by ADMIN_PASSWORD in the container settings.")
        if new and len(new) < MIN_LENGTH:
            raise ValueError(f"Use at least {MIN_LENGTH} characters.")
        if len(new) > 1024:
            raise ValueError("That password is too long.")
        if new:
            self.stored = hash_password(new)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.stored))
            tmp.chmod(0o600)
            tmp.replace(self.path)
        else:
            self.stored = None
            self.path.unlink(missing_ok=True)
        self._key = secrets.token_bytes(32)

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
            "password_source": self.source,
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
        if self.password:
            ok = hmac.compare_digest(password.encode(), self.password.encode())
        else:
            ok = bool(self.stored) and verify_password(password, self.stored)
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
