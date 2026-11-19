"""The app proves its own storage answers with its own credentials (DEP-08).

The release smoke test probed the Spaces bucket with the CI runner's
credentials, which CI never had, so it could only ever warn; the last green
staging run logged exactly that. /health/storage makes the app probe with its
keys, and the smoke test asserts the answer.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import object_storage as os_mod

SMOKE = Path(__file__).resolve().parents[2] / "infra" / "digitalocean" / "smoke-test.sh"


class _Client:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def head_bucket(self, Bucket):
        self.calls.append(Bucket)
        if self.error:
            raise self.error


def _err(code, status):
    return ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "HeadBucket")


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setattr(os_mod, "BACKEND", "s3")
    monkeypatch.setattr(os_mod, "S3_BUCKET", "neoh-media-test")

    def use(client):
        monkeypatch.setattr(os_mod, "_s3_probe_client", lambda: client)
        return client
    return use


@pytest.mark.parametrize("error,state", [
    (None, "healthy"),
    (_err("403", 403), "auth_failed"),
    (_err("InvalidAccessKeyId", 403), "auth_failed"),
    (_err("404", 404), "misconfigured"),
    (_err("NoSuchBucket", 404), "misconfigured"),
    (_err("503", 503), "degraded"),
    (TimeoutError("read timed out"), "degraded"),
])
def test_the_probe_names_whose_problem_it_is(s3, error, state):
    client = s3(_Client(error))
    assert os_mod.probe() == {"backend": "s3", "state": state}
    assert client.calls == ["neoh-media-test"]


def test_an_unset_bucket_is_misconfigured_without_a_network_call(monkeypatch):
    monkeypatch.setattr(os_mod, "BACKEND", "s3")
    monkeypatch.setattr(os_mod, "S3_BUCKET", "")
    monkeypatch.setattr(os_mod, "_s3_probe_client", lambda: pytest.fail("no call expected"))
    assert os_mod.probe()["state"] == "misconfigured"


def _endpoint(monkeypatch, body):
    import server

    monkeypatch.setattr(server, "_storage_probe_cache", {"at": 0.0, "body": None})
    calls = []

    def probe():
        calls.append(1)
        return body
    monkeypatch.setattr(os_mod, "probe", probe)
    return TestClient(server.app), calls


@pytest.mark.parametrize("state,code", [("healthy", 200), ("degraded", 200), ("not_probed", 200),
                                        ("auth_failed", 503), ("misconfigured", 503), ("unavailable", 503)])
def test_the_endpoint_fails_only_for_causes_that_are_ours(monkeypatch, state, code):
    client, _ = _endpoint(monkeypatch, {"backend": "s3", "state": state})
    r = client.get("/health/storage")
    assert r.status_code == code and r.json() == {"backend": "s3", "state": state}


def test_the_endpoint_needs_no_credential_and_caches_its_probe(monkeypatch):
    client, calls = _endpoint(monkeypatch, {"backend": "s3", "state": "healthy"})
    for _ in range(3):
        assert client.get("/health/storage").status_code == 200
    assert calls == [1], "a burst of calls costs one probe"


def test_the_smoke_test_fails_the_release_on_the_apps_own_storage_errors():
    smoke = SMOKE.read_text()
    assert '"$APP_URL/health/storage"' in smoke
    for state in ("auth_failed", "misconfigured", "unavailable"):
        line = next(ln for ln in smoke.splitlines() if ln.strip().startswith(f"{state})"))
        assert "fail " in line, line
    assert "warn " in next(ln for ln in smoke.splitlines() if ln.strip().startswith("degraded)"))


# ── Go-live storage round trip (audit 30.8) ─────────────────────────────────

def test_round_trip_on_a_local_mount_stores_returns_and_deletes(monkeypatch, tmp_path):
    import object_storage

    monkeypatch.setattr(object_storage, "BACKEND", "local")
    monkeypatch.setattr(object_storage, "MEDIA_ROOT", tmp_path)
    result = object_storage.round_trip()
    assert result["ok"] is True and result["steps"] == {
        "put": True, "get": True, "match": True, "private": "not_applicable", "delete": True, "absent": True}
    assert not any(tmp_path.rglob("*.bin")), "the probe object is gone"


def test_round_trip_fails_when_storage_cannot_delete(monkeypatch, tmp_path):
    import object_storage

    monkeypatch.setattr(object_storage, "BACKEND", "local")
    monkeypatch.setattr(object_storage, "MEDIA_ROOT", tmp_path)
    monkeypatch.setattr(object_storage, "delete_object", lambda key: False)
    result = object_storage.round_trip()
    assert result["ok"] is False and result["steps"]["absent"] is False


def test_round_trip_fails_for_a_publicly_readable_bucket(monkeypatch):
    import object_storage

    store = {}
    monkeypatch.setattr(object_storage, "_check_backend", lambda: "s3")
    monkeypatch.setattr(object_storage, "put_bytes", lambda key, data, ct="": store.__setitem__(key, data) or key)

    def get(key):
        if key not in store:
            raise FileNotFoundError(key)
        return store[key]

    monkeypatch.setattr(object_storage, "get_bytes", get)
    monkeypatch.setattr(object_storage, "delete_object", lambda key: bool(store.pop(key, None)))
    monkeypatch.setattr(object_storage, "signed_url",
                        lambda key, expires_seconds=60: f"https://bucket.example.test/{key}?X-Amz-Signature=x")
    monkeypatch.setattr(object_storage, "_anonymous_get_status", lambda url: 200)
    result = object_storage.round_trip()
    assert result["ok"] is False and result["steps"]["private"] is False
    monkeypatch.setattr(object_storage, "_anonymous_get_status", lambda url: 403)
    assert object_storage.round_trip()["ok"] is True
    assert not store, "every probe object was deleted"
