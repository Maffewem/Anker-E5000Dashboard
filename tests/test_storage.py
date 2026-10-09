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
