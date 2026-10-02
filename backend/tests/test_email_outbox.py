"""email_outbox delivery: honest states, never a duplicate (resilience 2026-10-02)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

import email_outbox
from command_providers import ProviderRejectedError, ProviderRequestError, ProviderResult

T = "11111111-1111-1111-1111-111111111111"
ROW = {"id": "o1", "client_id": "c1", "to_email": "a@b.test", "subject": "s", "body_text": "b",
       "created_by": "agent@b.test"}


class Conn:
    def __init__(self, claimable=True):
        self.claimable = claimable
        self.executed = []

    async def fetchrow(self, sql, *args):
        return dict(ROW) if self.claimable and "status='queued'" in sql else None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


@pytest.fixture
def env(monkeypatch):
    conn = Conn()

    @asynccontextmanager
    async def tx(ctx):
        yield conn

    monkeypatch.setattr(email_outbox, "tenant_tx", tx)
    import commands_api

    async def no_cred(*a, **k):
        return None

    async def identity(ctx):
        return {"public_email": "agent@b.test"}

    monkeypatch.setattr(commands_api, "_load_provider_credential", no_cred)
    monkeypatch.setattr(commands_api, "load_agent_identity", identity)
    return conn


def _sender(monkeypatch, behaviour):
    import commands_api

    calls = []

    async def send(draft, credentials=None, reply_to=None):
        calls.append(draft)
        if isinstance(behaviour, Exception):
            raise behaviour
        return ProviderResult("smtp", "<mid@b.test>", "submitted", {})

    monkeypatch.setattr(commands_api, "_resolve_email_provider", lambda: (send, "smtp"))
    return calls


def _statuses(conn):
    return [args[2] for sql, args in conn.executed if "SET status=$3" in sql] + \
           ["sent" for sql, _ in conn.executed if "status='sent'" in sql]


def test_delivered_marks_sent_and_only_then_counts_as_contact(env, monkeypatch):
    _sender(monkeypatch, None)
    assert asyncio.run(email_outbox.deliver(T, "o1")) == {"status": "sent"}
    assert "sent" in _statuses(env)
    assert any("last_contacted_at" in sql for sql, _ in env.executed)


def test_unknown_delivery_stays_sending_and_is_never_resent(env, monkeypatch):
    calls = _sender(monkeypatch, ProviderRequestError("connection lost after DATA"))
    out = asyncio.run(email_outbox.deliver(T, "o1"))   # does not raise → job does not retry
    assert out == {"status": email_outbox.UNKNOWN_DELIVERY}
    assert _statuses(env) == ["sending"]
    assert not any("last_contacted_at" in sql for sql, _ in env.executed)
    assert len(calls) == 1


def test_definite_refusal_requeues_and_lets_the_job_retry(env, monkeypatch):
    _sender(monkeypatch, ProviderRejectedError("connection refused"))
    with pytest.raises(ProviderRejectedError):
        asyncio.run(email_outbox.deliver(T, "o1"))
    assert _statuses(env) == ["queued"]


def test_an_already_claimed_row_is_not_sent_again(monkeypatch):
    conn = Conn(claimable=False)

    @asynccontextmanager
    async def tx(ctx):
        yield conn

    monkeypatch.setattr(email_outbox, "tenant_tx", tx)
    calls = _sender(monkeypatch, None)
    assert asyncio.run(email_outbox.deliver(T, "o1")) == {"status": "not_queued"}
    assert calls == []


def test_recovery_mode_block_is_not_sent_and_not_unknown(env, monkeypatch):
    """Found by the resilience drill: a send refused by recovery mode (before
    any network call) was misfiled as 'delivery unknown'."""
    import recovery_mode

    _sender(monkeypatch, recovery_mode.RecoveryModeBlocked("recovery mode"))
    with pytest.raises(recovery_mode.RecoveryModeBlocked):
        asyncio.run(email_outbox.deliver(T, "o1"))
    assert _statuses(env) == ["queued"]
