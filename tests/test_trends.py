import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.storage import Storage, discharge_runs
from app.tariff import Tariff
from app.trends import daily_totals, health, month_projection, payback_outlook, periods, savings_curve

TARIFF = Tariff(battery_cost=100, peak_rate=30, offpeak_rate=10, offpeak_start="00:00", offpeak_end="05:00",
                export_rate=15)


def slot(day, slot, **wh):
    return {"day": day, "slot": slot, **{k: wh.get(k, 0.0) for k in (
        "discharge_wh", "grid_charge_wh", "solar_charge_wh", "import_wh", "export_wh", "solar_wh", "home_wh")}}


def test_daily_totals_price_each_half_hour():
    days = daily_totals(TARIFF, [
        slot("2026-10-01", 2, import_wh=5000, grid_charge_wh=5000),  # 01:00, off-peak
        slot("2026-10-01", 36, import_wh=1000, discharge_wh=4000),  # 18:00, peak
        slot("2026-10-01", 26, export_wh=2000, solar_wh=6000, solar_charge_wh=2000),
    ])
    d = days["2026-10-01"]
    assert d["import_cost"] == pytest.approx(0.5 + 0.3)
    assert d["export_earned"] == pytest.approx(0.3)
    assert d["net_cost"] == pytest.approx(0.5)
    assert d["battery_saved"] == pytest.approx(4 * 0.3 - 5 * 0.1 - 2 * 0.15)
    assert d["charge"] == 7 and d["solar"] == 6
    # Fetched prices win over the tariff.
    priced = daily_totals(TARIFF, [slot("2026-10-01", 36, import_wh=1000)], {("2026-10-01", 36): (50.0, None)})
    assert priced["2026-10-01"]["import_cost"] == pytest.approx(0.5)


def test_periods_group_by_week_and_month():
    days = daily_totals(TARIFF, [slot(f"2026-10-{d:02d}", 20, solar_wh=1000) for d in range(1, 21)])
    weeks = periods(days, "week", date(2026, 10, 20), 3)
    assert [w["start"] for w in weeks] == ["2026-10-05", "2026-10-12", "2026-10-19"]  # Mondays
    assert [w["solar"] for w in weeks] == [7, 7, 2]
    assert weeks[-1]["current"] and not weeks[0]["current"]
    avg = {k: 1.0 for k in weeks[0] if k in days["2026-10-01"]}
    projected = periods(days, "week", date(2026, 10, 20), 3, avg)
    assert projected[-1]["projected"]["solar"] == 1 + 6  # Monday recorded, then 1 a day from today to Sunday
    assert "projected" not in projected[0]
    # Nothing is shown from before the first recorded day.
    months = periods(days, "month", date(2026, 10, 20), 2)
    assert [(m["start"], m["end"], m["days"]) for m in months] == [("2026-10-01", "2026-10-31", 20)]
    assert len(periods(days, "month", date(2026, 11, 3), 6)) == 2


def test_month_projection_uses_recent_days():
    rows = [slot(f"2026-10-{d:02d}", 36, import_wh=10000) for d in range(1, 21)]  # £3 a day
    rows.append(slot("2026-09-30", 36, import_wh=10000))
    p = month_projection(daily_totals(TARIFF, rows), date(2026, 10, 20))
    assert p["so_far"]["import_cost"] == 60  # today counts as recorded so far
    assert p["days_left"] == 12  # today to the 31st
    assert p["projected"]["import_cost"] == pytest.approx(19 * 3 + 12 * 3)
    assert p["last_month"]["import_cost"] == 3 and p["last_month_days"] == 1
    assert month_projection({}, date(2026, 10, 20))["projected"] is None


def test_payback_outlook_and_curve():
    days = daily_totals(TARIFF, [slot(f"2026-10-{d:02d}", 36, discharge_wh=5000) for d in range(1, 15)])  # £1.50 a day
    curve = savings_curve(days, 10.0)
    assert curve[0]["saved"] == 11.5 and curve[-1]["saved"] == 31.0
    out = payback_outlook(curve, 100, days, date(2026, 10, 15))
    assert out["recent_per_day"] == 1.5
    assert out["recent_date"] == "2026-11-30"  # £69 left at £1.50 a day: 46 days
    assert payback_outlook(curve, 20, days, date(2026, 10, 15))["recent_date"] == "2026-10-15"


