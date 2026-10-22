import time
from datetime import date

import pytest

from app.storage import MinuteBucket, Storage
from app.tariff import Tariff, daily_costs, payback


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
    assert p["days"] == 2 and p["per_day"] == -0.8  # today (the 9th) is left out of the rate: only the charging day counts
    assert p["remaining"] == 99.0
    assert p["payback_days"] is None  # two days is too little to project from
    # Seven finished days of +£1.00 each (5 kWh in at 10p, 6 kWh out at 30p, 2 kWh solar at 15p).
    week_slots = [{**r, "day": f"2026-10-{d:02d}"} for d in range(1, 8) for r in slots[:2] + [{**slots[2]}]]
    week = payback(tariff, week_slots, date(2026, 10, 8))
    assert week["recorded_days"] == 7 and week["per_day"] == 1.0 and week["early_estimate"]
    assert week["saved"] == 7.0 and week["payback_days"] == 93
    assert (week["payback_days_low"], week["payback_days_high"]) == (74, 124)
    pricey = payback(Tariff(**{**tariff.__dict__, "battery_cost": 1e6}), week_slots, date(2026, 10, 8))
    assert pricey["payback_too_long"] and pricey["payback_date"] is None


def test_rate_uses_the_latest_finished_days_and_skips_gaps():
    tariff = Tariff(battery_cost=1000, peak_rate=30, offpeak_rate=30, export_rate=0)
    slots = [{"day": f"2026-09-{d:02d}", "slot": 36, "discharge_wh": 1000, "grid_charge_wh": 0, "solar_charge_wh": 0}
             for d in range(1, 11)]  # 10 slow days at 30p
    slots += [{"day": f"2026-10-{d:02d}", "slot": 36, "discharge_wh": 10000, "grid_charge_wh": 0, "solar_charge_wh": 0}
              for d in range(1, 15)]  # 14 good days at £3.00, with nothing recorded between the groups
    p = payback(tariff, slots, date(2026, 10, 15))
    assert p["per_day"] == 3.0 and p["recorded_days"] == 24 and not p["early_estimate"]


def test_lifetime_totals_count_savings_from_before_recording():
    tariff = Tariff(battery_cost=3000, peak_rate=30, offpeak_rate=8, offpeak_start="00:30",
                    offpeak_end="05:30", export_rate=15, installed="2026-01-01")
    slots = [  # one recorded day: 10 kWh charged off-peak, 9 kWh discharged at peak
        {"day": "2026-10-09", "slot": 2, "discharge_wh": 0, "grid_charge_wh": 10000, "solar_charge_wh": 0},
        {"day": "2026-10-09", "slot": 36, "discharge_wh": 9000, "grid_charge_wh": 0, "solar_charge_wh": 0},
    ]
    lifetime = {"charged_kwh": 2810, "discharged_kwh": 2529}  # 280 days like that, the last one recorded
    p = payback(tariff, slots, date(2026, 10, 9), lifetime=lifetime)
    assert p["since"] == "2026-01-01" and p["days"] == 282
    # Before recording: 2520 kWh out at 30p, 2800 kWh in at 8p.
    assert p["saved_before_recording"] == 532.0
    assert p["saved"] == 533.9
    assert p["payback_days"] is None and p["recorded_days"] == 0  # the one recorded day is today, too little to project from
    assert p["per_day"] == 0.0  # nothing finished yet; not the 282-day average
    # Without an install date the lifetime totals aren't used.
    plain = payback(Tariff(battery_cost=3000, peak_rate=30, offpeak_rate=8, export_rate=15), slots,
                    date(2026, 10, 9), lifetime=lifetime)
    assert plain["saved_before_recording"] is None and plain["days"] == 1 and plain["payback_days"] is None


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
    with pytest.raises(ValueError):
        Tariff(installed="2999-01-01").validate()
    with pytest.raises(ValueError):
        Tariff(installed="last year").validate()


