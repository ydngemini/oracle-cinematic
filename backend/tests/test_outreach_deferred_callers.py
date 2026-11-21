"""A gate that could not decide yet is never a final refusal (review of PR
#173). When pending spoken STOPs cannot be read (Valkey down), the outreach
gate denies with OutreachDecision.transient set; every caller must turn that
into a retry or a "try again", never a recorded refusal that drops the send.
Unattended callers are covered beside their own tests (speed-to-lead,
missions, the command worker); this file holds the interactive ones and the
job worker's retry spacing."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

import outreach_compliance as oc
from tenancy import Role, TenantContext

TENANT = "11111111-1111-1111-1111-111111111111"
CONTACT = "22222222-2222-4222-8222-222222222222"
CTX = TenantContext(agent_id="agent@x.test", tenant_id=TENANT, role=Role.AGENT)


def undecidable(*_a, **_k):
    async def run():
        return oc.OutreachDecision(allowed=False, channel="voice", contact="+13025550142", state_code="DE",
                                   blockers=(oc.OPT_OUT_STATUS_UNAVAILABLE,),
                                   transient=oc.OPT_OUT_STATUS_UNAVAILABLE_CODE)
    return run()


def refused(*_a, **_k):
    async def run():
        return oc.OutreachDecision(allowed=False, channel="voice", contact="+13025550142", state_code="DE",
                                   blockers=("contact is on the do-not-contact / opt-out list",))
    return run()


def _prepare(monkeypatch, decide):
    import record_access
    import telephony_api

    rows = iter([
        {"id": CONTACT},
        {"id": "r1", "voice_caller_id_e164": "+13025550100", "voice_caller_id_verified": True},
    ])

    class Conn:
        async def fetchrow(self, *_a):
            return next(rows)

    @asynccontextmanager
    async def tx(_ctx):
        yield Conn()

    async def contact_json(_conn, _ctx, _row):
        return {"phone": "+13025550142", "state_code": "DE", "timezone": "America/New_York"}

    async def may_edit(*_a):
        return None

    monkeypatch.setattr(telephony_api, "require_feature", lambda *_a: None)
    monkeypatch.setattr(telephony_api, "tenant_tx", tx)
    monkeypatch.setattr(telephony_api, "_contact_json", contact_json)
    monkeypatch.setattr(record_access, "require_contact_edit", may_edit)
    monkeypatch.setattr(telephony_api, "guard_outreach", decide)
    body = telephony_api.AgentCallPrepare(contact_id=CONTACT)
    with pytest.raises(HTTPException) as err:
        asyncio.run(telephony_api.prepare_agent_call(body, CTX))
    return err.value


def test_an_agent_call_the_gate_cannot_decide_says_try_again(monkeypatch):
    err = _prepare(monkeypatch, undecidable)
    assert err.status_code == 503
    assert err.headers["Retry-After"] == str(oc.TRANSIENT_RETRY_SECONDS)
    assert err.detail["code"] == oc.OPT_OUT_STATUS_UNAVAILABLE_CODE


def test_an_agent_call_the_gate_refuses_is_still_a_legal_refusal(monkeypatch):
    assert _prepare(monkeypatch, refused).status_code == 451


def test_the_bridged_agent_call_says_try_again_when_undecided():
    """The TwiML leg (twilio_agent_outbound) needs a signed Twilio request to
    reach; checked at source: a transient denial picks the try-again message."""
    import inspect

    import telephony_api

    src = inspect.getsource(telephony_api.twilio_agent_outbound)
    at = src.index('if getattr(decision, "transient", None):')
    assert "try again in a few minutes" in src[at:at + 200]
    assert at < src.index("contact permission or calling-hour requirements are not met")


def test_the_sales_preview_marks_an_undecided_gate_as_try_again():
    import sales_api

    d = asyncio.run(undecidable())
    assert sales_api._gate_blockers("c1", "s1", d) == [
        {"contact_id": "c1", "step_key": "s1", "code": oc.OPT_OUT_STATUS_UNAVAILABLE_CODE,
         "message": oc.OPT_OUT_STATUS_UNAVAILABLE}]
    assert sales_api._gate_blockers("c1", "s1", asyncio.run(refused()))[0]["code"] == "compliance_block"


def test_the_job_worker_spaces_a_deferred_retry_past_a_failover(monkeypatch):
    import automation_jobs

    seen = {}

    async def lease_update(job, worker_id, sql, *args, finish=None):
        seen["state"], seen["retry_seconds"] = args[0], int(args[4])
        return {"id": job["id"]}

    monkeypatch.setattr(automation_jobs, "_lease_update", lease_update)
    job = {"id": "j-1", "attempt_count": 1, "max_attempts": 5}
    deferred = oc.OutreachDeferred(asyncio.run(undecidable()))
    asyncio.run(automation_jobs.fail_job(job, "w-1", deferred))
    assert seen == {"state": "failed", "retry_seconds": oc.TRANSIENT_RETRY_SECONDS}
    asyncio.run(automation_jobs.fail_job(job, "w-1", RuntimeError("ordinary")))
    assert seen["retry_seconds"] == 5                       # unchanged for everything else
    asyncio.run(automation_jobs.fail_job({**job, "attempt_count": 5}, "w-1", deferred))
    assert seen["state"] == "dead_letter"                   # the attempt budget still ends it


def test_the_last_attempt_is_undecided_not_retrying_and_matches_the_dead_letter_rule(monkeypatch):
    """raise_if_deferred(final_attempt=True) raises a terminal OutreachUndecided
    whose words say it was not sent; is_final_attempt is exactly fail_job's
    dead-letter rule, so a retryable deferral is never what a dead-lettered
    job is left saying (review of PR #173)."""
    import automation_jobs

    decision = asyncio.run(undecidable())
    with pytest.raises(oc.OutreachUndecided) as err:
        oc.raise_if_deferred(decision, final_attempt=True)
    assert err.value.job_terminal is True and err.value.code == oc.OPT_OUT_STATUS_UNAVAILABLE_CODE
    assert str(err.value).startswith("Not sent: ") and "retrying" not in str(err.value)
    with pytest.raises(oc.OutreachDeferred):
        oc.raise_if_deferred(decision, final_attempt=False)
    oc.raise_if_deferred(asyncio.run(refused()), final_attempt=True)   # a refusal is the caller's to record

    seen = {}

    async def lease_update(job, worker_id, sql, *args, finish=None):
        seen["state"] = args[0]
        return {"id": job["id"]}

    monkeypatch.setattr(automation_jobs, "_lease_update", lease_update)
    for attempt, maximum in ((1, 5), (4, 5), (5, 5), (6, 5), (1, 1)):
        job = {"id": "j-1", "attempt_count": attempt, "max_attempts": maximum}
        asyncio.run(automation_jobs.fail_job(job, "w-1", RuntimeError("retryable")))
        assert (seen["state"] == "dead_letter") is automation_jobs.is_final_attempt(job), (attempt, maximum)
