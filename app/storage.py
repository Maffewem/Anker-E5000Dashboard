"""SQLite history: one row per minute with average power and energy used."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

POWER_FIELDS = ("solar_w", "home_w", "battery_w", "grid_w", "soc")
# The highest and lowest single reading within each minute, so short spikes
# (a kettle, the battery ramping) aren't lost in the minute's average. Added
# after `minutes` was created, so older databases get them by migration.
PEAK_FIELDS = ("solar_w_max", "home_w_max", "grid_w_max", "grid_w_min", "battery_w_max", "battery_w_min")
# The battery's own lifetime counters (kWh, 0.1 resolution), last reading in
# each minute. They keep counting while the dashboard can't see the battery, so
# the energy of a gap in the history can be recovered from their difference.
COUNTER_FIELDS = ("solar_total_kwh", "charged_total_kwh", "discharged_total_kwh")
# Every poll as read, kept for SAMPLE_DAYS (a week by default).
SAMPLE_FIELDS = ("solar_w", "home_w", "battery_w", "grid_w", "ac_output_w", "soc")
ENERGY_FIELDS = (
    "solar_wh",
    "home_wh",
    "import_wh",
    "export_wh",
    "charge_wh",
    "discharge_wh",
)

# What each day summary holds besides the minutes recorded.
DAY_FIELDS = (
    "soc_min", "soc_max", "soc_avg",
    "solar_peak_w", "home_peak_w", "import_peak_w", "export_peak_w", "charge_peak_w", "discharge_peak_w",
    "voltage_min", "voltage_avg", "voltage_max",
)
TARIFF_PRICES = ("peak_rate", "offpeak_rate", "offpeak_start", "offpeak_end", "export_rate")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS minutes (
    ts INTEGER PRIMARY KEY,  -- unix seconds, start of the minute (UTC)
    {", ".join(f"{f} REAL" for f in POWER_FIELDS + ENERGY_FIELDS)}
    -- estimated (1) is added by migration: a minute filled in after a gap, not measured
);
-- Every reading as polled (every few seconds), for looking closely at a
-- recent spike. Pruned after SAMPLE_DAYS.
CREATE TABLE IF NOT EXISTS samples (
    ts REAL PRIMARY KEY,
    {", ".join(f"{f} REAL" for f in SAMPLE_FIELDS)}
);
-- One row per finished local day, worked out from the minute history before
-- it is pruned and kept for good: battery level range, peak power and grid
-- voltage, for long-term trends.
CREATE TABLE IF NOT EXISTS day_summaries (
    day TEXT PRIMARY KEY,
    minutes INTEGER NOT NULL,    -- minutes recorded
    {", ".join(f"{f} REAL" for f in DAY_FIELDS)}
);
-- The typed prices in force from each date, so past days stay priced at what
-- they cost when the prices in Edit costs change. Empty until they first do.
CREATE TABLE IF NOT EXISTS tariff_history (
    since TEXT PRIMARY KEY,      -- local date these prices apply from
    prices TEXT NOT NULL         -- JSON: peak_rate, offpeak_rate, offpeak_start, offpeak_end, export_rate
);
-- Weather per UTC hour from Open-Meteo for the location set, past and
-- forecast (forecast hours are overwritten as they pass). Kept for good.
CREATE TABLE IF NOT EXISTS weather (
    ts INTEGER PRIMARY KEY,      -- unix seconds, start of the hour
    temp_c REAL,
    cloud_pct REAL,
    radiation_wm2 REAL           -- sunshine on flat ground (global horizontal), hour average
);
-- Energy per local half hour, kept for good (not pruned), so the payback
-- and long-term trends can be worked out over the battery's whole life for
-- any tariff. Columns after solar_charge_wh were added later (see SLOT_EXTRA).
CREATE TABLE IF NOT EXISTS slots (
    day TEXT NOT NULL,       -- local date
    slot INTEGER NOT NULL,   -- half hour of the local day, 0-47
    discharge_wh REAL NOT NULL DEFAULT 0,
    grid_charge_wh REAL NOT NULL DEFAULT 0,
    solar_charge_wh REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, slot)
);
-- Battery capacity measured per local day: energy discharged over long
-- uninterrupted discharges and the battery level they took off. Kept for good
-- so the health trend outlives the minute history.
CREATE TABLE IF NOT EXISTS capacity_days (
    day TEXT PRIMARY KEY,
    discharge_wh REAL NOT NULL,  -- energy over the counted discharges
    soc_drop REAL NOT NULL,      -- battery level points they took off
    soc_min REAL,
    soc_max REAL
);
-- Smart Meter readings per minute: average power and voltage, plus the
-- meter's own lifetime import/export counters at the end of the minute.
CREATE TABLE IF NOT EXISTS meter_minutes (
    ts INTEGER PRIMARY KEY,
    grid_w REAL,
    voltage REAL,
    import_total_kwh REAL,
    export_total_kwh REAL
);
-- Prices per local half hour in p/kWh, from Octopus when it is connected.
CREATE TABLE IF NOT EXISTS rates (
    day TEXT NOT NULL,
    slot INTEGER NOT NULL,
    import_p REAL,
    export_p REAL,
    PRIMARY KEY (day, slot)
);
-- What happened and when: battery mode, charging and settings changes,
-- devices connecting and dropping, and actions taken on the battery.
-- Kept for good (it only grows on changes).
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,     -- unix seconds
    device TEXT NOT NULL,    -- battery, meter
    kind TEXT NOT NULL,      -- see EVENT_KINDS
    field TEXT,              -- the reading that changed, if any
    old TEXT,
    new TEXT,
    message TEXT NOT NULL,   -- one readable line
    source TEXT NOT NULL     -- who did it: "device" (seen while polling), "dashboard", ...
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
-- The last value seen for each watched reading, so a change made while the
-- dashboard was stopped still shows up when it starts again.
-- Half hours of a booked Octopus Happy Hour: grid power in them is free. Kept
-- after the day so past charging costs stay at 0p.
CREATE TABLE IF NOT EXISTS free_slots (
    day TEXT NOT NULL,
    slot INTEGER NOT NULL,
    PRIMARY KEY (day, slot)
);
CREATE TABLE IF NOT EXISTS event_state (
    device TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (device, field)
);
"""

