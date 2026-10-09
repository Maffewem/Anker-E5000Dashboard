import time
from datetime import date

import pytest

from app.storage import MinuteBucket, Storage
from app.tariff import Tariff, payback


def test_offpeak_window_can_cross_midnight():
    rates = Tariff(peak_rate=30, offpeak_rate=7, offpeak_start="23:30", offpeak_end="05:30").slot_rates()
    assert rates[47] == 7  # 23:30
    assert rates[0] == 7 and rates[10] == 7  # 00:00, 05:00
    assert rates[11] == 30  # 05:30
    assert rates[46] == 30  # 23:00


def test_payback_values_discharge_and_charging():
    tariff = Tariff(battery_cost=100, peak_rate=30, offpeak_rate=10, offpeak_start="00:00",
                    offpeak_end="05:00", export_rate=15)
    slots = [
        {"day": "2026-10-08", "slot": 2, "discharge_wh": 0, "grid_charge_wh": 5000, "solar_charge_wh": 0},  # 01:00
        {"day": "2026-10-08", "slot": 26, "discharge_wh": 0, "grid_charge_wh": 0, "solar_charge_wh": 2000},  # 13:00
        {"day": "2026-10-09", "slot": 36, "discharge_wh": 6000, "grid_charge_wh": 0, "solar_charge_wh": 0},  # 18:00
    ]
    p = payback(tariff, slots, date(2026, 10, 9))
    assert p["discharge_value"] == 1.80  # 6 kWh at 30p
    assert p["grid_charge_cost"] == 0.50  # 5 kWh at 10p
    assert p["solar_charge_cost"] == 0.30  # 2 kWh of lost export at 15p
    assert p["saved"] == 1.00
    assert p["days"] == 2 and p["per_day"] == 0.5
    assert p["remaining"] == 99.0
    assert p["payback_days"] == 198


def test_storage_splits_grid_and_solar_charging_by_half_hour():
    s = Storage(":memory:", 365, "UTC")
    minute = int(time.time() // 1800 * 1800)
    b = MinuteBucket(minute)
    b.add({"solar_w": 1000, "home_w": 200, "grid_w": 300, "battery_w": -1200}, 60)  # 20 Wh charge, 5 Wh imported
    s.write_minute(b.row())
    (slot,) = s.battery_slots()
    assert slot["grid_charge_wh"] == pytest.approx(5)
    assert slot["solar_charge_wh"] == pytest.approx(15)


def test_validate_rejects_bad_times():
    with pytest.raises(ValueError):
        Tariff(offpeak_start="25:00").validate()
