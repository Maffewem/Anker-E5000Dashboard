import asyncio
import json
import socket

import pytest
from fastapi.testclient import TestClient

from app.collector import tcp_check
from app.config import Connection, ConnectionStore


def test_validate_rejects_junk():
    assert Connection(" 192.168.0.42 ").validate().host == "192.168.0.42"
    for bad in ["", "http://x/", "1.2.3.4; rm", "a b"]:
        with pytest.raises(ValueError):
            Connection(bad).validate()
    with pytest.raises(ValueError):
        Connection("1.2.3.4", port=0).validate()


def test_store_round_trip_and_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("SOLARBANK_HOST", raising=False)
    store = ConnectionStore(str(tmp_path / "settings.json"))
    assert store.load().host == ""
    store.save(Connection("10.0.0.7", 1502, 2))
    assert store.load() == Connection("10.0.0.7", 1502, 2)
    assert json.loads((tmp_path / "settings.json").read_text())["host"] == "10.0.0.7"

    monkeypatch.setenv("SOLARBANK_HOST", "10.0.0.9")
    assert store.from_env
    assert store.load().host == "10.0.0.9"


def test_closed_port_explains_modbus_setting():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # bound but not listening: refused
        with pytest.raises(ConnectionError, match="Modbus TCP"):
            asyncio.run(tcp_check("127.0.0.1", port))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("SOLARBANK_HOST", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "solarbank.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_first_run_is_unconfigured_then_saves(client, tmp_path):
    assert client.get("/api/live").json()["status"]["configured"] is False
    assert client.get("/api/settings").json() == {"host": "", "port": 502, "unit_id": 1, "locked": False}

    bad = client.post("/api/settings", json={"host": "not a host"})
    assert bad.status_code == 422

    ok = client.post("/api/settings?skip_test=true", json={"host": "127.0.0.1", "port": 1})
    assert ok.status_code == 200
    assert json.loads((tmp_path / "settings.json").read_text())["host"] == "127.0.0.1"
    assert client.get("/api/live").json()["status"]["host"] == "127.0.0.1"


def test_env_locks_settings(client, monkeypatch):
    monkeypatch.setenv("SOLARBANK_HOST", "10.0.0.9")
    assert client.get("/api/settings").json()["locked"] is True
    assert client.post("/api/settings?skip_test=true", json={"host": "127.0.0.1"}).status_code == 409
