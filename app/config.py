"""Settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    unit_id: int
    poll_seconds: int
    retention_days: int
    db_path: str
    timezone: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.environ.get("SOLARBANK_HOST", "").strip(),
            port=_int("SOLARBANK_PORT", 502),
            unit_id=_int("SOLARBANK_UNIT_ID", 1),
            poll_seconds=max(2, _int("POLL_SECONDS", 5)),
            retention_days=_int("RETENTION_DAYS", 365),
            db_path=os.environ.get("DB_PATH", "/data/solarbank.db"),
            timezone=os.environ.get("TZ", "UTC"),
        )
