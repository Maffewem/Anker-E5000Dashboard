import pytest
from fastapi.testclient import TestClient

from app.auth import Auth


def _client(tmp_path, monkeypatch, **env):
    for name in ("SOLARBANK_HOST", "METER_HOST", "OCTOPUS_API_KEY", "ADMIN_PASSWORD", "READ_ONLY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "solarbank.db"))
    from app.main import app

    return TestClient(app)


TARIFF = {"battery_cost": 3000, "peak_rate": 28, "offpeak_rate": 7, "export_rate": 15}


def test_open_by_default(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.get("/api/auth").json()["can_edit"] is True
        assert c.post("/api/tariff", json=TARIFF).status_code == 200
        assert c.get("/api/export/backup").status_code == 200


def test_password_locks_every_change(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, ADMIN_PASSWORD="correct horse") as c:
        state = c.get("/api/auth").json()
        assert state == {"password_set": True, "read_only": False, "signed_in": False, "can_edit": False, "csrf": None}
        for path, body in [("/api/tariff", TARIFF), ("/api/control", {}), ("/api/control/mode", {"mode": 1}),
                           ("/api/octopus", {}), ("/api/compare/custom", []),
                           ("/api/settings/battery", {"host": "10.0.0.1"}),
                           ("/api/settings/battery/test", {"host": "10.0.0.1"})]:
            assert c.post(path, json=body).status_code == 401, path
        assert c.put("/api/tariff", json=TARIFF).status_code == 401  # any method that isn't a read
        assert c.get("/api/export/backup").status_code == 401
        assert c.get("/api/live").status_code == 200  # watching still works

        assert c.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
        r = c.post("/api/auth/login", json={"password": "correct horse"})
        assert r.status_code == 200
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        csrf = r.json()["csrf"]
        assert c.get("/api/auth").json()["can_edit"] is True

        # The cookie alone isn't enough: a change also needs the CSRF token.
        assert c.post("/api/tariff", json=TARIFF).status_code == 403
        assert c.post("/api/tariff", json=TARIFF, headers={"X-CSRF-Token": "nope"}).status_code == 403
        assert c.post("/api/tariff", json=TARIFF, headers={"X-CSRF-Token": csrf}).status_code == 200
        assert c.get("/api/export/backup").status_code == 200

        c.post("/api/auth/logout")
        assert c.post("/api/tariff", json=TARIFF, headers={"X-CSRF-Token": csrf}).status_code == 401


def test_read_only_refuses_even_when_signed_in(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, ADMIN_PASSWORD="pw", READ_ONLY="true") as c:
        csrf = c.post("/api/auth/login", json={"password": "pw"}).json()["csrf"]
        assert c.get("/api/auth").json()["can_edit"] is False
        r = c.post("/api/tariff", json=TARIFF, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 403 and "read-only" in r.json()["detail"]


def test_security_headers(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        h = c.get("/").headers
        assert h["x-frame-options"] == "DENY"
        assert h["x-content-type-options"] == "nosniff"


def test_octopus_key_never_sent(tmp_path, monkeypatch):
    monkeypatch.setattr("app.octopus.Octopus.sync", lambda self: None)  # no network in tests
    with _client(tmp_path, monkeypatch, ADMIN_PASSWORD="pw",
                 OCTOPUS_API_KEY="sk_live_" + "x" * 24, OCTOPUS_ACCOUNT="A-1234ABCD") as c:
        body = c.get("/api/octopus").text
        assert "sk_live" not in body and "1234ABCD" not in body


def test_session_tokens_are_signed_and_expire(monkeypatch):
    auth = Auth("pw")
    token = auth.new_session()
    assert auth.session_valid(token)
    expiry, nonce, sig = token.split(".")
    assert not auth.session_valid(f"{int(expiry) + 1}.{nonce}.{sig}")
    assert not auth.session_valid(Auth("pw").new_session())  # another process's key
    monkeypatch.setattr("app.auth.time.time", lambda: int(expiry) + 1)
    assert not auth.session_valid(token)


def test_too_many_wrong_passwords_are_refused():
    auth = Auth("pw")
    for _ in range(5):
        assert not auth.check_password("1.2.3.4", "guess")
    with pytest.raises(Exception, match="Too many"):
        auth.check_password("1.2.3.4", "pw")
    assert auth.check_password("5.6.7.8", "pw")