def test_daily_costs_per_day():
    tariff = Tariff(peak_rate=30, offpeak_rate=10, offpeak_start="00:00", offpeak_end="05:00", export_rate=15)
    slots = [
        {"day": "2026-10-08", "slot": 2, "discharge_wh": 0, "grid_charge_wh": 5000, "solar_charge_wh": 0},  # 01:00
        {"day": "2026-10-08", "slot": 36, "discharge_wh": 4000, "grid_charge_wh": 0, "solar_charge_wh": 0},  # 18:00
        {"day": "2026-10-09", "slot": 30, "discharge_wh": 0, "grid_charge_wh": 1000, "solar_charge_wh": 2000},  # 15:00
        {"day": "2026-09-01", "slot": 2, "discharge_wh": 0, "grid_charge_wh": 9000, "solar_charge_wh": 0},  # too old
    ]
    c = daily_costs(tariff, slots, date(2026, 10, 10), days=7)
    assert [d["day"] for d in c["days"]][:3] == ["2026-10-10", "2026-10-09", "2026-10-08"]
    assert len(c["days"]) == 7 and not c["days"][0]["recorded"]
    oct9, oct8 = c["days"][1], c["days"][2]
    assert oct8["grid_charge_kwh"] == 5 and oct8["grid_charge_cost"] == 0.50 and oct8["grid_charge_p_per_kwh"] == 10
    assert oct8["discharge_value"] == 1.20 and oct8["saved"] == 0.70
    assert oct9["grid_charge_cost"] == 0.30 and oct9["solar_charge_cost"] == 0.30 and oct9["saved"] == -0.60
    assert c["today"]["grid_charge_cost"] == 0 and c["week"] == {"days": 2, "grid_charge_kwh": 6, "grid_charge_cost": 0.80, "saved": 0.10}
    assert c["average_per_day"] == 0.40


def test_daily_costs_use_actual_prices():
    tariff = Tariff(peak_rate=30, offpeak_rate=30)
    slots = [{"day": "2026-10-09", "slot": 4, "discharge_wh": 0, "grid_charge_wh": 2000, "solar_charge_wh": 0}]
    c = daily_costs(tariff, slots, date(2026, 10, 9), days=1, prices={("2026-10-09", 4): (7.5, None)})
    assert c["days"][0]["grid_charge_cost"] == 0.15


def test_discharge_sent_to_the_grid_earns_export_not_import():
    tariff = Tariff(peak_rate=30, offpeak_rate=30, export_rate=10)
    slots = [{"day": "2026-10-09", "slot": 36, "discharge_wh": 4000, "discharge_export_wh": 1000,
              "grid_charge_wh": 0, "solar_charge_wh": 0}]
    # 3 kWh met the house at 30p, 1 kWh was exported at 10p.
    c = daily_costs(tariff, slots, date(2026, 10, 9), days=1)
    assert c["days"][0]["discharge_value"] == 1.00
    assert payback(tariff, slots, date(2026, 10, 9))["discharge_value"] == 1.00


