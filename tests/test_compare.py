import json
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from app import octopus as oct
from app.compare import Candidate, Comparer, compare, simulate, usage_by_day
from app.tariff import fixed_profile
from tests.test_octopus import go_rows

UTC = timezone.utc


def usage(days=7, evening_kw=2.0, base_kw=0.3):
    """A house using base_kw all day, more from 17:00-21:00, and no solar."""
    out = []
    for d in range(days):
        day = f"2026-10-{d + 1:02d}"
        for s in range(48):
            kw = evening_kw if 34 <= s < 42 else base_kw
            out.append({"day": day, "slot": s, "home_wh": kw * 500, "solar_wh": 0.0, "minutes": 30})
    return out


def flat(p):
    return Candidate("flat", f"Flat {p}p", 50.0, import_profile=[p] * 48, export_profile=[15.0] * 48)


def test_battery_shifts_evening_use_to_cheap_hours():
    go = Candidate("go", "Go", 50.0, import_profile=fixed_profile(30, 8, "00:30", "05:30"), export_profile=[15.0] * 48)
    days = usage_by_day(usage())
    with_battery = simulate(go, days, cap_kwh=5, power_kw=2.5)
    without = simulate(go, days, cap_kwh=5, power_kw=2.5, battery=False)
    assert with_battery["cost"] < without["cost"]
    assert with_battery["days"] == 7
    # On a flat price there is nothing to gain from grid charging.
    assert simulate(flat(25), days, 5, 2.5)["cost"] == simulate(flat(25), days, 5, 2.5, battery=False)["cost"]


def test_compare_ranks_cheapest_first_and_compares_to_current():
    current = flat(28)
    current.key = "current"
    go = Candidate("go", "Go", 50.0, import_profile=fixed_profile(30, 8, "00:30", "05:30"), export_profile=[15.0] * 48)
    out = compare([current, go], usage(), 5, 2.5)
    assert out["best"] == "Go"
    assert out["rows"][0]["vs_current"] < 0
    assert out["without_battery"]["annual"] > 0
    assert out["days"] == 7


def test_partial_half_hours_are_scaled_and_tiny_ones_dropped():
    days = usage_by_day([{"day": "d", "slot": 0, "home_wh": 100, "solar_wh": 0, "minutes": 15},
                         {"day": "d", "slot": 1, "home_wh": 100, "solar_wh": 0, "minutes": 5}])
    assert days["d"][0] == 0.2 and days["d"][1] is None


class FakeProducts:
    def __call__(self, url, headers, body):
        u = urllib.parse.urlparse(url)
        if u.path == "/v1/products/":
            return {"results": [
                {"code": "GO-VAR-22-10-14", "display_name": "Octopus Go", "available_from": "2022-10-14T00:00:00Z"},
                {"code": "GO-VAR-20-01-01", "display_name": "Old Go", "available_from": "2020-01-01T00:00:00Z"},
                {"code": "VAR-22-11-01", "display_name": "Flexible Octopus", "available_from": "2022-11-01T00:00:00Z"},
                {"code": "OUTGOING-VAR-24-10-26", "display_name": "Outgoing Octopus", "available_from": "2024-10-26T00:00:00Z"},
            ], "next": None}
        if "standing-charges" in url:
            return {"results": [{"value_inc_vat": 50.0}], "next": None}
        if "standard-unit-rates" in url:
            q = urllib.parse.parse_qs(u.query)
            start = datetime.fromisoformat(q["period_from"][0].replace("Z", "+00:00"))
            if "OUTGOING" in url:
                return {"results": [{"value_inc_vat": 15.0, "valid_from": "2024-01-01T00:00:00Z", "valid_to": None}], "next": None}
            if "GO-VAR" in url:
                assert "GO-VAR-22-10-14-B" in url  # newest version, user's region
                return {"results": go_rows(start, 12), "next": None}
            return {"results": [{"value_inc_vat": 24.5, "valid_from": "2024-01-01T00:00:00Z", "valid_to": None}], "next": None}
        raise AssertionError(url)


