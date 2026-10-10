import json
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from app import octopus as oct
from app.octopus import Client, Octopus, OctopusError, recommend, tariff_kind, tariff_parts
from app.storage import Storage
from app.tariff import Tariff, payback

UTC = timezone.utc
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def go_rows(start: datetime, days: int) -> list[dict]:
    """Octopus Go: 8.5p from 00:30 to 05:30 UTC, 27p otherwise, listed as Octopus does."""
    rows = []
    day = start.replace(hour=0, minute=0)
    for _ in range(days):
        cheap_a, cheap_b = day + timedelta(minutes=30), day + timedelta(hours=5, minutes=30)
        rows += [
            {"value_inc_vat": 27.0, "valid_from": day.isoformat(), "valid_to": cheap_a.isoformat()},
            {"value_inc_vat": 8.5, "valid_from": cheap_a.isoformat(), "valid_to": cheap_b.isoformat()},
            {"value_inc_vat": 27.0, "valid_from": cheap_b.isoformat(), "valid_to": (day + timedelta(days=1, minutes=30)).isoformat()},
        ]
        day += timedelta(days=1)
    return rows


class FakeOctopus:
    def __init__(self, import_tariff="E-1R-GO-VAR-22-10-14-C", dispatches=None):
        self.import_tariff = import_tariff
        self.dispatches = dispatches or {"completed": [], "planned": []}
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append(url)
        if "/v1/accounts/" in url:
            if headers.get("Authorization") != "Basic c2tfbGl2ZV9hYmNkZWZnaGlqOg==":
                raise urllib.error.HTTPError(url, 401, "no", {}, None)
            return {"properties": [{"moved_in_at": "2020-01-01T00:00:00Z", "moved_out_at": None, "electricity_meter_points": [
                {"is_export": False, "agreements": [{"tariff_code": self.import_tariff, "valid_from": "2025-01-01T00:00:00Z", "valid_to": None}]},
                {"is_export": True, "agreements": [{"tariff_code": "E-1R-OUTGOING-VAR-24-10-26-C", "valid_from": "2025-01-01T00:00:00Z", "valid_to": None}]},
            ]}]}
        if url.endswith("/graphql/"):
            q = json.loads(body)["query"]
            if "obtainKrakenToken" in q:
                return {"data": {"obtainKrakenToken": {"token": "jwt"}}}
            if "completedDispatches" in q:
                return {"data": {"devices": [{"id": "dev1", "deviceType": "ELECTRIC_VEHICLES"}],
                                 "completedDispatches": self.dispatches["completed"]}}
            if "flexPlannedDispatches" in q:
                return {"data": {"flexPlannedDispatches": self.dispatches["planned"]}}
            return {"data": {}}
        if "standing-charges" in url:
            return {"results": [{"value_inc_vat": 48.0, "payment_method": "DIRECT_DEBIT"},
                                {"value_inc_vat": 50.0, "payment_method": "NON_DIRECT_DEBIT"}], "next": None}
        if "standard-unit-rates" in url:
            if "OUTGOING" in url:
                return {"results": [{"value_inc_vat": 15.0, "valid_from": "2025-01-01T00:00:00Z", "valid_to": None}], "next": None}
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            start = datetime.fromisoformat(q["period_from"][0].replace("Z", "+00:00"))
            end = datetime.fromisoformat(q["period_to"][0].replace("Z", "+00:00"))
            return {"results": go_rows(start - timedelta(days=1), (end - start).days + 3), "next": None}
        if "/v1/products/" in url:
            return {"display_name": "Outgoing Octopus" if "OUTGOING" in url else "Octopus Go"}
        raise AssertionError(url)


@pytest.fixture
def storage():
    s = Storage(":memory:", 365, "UTC")
    yield s
    s.close()


def make(storage, fake):
    return Octopus(storage, "sk_live_abcdefghij", "A-1234ABCD", fetch=fake)


def test_tariff_codes():
    assert tariff_parts("E-1R-AGILE-24-10-01-C") == {"energy": "E", "rates": "1R", "product": "AGILE-24-10-01", "region": "C"}
    assert tariff_parts("nonsense") is None
    assert tariff_kind("INTELLI-VAR-24-10-29") == "intelligent_go"
    assert tariff_kind("AGILE-OUTGOING-19-05-13") == "agile_outgoing"
    assert tariff_kind("COSY-22-12-08") == "cosy"
    assert tariff_kind("OUTGOING-VAR-24-10-26") == "outgoing"
    assert tariff_kind("GO-VAR-22-10-14") == "go"
    assert tariff_kind("VAR-22-11-01") == "flat"