def test_storage_counts_battery_export_after_the_solar_surplus():
    s = Storage(":memory:", 365, "UTC")
    b = MinuteBucket(int(time.time() // 1800 * 1800))
    # 1200 W solar, 200 W house: 1000 W surplus. 3000 W leaves for the grid, so
    # 2000 W of it is battery. The battery gave 2400 W, so 2000 W was exported.
    b.add({"solar_w": 1200, "home_w": 200, "grid_w": -3000, "battery_w": 2400}, 60)
    s.write_minute(b.row())
    (slot,) = s.battery_slots()
    assert slot["discharge_wh"] == pytest.approx(40)
    assert slot["discharge_export_wh"] == pytest.approx(2000 / 60)


def test_round_trip_losses_are_in_the_measured_energy():
    # 10 kWh in and 9 kWh out at the battery: the 1 kWh lost is paid for but earns nothing.
    tariff = Tariff(peak_rate=30, offpeak_rate=10, offpeak_start="00:00", offpeak_end="05:00", export_rate=15)
    slots = [{"day": "2026-10-09", "slot": 2, "discharge_wh": 0, "grid_charge_wh": 10000, "solar_charge_wh": 0},
             {"day": "2026-10-09", "slot": 36, "discharge_wh": 9000, "grid_charge_wh": 0, "solar_charge_wh": 0}]
    assert daily_costs(tariff, slots, date(2026, 10, 9), days=1)["days"][0]["saved"] == 1.70  # 2.70 - 1.00


def test_free_window_is_priced_at_zero():
    tariff = Tariff(peak_rate=30, offpeak_rate=30)
    slots = [{"day": "2026-10-09", "slot": 26, "discharge_wh": 0, "grid_charge_wh": 3000, "solar_charge_wh": 0}]
    c = daily_costs(tariff, slots, date(2026, 10, 9), days=1, prices={("2026-10-09", 26): (0.0, 0.0)})
    assert c["days"][0]["grid_charge_cost"] == 0


def test_octopus_without_an_export_price_keeps_the_typed_export_rate():
    from app.tariff import with_octopus_prices
    typed = Tariff(peak_rate=28, offpeak_rate=28, export_rate=0.0, battery_cost=3000)
    profile = {"peak_rate": 35.68, "offpeak_rate": 7.5, "offpeak_start": "00:30", "offpeak_end": "05:30",
               "export_rate": None}
    t = with_octopus_prices(typed, profile)
    assert t.export_rate == 0.0 and t.peak_rate == 35.68 and t.battery_cost == 3000
    assert with_octopus_prices(typed, {**profile, "export_rate": 4.1}).export_rate == 4.1
    assert Tariff(export_rate=0).validate().export_rate == 0.0


def test_export_valued_at_zero_when_exporting_earns_nothing():
    tariff = Tariff(peak_rate=30, offpeak_rate=30, export_rate=0.0)
    slots = [{"day": "2026-10-09", "slot": 36, "discharge_wh": 2000, "discharge_export_wh": 500,
              "grid_charge_wh": 0, "solar_charge_wh": 1000}]
    c = daily_costs(tariff, slots, date(2026, 10, 9), days=1)["days"][0]
    assert c["discharge_value"] == 0.45 and c["solar_charge_cost"] == 0 and c["saved"] == 0.45


def test_daily_costs_break_import_into_battery_and_house():
    tariff = Tariff(peak_rate=30, offpeak_rate=30, export_rate=0.0)
    slots = [{"day": "2026-10-09", "slot": 2, "discharge_wh": 0, "grid_charge_wh": 4000, "solar_charge_wh": 0,
              "import_wh": 5000},
             {"day": "2026-10-09", "slot": 36, "discharge_wh": 3000, "discharge_export_wh": 0,
              "grid_charge_wh": 0, "solar_charge_wh": 0, "import_wh": 1000}]
    d = daily_costs(tariff, slots, date(2026, 10, 9), days=1)["days"][0]
    assert d["import_kwh"] == 6.0 and d["house_import_kwh"] == 2.0  # 4 kWh of it charged the battery
    assert d["export_p_per_kwh"] == 0.0 and d["saved"] == -0.30  # 3 kWh saved at 30p, 4 kWh charged at 30p


def test_projection_rate_uses_recorded_days_but_total_includes_before_recording():
    tariff = Tariff(battery_cost=3000, peak_rate=30, offpeak_rate=8, offpeak_start="00:30",
                    offpeak_end="05:30", export_rate=15, installed="2026-01-01")
    slots = []
    for d in range(3, 10):  # 7 recorded days (the last is today), each saving £1.90
        day = f"2026-10-{d:02d}"
        slots += [{"day": day, "slot": 2, "discharge_wh": 0, "grid_charge_wh": 10000, "solar_charge_wh": 0},
                  {"day": day, "slot": 36, "discharge_wh": 9000, "grid_charge_wh": 0, "solar_charge_wh": 0}]
    lifetime = {"charged_kwh": 2800, "discharged_kwh": 2520}  # 280 kWh out and 2800 kWh in by then... plus these days
    p = payback(tariff, slots, date(2026, 10, 10), lifetime=lifetime)  # the 10th: all seven days are finished
    assert p["per_day"] == 1.9 and p["recorded_days"] == 7 and p["days"] == 283
    assert p["saved"] == pytest.approx(7 * 1.9 + p["saved_before_recording"], abs=0.01)
    assert p["payback_days"] == round((3000 - p["saved"]) / 1.9)
