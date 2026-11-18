"""The invitation delivery job's contract with the worker (external audit
21.5): registered under its type, last attempt detected from the claimed job
row, and errors recorded by type only with the right retry decision."""
from __future__ import annotations

import asyncio

import pytest

import automation_jobs
import brokerage_onboarding as bo
import smtp_mailer


def test_the_job_is_registered_under_its_type():
    bo.register()
    assert bo.INVITATION_JOB_TYPE in automation_jobs.registered_handlers()


@pytest.mark.parametrize("attempt,last", [(1, False), (3, False), (4, True)])
def test_the_last_attempt_comes_from_the_claimed_job(monkeypatch, attempt, last):
    seen = {}

    async def deliver(tenant_id, invitation_id, *, last_attempt=False):
        seen.update(tenant=tenant_id, invitation=invitation_id, last=last_attempt)
        return {"status": "sent"}

    monkeypatch.setattr(bo, "deliver_invitation_email", deliver)
    reporter = type("R", (), {"job": {"tenant_id": "t1", "attempt_count": attempt, "max_attempts": 4}})()
    asyncio.run(bo._invitation_job({"invitation_id": "i1"}, reporter))
    assert seen == {"tenant": "t1", "invitation": "i1", "last": last}


@pytest.mark.parametrize("error,status,terminal,cleared", [
    (smtp_mailer.SmtpUncertainError("421 after DATA"), "unconfirmed", True, True),
    (TimeoutError(), "unconfirmed", True, True),
    (smtp_mailer.SmtpConfigurationError("no relay"), "failed", True, True),
    (smtp_mailer.SmtpSendError("550 no such user agent@x.test"), "failed", False, False),
])
def test_each_send_error_is_recorded_by_type_with_its_retry_decision(monkeypatch, error, status, terminal, cleared):
    marks = []

    async def prepared(*_a, **_k):
        return None

    async def mark(ctx, invitation_id, status_, err, *, clear_link):
        marks.append((status_, err, clear_link))

    def send(**_kw):
        raise error

    class _Conn:
        async def fetchrow(self, *_a):
            from datetime import datetime, timedelta, timezone
            return {"email": "agent@x.test", "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
                    "pending_link_ciphertext": b"x", "consumed_at": None, "revoked_at": None,
                    "invited_by_agent_id": "owner@x.test", "expired": False, "brokerage": "B"}

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def tx(_ctx):
        yield _Conn()

    async def decrypt(*_a):
        return "/accept-invite?token=abc"

    import crypto

    monkeypatch.setattr(bo, "tenant_tx", tx)
    monkeypatch.setattr(crypto, "decrypt_pii", decrypt)
    monkeypatch.setattr(bo, "_invitation_key", lambda _t: "k")
    monkeypatch.setattr(bo, "_mark_invitation", mark)
    monkeypatch.setattr(smtp_mailer, "send", send)
    with pytest.raises(bo.InvitationDeliveryError) as exc:
        asyncio.run(bo.deliver_invitation_email("t1", "i1"))
    assert exc.value.job_terminal is terminal
    assert str(exc.value) == type(error).__name__ and "x.test" not in str(exc.value)   # no address stored
    assert marks == [(status, type(error).__name__, cleared)]
