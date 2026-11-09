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


def _minute(s, ts, **snap):
    b = MinuteBucket(ts)
    b.add({"solar_w": 0, "home_w": 0, "grid_w": 0, "battery_w": 0, "soc": 50, **snap}, 60)
    s.write_minute(b.row())


def test_gap_is_filled_from_lifetime_counters_and_marked_estimated():
    s = Storage(":memory:", 365, "UTC")
    start = int(time.time() // 60 * 60) - 7200
    _minute(s, start, solar_w=600, home_w=600, soc=50, solar_total_kwh=10.0, charged_total_kwh=5.0, discharged_total_kwh=4.0)
    # an hour with nothing recorded: the counters say 0.6 kWh solar, 0.2 kWh into the battery
    _minute(s, start + 3660, solar_w=600, home_w=600, soc=60, solar_total_kwh=10.6, charged_total_kwh=5.2, discharged_total_kwh=4.0)
    filled = s.fill_gaps()
    assert len(filled) == 1 and filled[0]["minutes"] == 60 and filled[0]["method"] == "lifetime counters"
    with s._lock:
        n, solar, charge = s._db.execute("SELECT COUNT(*), SUM(solar_wh), SUM(charge_wh) FROM minutes WHERE estimated").fetchone()
    assert n == 60 and round(solar) == 600 and round(charge) == 200
    assert s.fill_gaps() == []  # nothing more to do the second time
    assert any("filled with an estimate" in e["message"] for e in s.events())


def test_gap_without_counters_interpolates_and_estimates_do_not_count_as_measured():
    s = Storage(":memory:", 365, "UTC")
    start = int(time.time() // 60 * 60) - 7200
    _minute(s, start, solar_w=0, soc=100)
    _minute(s, start + 1200, solar_w=600, soc=100)
    assert s.fill_gaps()[0]["method"] == "readings either side of it"
    with s._lock:
        solar = s._db.execute("SELECT SUM(solar_wh) FROM minutes WHERE estimated").fetchone()[0]
    assert 80 < solar < 120  # ~300 W average for 19 minutes
    assert s.last_full(99) == start + 1200  # estimates aren't evidence of a full battery


def test_short_gaps_are_left_alone():
    s = Storage(":memory:", 365, "UTC")
    start = int(time.time() // 60 * 60) - 600
    _minute(s, start)
    _minute(s, start + 120)
    assert s.fill_gaps() == []


def _day_with_import(s, day_start, import_wh_per_slot, meter_kwh):
    """A day of 48 half hours charging 1 kWh each from a recorded import, and a meter counter over the day."""
    for slot in range(48):
        s.write_minute({"ts": day_start + slot * 1800, "grid_w": 0.0, "import_wh": import_wh_per_slot,
                        "charge_wh": 1000.0})
    for ts, total in ((day_start, 100.0), (day_start + 86400, 100.0 + meter_kwh)):
        s.write_meter_minute({"ts": ts, "grid_w": 0.0, "voltage": 240.0, "import_total_kwh": total,
                              "export_total_kwh": 0.0})


def _tz_day(year, month, day):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return int(datetime(year, month, day, tzinfo=ZoneInfo("UTC")).timestamp())


def test_reconcile_scales_when_the_minutes_disagree_with_the_meter_too():
    s = Storage(":memory:", 0, "UTC")
    start = _tz_day(2026, 11, 4)
    _day_with_import(s, start, 1000.0, 30.0)  # recorded 48 kWh in the minutes too, the meter saw 30
    fixed = s.reconcile_import(now=start + 2 * 86400)
    assert fixed == [{"day": "2026-11-04", "recorded_kwh": 48.0, "meter_kwh": 30.0}]
    rows = s.battery_slots()
    assert round(sum(r["import_wh"] for r in rows)) == 30000
    # The 48 kWh charged stays, but only part of it came from the grid now.
    assert round(sum(r["grid_charge_wh"] + r["solar_charge_wh"] for r in rows)) == 48000
    assert s.import_checks()[0]["corrected"] is True
    assert s.events(kinds=["data"])
    assert s.reconcile_import(now=start + 2 * 86400) == []  # checked once


def test_reconcile_leaves_a_matching_day_alone():
    s = Storage(":memory:", 0, "UTC")
    start = _tz_day(2026, 11, 2)
    _day_with_import(s, start, 1000.0, 46.0)  # 48 recorded, 46 metered: within tolerance
    assert s.reconcile_import(now=start + 2 * 86400) == []
    assert round(sum(r["import_wh"] for r in s.battery_slots())) == 48000
    assert s.import_checks()[0]["corrected"] is False


def test_reconcile_skips_today_and_days_without_meter_readings():
    s = Storage(":memory:", 0, "UTC")
    start = _tz_day(2026, 11, 4)
    for slot in range(4):
        s.write_minute({"ts": start + slot * 1800, "import_wh": 1000.0, "charge_wh": 500.0})
    assert s.reconcile_import(now=start + 3600) == []  # still today
    assert s.reconcile_import(now=start + 2 * 86400) == []  # no meter readings
    assert s.import_checks() == []


def test_a_day_written_twice_is_rebuilt_from_its_minutes():
    """Two copies writing to one database: the minute rows are replaced, the half-hour totals are added twice."""
    s = Storage(":memory:", 0, "UTC")
    start = _tz_day(2026, 11, 4)
    for slot in range(48):
        row = {"ts": start + slot * 1800, "import_wh": 500.0, "charge_wh": 500.0, "discharge_wh": 300.0,
               "home_wh": 400.0}
        s.write_minute(row)
        s.write_minute(row)  # the second copy: the minute is replaced, the slot is added to
    assert round(sum(r["import_wh"] for r in s.battery_slots())) == 48000
    for ts, total in ((start, 100.0), (start + 86400, 124.0)):
        s.write_meter_minute({"ts": ts, "grid_w": 0.0, "voltage": 240.0, "import_total_kwh": total,
                              "export_total_kwh": 0.0})
    s.reconcile_import(now=start + 2 * 86400)
    rows = s.energy_slots()
    assert round(sum(r["import_wh"] for r in rows)) == 24000
    assert round(sum(r["discharge_wh"] for r in rows)) == 14400
    assert round(sum(r["home_wh"] for r in rows)) == 19200
    assert round(sum(r["grid_charge_wh"] + r["solar_charge_wh"] for r in rows)) == 24000
    assert s.import_checks()[0]["rebuilt"] is True


def test_days_corrected_for_import_only_are_rebuilt_later():
    s = Storage(":memory:", 0, "UTC")
    start = _tz_day(2026, 11, 4)
    for slot in range(48):
        s.write_minute({"ts": start + slot * 1800, "import_wh": 500.0, "charge_wh": 500.0, "discharge_wh": 300.0})
    for slot in range(48):  # the first version's result: slots doubled, then import scaled, nothing else
        s._db.execute("UPDATE slots SET discharge_wh = 600, grid_charge_wh = 250 WHERE day = '2026-11-04' AND slot = ?",
                      (slot,))
    s._db.execute("INSERT INTO import_checks (day, recorded_kwh, meter_kwh, factor, corrected, rebuilt) "
                  "VALUES ('2026-11-04', 48, 24, 0.5, 1, 0)")
    s._db.commit()
    s.reconcile_import(now=start + 2 * 86400)
    assert round(sum(r["discharge_wh"] for r in s.energy_slots())) == 14400
    assert s.import_checks()[0]["rebuilt"] is True
    s.reconcile_import(now=start + 2 * 86400)
    assert round(sum(r["discharge_wh"] for r in s.energy_slots())) == 14400  # once only


def test_duplicate_event_lines_from_a_second_copy_are_dropped():
    s = Storage(":memory:", 0, "UTC")
    start = _tz_day(2026, 11, 4)
    _day_with_import(s, start, 1000.0, 30.0)
    for ts in (start + 100, start + 100, start + 130, start + 5000):
        s.record_event("charging", "Charging at 2500 W", ts=ts)
    s.reconcile_import(now=start + 2 * 86400)
    assert [e["ts"] for e in s.events(kinds=["charging"])] == [start + 5000, start + 100]


def test_grid_compare_lines_up_recorded_and_metered_import_per_hour():
    from datetime import date

    s = Storage(":memory:", 0, "UTC")
    start = _tz_day(2026, 11, 4)
    for m in range(120):  # two hours: recorded double what the meter saw
        ts = start + m * 60
        s.write_minute({"ts": ts, "import_wh": 20.0, "charge_wh": 10.0})
        s.write_meter_minute({"ts": ts, "grid_w": 600.0, "voltage": 240.0,
                              "import_total_kwh": 100 + (m + 1) * 0.01, "export_total_kwh": 0.0})
    rows = s.grid_compare(date(2026, 11, 4), date(2026, 11, 4))
    assert [r["hour"] for r in rows] == [0, 1]
    assert rows[0]["recorded_import_kwh"] == 1.2
    assert rows[0]["meter_power_kwh"] == 0.6
    assert rows[1]["meter_counter_kwh"] == 0.6  # 60 minutes at 0.01 kWh


def test_a_second_dashboard_on_the_same_data_folder_is_refused(tmp_path):
    import pytest

    from app.storage import AlreadyRunning, lock_data_folder

    db = str(tmp_path / "solarbank.db")
    first = lock_data_folder(db)
    with pytest.raises(AlreadyRunning):
        lock_data_folder(db)
    first.close()  # the lock goes with the process
    lock_data_folder(db).close()
    assert lock_data_folder(":memory:") is None


def test_a_day_with_less_recorded_than_metered_is_not_scaled_up():
    s = Storage(":memory:", 0, "UTC")
    start = _tz_day(2026, 10, 11)
    _day_with_import(s, start, 1000.0, 60.0)  # the dashboard was off for part of the day: 48 recorded, 60 metered
    assert s.reconcile_import(now=start + 2 * 86400) == []
    assert round(sum(r["import_wh"] for r in s.battery_slots())) == 48000
    assert s.import_checks()[0]["corrected"] is False


def test_an_earlier_scaling_up_is_taken_back():
    s = Storage(":memory:", 0, "UTC")
    start = _tz_day(2026, 10, 11)
    for slot in range(48):  # as left by the first version: x1.25 on import and grid charge
        s._db.execute("INSERT INTO slots (day, slot, import_wh, grid_charge_wh, solar_charge_wh) "
                      "VALUES ('2026-10-11', ?, 1250, 250, 750)", (slot,))
    s._db.execute("INSERT INTO import_checks (day, recorded_kwh, meter_kwh, factor, corrected, rebuilt) "
                  "VALUES ('2026-10-11', 48, 60, 1.25, 1, 0)")
    s._db.commit()
    s.reconcile_import(now=start + 2 * 86400)
    rows = s.battery_slots()
    assert round(sum(r["import_wh"] for r in rows)) == 48000
    assert round(sum(r["grid_charge_wh"] for r in rows)) == 9600
    assert round(sum(r["grid_charge_wh"] + r["solar_charge_wh"] for r in rows)) == 48000
    assert s.import_checks()[0]["corrected"] is False
