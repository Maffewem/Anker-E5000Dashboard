"""Whether a newer image is published, and asking Portainer to deploy it.

The dashboard never updates itself: that would need the Docker socket, which
would hand anyone who got into this web-facing container the whole host.
Instead it compares its own commit with the image tagged :latest on GHCR
(public, no account needed) and shows a notice. If you paste your Portainer
stack's webhook URL in, "Update now" calls it and Portainer re-pulls the
image and recreates the container.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

log = logging.getLogger("solarbank.updates")

REGISTRY = "https://ghcr.io"
IMAGE = "maffewem/anker-e5000dashboard"
CHECK_SECONDS = 6 * 3600
RETRY_SECONDS = 30 * 60
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
# Portainer's stack and service webhooks: /api/stacks/webhooks/<id> and /api/webhooks/<id>.
WEBHOOK_PATH_RE = re.compile(r"^(/[\w.-]+)*/api/(stacks/)?webhooks/[A-Za-z0-9-]{8,64}$")

Fetch = Callable[[str, dict], bytes]


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """GHCR sends blobs from a signed storage URL that rejects our token, as
    Docker's own clients know: don't carry it to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(_DropAuthOnRedirect)


def _fetch(url: str, headers: dict) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "solarbank-dashboard", **headers})
    with _opener.open(req, timeout=20) as res:
        return res.read()


def _env(config: dict) -> dict:
    out = {}
    for item in (config.get("config") or {}).get("Env") or []:
        name, _, value = item.partition("=")
        out[name] = value
    return out


class Updates:
    def __init__(self, commit: str, image: str = IMAGE, fetch: Fetch | None = None) -> None:
        self.commit = commit
        self.image = image
        self.fetch = fetch or _fetch
        self.latest: dict | None = None
        self.checked_at: float | None = None
        self.error: str | None = None

    def _json(self, path: str, token: str, accept: str = "application/json") -> dict:
        return json.loads(self.fetch(f"{REGISTRY}{path}", {"Authorization": f"Bearer {token}", "Accept": accept}))

    def check(self) -> None:
        """Read the version, commit and build date baked into the :latest image."""
        try:
            scope = urllib.parse.quote(f"repository:{self.image}:pull")
            token = json.loads(self.fetch(f"{REGISTRY}/token?scope={scope}&service=ghcr.io", {}))["token"]
            manifest = self._json(f"/v2/{self.image}/manifests/latest", token, ACCEPT)
            if "manifests" in manifest:  # multi-arch: every platform carries the same build args
                first = next(m for m in manifest["manifests"]
                             if (m.get("platform") or {}).get("os") not in (None, "unknown"))
                manifest = self._json(f"/v2/{self.image}/manifests/{first['digest']}", token, ACCEPT)
            config = self._json(f"/v2/{self.image}/blobs/{manifest['config']['digest']}", token)
            env = _env(config)
            self.latest = {"name": env.get("APP_VERSION") or None, "commit": env.get("APP_COMMIT") or None,
                           "built": env.get("APP_BUILT") or None}
            self.error = None
        except (OSError, ValueError, KeyError, StopIteration) as err:
            self.error = f"Couldn't check for updates: {err}"
            log.info(self.error)
        self.checked_at = time.time()

    @property
    def available(self) -> bool:
        latest = (self.latest or {}).get("commit")
        return bool(self.commit and latest and latest != self.commit)

    def status(self) -> dict:
        return {"latest": self.latest, "update_available": self.available,
                "checked_at": self.checked_at, "error": self.error}


def validate_webhook(url: str) -> str:
    url = url.strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Paste the full webhook URL from Portainer, starting with http:// or https://")
    if not WEBHOOK_PATH_RE.match(parts.path) or parts.fragment:
        raise ValueError("That isn't a Portainer webhook URL. It ends in /api/stacks/webhooks/ and an id.")
    return url


def _private(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return host.endswith((".local", ".lan", ".home.arpa")) or "." not in host


def call_webhook(url: str) -> str | None:
    """POST to the webhook; None when Portainer accepted it, else what went wrong.

    Portainer on the home network usually has a self-signed certificate, so
    it isn't checked for private addresses; anything public must have a
    valid one.
    """
    host = urllib.parse.urlsplit(url).hostname or ""
    context = ssl.create_default_context()
    if _private(host):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, data=b"", method="POST", headers={"User-Agent": "solarbank-dashboard"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=context) as res:
            log.info("Portainer webhook answered %s", res.status)
            return None
    except urllib.error.HTTPError as err:
        return f"Portainer refused the webhook ({err.code}). Check the URL in Update settings."
    except (OSError, ValueError) as err:
        # Portainer may be recreating this container already; the request dies with it.
        return f"Couldn't reach Portainer: {getattr(err, 'reason', err)}"