def test_validate():
    assert oct.validate(" sk_live_abcdefghij ", "a-1234abcd") == ("sk_live_abcdefghij", "A-1234ABCD")
    with pytest.raises(ValueError):
        oct.validate("abc", "A-1234ABCD")
    with pytest.raises(ValueError):
        oct.validate("sk_live_abcdefghij", "1234")


def test_sync_reads_tariff_prices_and_standing_charge(storage):
    o = make(storage, FakeOctopus())
    o.sync(NOW)
    assert o.last_error is None
    assert o.info["import"]["name"] == "Octopus Go"
    assert o.info["import"]["kind"] == "go"
    assert o.info["import"]["standing_charge_p"] == 48.0  # direct debit price
    assert o.info["export"]["name"] == "Outgoing Octopus"
    rates = storage.rates()
    assert rates[("2026-10-09", 0)] == (27.0, 15.0)
    assert rates[("2026-10-09", 1)] == (8.5, 15.0)  # 00:30
    assert rates[("2026-10-09", 11)] == (27.0, 15.0)  # 05:30


def test_bad_key_is_reported_not_raised(storage):
    o = Octopus(storage, "sk_live_wrongwrong", "A-1234ABCD", fetch=FakeOctopus())
    o.sync(NOW)
    assert "API key" in o.last_error


def test_unreachable_tries_the_other_host(storage):
    fake = FakeOctopus()

    def flaky(url, headers, body):
        if url.startswith(oct.API_HOSTS[0]):
            raise urllib.error.URLError("down")
        return fake(url, headers, body)

    o = make(storage, flaky)
    o.sync(NOW)
    assert o.last_error is None
    assert o.client._host == oct.API_HOSTS[1]


def test_payback_uses_fetched_prices_over_typed_ones(storage):
    make(storage, FakeOctopus()).sync(NOW)
    typed = Tariff(battery_cost=100, peak_rate=40, offpeak_rate=40, export_rate=0)
    slots = [{"day": "2026-10-09", "slot": 2, "discharge_wh": 0, "grid_charge_wh": 4000, "solar_charge_wh": 1000},
             {"day": "2026-10-09", "slot": 36, "discharge_wh": 4000, "grid_charge_wh": 0, "solar_charge_wh": 0},
             {"day": "2020-01-01", "slot": 36, "discharge_wh": 1000, "grid_charge_wh": 0, "solar_charge_wh": 0}]
    p = payback(typed, slots, NOW.date(), storage.rates())
    assert p["grid_charge_cost"] == 0.34  # 4 kWh at 8.5p, not 40p
    assert p["solar_charge_cost"] == 0.15  # 1 kWh of export at 15p
    assert p["discharge_value"] == 1.48  # 4 kWh at 27p + 1 kWh at the typed 40p (no Octopus price that day)


def test_profile_fills_the_payback_form(storage):
    o = make(storage, FakeOctopus())
    o.sync(NOW)
    prof = o.profile()
    assert prof["peak_rate"] == 27.0 and prof["offpeak_rate"] == 8.5
    assert (prof["offpeak_start"], prof["offpeak_end"]) == ("00:30", "05:30")
    assert prof["export_rate"] == 15.0


def test_go_recommends_the_night_window(storage):
    o = make(storage, FakeOctopus())
    o.sync(NOW)
    status = o.status({"rated_kwh": 5.12, "max_charge_w": 2500}, NOW)
    assert status["current_p"] == 27.0
    charge = [w for w in status["recommendations"] if w["kind"] == "charge"]
    assert charge[0]["start"] == "2026-10-10T00:30:00Z" and charge[0]["end"] == "2026-10-10T05:30:00Z"
    assert charge[0]["avg_p"] == 8.5


