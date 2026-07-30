"""Regression tests for distributed ACS callback state."""

import asyncio
import json
import sys
from types import SimpleNamespace

import acs_call_handler


class _FakeLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.expirations = {}

    async def set(self, key, value, *, ex=None, **_kwargs):
        self.values[key] = value
        self.expirations[key] = ex
        return True

    async def get(self, key):
        return self.values.get(key)

    async def exists(self, key):
        return int(key in self.values)

    async def ping(self):
        return True

    async def delete(self, key):
        self.values.pop(key, None)
        self.expirations.pop(key, None)
        return 1

    def lock(self, _key, **_kwargs):
        return _FakeLock()


def test_call_state_is_tenant_scoped_and_ttl_bounded(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-acs-state-master-key")

    async def get_redis():
        return redis

    monkeypatch.setattr(acs_call_handler, "_get_redis", get_redis)
    state = asyncio.run(
        acs_call_handler.initialize_call_state(
            "call-123",
            "+15551234567",
            tenant_id="tenant-abc",
            credentials={
                "connection_string": "endpoint=https://tenant.example;accesskey=secret",
                "from_number": "+15557654321",
            },
        )
    )

    key = "acs:call_state:call-123"
    persisted = json.loads(redis.values[key])
    assert state["tenant_id"] == "tenant-abc"
    assert persisted["tenant_id"] == "tenant-abc"
    assert persisted["direction"] == "outbound"
    assert "acs_credentials" not in persisted
    assert "secret" not in redis.values[key]
    assert persisted["acs_credentials_ciphertext"]
    assert redis.expirations[key] == acs_call_handler._STALE_CALL_TTL_SECONDS


def test_acs_client_uses_credentials_from_persisted_state(monkeypatch):
    observed = {}

    class _Client:
        @classmethod
        def from_connection_string(cls, connection_string):
            observed["connection_string"] = connection_string
            return object()

    callautomation = SimpleNamespace(CallAutomationClient=_Client)
    monkeypatch.setitem(
        sys.modules,
        "azure.communication.callautomation",
        callautomation,
    )
    client = acs_call_handler._get_client({
        "tenant_id": "tenant-abc",
        "acs_credentials": {
            "connection_string": "endpoint=https://tenant.example;accesskey=secret",
            "from_number": "+15557654321",
        },
    })

    assert client is not None
    assert observed["connection_string"] == (
        "endpoint=https://tenant.example;accesskey=secret"
    )


def test_missing_distributed_state_does_not_dereference_caller(monkeypatch):
    redis = _FakeRedis()

    async def get_redis():
        return redis

    monkeypatch.setattr(acs_call_handler, "_get_redis", get_redis)
    asyncio.run(
        acs_call_handler.handle_play_completed(
            "unknown-call",
            "greeting",
        )
    )


def test_inbound_call_connected_event_does_not_replay_greeting(monkeypatch):
    redis = _FakeRedis()
    played = []
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-acs-state-master-key")

    async def get_redis():
        return redis

    async def play_text(*_args, **_kwargs):
        played.append(True)

    monkeypatch.setattr(acs_call_handler, "_get_redis", get_redis)
    monkeypatch.setattr(acs_call_handler, "_play_text", play_text)
    asyncio.run(
        acs_call_handler.initialize_call_state(
            "inbound-call",
            "+15551234567",
            tenant_id="tenant-abc",
            credentials={
                "connection_string": "endpoint=https://tenant.example;accesskey=secret",
                "from_number": "+15557654321",
            },
            direction="inbound",
        )
    )
    asyncio.run(
        acs_call_handler.start_outbound_conversation(
            "inbound-call",
            "+15551234567",
        )
    )
    assert played == []