def test_discharge_runs_split_on_charging_and_gaps():
    rows = [{"ts": i * 60, "soc": 80 - i, "charge_wh": 0, "discharge_wh": 50} for i in range(30)]
    rows.append({"ts": 30 * 60, "soc": 50, "charge_wh": 10, "discharge_wh": 0})
    rows += [{"ts": t * 60, "soc": 60, "charge_wh": 0, "discharge_wh": 50} for t in (40, 41)]
    rows.append({"ts": 50 * 60, "soc": 59, "charge_wh": 0, "discharge_wh": 50})  # after a gap
    assert discharge_runs(rows) == [(1500, 29), (100, 0), (50, 0)]


def test_capacity_measured_per_finished_day():
    tz = ZoneInfo("UTC")
    s = Storage(":memory:", 0, "UTC")
    start = int(datetime(2026, 10, 1, 18, 0, tzinfo=tz).timestamp())
    for i in range(50):  # 50 points over 50 minutes, 100 Wh each: 10 kWh battery
        s.write_minute({"ts": start + i * 60, "soc": 90 - i, "discharge_wh": 100.0})
    caps = s.capacity_days()
    assert caps[0]["day"] == "2026-10-01"
    assert caps[0]["soc_drop"] == 49 and caps[0]["soc_min"] == 41
    days = daily_totals(TARIFF, [slot("2026-09-30", 0, solar_wh=1000), slot("2026-10-01", 36, discharge_wh=5000)])
    h = health(caps, "month", date(2026, 10, 20), 2, 10.0, days)
    assert h["periods"][-1]["capacity_kwh"] == pytest.approx(5.0 / 0.49, abs=0.01)
    assert h["periods"][-1]["cycles"] == 0.5
    assert h["periods"][0]["capacity_kwh"] is None  # September: nothing measured


def test_slots_keep_trend_energy_and_older_databases_migrate(tmp_path):
    s = Storage(":memory:", 0, "UTC")
    s.write_minute({"ts": 0, "solar_wh": 10.0, "home_wh": 4.0, "export_wh": 6.0})
    assert s.energy_slots() == [{"day": "1970-01-01", "slot": 0, "discharge_wh": 0, "grid_charge_wh": 0,
                                 "solar_charge_wh": 0, "import_wh": 0, "export_wh": 6.0, "solar_wh": 10.0,
                                 "home_wh": 4.0}]

    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE minutes (ts INTEGER PRIMARY KEY, solar_w REAL, home_w REAL, battery_w REAL, grid_w REAL,
            soc REAL, solar_wh REAL, home_wh REAL, import_wh REAL, export_wh REAL, charge_wh REAL, discharge_wh REAL);
        CREATE TABLE slots (day TEXT NOT NULL, slot INTEGER NOT NULL, discharge_wh REAL NOT NULL DEFAULT 0,
            grid_charge_wh REAL NOT NULL DEFAULT 0, solar_charge_wh REAL NOT NULL DEFAULT 0, PRIMARY KEY (day, slot));
        INSERT INTO minutes (ts, solar_wh, import_wh, charge_wh) VALUES (0, 20, 3, 3), (60, 20, 0, 0);
        INSERT INTO slots VALUES ('1970-01-01', 0, 0, 3, 0), ('1960-01-01', 0, 7, 0, 0);
    """)
    db.commit()
    db.close()
    rows = {r["day"]: r for r in Storage(path, 0, "UTC").energy_slots()}
    assert rows["1970-01-01"]["solar_wh"] == 40 and rows["1970-01-01"]["import_wh"] == 3
    assert rows["1970-01-01"]["grid_charge_wh"] == 3  # battery totals left as they were
    assert rows["1960-01-01"]["discharge_wh"] == 7 and rows["1960-01-01"]["solar_wh"] == 0
