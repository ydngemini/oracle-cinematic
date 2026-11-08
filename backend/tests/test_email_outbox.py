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
    _gate(monkeypatch, allowed=True)
    return conn


def _gate(monkeypatch, *, allowed):
    """The send-time compliance re-check and the commercial footer (no DB)."""
    from commercial_email import PreparedEmail
    from outreach_compliance import OutreachDecision

    async def guard(ctx, **kw):
        return OutreachDecision(allowed=allowed, channel="email", contact=kw["contact"], state_code=None,
                                blockers=() if allowed else ("contact is on the do-not-contact / opt-out list",))

    async def prepare(tenant_id, *, recipient, body):
        return PreparedEmail(body=body + "\n--\nfooter", unsubscribe_url="https://neoh.test/u",
                             headers={}, postal_address_configured=True)

    monkeypatch.setattr(email_outbox.outreach_compliance, "guard_outreach", guard)
    monkeypatch.setattr(email_outbox.commercial_email, "prepare", prepare)


def _sender(monkeypatch, behaviour):
    import commands_api

    calls = []

    async def send(draft, credentials=None, reply_to=None, list_unsubscribe_url=None):
        calls.append({**draft, "list_unsubscribe_url": list_unsubscribe_url})
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


def test_an_opt_out_after_queueing_blocks_the_send(env, monkeypatch):
    """CONTRACT C3.4: crm.send_message checked the opt-out list when the row was
    queued; deliver re-checks at execution, so an unsubscribe in between (from
    any agent's email) stops this one. Blocked is final, never retried."""
    calls = _sender(monkeypatch, None)
    _gate(monkeypatch, allowed=False)
    assert asyncio.run(email_outbox.deliver(T, "o1")) == {"status": "blocked"}
    assert calls == []
    assert _statuses(env) == ["failed"]
    error = next(args[3] for sql, args in env.executed if "SET status=$3" in sql)
    assert error.startswith("blocked: ")
    assert not any("last_contacted_at" in sql for sql, _ in env.executed)


def test_a_failed_pre_send_check_requeues_without_sending(env, monkeypatch):
    calls = _sender(monkeypatch, None)

    async def broken(ctx, **kw):
        raise ConnectionError("db down")

    monkeypatch.setattr(email_outbox.outreach_compliance, "guard_outreach", broken)
    with pytest.raises(ConnectionError):
        asyncio.run(email_outbox.deliver(T, "o1"))
    assert calls == [] and _statuses(env) == ["queued"]


def test_the_sent_email_carries_the_footer_and_the_one_click_url(env, monkeypatch):
    calls = _sender(monkeypatch, None)
    assert asyncio.run(email_outbox.deliver(T, "o1")) == {"status": "sent"}
    assert calls[0]["body"].endswith("footer")
    assert calls[0]["list_unsubscribe_url"] == "https://neoh.test/u"


def _entitlement(monkeypatch, refusal):
    calls = []

    async def reading(ctx):
        calls.append((ctx.tenant_id, ctx.role.value))
        if isinstance(refusal, Exception):
            raise refusal
        return refusal

    monkeypatch.setattr(email_outbox.billing, "entitlement_refusal", reading)
    return calls


def test_a_subscription_that_lapsed_after_queueing_blocks_the_send(env, monkeypatch):
    """F18.07: queued while the brokerage paid, sent after it lapsed — no. The
    re-check reads the tenant's own subscription (as the platform: the worker
    has no person), and blocked is final, never retried."""
    calls = _sender(monkeypatch, None)
    reads = _entitlement(monkeypatch, (402, "An active Neoh subscription is required for this action."))
    assert asyncio.run(email_outbox.deliver(T, "o1")) == {"status": "blocked"}
    assert calls == [] and reads == [(T, "platform_admin")]
    assert _statuses(env) == ["failed"]
    error = next(args[3] for sql, args in env.executed if "SET status=$3" in sql)
    assert error == "blocked: An active Neoh subscription is required for this action."
    assert not any("last_contacted_at" in sql for sql, _ in env.executed)


def test_an_unreadable_subscription_status_holds_the_email(env, monkeypatch):
    """Unknown is not missing: neither sent nor finally blocked; the job retries."""
    from fastapi import HTTPException

    calls = _sender(monkeypatch, None)
    _entitlement(monkeypatch, HTTPException(status_code=503, detail="Billing status is unavailable."))
    with pytest.raises(HTTPException):
        asyncio.run(email_outbox.deliver(T, "o1"))
    assert calls == [] and _statuses(env) == ["queued"]


def test_an_entitled_brokerage_still_sends(env, monkeypatch):
    calls = _sender(monkeypatch, None)
    _entitlement(monkeypatch, None)
    assert asyncio.run(email_outbox.deliver(T, "o1")) == {"status": "sent"}
    assert len(calls) == 1
