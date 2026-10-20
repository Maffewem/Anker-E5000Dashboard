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


def test_day_periods_and_insights():
    days = daily_totals(TARIFF, [slot(f"2026-10-{d:02d}", 36, home_wh=10000, import_wh=2500, solar_wh=4000, export_wh=1000)
                                 for d in range(1, 11)])
    ps = periods(days, "day", date(2026, 10, 10), 4)
    assert [p["start"] for p in ps] == ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10"]
    assert all(p["start"] == p["end"] for p in ps)
    from app.trends import insights
    i = insights(days, date(2026, 10, 10))
    assert i["days"] == 9 and i["self_sufficiency"] == 75 and i["solar_used"] == 75
    assert insights({}, date(2026, 10, 10)) == {"days": 0}


def test_conditions_and_temperature_effect():
    from app.trends import conditions, temperature_effect
    days = daily_totals(TARIFF, [slot("2026-10-01", 20, solar_wh=6000)])
    weather = {"2026-10-01": {"hours": 24, "sun_kwh_m2": 3.0, "temp_avg": 12.0, "temp_min": 8, "temp_max": 16, "cloud_avg": 40}}
    rows = conditions(days, [{"day": "2026-10-01", "solar_peak_w": 3100, "voltage_min": 238}], weather,
                      [{"day": "2026-10-01", "soc_drop": 80, "discharge_wh": 4000}], date(2026, 10, 1), date(2026, 10, 2))
    assert rows[0]["solar_yield"] == 2.0 and rows[0]["capacity_kwh"] == 5.0 and rows[0]["solar_peak_w"] == 3100
    assert rows[1]["solar_kwh"] is None and rows[1]["temp_avg"] is None
    assert temperature_effect(rows) is None
    many = [{"temp_avg": t, "capacity_kwh": 4.0 + t / 20} for t in range(0, 30)]
    effect = temperature_effect(many)
    assert effect["cold_kwh"] < effect["warm_kwh"] and effect["days"] == 30


def test_peaks_summaries_and_samples(tmp_path):
    from app.storage import MinuteBucket
    st = Storage(str(tmp_path / "t.db"), 365, "UTC", sample_days=7)
    now = int(datetime.now(ZoneInfo("UTC")).timestamp()) // 60 * 60 - 2 * 86400
    b = MinuteBucket(now)
    for grid, solar in ((500, 100), (-800, 3000), (200, 50)):
        b.add({"grid_w": grid, "solar_w": solar, "soc": 50, "battery_w": 0}, 5)
    row = b.row()
    assert row["grid_w_max"] == 500 and row["grid_w_min"] == -800 and row["solar_w_max"] == 3000
    st.write_minute(row)
    st.write_meter_minute({"ts": now, "voltage": 241.0})
    st.write_sample(now + 1, {"solar_w": 5, "soc": 50})
    summary = st.day_summaries()[0]
    assert summary["import_peak_w"] == 500 and summary["export_peak_w"] == 800 and summary["solar_peak_w"] == 3000
    assert summary["voltage_max"] == 241.0 and summary["soc_min"] == 50
    # Raw readings go after SAMPLE_DAYS but the summary stays after the minutes are pruned.
    st.write_sample(now - 9 * 86400, {"solar_w": 1})
    st.prune()
    assert st.export_rows("samples", date(2020, 1, 1), date(2100, 1, 1))[1][0]["solar_w"] == 5
    assert len(st.export_rows("samples", date(2020, 1, 1), date(2100, 1, 1))[1]) == 1
    st.retention_days = 1
    st.prune()
    assert st.day_summaries()[0]["solar_peak_w"] == 3000


def test_old_minutes_table_gets_peak_columns(tmp_path):
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE minutes (ts INTEGER PRIMARY KEY, solar_w REAL, home_w REAL, battery_w REAL, grid_w REAL, soc REAL, "
               "solar_wh REAL, home_wh REAL, import_wh REAL, export_wh REAL, charge_wh REAL, discharge_wh REAL)")
    db.commit()
    db.close()
    Storage(path, 365, "UTC").write_minute({"ts": 60, "grid_w_max": 5})


def test_price_history_keeps_old_days_at_old_prices(tmp_path):
    from app.tariff import PriceHistory, prices_changed
    st = Storage(":memory:", 365, "UTC")
    old, new = Tariff(peak_rate=20, offpeak_rate=20), Tariff(peak_rate=30, offpeak_rate=30)
    assert prices_changed(old, new) == ["peak 20p to 30p", "off-peak 20p to 30p"]
    from dataclasses import asdict
    st.save_tariff_change(asdict(old), asdict(new), "2026-10-10")
    history = PriceHistory(st.tariff_history())
    assert history.get(("2026-10-09", 10))[0] == 20 and history.get(("2026-10-10", 10))[0] == 30
    # Fetched prices still win where there are any.
    both = PriceHistory(st.tariff_history(), {("2026-10-09", 10): (99.0, None)})
    assert both.get(("2026-10-09", 10)) == (99.0, old.export_rate)


def test_weather_parse_and_store():
    from app import weather
    assert weather.parse_location("51.5044", "-0.1") == (51.5, -0.1)
    with pytest.raises(ValueError):
        weather.parse_location(95, 0)
    seen = []
    fake = lambda url: seen.append(url) or {"hourly": {"time": ["2026-10-20T10:00", "2026-10-20T11:00"],
                                                     "temperature_2m": [10.0, 11.0], "cloud_cover": [50, 60],
                                                     "shortwave_radiation": [200.0, 400.0]}}
    st = Storage(":memory:", 365, "UTC")
    assert weather.sync(st, 51.5, -0.1, fake) == 2
    day = st.weather_days()["2026-10-20"]
    assert day["sun_kwh_m2"] == 0.6 and day["temp_avg"] == 10.5 and day["cloud_avg"] == 55
    assert "latitude=51.5" in seen[0] and "past_days=3" in seen[0]


def test_api_tariff_change_event_trends_and_location(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import weather

    for name in ("SOLARBANK_HOST", "METER_HOST", "OCTOPUS_API_KEY", "OCTOPUS_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "solarbank.db"))
    monkeypatch.setattr(weather, "fetch_hours", lambda lat, lon, fetch, past_days: [(1760000000, 11.0, 40, 300.0)])
    from app.main import app

    with TestClient(app) as c:
        prices = {"peak_rate": 28, "offpeak_rate": 10, "offpeak_start": "00:30", "offpeak_end": "05:30", "export_rate": 15}
        assert c.post("/api/tariff", json=prices).status_code == 200  # first save: nothing to compare
        assert c.get("/api/events?kind=tariff").json() == []
        assert c.post("/api/tariff", json={**prices, "peak_rate": 30, "prices_from": "2026-10-01"}).status_code == 200
        ev = c.get("/api/events?kind=tariff").json()
        assert len(ev) == 1 and "peak 28p to 30p" in ev[0]["message"] and "1 Oct 2026" in ev[0]["message"]
        assert c.post("/api/tariff", json={**prices, "peak_rate": 30, "prices_from": "2999-01-01"}).status_code == 422
        assert c.post("/api/location", json={"lat": 200, "lon": 0}).status_code == 422
        assert c.post("/api/location", json={"lat": 51.5, "lon": -0.12}).json()["days"] == 1
        t = c.get("/api/trends?period=day&count=30").json()
        assert {"insights", "conditions", "temperature_effect", "has_weather"} <= set(t)
        assert c.get("/api/export?data=days").status_code == 200
        assert c.get("/api/export?data=weather").status_code == 200