# charging: started/stopped charging or discharging. mode: operating mode.
# setting: SOC limits, reserve, power limits. connection: online/offline or a
# new address. firmware: a firmware update. control: something the dashboard
# itself asked the battery to do. tariff: electricity prices or tariff changed.
EVENT_KINDS = ("charging", "mode", "setting", "connection", "firmware", "control", "tariff")

METER_FIELDS = ("grid_w", "voltage", "import_total_kwh", "export_total_kwh")

# Energy per half hour beyond the battery's own, for trends; added to `slots`
# after it was created, so older databases get them by migration.
SLOT_EXTRA = ("import_wh", "export_wh", "solar_wh", "home_wh", "discharge_export_wh")
SLOT_FIELDS = ("discharge_wh", "grid_charge_wh", "solar_charge_wh", *SLOT_EXTRA)

# A discharge counts towards the capacity estimate when it takes at least this
# many battery level points off without charging in between; shorter ones are
# swamped by the level being reported in whole percent.
CAPACITY_MIN_DROP = 20

# A gap is three or more missing minutes in a row. Gaps longer than MAX_GAP
# are left empty: estimating days would be fiction. Gaps are looked for within
# GAP_LOOKBACK of now.
GAP_SECONDS = 180
MAX_GAP = 3 * 86400
GAP_LOOKBACK = 14 * 86400


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
    peaks: dict[str, float] = field(default_factory=dict)
    counters: dict[str, float] = field(default_factory=dict)

    def add(self, snapshot: dict, seconds: float) -> None:
        for key in COUNTER_FIELDS:
            if snapshot.get(key) is not None:
                self.counters[key] = snapshot[key]
        for key in POWER_FIELDS:
            v = snapshot.get(key)
            if v is not None:
                self.sums[key] = self.sums.get(key, 0.0) + v
                self.counts[key] = self.counts.get(key, 0) + 1
        for name in PEAK_FIELDS:
            v = snapshot.get(name[:-4])
            if v is not None:
                pick = max if name.endswith("_max") else min
                self.peaks[name] = pick(self.peaks.get(name, v), v)

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
        row.update({k: self.peaks.get(k) for k in PEAK_FIELDS})
        row.update({k: self.counters.get(k) for k in COUNTER_FIELDS})
        return row


@dataclass
class MeterBucket:
    """One minute of Smart Meter samples."""

    minute: int
    sums: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    last: dict[str, float] = field(default_factory=dict)

    def add(self, snapshot: dict, seconds: float) -> None:
        phases = snapshot.get("phases") or [{}]
        averaged = {"grid_w": snapshot.get("grid_w"), "voltage": phases[0].get("voltage")}
        for key, v in averaged.items():
            if v is not None:
                self.sums[key] = self.sums.get(key, 0.0) + v
                self.counts[key] = self.counts.get(key, 0) + 1
        for key in ("import_total_kwh", "export_total_kwh"):
            if snapshot.get(key) is not None:
                self.last[key] = snapshot[key]

    def row(self) -> dict:
        row = {"ts": self.minute, **self.last}
        for key in ("grid_w", "voltage"):
            n = self.counts.get(key)
            row[key] = self.sums[key] / n if n else None
        return row


# What each export contains: (table, time column, columns), oldest first.
EXPORTS = {
    "minutes": ("minutes", "ts", ("ts", *POWER_FIELDS, *ENERGY_FIELDS, *PEAK_FIELDS, "estimated")),
    "samples": ("samples", "ts", ("ts", *SAMPLE_FIELDS)),
    "meter": ("meter_minutes", "ts", ("ts", *METER_FIELDS)),
    "events": ("events", "ts", ("ts", "device", "kind", "field", "old", "new", "message", "source")),
    "slots": ("slots", "day", ("day", "slot", *SLOT_FIELDS, "estimated_minutes")),
    "days": ("day_summaries", "day", ("day", "minutes", *DAY_FIELDS)),
    "weather": ("weather", "ts", ("ts", "temp_c", "cloud_pct", "radiation_wm2")),
}


