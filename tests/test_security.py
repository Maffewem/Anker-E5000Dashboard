import asyncio
import json
import threading

import pytest

from app.config import ConnectionStore
from app.control import MAX_SETPOINT_W
from tests.test_auth import TARIFF, _client
from tests.test_control import IN, FakeCollector, make


def test_concurrent_saves_never_corrupt_or_lose_settings(tmp_path):
    store = ConnectionStore(str(tmp_path / "settings.json"))
    errors = []

    def save(name):
        try:
            for i in range(150):
                store.save_section(name, {"i": i})
        except Exception as err:  # pragma: no cover - the failure being tested for
            errors.append(err)

    threads = [threading.Thread(target=save, args=(f"s{k}",)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(json.loads((tmp_path / "settings.json").read_text())) == ["s0", "s1", "s2", "s3"]
    assert (tmp_path / "settings.json").stat().st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]  # no temp files left behind


def test_unreadable_settings_file_is_kept_before_the_next_save(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    store = ConnectionStore(str(path))
    store.save_section("tariff", {"peak_rate": 30})
    assert (tmp_path / "settings.json.corrupt").read_text() == "{not json"


def test_changes_from_another_site_are_refused_even_without_a_password(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        evil = {"Origin": "https://evil.example"}
        assert c.post("/api/auth/logout", headers=evil).status_code == 403
        assert c.post("/api/tariff", json=TARIFF, headers=evil).status_code == 403
        assert c.post("/api/tariff", json=TARIFF, headers={"Origin": "http://testserver"}).status_code == 200
        assert c.post("/api/tariff", json=TARIFF).status_code == 200  # no Origin: curl, scripts
        assert c.get("/api/live", headers=evil).status_code == 200  # reading is unchanged


def test_allowed_origins_for_a_proxy_that_rewrites_host(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, ALLOWED_ORIGINS="https://solar.example.com") as c:
        assert c.post("/api/tariff", json=TARIFF, headers={"Origin": "https://solar.example.com"}).status_code == 200
        assert c.post("/api/tariff", json=TARIFF, headers={"Origin": "https://other.example.com"}).status_code == 403


def test_signing_out_revokes_the_token_itself(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, ADMIN_PASSWORD="correct horse") as c:
        r = c.post("/api/auth/login", json={"password": "correct horse"})
        csrf, token = r.json()["csrf"], c.cookies.get("solarbank_session")
        c.post("/api/auth/logout")
        # A copied cookie from before signing out no longer works.
        c.cookies.set("solarbank_session", token)
        assert c.get("/api/auth").json()["signed_in"] is False
        assert c.post("/api/tariff", json=TARIFF, headers={"X-CSRF-Token": csrf}).status_code == 401


def test_failed_logins_are_counted_per_visitor_behind_a_trusted_proxy(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, ADMIN_PASSWORD="correct horse", TRUST_PROXY="true") as c:
        attacker = {"CF-Connecting-IP": "203.0.113.9"}
        for _ in range(5):
            assert c.post("/api/auth/login", json={"password": "x"}, headers=attacker).status_code == 401
        assert c.post("/api/auth/login", json={"password": "x"}, headers=attacker).status_code == 429
        # The owner, arriving through the same proxy, is not locked out by the attacker.
        owner = {"CF-Connecting-IP": "198.51.100.7"}
        assert c.post("/api/auth/login", json={"password": "correct horse"}, headers=owner).status_code == 200


def test_without_trust_proxy_the_forwarded_address_is_ignored(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, ADMIN_PASSWORD="correct horse") as c:
        for i in range(5):
            c.post("/api/auth/login", json={"password": "x"}, headers={"X-Forwarded-For": f"10.0.0.{i}"})
        assert c.post("/api/auth/login", json={"password": "x"}, headers={"X-Forwarded-For": "10.9.9.9"}).status_code == 429


@pytest.mark.parametrize("key,value", [("battery_power_setpoint", MAX_SETPOINT_W + 1),
                                       ("battery_power_setpoint", -10**6),
                                       ("operating_mode", 99), ("charging_limit_soc", 250),
                                       ("discharge_limit_soc", -1), ("something_else", 1)])
def test_nothing_outside_the_safe_ranges_reaches_the_battery(monkeypatch, key, value):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c = FakeCollector()
    ctl = make(c, {"enabled": False}, {})
    with pytest.raises(ValueError):
        asyncio.run(ctl._write(key, value))
    assert c.writes == []


def test_a_battery_reporting_an_absurd_maximum_is_still_clamped(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c = FakeCollector()
    c.snapshot = {"soc": 10, "max_charge_w": 10**6, "rated_kwh": 5.0}
    ctl = make(c, {"enabled": True, "grid_charge": True, "charge_power_w": 5000}, {})
    asyncio.run(ctl.tick(IN))
    assert all(abs(v) <= MAX_SETPOINT_W for k, v in c.writes if k == "battery_power_setpoint")