def test_agile_recommends_cheapest_stretch_and_negative_prices():
    start = NOW
    prices = [30.0] * 48
    prices[10:16] = [12, 11, 10, 9, 10, 11]  # cheapest 2h is slots 11-14
    prices[30] = -2.0
    points = [(start + timedelta(minutes=30 * i), p) for i, p in enumerate(prices)]
    recs = recommend(points, charge_hours=2)
    cheapest = [r for r in recs if r["note"].startswith("Cheapest")][0]
    assert cheapest["start"] == oct._iso(start + timedelta(minutes=30 * 11))
    assert cheapest["end"] == oct._iso(start + timedelta(minutes=30 * 15))
    assert any(r["min_p"] == -2.0 for r in recs)


def test_flat_tariff_recommends_nothing():
    points = [(NOW + timedelta(minutes=30 * i), 24.5) for i in range(48)]
    assert recommend(points, 2) == []


def test_intelligent_go_dispatches_are_priced_off_peak(storage):
    completed = [{"start": "2026-10-09T10:00:00Z", "end": "2026-10-09T11:00:00Z", "delta": -3.2}]
    planned = [{"start": "2026-10-09T18:00:00Z", "end": "2026-10-09T19:30:00Z", "type": "SMART"}]
    o = make(storage, FakeOctopus("E-1R-INTELLI-VAR-24-10-29-C", {"completed": completed, "planned": planned}))
    o.sync(NOW)
    assert o.info["import"]["kind"] == "intelligent_go"
    rates = storage.rates()
    assert rates[("2026-10-09", 20)][0] == 8.5  # 10:00 dispatch billed off-peak
    assert rates[("2026-10-09", 22)][0] == 27.0  # 11:00 normal
    assert o.status({}, NOW)["dispatches"] == [{"start": "2026-10-09T18:00:00Z", "end": "2026-10-09T19:30:00Z"}]


def test_graphql_error_surfaces_as_octopus_error():
    def fetch(url, headers, body):
        return {"errors": [{"message": "Invalid data"}]}
    with pytest.raises(OctopusError):
        Client("sk_live_abcdefghij", "A-1234ABCD", fetch).dispatches()