def discharge_runs(rows) -> list[tuple[float, float]]:
    """(energy Wh, battery level points) for each uninterrupted discharge.

    A run ends at a gap in the readings or at any minute that charges or
    stands idle, so the level drop belongs to that energy alone.
    """
    runs: list[tuple[float, float]] = []
    wh = 0.0
    top = last = prev_ts = None

    def close() -> None:
        if top is not None and last is not None and wh > 0:
            runs.append((wh, top - last))

    for r in rows:
        discharging = (r["discharge_wh"] or 0) > 0 and not (r["charge_wh"] or 0) and r["soc"] is not None
        if not discharging or (prev_ts is not None and r["ts"] - prev_ts > 120):
            close()
            wh, top, last = 0.0, None, None
        if discharging:
            if top is None:
                top = r["soc"]
            wh += r["discharge_wh"]
            last = r["soc"]
        prev_ts = r["ts"]
    close()
    return runs


class Storage:
    def __init__(self, path: str, retention_days: int, timezone: str = "UTC", sample_days: int = 7) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self.retention_days = retention_days
        self.sample_days = sample_days
        try:
            self.tz = ZoneInfo(timezone)
        except Exception:
            self.tz = ZoneInfo("UTC")
        self._migrate_slots()
        self._backfill_slots()
        self._add_columns("minutes", PEAK_FIELDS + COUNTER_FIELDS)
        self._add_columns("minutes", ("estimated",), "INTEGER NOT NULL DEFAULT 0")
        self._add_columns("slots", ("estimated_minutes",), "INTEGER NOT NULL DEFAULT 0")

    def _add_columns(self, table: str, names: tuple[str, ...], kind: str = "REAL") -> None:
        with self._lock:
            have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            for name in names:
                if name not in have:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
            self._db.commit()

    def write_sample(self, ts: float, snapshot: dict) -> None:
        """One poll's readings, as read."""
        if self.sample_days <= 0:
            return
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO samples (ts, {', '.join(SAMPLE_FIELDS)}) VALUES (?{', ?' * len(SAMPLE_FIELDS)})",
                (round(ts, 1), *(snapshot.get(f) for f in SAMPLE_FIELDS)),
            )
            self._db.commit()

    def write_minute(self, row: dict) -> None:
        cols = ["ts", *POWER_FIELDS, *ENERGY_FIELDS, *PEAK_FIELDS, *COUNTER_FIELDS]
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO minutes ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [row.get(c) for c in cols],
            )
            self._add_slot(row)
            self._db.commit()

    def write_meter_minute(self, row: dict) -> None:
        cols = ["ts", *METER_FIELDS]
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO meter_minutes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [row.get(c) for c in cols],
            )
            self._db.commit()

    def record_event(
        self,
        kind: str,
        message: str,
        *,
        device: str = "battery",
        field: str | None = None,
        old: object = None,
        new: object = None,
        source: str = "device",
        ts: float | None = None,
    ) -> None:
        """Add one line to the event log. Anything that changes the battery should call this."""
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        text = lambda v: None if v is None else str(v)  # noqa: E731
        with self._lock:
            self._db.execute(
                "INSERT INTO events (ts, device, kind, field, old, new, message, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (int(ts if ts is not None else time.time()), device, kind, field, text(old), text(new), message, source),
            )
            self._db.commit()

    def events(self, limit: int = 100, before: int | None = None, kinds: list[str] | None = None,
               offset: int = 0) -> list[dict]:
        """Newest first; page back with `offset` or `before` (an event id)."""
        where, args = self._event_filter(before, kinds)
        sql = "SELECT * FROM events" + where + " ORDER BY id DESC LIMIT ? OFFSET ?"
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, (*args, limit, offset)).fetchall()]

    def count_events(self, kinds: list[str] | None = None) -> int:
        where, args = self._event_filter(None, kinds)
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM events" + where, args).fetchone()[0]

    @staticmethod
    def _event_filter(before: int | None, kinds: list[str] | None) -> tuple[str, list]:
        where, args = [], []
        if before is not None:
            where.append("id < ?")
            args.append(before)
        if kinds:
            where.append(f"kind IN ({','.join('?' * len(kinds))})")
            args.extend(kinds)
        return (" WHERE " + " AND ".join(where) if where else ""), args

    def event_state(self, device: str) -> dict[str, str | None]:
        with self._lock:
            rows = self._db.execute("SELECT field, value FROM event_state WHERE device = ?", (device,)).fetchall()
        return {r["field"]: r["value"] for r in rows}

    def save_event_state(self, device: str, field: str, value: object) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO event_state (device, field, value) VALUES (?, ?, ?)",
                (device, field, None if value is None else str(value)),
            )
            self._db.commit()

    def _add_slot(self, row: dict, fields: tuple[str, ...] = SLOT_FIELDS, estimated: bool = False) -> None:
        charge = row.get("charge_wh") or 0.0
        # Charging while importing is counted as grid charging; the rest came from solar.
        grid_charge = min(charge, row.get("import_wh") or 0.0)
        discharge = row.get("discharge_wh") or 0.0
        # Discharge beyond what the house used went to the grid and earned the
        # export price, not the import price. Solar surplus goes out first.
        surplus = max(0.0, (row.get("solar_wh") or 0.0) - (row.get("home_wh") or 0.0))
        discharge_export = min(discharge, max(0.0, (row.get("export_wh") or 0.0) - surplus))
        values = {
            "discharge_wh": discharge,
            "discharge_export_wh": discharge_export,
            "grid_charge_wh": grid_charge,
            "solar_charge_wh": charge - grid_charge,
            **{f: row.get(f) or 0.0 for f in SLOT_EXTRA if f != "discharge_export_wh"},
        }
        values = {f: values[f] for f in fields}
        if not any(values.values()):
            return
        if estimated:
            values["estimated_minutes"] = 1
        local = datetime.fromtimestamp(row["ts"], self.tz)
        cols = ", ".join(values)
        self._db.execute(
            f"INSERT INTO slots (day, slot, {cols}) VALUES (?, ?{', ?' * len(values)}) "
            f"ON CONFLICT (day, slot) DO UPDATE SET {', '.join(f'{f} = {f} + excluded.{f}' for f in values)}",
            (local.date().isoformat(), (local.hour * 60 + local.minute) // 30, *values.values()),
        )

    def _migrate_slots(self) -> None:
        """Add the trend columns to an older `slots` table, filled from the minute history still kept."""
        with self._lock:
            have = {r["name"] for r in self._db.execute("PRAGMA table_info(slots)")}
            missing = [f for f in SLOT_EXTRA if f not in have]
            if not missing:
                return
            for f in missing:
                self._db.execute(f"ALTER TABLE slots ADD COLUMN {f} REAL NOT NULL DEFAULT 0")
            if self._db.execute("SELECT 1 FROM slots LIMIT 1").fetchone():
                rows = self._db.execute(f"SELECT ts, {', '.join(ENERGY_FIELDS)} FROM minutes").fetchall()
                for r in rows:
                    self._add_slot(dict(r), tuple(missing))
            self._db.commit()

    def _backfill_slots(self) -> None:
        """Fill the half-hour table from minute history recorded before it existed."""
        with self._lock:
            if self._db.execute("SELECT 1 FROM slots LIMIT 1").fetchone():
                return
            rows = self._db.execute(f"SELECT ts, {', '.join(ENERGY_FIELDS)} FROM minutes").fetchall()
            for r in rows:
                self._add_slot(dict(r))
            self._db.commit()

    def battery_power_since(self, start: int) -> list[tuple[int, float | None]]:
        """(ts, average battery W) for every recorded minute since `start`."""
        with self._lock:
            return [tuple(r) for r in self._db.execute(
                "SELECT ts, battery_w FROM minutes WHERE ts >= ?", (start,)
            ).fetchall()]

    def battery_slots(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT day, slot, discharge_wh, discharge_export_wh, grid_charge_wh, solar_charge_wh, import_wh FROM slots"
            ).fetchall()
        return [dict(r) for r in rows]

    def energy_slots(self) -> list[dict]:
        """Every recorded half hour with all its energy totals, for trends."""
        with self._lock:
            rows = self._db.execute(f"SELECT day, slot, {', '.join(SLOT_FIELDS)} FROM slots").fetchall()
        return [dict(r) for r in rows]

    def capacity_days(self) -> list[dict]:
        """Measured capacity per day, oldest first, measuring any finished day not done yet."""
        today = datetime.now(self.tz).date()
        with self._lock:
            done = {r[0] for r in self._db.execute("SELECT day FROM capacity_days")}
            first = self._db.execute("SELECT MIN(ts) FROM minutes").fetchone()[0]
        if first is not None:
            day = datetime.fromtimestamp(first, self.tz).date()
            while day < today:
                if day.isoformat() not in done:
                    self._measure_capacity(day)
                day += timedelta(days=1)
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM capacity_days ORDER BY day")]

    def _measure_capacity(self, day: date) -> None:
        start, end = self._day_start(day), self._day_start(day + timedelta(days=1))
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, soc, charge_wh, discharge_wh FROM minutes WHERE ts >= ? AND ts < ? AND NOT estimated ORDER BY ts",
                (start, end),
            ).fetchall()
        if not rows:
            return
        socs = [r["soc"] for r in rows if r["soc"] is not None]
        energy = drop = 0.0
        for run_wh, run_drop in discharge_runs(rows):
            if run_drop >= CAPACITY_MIN_DROP:
                energy += run_wh
                drop += run_drop
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO capacity_days (day, discharge_wh, soc_drop, soc_min, soc_max) VALUES (?, ?, ?, ?, ?)",
                (day.isoformat(), energy, drop, min(socs) if socs else None, max(socs) if socs else None),
            )
            self._db.commit()

    def usage_slots(self, days: int = 365) -> list[dict]:
        """Home use and solar per local half hour, for comparing tariffs.

        These don't depend on what the battery did, so they can be replayed
        against any tariff and battery schedule.
        """
        since = int(time.time()) - days * 86400
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, home_wh, solar_wh FROM minutes WHERE ts >= ? ORDER BY ts", (since,)
            ).fetchall()
        out: dict[tuple[str, int], dict] = {}
        for r in rows:
            local = datetime.fromtimestamp(r["ts"], self.tz)
            key = (local.date().isoformat(), (local.hour * 60 + local.minute) // 30)
            slot = out.setdefault(key, {"day": key[0], "slot": key[1], "home_wh": 0.0, "solar_wh": 0.0, "minutes": 0})
            slot["home_wh"] += r["home_wh"] or 0.0
            slot["solar_wh"] += r["solar_wh"] or 0.0
            slot["minutes"] += 1
        return list(out.values())

    def soc_stats(self, days: int = 30) -> dict:
        """Minutes spent nearly full and nearly empty, for battery care tips."""
        since = int(time.time()) - days * 86400
        with self._lock:
            r = self._db.execute(
                "SELECT COUNT(soc) AS n, SUM(soc >= 98) AS full, SUM(soc <= 7) AS empty, AVG(soc) AS avg, "
                "MIN(ts) AS first FROM minutes WHERE ts >= ? AND soc IS NOT NULL AND NOT estimated", (since,)
            ).fetchone()
        n = r["n"] or 0
        return {
            "minutes": n,
            "days": round((time.time() - r["first"]) / 86400, 1) if r["first"] else 0,
            "full_share": (r["full"] or 0) / n if n else None,
            "empty_share": (r["empty"] or 0) / n if n else None,
            "avg_soc": round(r["avg"], 1) if r["avg"] is not None else None,
        }

    def last_full(self, soc: float = 99) -> float | None:
        """Unix time the battery was last at or above `soc`%, from the minutes or the older daily summaries."""
        with self._lock:
            r = self._db.execute("SELECT MAX(ts) FROM minutes WHERE soc >= ? AND NOT estimated", (soc,)).fetchone()[0]
            if r is not None:
                return float(r)
            day = self._db.execute("SELECT MAX(day) FROM day_summaries WHERE soc_max >= ?", (soc,)).fetchone()[0]
        if day is None:
            return None
        return datetime.fromisoformat(day).replace(hour=12, tzinfo=self.tz).timestamp()

    def first_slot_day(self) -> str | None:
        with self._lock:
            return self._db.execute("SELECT MIN(day) FROM slots").fetchone()[0]

    def save_rates(self, rows: list[tuple]) -> None:
        """(day, slot, import_p, export_p); a None leaves that price as it was."""
        with self._lock:
            self._db.executemany(
                "INSERT INTO rates (day, slot, import_p, export_p) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (day, slot) DO UPDATE SET import_p = COALESCE(excluded.import_p, import_p), "
                "export_p = COALESCE(excluded.export_p, export_p)",
                rows,
            )
            self._db.commit()

    def set_free_slots(self, slots: set[tuple[str, int]], from_day: str) -> None:
        """Make `slots` the free half hours from `from_day` on; earlier ones are history and stay."""
        with self._lock:
            self._db.execute("DELETE FROM free_slots WHERE day >= ?", (from_day,))
            self._db.executemany("INSERT OR IGNORE INTO free_slots (day, slot) VALUES (?, ?)", sorted(slots))
            self._db.commit()

    def free_slots(self) -> set[tuple[str, int]]:
        with self._lock:
            return {(r["day"], r["slot"]) for r in self._db.execute("SELECT day, slot FROM free_slots")}

    def rates(self, since: str = "") -> dict[tuple[str, int], tuple]:
        with self._lock:
            rows = self._db.execute("SELECT day, slot, import_p, export_p FROM rates WHERE day >= ?", (since,)).fetchall()
        return {(r["day"], r["slot"]): (r["import_p"], r["export_p"]) for r in rows}

    def rate_days(self) -> tuple[str, str] | None:
        with self._lock:
            lo, hi = self._db.execute("SELECT MIN(day), MAX(day) FROM rates").fetchone()
        return (lo, hi) if lo else None

    def clear_rates(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM rates")
            self._db.commit()

    def fill_gaps(self, now: float | None = None) -> list[dict]:
        """Estimate the minutes missing between recorded ones, e.g. after a network outage.

        Looks back GAP_LOOKBACK for runs of three or more missing minutes and
        fills each with rows marked `estimated`. Real rows are never touched,
        so this is safe to call as often as wanted. Returns one dict per gap
        filled: start, end (unix seconds) and the method used.
        """
        now = now if now is not None else time.time()
        with self._lock:
            stamps = [r[0] for r in self._db.execute(
                "SELECT ts FROM minutes WHERE ts >= ? AND NOT estimated ORDER BY ts", (int(now - GAP_LOOKBACK),))]
        filled = []
        for a, b in zip(stamps, stamps[1:]):
            if GAP_SECONDS < b - a <= MAX_GAP:
                result = self._fill_gap(a, b)
                if result:
                    filled.append(result)
        return filled

    def _fill_gap(self, a: int, b: int) -> dict | None:
        with self._lock:
            if self._db.execute("SELECT 1 FROM minutes WHERE ts > ? AND ts < ?", (a, b)).fetchone():
                return None  # already filled
            ra = dict(self._db.execute("SELECT * FROM minutes WHERE ts = ?", (a,)).fetchone())
            rb = dict(self._db.execute("SELECT * FROM minutes WHERE ts = ?", (b,)).fetchone())
            meter_a = self._db.execute(
                "SELECT ts, import_total_kwh, export_total_kwh FROM meter_minutes WHERE ts <= ? AND ts >= ? "
                "AND import_total_kwh IS NOT NULL ORDER BY ts DESC LIMIT 1", (a, a - 120)).fetchone()
            meter_b = self._db.execute(
                "SELECT ts, import_total_kwh, export_total_kwh FROM meter_minutes WHERE ts >= ? AND ts <= ? "
                "AND import_total_kwh IS NOT NULL ORDER BY ts LIMIT 1", (b, b + 120)).fetchone()
        stamps = list(range(a + 60, b, 60))
        hours = (b - a) / 3600
        # Nothing here exceeds what the inverter can do; a bigger counter jump is a reset or a glitch.
        plausible = lambda kwh: kwh is not None and 0 <= kwh <= 20 * hours  # noqa: E731

        def lerp(field: str) -> list[float | None]:
            lo, hi = ra.get(field), rb.get(field)
            if lo is None and hi is None:
                return [None] * len(stamps)
            lo, hi = (hi if lo is None else lo), (lo if hi is None else hi)
            return [lo + (hi - lo) * (t - a) / (b - a) for t in stamps]

        solar_w, home_w, grid_w, batt_w, soc = (lerp(f) for f in ("solar_w", "home_w", "grid_w", "battery_w", "soc"))
        positive = lambda w, sign=1: [max(0.0, sign * (x or 0.0)) / 60 for x in w]  # noqa: E731
        wh = {
            "solar_wh": positive(solar_w), "home_wh": positive(home_w),
            "import_wh": positive(grid_w), "export_wh": positive(grid_w, -1),
            "discharge_wh": positive(batt_w), "charge_wh": positive(batt_w, -1),
        }

        def delta(lo, hi) -> float | None:
            kwh = None if lo is None or hi is None else hi - lo
            return kwh * 1000 if plausible(kwh) else None

        counted = {
            "solar_wh": delta(ra.get("solar_total_kwh"), rb.get("solar_total_kwh")),
            "charge_wh": delta(ra.get("charged_total_kwh"), rb.get("charged_total_kwh")),
            "discharge_wh": delta(ra.get("discharged_total_kwh"), rb.get("discharged_total_kwh")),
        }
        if meter_a and meter_b:
            counted["import_wh"] = delta(meter_a["import_total_kwh"], meter_b["import_total_kwh"])
            counted["export_wh"] = delta(meter_a["export_total_kwh"], meter_b["export_total_kwh"])
        used = [f for f, total in counted.items() if total is not None]
        for f in used:
            shape = wh[f]
            weight = sum(shape)
            wh[f] = [counted[f] * (x / weight if weight > 0 else 1 / len(stamps)) for x in shape]
        if used:
            # The house used whatever came in and didn't go out or into the battery.
            wh["home_wh"] = [
                max(0.0, wh["solar_wh"][i] + wh["discharge_wh"][i] + wh["import_wh"][i]
                    - wh["charge_wh"][i] - wh["export_wh"][i])
                for i in range(len(stamps))
            ]

        with self._lock:
            for i, ts in enumerate(stamps):
                row = {"ts": ts, **{f: wh[f][i] for f in ENERGY_FIELDS}}
                row.update({
                    "solar_w": row["solar_wh"] * 60, "home_w": row["home_wh"] * 60,
                    "grid_w": (row["import_wh"] - row["export_wh"]) * 60,
                    "battery_w": (row["discharge_wh"] - row["charge_wh"]) * 60,
                    "soc": soc[i],
                })
                cols = ["ts", *POWER_FIELDS, *ENERGY_FIELDS]
                cur = self._db.execute(
                    f"INSERT OR IGNORE INTO minutes ({','.join(cols)}, estimated) VALUES ({','.join('?' * len(cols))}, 1)",
                    [row.get(c) for c in cols])
                if cur.rowcount:
                    self._add_slot(row, estimated=True)
            self._db.commit()
        method = "lifetime counters" if used else "readings either side of it"
        start, end = datetime.fromtimestamp(a + 60, self.tz), datetime.fromtimestamp(b, self.tz)
        mins = len(stamps)
        length = f"{mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"{mins}m"
        clock = "%H:%M" if start.date() == end.date() else "%d %b %H:%M"
        self.record_event(
            "connection",
            f"No readings recorded {start.strftime(clock)} to {end.strftime('%H:%M')} ({length}); filled with an estimate from {method}",
            source="dashboard", ts=a + 60,
        )
        return {"start": a + 60, "end": b, "minutes": mins, "method": method, "counters": sorted(used)}

    def prune(self) -> None:
        # Summarise finished days first, so nothing goes before it is kept.
        self.day_summaries()
        now = int(time.time())
        with self._lock:
            if self.sample_days > 0:
                self._db.execute("DELETE FROM samples WHERE ts < ?", (now - self.sample_days * 86400,))
            if self.retention_days > 0:
                cutoff = now - self.retention_days * 86400
                self._db.execute("DELETE FROM minutes WHERE ts < ?", (cutoff,))
                self._db.execute("DELETE FROM meter_minutes WHERE ts < ?", (cutoff,))
            self._db.commit()

    def day_summaries(self, include_today: bool = False) -> list[dict]:
        """Per-day battery level range, peaks and voltage, oldest first.

        Finished days are worked out once and kept; today, if asked for, is
        worked out afresh each time.
        """
        today = datetime.now(self.tz).date()
        with self._lock:
            done = {r[0] for r in self._db.execute("SELECT day FROM day_summaries")}
            first = self._db.execute("SELECT MIN(ts) FROM minutes").fetchone()[0]
        if first is not None:
            day = datetime.fromtimestamp(first, self.tz).date()
            while day < today:
                if day.isoformat() not in done:
                    row = self._summarise(day)
                    if row:
                        cols = ["day", "minutes", *DAY_FIELDS]
                        with self._lock:
                            self._db.execute(
                                f"INSERT OR REPLACE INTO day_summaries ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                [row[c] for c in cols],
                            )
                            self._db.commit()
                day += timedelta(days=1)
        with self._lock:
            rows = [dict(r) for r in self._db.execute("SELECT * FROM day_summaries ORDER BY day")]
        if include_today:
            row = self._summarise(today)
            if row:
                rows.append(row)
        return rows

    def _summarise(self, day: date) -> dict | None:
        start, end = self._day_start(day), self._day_start(day + timedelta(days=1))
        # Minutes recorded before the peak columns existed only have averages.
        peak = lambda name, avg: f"COALESCE({name}, {avg})"  # noqa: E731
        with self._lock:
            r = self._db.execute(
                "SELECT COUNT(*) AS minutes, MIN(soc) AS soc_min, MAX(soc) AS soc_max, AVG(soc) AS soc_avg, "
                f"MAX({peak('solar_w_max', 'solar_w')}) AS solar_peak_w, MAX({peak('home_w_max', 'home_w')}) AS home_peak_w, "
                f"MAX({peak('grid_w_max', 'grid_w')}) AS import_peak_w, -MIN({peak('grid_w_min', 'grid_w')}) AS export_peak_w, "
                f"-MIN({peak('battery_w_min', 'battery_w')}) AS charge_peak_w, "
                f"MAX({peak('battery_w_max', 'battery_w')}) AS discharge_peak_w "
                "FROM minutes WHERE ts >= ? AND ts < ?", (start, end),
            ).fetchone()
            v = self._db.execute(
                "SELECT MIN(voltage) AS voltage_min, AVG(voltage) AS voltage_avg, MAX(voltage) AS voltage_max "
                "FROM meter_minutes WHERE ts >= ? AND ts < ?", (start, end),
            ).fetchone()
        if not r["minutes"]:
            return None
        row = {"day": day.isoformat(), **dict(r), **dict(v)}
        for k in DAY_FIELDS:
            if row[k] is not None:
                row[k] = round(max(0.0, row[k]) if k.endswith("_peak_w") else row[k], 1)
        return row

    def tariff_history(self) -> list[tuple[str, dict]]:
        """(local date, typed prices) for each change of the typed prices, oldest first."""
        with self._lock:
            rows = self._db.execute("SELECT since, prices FROM tariff_history ORDER BY since").fetchall()
        return [(r["since"], json.loads(r["prices"])) for r in rows]

    def save_tariff_change(self, old: dict, new: dict, since: str) -> None:
        """New typed prices from local date `since`; days before it keep the ones they had.

        The first change also records the prices before it, as applying to
        everything earlier. A later change on or before an earlier one's date
        replaces it.
        """
        pick = lambda t: json.dumps({k: t.get(k) for k in TARIFF_PRICES})  # noqa: E731
        with self._lock:
            if not self._db.execute("SELECT 1 FROM tariff_history LIMIT 1").fetchone():
                self._db.execute("INSERT INTO tariff_history (since, prices) VALUES ('', ?)", (pick(old),))
            self._db.execute("DELETE FROM tariff_history WHERE since >= ? AND since != ''", (since,))
            self._db.execute("INSERT OR REPLACE INTO tariff_history (since, prices) VALUES (?, ?)", (since, pick(new)))
            self._db.commit()

    def save_weather(self, rows: list[tuple]) -> None:
        """(hour start unix seconds, temp °C, cloud %, sunshine W/m²)."""
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO weather (ts, temp_c, cloud_pct, radiation_wm2) VALUES (?, ?, ?, ?)", rows)
            self._db.commit()

    def weather_days(self, first: date | None = None) -> dict[str, dict]:
        """Weather per local day: sunshine in kWh/m², temperatures and average cloud."""
        start = self._day_start(first) if first else 0
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, temp_c, cloud_pct, radiation_wm2 FROM weather WHERE ts >= ? ORDER BY ts", (start,)).fetchall()
        days: dict[str, dict] = {}
        for r in rows:
            day = datetime.fromtimestamp(r["ts"], self.tz).date().isoformat()
            d = days.setdefault(day, {"hours": 0, "sun_kwh_m2": 0.0, "temps": [], "clouds": []})
            d["hours"] += 1
            d["sun_kwh_m2"] += (r["radiation_wm2"] or 0.0) / 1000
            if r["temp_c"] is not None:
                d["temps"].append(r["temp_c"])
            if r["cloud_pct"] is not None:
                d["clouds"].append(r["cloud_pct"])
        out = {}
        for day, d in days.items():
            t, c = d["temps"], d["clouds"]
            out[day] = {"hours": d["hours"], "sun_kwh_m2": round(d["sun_kwh_m2"], 2),
                        "temp_min": min(t) if t else None, "temp_max": max(t) if t else None,
                        "temp_avg": round(sum(t) / len(t), 1) if t else None,
                        "cloud_avg": round(sum(c) / len(c)) if c else None}
        return out

    def history(self, hours: int, max_points: int = 720, offset_hours: int = 0) -> dict:
        """Average power over evenly sized buckets covering the last `hours`.

        `offset_hours` moves the window back in time, e.g. the previous day
        for comparison; timestamps stay the real ones.
        """
        end = int(time.time()) - offset_hours * 3600
        start = end - hours * 3600
        bucket = max(60, (hours * 3600 // max_points) // 60 * 60)
        avgs = ", ".join(f"AVG({f}) AS {f}" for f in POWER_FIELDS) + ", MAX(estimated) AS estimated"
        with self._lock:
            rows = self._db.execute(
                f"SELECT (ts / ?) * ? AS t, {avgs} FROM minutes "
                "WHERE ts >= ? AND ts <= ? GROUP BY t ORDER BY t",
                (bucket, bucket, start, end),
            ).fetchall()
        return {"bucket_seconds": bucket, "points": [dict(r) for r in rows]}

    def daily_energy(self, days: int) -> list[dict]:
        """kWh per local calendar day for the last `days` days (today included)."""
        today = datetime.now(self.tz).date()
        return self.daily_range(today - timedelta(days=days - 1), today)

    def _day_start(self, day: date) -> int:
        return int(datetime.combine(day, datetime.min.time(), self.tz).timestamp())

    def daily_range(self, first: date, last: date) -> list[dict]:
        """kWh per local calendar day from `first` to `last`, both included."""
        days = (last - first).days + 1
        with self._lock:
            rows = self._db.execute(
                f"SELECT ts, estimated, {', '.join(ENERGY_FIELDS)} FROM minutes WHERE ts >= ? AND ts < ?",
                (self._day_start(first), self._day_start(last + timedelta(days=1))),
            ).fetchall()

        totals: dict[str, dict[str, float]] = {}
        for i in range(days):
            day = (first + timedelta(days=i)).isoformat()
            totals[day] = {**dict.fromkeys(ENERGY_FIELDS, 0.0), "estimated": 0}
        for r in rows:
            day = datetime.fromtimestamp(r["ts"], self.tz).date().isoformat()
            if day in totals:
                totals[day]["estimated"] += r["estimated"] or 0
                for f in ENERGY_FIELDS:
                    totals[day][f] += r[f] or 0.0

        return [
            {"date": day, **{f.replace("_wh", "_kwh"): round(v[f] / 1000, 3) for f in ENERGY_FIELDS},
             "estimated_minutes": int(v["estimated"])}
            for day, v in totals.items()
        ]

    def export_rows(self, kind: str, first: date, last: date) -> tuple[tuple[str, ...], list[dict]]:
        """Rows of one export for local dates `first` to `last`, with a local time column."""
        if kind == "daily":
            rows = self.daily_range(first, last)
            return tuple(rows[0]) if rows else ("date",), rows
        table, key, cols = EXPORTS[kind]
        if key == "day":
            bounds: tuple = (first.isoformat(), last.isoformat())
            where = "day >= ? AND day <= ?"
        else:
            bounds = (self._day_start(first), self._day_start(last + timedelta(days=1)))
            where = "ts >= ? AND ts < ?"
        with self._lock:
            rows = [dict(r) for r in self._db.execute(
                f"SELECT {', '.join(cols)} FROM {table} WHERE {where} ORDER BY {', '.join(cols[:2] if key == 'day' else cols[:1])}",
                bounds,
            ).fetchall()]
        if key == "day":
            return cols, rows
        for r in rows:
            r["time"] = datetime.fromtimestamp(r["ts"], self.tz).isoformat()
        return ("time", *cols), rows

    def backup(self, path: str) -> None:
        """A consistent copy of the whole database, safe while recording continues."""
        target = sqlite3.connect(path)
        try:
            with self._lock:
                self._db.backup(target)
        finally:
            target.close()

    def close(self) -> None:
        with self._lock:
            self._db.close()
