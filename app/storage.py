"""SQLite history: one row per minute with average power and energy used."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

POWER_FIELDS = ("solar_w", "home_w", "battery_w", "grid_w", "soc")
ENERGY_FIELDS = (
    "solar_wh",
    "home_wh",
    "import_wh",
    "export_wh",
    "charge_wh",
    "discharge_wh",
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS minutes (
    ts INTEGER PRIMARY KEY,  -- unix seconds, start of the minute (UTC)
    {", ".join(f"{f} REAL" for f in POWER_FIELDS + ENERGY_FIELDS)}
);
"""


@dataclass
class MinuteBucket:
    """Accumulates samples for one minute.

    Energy is integrated sample by sample so that, for example, a minute that
    both imports and exports counts each direction separately.
    """

    minute: int
    sums: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    energy: dict[str, float] = field(default_factory=lambda: dict.fromkeys(ENERGY_FIELDS, 0.0))

    def add(self, snapshot: dict, seconds: float) -> None:
        for key in POWER_FIELDS:
            v = snapshot.get(key)
            if v is not None:
                self.sums[key] = self.sums.get(key, 0.0) + v
                self.counts[key] = self.counts.get(key, 0) + 1

        hours = seconds / 3600

        def wh(name: str, watts: float | None) -> None:
            if watts is not None and watts > 0:
                self.energy[name] += watts * hours

        solar, home = snapshot.get("solar_w"), snapshot.get("home_w")
        grid, battery = snapshot.get("grid_w"), snapshot.get("battery_w")
        wh("solar_wh", solar)
        wh("home_wh", home)
        if grid is not None:
            wh("import_wh", grid)
            wh("export_wh", -grid)
        if battery is not None:
            wh("discharge_wh", battery)
            wh("charge_wh", -battery)

    def row(self) -> dict:
        row = {"ts": self.minute}
        for key in POWER_FIELDS:
            n = self.counts.get(key)
            row[key] = self.sums[key] / n if n else None
        row.update(self.energy)
        return row


class Storage:
    def __init__(self, path: str, retention_days: int, timezone: str = "UTC") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self.retention_days = retention_days
        try:
            self.tz = ZoneInfo(timezone)
        except Exception:
            self.tz = ZoneInfo("UTC")

    def write_minute(self, row: dict) -> None:
        cols = ["ts", *POWER_FIELDS, *ENERGY_FIELDS]
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO minutes ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [row.get(c) for c in cols],
            )
            self._db.commit()

    def prune(self) -> None:
        if self.retention_days <= 0:
            return
        cutoff = int(time.time()) - self.retention_days * 86400
        with self._lock:
            self._db.execute("DELETE FROM minutes WHERE ts < ?", (cutoff,))
            self._db.commit()

    def history(self, hours: int, max_points: int = 720) -> dict:
        """Average power over evenly sized buckets covering the last `hours`."""
        now = int(time.time())
        start = now - hours * 3600
        bucket = max(60, (hours * 3600 // max_points) // 60 * 60)
        avgs = ", ".join(f"AVG({f}) AS {f}" for f in POWER_FIELDS)
        with self._lock:
            rows = self._db.execute(
                f"SELECT (ts / ?) * ? AS t, {avgs} FROM minutes "
                "WHERE ts >= ? GROUP BY t ORDER BY t",
                (bucket, bucket, start),
            ).fetchall()
        return {"bucket_seconds": bucket, "points": [dict(r) for r in rows]}

    def daily_energy(self, days: int) -> list[dict]:
        """kWh per local calendar day for the last `days` days (today included)."""
        today = datetime.now(self.tz).date()
        first = today - timedelta(days=days - 1)
        start = int(datetime.combine(first, datetime.min.time(), self.tz).timestamp())
        with self._lock:
            rows = self._db.execute(
                f"SELECT ts, {', '.join(ENERGY_FIELDS)} FROM minutes WHERE ts >= ?",
                (start,),
            ).fetchall()

        totals: dict[str, dict[str, float]] = {}
        for i in range(days):
            day = (first + timedelta(days=i)).isoformat()
            totals[day] = dict.fromkeys(ENERGY_FIELDS, 0.0)
        for r in rows:
            day = datetime.fromtimestamp(r["ts"], self.tz).date().isoformat()
            if day in totals:
                for f in ENERGY_FIELDS:
                    totals[day][f] += r[f] or 0.0

        return [
            {"date": day, **{f.replace("_wh", "_kwh"): round(v[f] / 1000, 3) for f in ENERGY_FIELDS}}
            for day, v in totals.items()
        ]

    def close(self) -> None:
        with self._lock:
            self._db.close()
