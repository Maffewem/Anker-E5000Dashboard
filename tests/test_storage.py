import time

from app.storage import MinuteBucket, Storage


def test_bucket_splits_energy_by_direction():
    b = MinuteBucket(0)
    b.add({"solar_w": 1200, "home_w": 600, "grid_w": 600, "battery_w": -600, "soc": 50}, 30)
    b.add({"solar_w": 0, "home_w": 600, "grid_w": -600, "battery_w": 600, "soc": 52}, 30)
    row = b.row()
    assert row["solar_w"] == 600
    assert row["soc"] == 51
    assert row["import_wh"] == 5  # 600 W for 30 s
    assert row["export_wh"] == 5
    assert row["charge_wh"] == 5
    assert row["discharge_wh"] == 5
    assert row["home_wh"] == 10


def test_history_and_daily_energy():
    s = Storage(":memory:", 365, "UTC")
    now = int(time.time() // 60 * 60)
    for i in range(10):
        b = MinuteBucket(now - (10 - i) * 60)
        b.add({"solar_w": 600, "home_w": 300, "grid_w": 0, "battery_w": -300, "soc": 50}, 60)
        s.write_minute(b.row())
    hist = s.history(1)
    assert hist["bucket_seconds"] == 60
    assert len(hist["points"]) == 10
    days = s.daily_energy(2)
    assert len(days) == 2
    assert sum(d["solar_kwh"] for d in days) == 0.1  # 600 W for 10 min


def test_history_offset_returns_the_previous_period():
    s = Storage(":memory:", 365, "UTC")
    now = int(time.time() // 60 * 60)
    for minutes_ago, solar in ((5, 100), (65, 200)):  # this hour, and the hour before
        b = MinuteBucket(now - minutes_ago * 60)
        b.add({"solar_w": solar, "home_w": 0, "grid_w": 0, "battery_w": 0, "soc": 50}, 60)
        s.write_minute(b.row())
    assert [p["solar_w"] for p in s.history(1)["points"]] == [100]
    assert [p["solar_w"] for p in s.history(1, offset_hours=1)["points"]] == [200]


def test_exports_cover_whole_local_days():
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    from app.storage import MeterBucket

    tz = ZoneInfo("Europe/London")
    s = Storage(":memory:", 0, "Europe/London")
    for hour in (0, 23):
        ts = int(datetime(2026, 7, 1, hour, 30, tzinfo=tz).timestamp())
        s.write_minute({"ts": ts, "grid_w": 100.0, "import_wh": 1.0, "charge_wh": 2.0})
        bucket = MeterBucket(ts)
        bucket.add({"grid_w": 100, "phases": [{"voltage": 240.0}], "import_total_kwh": 5.5}, 5)
        bucket.add({"grid_w": 300, "phases": [{"voltage": 242.0}], "import_total_kwh": 5.6}, 5)
        s.write_meter_minute(bucket.row())
    s.write_minute({"ts": int(datetime(2026, 7, 2, 0, 0, tzinfo=tz).timestamp()), "import_wh": 9.0})

    day = date(2026, 7, 1)
    cols, rows = s.export_rows("minutes", day, day)
    assert cols[0] == "time" and len(rows) == 2
    assert rows[0]["time"] == "2026-07-01T00:30:00+01:00"
    cols, rows = s.export_rows("meter", day, day)
    assert [r["grid_w"] for r in rows] == [200, 200]
    assert rows[0]["voltage"] == 241 and rows[0]["import_total_kwh"] == 5.6
    cols, rows = s.export_rows("daily", day, date(2026, 7, 2))
    assert [r["import_kwh"] for r in rows] == [0.002, 0.009]
    cols, rows = s.export_rows("slots", day, day)
    assert [(r["slot"], r["grid_charge_wh"], r["solar_charge_wh"]) for r in rows] == [(1, 1.0, 1.0), (47, 1.0, 1.0)]
