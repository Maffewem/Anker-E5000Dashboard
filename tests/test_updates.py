import json

import pytest
from fastapi.testclient import TestClient

from app.updates import Updates, validate_webhook


def registry(commit="b" * 40, version="1.0.90"):
    """A fake GHCR serving a multi-arch :latest whose image carries these build args."""
    calls = []

    def fetch(url, headers):
        calls.append(url)
        if "/token?" in url:
            return json.dumps({"token": "t"}).encode()
        assert headers["Authorization"] == "Bearer t"
        if url.endswith("/manifests/latest"):
            return json.dumps({"manifests": [
                {"digest": "sha256:att", "platform": {"os": "unknown", "architecture": "unknown"}},
                {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
            ]}).encode()
        if url.endswith("/manifests/sha256:arm"):
            return json.dumps({"config": {"digest": "sha256:cfg"}}).encode()
        if url.endswith("/blobs/sha256:cfg"):
            return json.dumps({"config": {"Env": ["PATH=/usr/bin", f"APP_VERSION={version}",
                                                  f"APP_COMMIT={commit}", "APP_BUILT=2026-10-10"]}}).encode()
        raise OSError(f"unexpected {url}")

    return fetch, calls


def test_newer_image_is_an_update():
    fetch, _ = registry()
    u = Updates("a" * 40, fetch=fetch)
    u.check()
    assert u.status()["latest"] == {"name": "1.0.90", "commit": "b" * 40, "built": "2026-10-10"}
    assert u.available and u.error is None


def test_same_commit_is_up_to_date():
    fetch, _ = registry(commit="a" * 40)
    u = Updates("a" * 40, fetch=fetch)
    u.check()
    assert not u.available


def test_registry_down_is_reported_not_raised():
    def down(url, headers):
        raise OSError("no route")

    u = Updates("a" * 40, fetch=down)
    u.check()
    assert not u.available and "no route" in u.error


def test_webhook_must_be_portainer():
    ok = "https://192.168.0.10:9443/api/stacks/webhooks/0a1b2c3d-1111-2222-3333-444455556666"
    assert validate_webhook(f" {ok} ") == ok
    assert validate_webhook("http://pi.local:9000/api/webhooks/0a1b2c3d-1111")
    for bad in ["ftp://x/api/stacks/webhooks/12345678", "https://x/anything", "https://user:pw@x/api/webhooks/12345678",
                "https://x/api/stacks/webhooks/12345678/../../admin", "file:///etc/passwd"]:
        with pytest.raises(ValueError):
            validate_webhook(bad)


@pytest.fixture
def client(tmp_path, monkeypatch):
    for name in ("SOLARBANK_HOST", "METER_HOST", "ADMIN_PASSWORD", "READ_ONLY", "UPDATE_WEBHOOK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "solarbank.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_webhook_is_saved_but_never_sent_back(client, monkeypatch):
    url = "http://192.168.0.10:9000/api/stacks/webhooks/0a1b2c3d-1111-2222-3333-444455556666"
    assert client.post("/api/update").status_code == 409
    assert client.post("/api/update/webhook", json={"url": "https://example.com/"}).status_code == 422
    r = client.post("/api/update/webhook", json={"url": url})
    assert r.json()["webhook_set"] is True
    assert "0a1b2c3d" not in client.get("/api/version").text

    called = []
    monkeypatch.setattr("app.main.call_webhook", lambda u: called.append(u))
    assert client.post("/api/update").json() == {"started": True}
    assert called == [url]

    monkeypatch.setattr("app.main.call_webhook", lambda u: "Portainer refused the webhook (404).")
    assert client.post("/api/update").status_code == 502


def test_update_needs_sign_in_when_locked(tmp_path, monkeypatch):
    for name in ("SOLARBANK_HOST", "METER_HOST", "READ_ONLY", "UPDATE_WEBHOOK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "solarbank.db"))
    from app.main import app

    with TestClient(app) as c:
        assert c.post("/api/update").status_code == 401
        assert c.post("/api/update/webhook", json={"url": ""}).status_code == 401
        assert c.get("/api/version").status_code == 200