def test_api_connects_fills_payback_and_disconnects(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    for name in ("SOLARBANK_HOST", "METER_HOST", "OCTOPUS_API_KEY", "OCTOPUS_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "solarbank.db"))
    monkeypatch.setattr(oct, "_urllib_fetch", FakeOctopus())
    from app.main import app

    with TestClient(app) as c:
        assert c.get("/api/octopus").json()["configured"] is False
        assert c.post("/api/octopus", json={"api_key": "nope", "account": "A-1234ABCD"}).status_code == 422
        bad = c.post("/api/octopus", json={"api_key": "sk_live_wrongwrong", "account": "A-1234ABCD"})
        assert bad.status_code == 422 and "API key" in bad.json()["detail"]
        ok = c.post("/api/octopus", json={"api_key": "sk_live_abcdefghij", "account": "a-1234abcd"}).json()
        assert ok["configured"] and ok["import"]["name"] == "Octopus Go"
        assert "api_key" not in json.dumps(ok)  # the key never goes back to the browser
        saved = json.loads((tmp_path / "settings.json").read_text())
        assert saved["octopus"]["account"] == "A-1234ABCD"
        pb = c.get("/api/payback").json()
        assert pb["source"] == "octopus" and pb["tariff_name"] == "Octopus Go"
        assert pb["tariff"]["offpeak_rate"] == 8.5
        manual = c.post("/api/tariff", json={"battery_cost": 3000, "peak_rate": 30, "offpeak_rate": 7, "use_manual": True}).json()
        assert manual["source"] == "manual" and manual["use_manual"] and manual["tariff"]["peak_rate"] == 30
        back = c.post("/api/tariff", json={**manual["manual"], "use_manual": False}).json()
        assert back["source"] == "octopus" and back["manual"]["peak_rate"] == 30  # typed prices kept
        c.post("/api/octopus", json={"api_key": ""})
        assert c.get("/api/octopus").json()["configured"] is False
        assert c.get("/api/payback").json()["source"] == "manual"


def test_no_price_for_now_is_an_error_not_silence(storage):
    fake = FakeOctopus()

    def no_rates(url, headers, body):
        if "standard-unit-rates" in url:
            return {"results": [], "next": None}
        return fake(url, headers, body)

    o = make(storage, no_rates)
    o.sync(NOW)
    assert "no price for now" in o.last_error
    assert o.status({}, NOW)["diagnostics"]["prices_stored"] == 0


def test_dispatch_failure_keeps_prices(storage):
    fake = FakeOctopus("E-1R-INTELLI-VAR-24-10-29-C")

    def broken_graphql(url, headers, body):
        if url.endswith("/graphql/"):
            return {"errors": [{"message": "Unauthorized"}]}
        return fake(url, headers, body)

    o = make(storage, broken_graphql)
    o.sync(NOW)
    assert o.status({}, NOW)["current_p"] == 27.0
    assert "Intelligent Go" in o.last_error


def test_env_without_account_says_so(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DB_PATH", str(tmp_path / "solarbank.db"))
    monkeypatch.setenv("OCTOPUS_API_KEY", "sk_live_abcdefghij")
    monkeypatch.delenv("OCTOPUS_ACCOUNT", raising=False)
    from app.main import app

    with TestClient(app) as c:
        body = c.get("/api/octopus").json()
        assert body["configured"] is False and "OCTOPUS_ACCOUNT" in body["last_error"]


def test_smart_charge_slots_are_re_read_between_syncs(storage):
    fake = FakeOctopus("E-1R-INTELLI-VAR-24-10-29-C")
    o = make(storage, fake)
    o.sync(NOW)
    assert o.status({}, NOW)["dispatches"] == []
    fake.dispatches["planned"] = [{"start": "2026-10-09T12:20:00Z", "end": "2026-10-09T12:40:00Z", "type": "BOOST"}]
    o.refresh_dispatches()
    assert o.status({}, NOW)["dispatches"] == [{"start": "2026-10-09T12:20:00Z", "end": "2026-10-09T12:40:00Z"}]
    fake.dispatches["planned"] = []  # cancelled
    o.refresh_dispatches()
    assert o.status({}, NOW)["dispatches"] == []


def test_refresh_keeps_slots_when_octopus_fails(storage):
    planned = [{"start": "2026-10-09T12:20:00Z", "end": "2026-10-09T12:40:00Z"}]
    fake = FakeOctopus("E-1R-INTELLI-VAR-24-10-29-C", {"completed": [], "planned": planned})
    o = make(storage, fake)
    o.sync(NOW)
    o.client._fetch = lambda *a: (_ for _ in ()).throw(OctopusError("down"))
    o.refresh_dispatches()
    assert len(o.status({}, NOW)["dispatches"]) == 1


def test_refresh_does_nothing_off_intelligent_go(storage):
    fake = FakeOctopus()
    o = make(storage, fake)
    o.sync(NOW)
    n = len(fake.calls)
    o.refresh_dispatches()
    assert len(fake.calls) == n


def test_supplier_picks_whether_octopus_prices_are_used(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    for name in ("SOLARBANK_HOST", "METER_HOST", "OCTOPUS_API_KEY", "OCTOPUS_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "solarbank.db"))
    monkeypatch.setattr(oct, "_urllib_fetch", FakeOctopus())
    from app.main import app

    with TestClient(app) as c:
        pb = c.get("/api/payback").json()
        assert pb["supplier"] == "other" and not pb["supplier_picked"]  # nothing connected yet
        assert [s["value"] for s in pb["suppliers"]] == ["octopus", "eon", "edf", "british_gas", "other"]
        eon = c.post("/api/supplier", json={"supplier": "eon"}).json()
        assert eon["supplier"] == "eon" and eon["supplier_picked"]
        assert c.post("/api/supplier", json={"supplier": "nobody"}).status_code == 422
        # Saving costs without naming a supplier keeps the one picked.
        assert c.post("/api/tariff", json={"battery_cost": 2000, "peak_rate": 27, "offpeak_rate": 7}).json()["supplier"] == "eon"
        # Connecting Octopus means you're with Octopus, and its prices are used.
        c.post("/api/octopus", json={"api_key": "sk_live_abcdefghij", "account": "A-1234ABCD"})
        pb = c.get("/api/payback").json()
        assert pb["supplier"] == "octopus" and pb["source"] == "octopus"
        # Picking another supplier switches to the typed prices even with Octopus connected.
        pb = c.post("/api/supplier", json={"supplier": "edf"}).json()
        assert pb["source"] == "manual" and not pb["octopus_connected"] and pb["tariff"]["peak_rate"] == 27
        assert c.get("/api/octopus").json()["supplier"] == "edf"
