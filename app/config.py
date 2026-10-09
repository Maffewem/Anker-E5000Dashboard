"""Settings, read from environment variables and the saved settings file."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("solarbank.config")

# An IPv4/IPv6 address or a DNS name; nothing that could smuggle in a URL or path.
HOST_RE = re.compile(r"^[A-Za-z0-9.:\-\[\]]{1,253}$")


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


@dataclass(frozen=True)
class Connection:
    """Where the Solarbank is on the network."""

    host: str = ""
    port: int = 502
    unit_id: int = 1

    def validate(self) -> "Connection":
        host = self.host.strip()
        if not HOST_RE.match(host):
            raise ValueError("Enter the battery's IP address, for example 192.168.1.50")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        if not 0 <= int(self.unit_id) <= 247:
            raise ValueError("Unit id must be between 0 and 247")
        return Connection(host, int(self.port), int(self.unit_id))


class ConnectionStore:
    """The connection to use, from SOLARBANK_HOST if set, else the settings file.

    The environment variable wins so that a compose file stays authoritative
    for people who prefer configuring it there; the setup screen then shows
    the value as read-only.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    @property
    def from_env(self) -> bool:
        return bool(os.environ.get("SOLARBANK_HOST", "").strip())

    def load(self) -> Connection:
        if self.from_env:
            return Connection(
                os.environ["SOLARBANK_HOST"].strip(),
                _int("SOLARBANK_PORT", 502),
                _int("SOLARBANK_UNIT_ID", 1),
            )
        try:
            data = json.loads(self.path.read_text())
            return Connection(str(data.get("host", "")), int(data.get("port", 502)), int(data.get("unit_id", 1)))
        except FileNotFoundError:
            return Connection()
        except (ValueError, TypeError, OSError) as err:
            log.warning("Ignoring unreadable settings file %s: %s", self.path, err)
            return Connection()

    def save(self, conn: Connection) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(conn), indent=2))
        tmp.replace(self.path)


@dataclass(frozen=True)
class Settings:
    poll_seconds: int
    retention_days: int
    db_path: str
    settings_path: str
    timezone: str

    @classmethod
    def from_env(cls) -> "Settings":
        db_path = os.environ.get("DB_PATH", "/data/solarbank.db")
        return cls(
            poll_seconds=max(2, _int("POLL_SECONDS", 5)),
            retention_days=_int("RETENTION_DAYS", 365),
            db_path=db_path,
            settings_path=os.environ.get("SETTINGS_PATH", str(Path(db_path).parent / "settings.json")),
            timezone=os.environ.get("TZ", "UTC"),
        )
