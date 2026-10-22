"""Estimating when the battery runs empty or full."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.runtime import estimate, pattern_from_minutes

TZ = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 9, 18, 0, tzinfo=TZ)


def snap(soc=50, power=1000, floor=10, rated=5.0):
    return {"soc": soc, "battery_w": power, "rated_kwh": rated, "discharge_limit_soc": floor,
            "backup_reserve_soc": 5, "charging_limit_soc": 100}


def evening_pattern():
    """1 kW out 17:00-23:00, 2 kW in 00:30-05:30 (off-peak), idle otherwise."""
    p = [0.0] * 48
    for slot in range(34, 46):
        p[slot] = 1000.0
    for slot in range(1, 11):
        p[slot] = -2000.0
    return p


def test_without_history_it_uses_the_current_power():
    e = estimate(snap(soc=50, power=1000), [None] * 48, 2, NOW)
    # 40% of 5 kWh above the 10% limit = 2 kWh at 1 kW
    assert e["method"] == "current" and e["usable_kwh"] == 2.0
    assert e["empty_at"] == "2026-10-09T20:00:00+01:00"


def test_without_history_running_empty_over_a_day_away_is_not_reported():
    e = estimate(snap(soc=100, power=150), [None] * 48, 2, NOW)
    # 4.5 kWh at 150 W is 30 hours
    assert e["empty_at"] is None


def test_pattern_runs_out_during_the_evening():
    e = estimate(snap(soc=40, power=1000), evening_pattern(), 14 * 24, NOW)
    # 1.5 kWh above the floor at 1 kW from 18:00
    assert e["method"] == "pattern" and e["empty_at"] == "2026-10-09T19:30:00+01:00"
    assert e["recharges_at"] == "2026-10-10T00:30:00+01:00"  # sits at the floor until off-peak


def test_pattern_lasts_until_off_peak_charging():
    e = estimate(snap(soc=90, power=1000), evening_pattern(), 14 * 24, NOW)
    # 4 kWh above the floor at 1 kW from 18:00
    assert e["empty_at"] == "2026-10-09T22:00:00+01:00"
    e = estimate(snap(soc=100, power=500), evening_pattern(), 14 * 24, NOW)
    # 4.5 kWh: 0.25 kWh in the first half hour at today's 500 W, then 4.25 h at 1 kW
    assert e["empty_at"] == "2026-10-09T22:45:00+01:00"
    big = estimate(snap(soc=100, power=500, rated=10.0), evening_pattern(), 14 * 24, NOW)
    assert big["empty_at"] is None
    assert big["recharges_at"] == "2026-10-10T00:30:00+01:00"


def test_charging_now_then_discharging_reports_full_then_empty():
    # Charging at 2 kW now (off-peak) from 50%, then idle, then the evening.
    night = datetime(2026, 10, 10, 1, 0, tzinfo=TZ)
    e = estimate(snap(soc=50, power=-2000), evening_pattern(), 14 * 24, night)
    assert e["full_at"] == "2026-10-10T02:15:00+01:00"  # 2.5 kWh of room at 2 kW
    assert e["empty_at"] == "2026-10-10T21:30:00+01:00"  # 4.5 kWh at 1 kW from 17:00
    assert e["recharges_at"] == "2026-10-11T00:30:00+01:00"


def test_charging_now_reports_when_full():
    e = estimate(snap(soc=80, power=-1000), [None] * 48, 0, NOW)
    assert e["full_at"] == "2026-10-09T19:00:00+01:00"  # 1 kWh of room at 1 kW
    assert e["empty_at"] is None


def test_pattern_averages_by_local_half_hour():
    ts = int(datetime(2026, 10, 1, 18, 10, tzinfo=TZ).timestamp())
    pattern, hours = pattern_from_minutes([(ts, 800.0), (ts + 60, 1200.0), (ts + 120, None)], TZ)
    assert pattern[36] == 1000.0 and pattern[0] is None
    assert hours == 2 / 60


def test_one_off_charge_is_not_the_usual_pattern():
    rows = [(int(datetime(2026, 10, d, 13, 10, tzinfo=TZ).timestamp()), -2400.0 if d == 5 else 0.0) for d in range(1, 8)]
    pattern, _ = pattern_from_minutes(rows, TZ)
    assert pattern[26] == 0.0


def test_missing_readings_mean_no_estimate():
    assert estimate({"soc": None}, [None] * 48, 0, NOW) == {"available": False}


def test_a_scheduled_charge_ends_the_projection_before_the_usual_pattern():
    p = [0.0] * 48
    for slot in range(26, 30):  # solar charging from 13:00
        p[slot] = -1500.0
    charge = datetime(2026, 10, 10, 0, 30, tzinfo=TZ)
    e = estimate(snap(soc=80, power=220), p, 100, NOW, [(charge, "your schedule 00:30-05:30")])
    assert e["recharges_at"] == charge.isoformat() and e["recharges_by"] == "your schedule 00:30-05:30"
    e = estimate(snap(soc=80, power=220), p, 100, NOW)
    assert e["recharges_at"] == "2026-10-10T13:00:00+01:00" and e["recharges_by"] is None


def test_a_scheduled_charge_in_progress_is_reported_and_not_called_a_wait_for_solar():
    p = [0.0] * 48
    for slot in range(26, 30):
        p[slot] = -1500.0
    now = datetime(2026, 10, 21, 0, 39, tzinfo=TZ)
    end = datetime(2026, 10, 21, 5, 30, tzinfo=TZ)
    nxt = datetime(2026, 10, 22, 0, 30, tzinfo=TZ)
    e = estimate(snap(soc=10, power=-2510, floor=10), p, 100, now, [(nxt, "your schedule 00:30-05:30")],
                 (end, "your schedule 00:30-05:30"))
    assert e["charging"] and e["charging_until"] == end.isoformat()
    assert e["charging_by"] == "your schedule 00:30-05:30"
    idle = estimate(snap(soc=10, power=0, floor=10), p, 100, now, [(nxt, "x")], (end, "x"))
    assert not idle["charging"] and idle["charging_until"] is None


def test_full_time_while_charging_follows_the_live_power_to_the_target():
    p = [0.0] * 48  # the usual pattern says nothing useful about this charge
    now = datetime(2026, 10, 21, 0, 39, tzinfo=TZ)
    s = snap(soc=10, power=-2500, floor=10)
    e = estimate(s, p, 100, now, None, None, 90)
    # 80% of 5 kWh = 4 kWh at 2.5 kW is 1.6 h
    assert e["charge_target"] == 90 and e["full_at"] == "2026-10-21T02:15:00+01:00"
    e = estimate(s, p, 100, now)
    assert e["charge_target"] == 100 and e["full_at"] == "2026-10-21T02:27:00+01:00"
    e = estimate(s, [None] * 48, 2, now, None, None, 90)
    assert e["full_at"] == "2026-10-21T02:15:00+01:00"
