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


ENV_PREFIX = {"battery": "SOLARBANK", "meter": "METER"}


class ConnectionStore:
    """Where each device is: from environment variables if set, else the settings file.

    Devices are "battery" (required) and "meter" (optional Smart Meter). An
    environment variable such as SOLARBANK_HOST or METER_HOST wins, so a
    compose file stays authoritative for people who prefer configuring it
    there; the setup screen then shows that address read-only.

    File layout: the battery's fields at the top level (as before the meter
    existed), the meter's under "meter".
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def from_env(self, device: str = "battery") -> bool:
        return bool(os.environ.get(f"{ENV_PREFIX[device]}_HOST", "").strip())

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as err:
            log.warning("Ignoring unreadable settings file %s: %s", self.path, err)
            return {}

    def load(self, device: str = "battery") -> Connection:
        prefix = ENV_PREFIX[device]
        if self.from_env(device):
            return Connection(
                os.environ[f"{prefix}_HOST"].strip(),
                _int(f"{prefix}_PORT", 502),
                _int(f"{prefix}_UNIT_ID", 1),
            )
        data = self._read()
        if device == "meter":
            data = data.get("meter") or {}
        try:
            return Connection(str(data.get("host", "")), int(data.get("port", 502)), int(data.get("unit_id", 1)))
        except (ValueError, TypeError):
            return Connection()

    def load_tariff(self) -> dict:
        return self._read().get("tariff") or {}

    def save_tariff(self, tariff: dict) -> None:
        data = self._read()
        data["tariff"] = tariff
        self._write(data)

    def load_section(self, name: str) -> dict:
        value = self._read().get(name)
        return value if isinstance(value, dict) else {}

    def save_section(self, name: str, value: dict) -> None:
        data = self._read()
        data[name] = value
        self._write(data)

    def octopus_from_env(self) -> bool:
        return bool(os.environ.get("OCTOPUS_API_KEY", "").strip())

    def load_octopus(self) -> tuple[str, str]:
        """(api_key, account number); OCTOPUS_API_KEY and OCTOPUS_ACCOUNT win."""
        if self.octopus_from_env():
            return os.environ["OCTOPUS_API_KEY"].strip(), os.environ.get("OCTOPUS_ACCOUNT", "").strip().upper()
        data = self._read().get("octopus") or {}
        return str(data.get("api_key", "")), str(data.get("account", ""))

    def save_octopus(self, api_key: str, account: str) -> None:
        data = self._read()
        if api_key:
            data["octopus"] = {"api_key": api_key, "account": account}
        else:
            data.pop("octopus", None)
        self._write(data)

    def save(self, conn: Connection, device: str = "battery") -> None:
        data = self._read()
        fields = asdict(conn)
        if device == "meter":
            if conn.host:
                data["meter"] = fields
            else:
                data.pop("meter", None)
        else:
            data.update(fields)
        self._write(data)

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.chmod(0o600)  # it can hold the Octopus API key
        tmp.replace(self.path)


@dataclass(frozen=True)
class Settings:
    poll_seconds: int
    retention_days: int
    db_path: str
    settings_path: str
    timezone: str
    relay_meter: bool = False
    relay_port: int = 5020

    @classmethod
    def from_env(cls) -> "Settings":
        db_path = os.environ.get("DB_PATH", "/data/solarbank.db")
        return cls(
            poll_seconds=max(2, _int("POLL_SECONDS", 5)),
            retention_days=_int("RETENTION_DAYS", 365),
            db_path=db_path,
            settings_path=os.environ.get("SETTINGS_PATH", str(Path(db_path).parent / "settings.json")),
            timezone=os.environ.get("TZ", "UTC"),
            relay_meter=os.environ.get("RELAY_METER", "").strip().lower() in ("1", "true", "yes", "on"),
            relay_port=_int("RELAY_PORT", 5020),
        )