def test_candidates_from_octopus_products():
    comparer = Comparer(FakeProducts())
    start = datetime(2026, 10, 1, tzinfo=UTC)
    cands, problems = comparer.candidates("B", start, start + timedelta(days=7), UTC, None, [
        {"name": "E.ON Next Drive", "peak_rate": 27, "offpeak_rate": 7, "offpeak_start": "00:00",
         "offpeak_end": "07:00", "export_rate": 16, "standing_p": 45}])
    names = [c.name for c in cands]
    assert names == ["Octopus Go", "Flexible Octopus", "E.ON Next Drive"]
    assert problems == []
    go = cands[0]
    assert go.standing_p == 50.0 and go.export_name == "Outgoing Octopus"
    assert go.import_p("2026-10-02", 2) == 8.5  # 01:00 UTC
    out = compare(cands, usage(), 5, 2.5)
    assert out["best"] in ("Octopus Go", "E.ON Next Drive")


def test_api_compare_and_custom(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    for name in ("SOLARBANK_HOST", "METER_HOST", "OCTOPUS_API_KEY", "OCTOPUS_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "solarbank.db"))
    monkeypatch.setattr(oct, "_urllib_fetch", FakeProducts())
    from app.main import app

    with TestClient(app) as c:
        assert c.get("/api/compare").json()["days"] == 0
        storage = app.state.storage
        now = int(time.time()) // 60 * 60
        for i in range(3 * 24 * 60):
            ts = now - i * 60
            storage._db.execute("INSERT INTO minutes (ts, home_wh, solar_wh) VALUES (?, ?, ?)", (ts, 10.0, 0.0))
        storage._db.commit()
        bad = c.post("/api/compare/custom", json=[{"name": "X", "peak_rate": 27, "offpeak_rate": 7, "offpeak_start": "25:00"}])
        assert bad.status_code == 422
        ok = c.post("/api/compare/custom", json=[{"name": "EDF GoElectric", "peak_rate": 27, "offpeak_rate": 8,
                                                   "offpeak_start": "00:00", "offpeak_end": "05:00", "export_rate": 15}])
        assert ok.status_code == 200
        out = c.get("/api/compare?region=B").json()
        names = [r["name"] for r in out["rows"]]
        assert "EDF GoElectric" in names and "Octopus Go" in names
        assert any(r["key"] == "current" for r in out["rows"])
        assert out["days"] >= 3
        assert json.dumps(out)  # serialisable


def test_breakdown_adds_up_and_names_the_charge_window():
    go = Candidate("go", "Go", 50.0, import_profile=fixed_profile(30, 8, "00:30", "05:30"), export_profile=[15.0] * 48)
    r = simulate(go, usage_by_day(usage()), cap_kwh=5, power_kw=2.5)
    b = r["breakdown"]
    assert abs(b["home"] + b["battery_charging"] - b["export"] + b["standing"] - r["annual"]) <= 2
    assert b["battery_charging"] > 0 and r["daily"]["grid_charge_kwh"] > 0
    assert r["charge_window"] == "00:30-05:30"
    out = compare([go], usage(), 5, 2.5)
    assert out["daily_use_kwh"] == round((40 * 0.15 + 8 * 1.0), 1)


def test_ranges_wrap_midnight():
    from app.compare import _ranges
    assert _ranges({46, 47, 0, 1}) == "23:00-01:00"
    assert _ranges({2, 3, 10}) == "01:00-02:00, 05:00-05:30"


def test_flat_tariff_still_uses_stored_solar():
    sunny = [dict(u, solar_wh=1500.0 if 20 <= u["slot"] < 28 else 0.0) for u in usage()]
    days = usage_by_day(sunny)
    with_battery = simulate(flat(25), days, 5, 2.5)
    assert with_battery["daily"]["from_battery_kwh"] > 0
    assert with_battery["cost"] < simulate(flat(25), days, 5, 2.5, battery=False)["cost"]


def test_without_solar_the_battery_grid_charges_to_cover_the_peak():
    # Cheap overnight, a mid rate most of the day and a short evening peak; no solar at all.
    prices = [20.0] * 8 + [22.5] * 26 + [45.0] * 6 + [22.5] * 8
    cosy = Candidate("cosy", "Three-rate", 50.0, import_profile=prices, export_profile=[15.0] * 48)
    days = usage_by_day(usage())
    r = simulate(cosy, days, 5, 2.5)
    assert r["battery_mode"] == "grid" and r["daily"]["solar_stored_kwh"] == 0
    assert r["cost"] < simulate(cosy, days, 5, 2.5, battery=False)["cost"]
    assert simulate(flat(25), days, 5, 2.5)["battery_mode"] == "flat"
    close = Candidate("close", "Close", 50.0, import_profile=fixed_profile(25, 24, "00:30", "05:30"), export_profile=[15.0] * 48)
    assert simulate(close, days, 5, 2.5)["battery_mode"] == "small_gap"
