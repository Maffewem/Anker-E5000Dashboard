"""Clock changes: the last Sunday of October (25 hour day) and of March (23 hour day)."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.control import Window, _daily, pace_charge
from app.runtime import estimate
from app.storage import Storage

TZ = ZoneInfo("Europe/London")
UTC = timezone.utc
FALL = date(2026, 10, 25)   # clocks go back at 02:00 BST: 01:00-02:00 happens twice
SPRING = date(2027, 3, 28)  # clocks go forward at 01:00 GMT: 01:00-02:00 never happens


def hours(a, b):
    return (b - a).total_seconds() / 3600


def window_on(day, start="00:30", end="05:30"):
    now = datetime(day.year, day.month, day.day, 12, 0, tzinfo=TZ)
    return next((a, b) for a, b in _daily(start, end, TZ, now) if a.astimezone(TZ).date() == day)


def test_overnight_window_is_a_real_hour_longer_when_clocks_go_back():
    a, b = window_on(FALL)
    assert hours(a, b) == 6
    assert hours(*window_on(date(2026, 10, 24))) == 5


def test_overnight_window_loses_an_hour_when_clocks_go_forward():
    a, b = window_on(SPRING)
    assert hours(a, b) == 4
    assert hours(*window_on(date(2027, 3, 27))) == 5


def test_daily_window_starting_the_evening_before_spans_the_change():
    now = datetime(2026, 10, 24, 12, 0, tzinfo=TZ)
    a, b = next((a, b) for a, b in _daily("23:30", "07:30", TZ, now) if a.astimezone(TZ).date() == date(2026, 10, 24))
    assert hours(a, b) == 9
    now = datetime(2027, 3, 27, 12, 0, tzinfo=TZ)
    a, b = next((a, b) for a, b in _daily("23:30", "07:30", TZ, now) if a.astimezone(TZ).date() == date(2027, 3, 27))
    assert hours(a, b) == 7


def test_paced_charge_uses_the_extra_hour_in_october_and_speeds_up_in_march():
    soc, target, rated, cap = 20, 80, 5.0, 2500
    fall = Window(*window_on(FALL), "Off-peak")
    spring = Window(*window_on(SPRING), "Off-peak")
    normal = Window(*window_on(date(2026, 10, 24)), "Off-peak")
    p_normal = pace_charge(normal.start, normal, soc, target, rated, cap, 45)
    p_fall = pace_charge(fall.start, fall, soc, target, rated, cap, 45)
    p_spring = pace_charge(spring.start, spring, soc, target, rated, cap, 45)
    assert p_fall["power"] <= p_normal["power"]  # a longer window never needs more power
    assert p_spring["power"] > p_normal["power"]  # the shorter window needs a faster charge...
    assert p_spring["power"] <= cap  # ...but never beyond the cap
    assert p_spring["finish"] <= p_spring["deadline"] <= spring.end


def test_march_window_too_short_for_the_target_charges_flat_out():
    spring = Window(*window_on(SPRING), "Off-peak")
    plan = pace_charge(spring.start, spring, 5, 100, 5.0, 1500, 45)
    assert plan["kind"] == "full" and plan["power"] == 1500


def test_estimate_counts_real_hours_across_the_change():
    snap = {"soc": 50, "battery_w": 1000, "rated_kwh": 5.0, "discharge_limit_soc": 10,
            "backup_reserve_soc": 5, "charging_limit_soc": 100}
    # 2 kWh above the floor at 1 kW: empty 2 real hours after 23:30 BST on the 24th, i.e. 01:30 GMT (not 01:30 BST)
    now = datetime(2026, 10, 25, 0, 30, tzinfo=TZ)  # 00:30 BST
    e = estimate(snap, [None] * 48, 2, now)
    assert datetime.fromisoformat(e["empty_at"]) == now.astimezone(UTC) + timedelta(hours=2)
    assert datetime.fromisoformat(e["empty_at"]).astimezone(TZ).strftime("%H:%M %Z") == "01:30 GMT"
    # and in spring 2 real hours after 00:30 GMT is 03:30 BST
    now = datetime(2027, 3, 28, 0, 30, tzinfo=TZ)
    e = estimate(snap, [None] * 48, 2, now)
    assert datetime.fromisoformat(e["empty_at"]).astimezone(TZ).strftime("%H:%M %Z") == "03:30 BST"


def test_estimate_pattern_walks_real_half_hours_across_the_change():
    snap = {"soc": 50, "battery_w": 1000, "rated_kwh": 5.0, "discharge_limit_soc": 10,
            "backup_reserve_soc": 5, "charging_limit_soc": 100}
    pattern = [1000.0] * 48
    now = datetime(2026, 10, 25, 0, 30, tzinfo=TZ)
    e = estimate(snap, pattern, 14 * 24, now)
    assert datetime.fromisoformat(e["empty_at"]) == now.astimezone(UTC) + timedelta(hours=2)


def _minute(s, local, **kw):
    s.write_minute({"ts": int(local.timestamp()), "grid_w": 0.0, "import_wh": kw.get("import_wh", 1.0)})


def test_local_day_totals_cover_25_and_23_hours():
    for day, expected in ((FALL, 25), (SPRING, 23)):
        s = Storage(":memory:", 0, "Europe/London")
        start = int(datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp())
        nxt = day + timedelta(days=1)
        end = int(datetime(nxt.year, nxt.month, nxt.day, tzinfo=TZ).timestamp())
        assert (end - start) / 3600 == expected
        assert s._day_start(day) == start and s._day_start(nxt) == end
