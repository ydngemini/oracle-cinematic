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
    _access(monkeypatch, True)
    _gate(monkeypatch, allowed=True)
    return conn


def _access(monkeypatch, outcome):
    """The sender's live access to the client (audit 26.2): True, False, or an
    exception instance to raise. Records each check."""
    import commands_api

    checks = []

    async def check(tenant_id, requested_by, client_id, contact_id, *, allow_pipeline=True):
        # The 4th field is "members only": no system pipeline admitted
        # (L09-12). The real default is already False; the outbox must say
        # so explicitly, so this stand-in defaults the other way.
        checks.append((requested_by, client_id, contact_id, not allow_pipeline))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(commands_api, "requester_may_contact_record", check)
    return checks


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


def _real_smtp_sender(monkeypatch):
    """The real command_providers.send_smtp_email, with the mail server a
    tripwire: what it refuses, it refuses before connecting."""
    import command_providers
    import commands_api
    import smtp_mailer

    reached = []

    def server(**_k):
        reached.append("smtp")
        raise AssertionError("the mail server must not be reached")

    monkeypatch.setattr(smtp_mailer, "send", server)
    monkeypatch.setattr(commands_api, "_resolve_email_provider",
                        lambda: (command_providers.send_smtp_email, "smtp"))
    return reached


def test_an_email_refused_before_the_mail_server_is_failed_not_delivery_unknown(env, monkeypatch):
    """PR #147 review: an address send_smtp_email refuses before connecting was
    a plain ProviderRequestError, so the outbox filed a definitely-unsent email
    as 'delivery unknown' (status 'sending', never resent, never failed)."""
    monkeypatch.delenv("ORACLE_RECOVERY_MODE", raising=False)
    monkeypatch.setitem(ROW, "to_email", "not-an-address")
    reached = _real_smtp_sender(monkeypatch)
    assert asyncio.run(email_outbox.deliver(T, "o1")) == {"status": "failed"}   # final: the job does not retry
    assert _statuses(env) == ["failed"]
    error = next(args[3] for sql, args in env.executed if "SET status=$3" in sql)
    assert error.startswith("Not sent:") and email_outbox.UNKNOWN_DELIVERY not in error
    assert reached == []


def test_the_real_smtp_sender_in_recovery_mode_is_still_held_for_later(env, monkeypatch):
    """The send adapter's recovery-mode refusal is a RecoveryModeBlocked
    subclass now (so the command worker can tell it is certain): the outbox
    must still hold the email in the queue for after recovery, not fail it."""
    import recovery_mode

    monkeypatch.setenv("ORACLE_RECOVERY_MODE", "1")
    reached = _real_smtp_sender(monkeypatch)
    with pytest.raises(recovery_mode.RecoveryModeBlocked):
        asyncio.run(email_outbox.deliver(T, "o1"))
    assert _statuses(env) == ["queued"]
    assert reached == []


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


# ── the sender's live access (external audit 26.2) ─────────────────────────

def test_a_sender_who_lost_access_to_the_client_never_sends(env, monkeypatch):
    """Queued, then the client was reassigned / the share revoked / the agent
    left: final 'failed', nothing reaches the provider, no billing read."""
    sent = _sender(monkeypatch, "ok")
    checks = _access(monkeypatch, False)
    reads = _entitlement(monkeypatch, None)
    assert asyncio.run(email_outbox.deliver("t1", "o1")) == {"status": "blocked"}
    assert sent == []
    assert _statuses(env) == ["failed"]
    errors = [args[3] for sql, args in env.executed if "SET status=$3" in sql]
    assert "no longer has access" in errors[-1]
    assert checks and checks[0][3] is True      # departed/deactivated senders are refused
    assert reads == []


def test_the_access_check_runs_as_the_stored_sender_for_the_stored_client(env, monkeypatch):
    _sender(monkeypatch, "ok")
    checks = _access(monkeypatch, True)
    asyncio.run(email_outbox.deliver("t1", "o1"))
    (requested_by, client_id, contact_id, members_only), = checks
    assert (requested_by, client_id, contact_id, members_only) == (
        ROW["created_by"], ROW["client_id"], None, True)


def test_an_unreadable_access_check_holds_the_email(env, monkeypatch):
    sent = _sender(monkeypatch, "ok")
    _access(monkeypatch, RuntimeError("db down"))
    with pytest.raises(RuntimeError):
        asyncio.run(email_outbox.deliver("t1", "o1"))
    assert sent == []
    assert _statuses(env) == ["queued"]
