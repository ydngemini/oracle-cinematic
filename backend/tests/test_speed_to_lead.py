"""Speed-to-lead: channel resolution, the compliance-block outcome, and metering.

The behaviours worth pinning here are the ones where a plausible-looking
implementation would be wrong in a way that costs money or breaks the law:

  * a compliance block must SUCCEED the job and leave a counted ledger row,
    not fail/retry (retrying a TCPA denial is the abuse the gate prevents);
  * the feature must be OFF by default;
  * usage is never metered by this job — not on a block, not on a staged draft
    (A05: lead_engaged is metered at delivery by outreach_lifecycle);
  * a stated channel preference the contact cannot actually receive must not
    produce a message staged to nowhere.
"""

from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timedelta, timezone

import speed_to_lead
from outreach_compliance import OutreachDecision
from speed_to_lead import _enabled, _opening_draft, _resolve_channel

def sync(fn):
    """Run an async test body. The suite has no pytest-asyncio/anyio plugin —
    the house pattern is asyncio.run at the call site (see test_lead_routing);
    this is the same thing without repeating it in every test."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


TENANT_ID = "11111111-1111-4111-8111-111111111111"
CONTACT_ID = "22222222-2222-4222-8222-222222222222"


# ── Channel resolution ──────────────────────────────────────────────────────

def test_channel_falls_back_when_the_stated_preference_is_unreachable():
    # Prefers email but has no email address: staging an email here would
    # create a command addressed to nothing.
    contact = {"preferred_channel": "email", "phone": "+13025550100", "email": None}
    assert _resolve_channel(contact) == "sms"

    contact = {"preferred_channel": "voice", "phone": None, "email": "a@example.test"}
    assert _resolve_channel(contact) == "email"


def test_channel_honours_a_usable_preference_and_aliases():
    assert _resolve_channel(
        {"preferred_channel": "voice", "phone": "+13025550100"}
    ) == "voice"
    # 'call'/'text'/'phone' are user-facing spellings of the stored channels.
    assert _resolve_channel(
        {"preferred_channel": "call", "phone": "+13025550100"}
    ) == "voice"
    assert _resolve_channel(
        {"preferred_channel": "text", "phone": "+13025550100"}
    ) == "sms"


def test_channel_is_none_when_there_is_nothing_to_reach():
    assert _resolve_channel({"preferred_channel": "sms"}) is None
    assert _resolve_channel({"phone": "", "email": ""}) is None


def test_sms_is_the_default_for_an_unstated_preference():
    assert _resolve_channel(
        {"preferred_channel": "none", "phone": "+13025550100", "email": "a@b.test"}
    ) == "sms"


# ── Draft ───────────────────────────────────────────────────────────────────

def test_draft_uses_a_first_name_and_degrades_without_one():
    draft = _opening_draft({"full_name": "Sam Seller"}, "sms", "Alex Agent")
    assert "Hi Sam," in draft["body"]

    draft = _opening_draft({"full_name": None}, "sms", "Alex Agent")
    assert "Hi there," in draft["body"]


def test_email_draft_carries_a_subject_and_voice_draft_a_script():
    assert "subject" in _opening_draft({}, "email", "Alex Agent")
    assert "script" in _opening_draft({}, "voice", "Alex Agent")


# ── Feature gate ────────────────────────────────────────────────────────────

def test_feature_is_off_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("ORACLE_FEATURE_SPEED_TO_LEAD", raising=False)
    assert _enabled() is False, "an outbound automation must not default on"

    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "true")
    assert _enabled() is True
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "0")
    assert _enabled() is False


@sync
async def test_enqueue_is_a_no_op_when_disabled(monkeypatch):
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "0")

    async def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("enqueue_job called while the feature was disabled")

    monkeypatch.setattr(speed_to_lead, "enqueue_job", _boom)
    result = await speed_to_lead.enqueue_speed_to_lead(
        _ctx(), contact_id=CONTACT_ID, intake_event_id="evt-1"
    )
    assert result == {"state": "disabled", "created": False}


@sync
async def test_enqueue_never_raises_into_the_intake_path(monkeypatch):
    """A queue failure must degrade the lead, not reject it."""
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")

    async def _fail(*a, **k):
        raise RuntimeError("queue is down")

    monkeypatch.setattr(speed_to_lead, "enqueue_job", _fail)
    result = await speed_to_lead.enqueue_speed_to_lead(
        _ctx(), contact_id=CONTACT_ID, intake_event_id="evt-1"
    )
    assert result["state"] == "deferred"
    assert result["created"] is False


@sync
async def test_enqueue_uses_one_key_per_lead_so_a_replay_cannot_double_contact(monkeypatch):
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    seen: list[str] = []

    async def _capture(ctx, **kwargs):
        seen.append(kwargs["idempotency_key"])
        return {"id": "job-1"}, True

    monkeypatch.setattr(speed_to_lead, "enqueue_job", _capture)
    for _ in range(2):
        await speed_to_lead.enqueue_speed_to_lead(
            _ctx(), contact_id=CONTACT_ID, intake_event_id="evt-9"
        )
    # No time bucket in the key — unlike client-AI reconcile, a second enqueue
    # for the same lead is a duplicate to swallow, not a burst to coalesce.
    assert seen == ["speed-to-lead:evt-9", "speed-to-lead:evt-9"]


@sync
async def test_enqueue_priority_beats_ordinary_automation(monkeypatch):
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    captured = {}

    async def _capture(ctx, **kwargs):
        captured.update(kwargs)
        return {"id": "job-1"}, True

    monkeypatch.setattr(speed_to_lead, "enqueue_job", _capture)
    await speed_to_lead.enqueue_speed_to_lead(_ctx(), contact_id=CONTACT_ID)
    # Poller sorts priority ASC. Client-AI reconcile is 45; latency is the whole
    # value of this job, so it must not queue behind that.
    assert captured["priority"] < 45
    assert captured["max_attempts"] == speed_to_lead.JOB_MAX_ATTEMPTS
    assert captured.get("scheduled_at") is None


def test_the_attempt_budget_lets_a_deferral_retry_within_minutes():
    """Review of PR #176: with one attempt, every attempt was the last, so a
    lead that arrived during a Valkey blip never got a first-response draft.
    Enough attempts to outlast a failover, few enough that the last retry
    still lands within about five minutes of the first."""
    import outreach_compliance as oc

    assert speed_to_lead.JOB_MAX_ATTEMPTS == 3
    retries = speed_to_lead.JOB_MAX_ATTEMPTS - 1
    assert retries * oc.TRANSIENT_RETRY_SECONDS <= 300


@sync
async def test_enqueue_skips_without_a_contact_anchor(monkeypatch):
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    result = await speed_to_lead.enqueue_speed_to_lead(_ctx(), intake_event_id="evt-1")
    assert result["state"] == "skipped"
    assert result["reason"] == "no_contact_anchor"


@sync
async def test_enqueue_rejects_a_malformed_anchor(monkeypatch):
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    result = await speed_to_lead.enqueue_speed_to_lead(_ctx(), contact_id="not-a-uuid")
    assert result["state"] == "skipped"
    assert result["reason"] == "bad_anchor"


# ── The job: compliance block is a counted success ──────────────────────────

@sync
async def test_a_compliance_block_succeeds_and_is_recorded_not_retried(monkeypatch):
    """The single most important behaviour in this module.

    A TCPA/calling-window denial must not fail the job (which would retry it)
    and must not vanish from the ledger (which would make the latency metric
    survivorship-biased — reporting only leads we were allowed to contact).
    """
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    recorded: list[dict] = []
    _stub_contact(monkeypatch, {"phone": "+13025550100", "preferred_channel": "sms"})
    monkeypatch.setattr(
        speed_to_lead, "_record_response_event", _recorder(recorded)
    )

    async def _blocked(ctx, **kwargs):
        return OutreachDecision(
            allowed=False, channel="sms", contact="+13025550100", state_code="DE",
            blockers=("outside_calling_window",),
        )

    monkeypatch.setattr(speed_to_lead, "guard_outreach", _blocked)

    _forbid_metering(monkeypatch)

    result = await speed_to_lead._speed_to_lead_job(_payload(), _reporter())

    assert result["state"] == "blocked"
    assert result["reason"] == "outside_calling_window"
    assert len(recorded) == 1
    assert recorded[0]["disposition"] == "blocked"
    assert recorded[0]["blocked_reason"] == "outside_calling_window"


def _undecidable_gate(monkeypatch, recorded: list[dict]):
    import outreach_compliance as oc

    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    _stub_contact(monkeypatch, {"phone": "+13025550100", "preferred_channel": "sms"})
    monkeypatch.setattr(speed_to_lead, "_record_response_event", _recorder(recorded))

    async def _undecidable(ctx, **kwargs):
        return OutreachDecision(
            allowed=False, channel="sms", contact="+13025550100", state_code="DE",
            blockers=(oc.OPT_OUT_STATUS_UNAVAILABLE,), transient=oc.OPT_OUT_STATUS_UNAVAILABLE_CODE,
        )

    monkeypatch.setattr(speed_to_lead, "guard_outreach", _undecidable)
    _forbid_metering(monkeypatch)


@sync
async def test_an_undecidable_gate_retries_the_job_instead_of_withholding_for_good(monkeypatch):
    """PR #173 review: pending spoken STOPs unreadable (a Valkey failover) is
    not a compliance outcome. Recording "blocked" would withhold this lead's
    first reply forever; with attempts left the job must fail RETRYABLY and
    record nothing."""
    import pytest

    import outreach_compliance as oc

    recorded: list[dict] = []
    _undecidable_gate(monkeypatch, recorded)
    with pytest.raises(oc.OutreachDeferred) as err:
        await speed_to_lead._speed_to_lead_job(_payload(), _reporter(attempt_count=1, max_attempts=3))
    assert getattr(err.value, "job_terminal", False) is False
    assert recorded == []                                   # nothing final written


@sync
async def test_an_undecidable_gate_on_the_last_attempt_says_not_sent_and_records_the_lead_failed(monkeypatch):
    """Review of PR #175: on its last attempt the dead-lettered job read "Not
    sent yet, retrying"; it is OutreachUndecided: terminal, and its words say
    it was not sent and will not be retried. Review of PR #176: it also left
    no ledger row, so the lead vanished from the first-response metric. Now
    the lead is recorded 'failed' — never 'blocked' (no compliance outcome
    was reached), and with no reason: 0067 allows one on a blocked row only;
    the job keeps the cause (OUTREACH_UNDECIDED)."""
    import pytest

    import automation_jobs
    import outreach_compliance as oc

    recorded: list[dict] = []
    _undecidable_gate(monkeypatch, recorded)
    job = {"attempt_count": speed_to_lead.JOB_MAX_ATTEMPTS, "max_attempts": speed_to_lead.JOB_MAX_ATTEMPTS}
    assert automation_jobs.is_final_attempt(job)
    with pytest.raises(oc.OutreachUndecided, match="will not be retried") as err:
        await speed_to_lead._speed_to_lead_job(_payload(), _reporter(**job))
    assert err.value.job_terminal is True
    assert err.value.job_error_code == "OUTREACH_UNDECIDED"
    assert "retrying" not in str(err.value).lower()
    assert [(r["disposition"], r["channel"], r.get("blocked_reason"), r.get("command_id"),
             r["intake_event_id"]) for r in recorded] == [("failed", "sms", None, None, "evt-1")]


# ── Through the job worker: only a deferral is retried ──────────────────────

def _through_the_worker(monkeypatch, attempt_count: int) -> dict:
    """One claim of the speed-to-lead job by the real DurableJobWorkers._loop,
    with the real fail_job / complete_job deciding the job's next state.
    Returns what the worker wrote: the state ('failed' = retried later,
    'dead_letter', 'succeeded'), the retry delay, the error code."""
    import automation_jobs

    job = {"id": "job-1", "job_type": speed_to_lead.JOB_TYPE, "payload": _payload(),
           "attempt_count": attempt_count, "max_attempts": speed_to_lead.JOB_MAX_ATTEMPTS,
           "tenant_id": TENANT_ID, "lease_token": "lease-1"}
    workers = automation_jobs.DurableJobWorkers(worker_count=0, interactive_count=0)
    claims = {"n": 0}
    wrote: dict = {}

    async def claim(worker_id, queue_name="default"):
        claims["n"] += 1
        if claims["n"] > 1:
            workers._running = False
            return None
        return job

    async def lease_update(j, worker_id, sql, *args, finish=None):
        if "last_error_code=$6" in sql:              # fail_job
            wrote.update(state=args[0], error_code=args[2], error=args[3], retry_seconds=int(args[4]))
        elif "result=$5::jsonb" in sql:              # complete_job
            wrote.update(state=args[0], result=args[1])
        return {"id": j["id"]}

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(automation_jobs, "claim_next_job", claim)
    monkeypatch.setattr(automation_jobs, "_lease_update", lease_update)
    monkeypatch.setattr(automation_jobs, "mark_running", noop)
    monkeypatch.setattr(automation_jobs, "_keep_lease", noop)
    monkeypatch.setattr(automation_jobs.JobReporter, "progress", noop)
    monkeypatch.setattr(automation_jobs.asyncio, "sleep", noop)
    assert automation_jobs._HANDLERS[speed_to_lead.JOB_TYPE] is speed_to_lead._speed_to_lead_job
    workers._running = True
    asyncio.run(workers._loop("w-1", poll_seconds=0))
    return wrote


def test_a_deferral_before_the_last_attempt_is_retried_after_the_hint(monkeypatch):
    """Not terminal: the job goes back to 'failed' with next_retry_at the
    deferral's hint away (a failover takes tens of seconds), and nothing is
    recorded for the lead."""
    import outreach_compliance as oc

    assert speed_to_lead.JOB_MAX_ATTEMPTS > 1               # else every attempt is the last
    for attempt in range(1, speed_to_lead.JOB_MAX_ATTEMPTS):
        recorded: list[dict] = []
        _undecidable_gate(monkeypatch, recorded)
        wrote = _through_the_worker(monkeypatch, attempt)
        assert wrote["state"] == "failed", attempt
        assert wrote["retry_seconds"] == oc.TRANSIENT_RETRY_SECONDS
        assert wrote["error"].startswith("Not sent yet, retrying")
        assert recorded == []


def test_a_deferral_on_the_last_attempt_is_dead_lettered_and_the_lead_recorded_failed(monkeypatch):
    recorded: list[dict] = []
    _undecidable_gate(monkeypatch, recorded)
    wrote = _through_the_worker(monkeypatch, speed_to_lead.JOB_MAX_ATTEMPTS)
    assert wrote["state"] == "dead_letter"
    assert wrote["error_code"] == "OUTREACH_UNDECIDED"
    assert "will not be retried" in wrote["error"]
    assert [r["disposition"] for r in recorded] == ["failed"]


def _gate_raises(exc: Exception):
    async def gate(ctx, **kwargs):
        raise exc

    return gate


def test_any_other_failure_is_dead_lettered_on_its_first_attempt(monkeypatch):
    """Only a deferral earns a retry. A database error, a crash in the gate,
    anything else ends the job on the attempt that raised it — as when the
    job had one attempt — keeping the error's text, and its code when it
    names one; a recovery-mode refusal keeps the worker's own handling."""
    import recovery_mode

    class Unreadable(RuntimeError):
        job_terminal = False
        job_error_code = "AI_SPEND_UNAVAILABLE"

    cases = [
        (RuntimeError("database is down"), "JOB_HANDLER_ERROR", "database is down"),
        (ConnectionResetError("reset by peer"), "JOB_HANDLER_ERROR", "ConnectionResetError"),
        (Unreadable("budget unreadable"), "AI_SPEND_UNAVAILABLE", "budget unreadable"),
        (recovery_mode.RecoveryModeBlocked("recovery mode"), "RECOVERY_MODE_BLOCKED", "recovery mode"),
    ]
    for exc, code, words in cases:
        recorded: list[dict] = []
        monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
        _stub_contact(monkeypatch, {"phone": "+13025550100", "preferred_channel": "sms"})
        monkeypatch.setattr(speed_to_lead, "_record_response_event", _recorder(recorded))
        monkeypatch.setattr(speed_to_lead, "guard_outreach", _gate_raises(exc))
        wrote = _through_the_worker(monkeypatch, 1)
        assert wrote["state"] == "dead_letter", type(exc).__name__
        assert wrote["error_code"] == code
        assert words in wrote["error"]
        # Neutral: a failure after create_command must not claim "not staged".
        # (A recovery-mode refusal keeps its own text, unwrapped.)
        if not isinstance(exc, recovery_mode.RecoveryModeBlocked):
            assert wrote["error"].startswith("Speed-to-lead attempt failed, not retried"), wrote["error"]
        assert "not staged" not in wrote["error"]
        assert recorded == []


def test_a_compliance_block_is_a_successful_job_even_with_attempts_left(monkeypatch):
    recorded: list[dict] = []
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    _stub_contact(monkeypatch, {"phone": "+13025550100", "preferred_channel": "sms"})
    monkeypatch.setattr(speed_to_lead, "_record_response_event", _recorder(recorded))

    async def _blocked(ctx, **kwargs):
        return OutreachDecision(allowed=False, channel="sms", contact="+13025550100", state_code="DE",
                                blockers=("outside_calling_window",))

    monkeypatch.setattr(speed_to_lead, "guard_outreach", _blocked)
    wrote = _through_the_worker(monkeypatch, 1)
    assert wrote["state"] == "succeeded"                   # never 'failed': never retried
    assert '"state":"blocked"' in wrote["result"]
    assert [r["disposition"] for r in recorded] == ["blocked"]


def test_a_retry_after_a_deferral_stages_exactly_one_draft(monkeypatch):
    """Attempt 1: the gate cannot decide — nothing staged, nothing recorded.
    Attempt 2: the gate allows — one draft, one 'staged' row. And if the
    worker stopped after that before marking the job done, the re-run finds
    the row and stops: no second draft, no second row, no second gate."""
    import commands_api

    recorded: list[dict] = []
    _undecidable_gate(monkeypatch, recorded)
    staged: list[str] = []

    async def _create_command(body, ctx):
        staged.append(body.idempotency_key)
        return {"command": {"id": "cmd-1"}, "created": len(staged) == 1}

    async def on_record(ctx, *, intake_event_id, lead_id, contact_id, client_id):
        # 0067's unique index by intake event: the row a new write collides with.
        rows = [r for r in recorded if r["intake_event_id"] == intake_event_id]
        return rows[0]["disposition"] if rows else None

    monkeypatch.setattr(commands_api, "create_command", _create_command)
    monkeypatch.setattr(speed_to_lead, "_first_response_on_record", on_record)
    assert _through_the_worker(monkeypatch, 1)["state"] == "failed"
    assert staged == [] and recorded == []

    gates: list[str] = []

    async def _allowed(ctx, **kwargs):
        gates.append(kwargs["contact"])
        return OutreachDecision(allowed=True, channel="sms", contact="+13025550100", state_code="DE")

    monkeypatch.setattr(speed_to_lead, "guard_outreach", _allowed)
    wrote = _through_the_worker(monkeypatch, 2)
    assert wrote["state"] == "succeeded" and '"state":"staged"' in wrote["result"]
    assert staged == ["speed-to-lead:evt-1"]
    assert [(r["disposition"], r["command_id"]) for r in recorded] == [("staged", "cmd-1")]

    wrote = _through_the_worker(monkeypatch, 3)            # a re-run after a worker stopped
    assert wrote["state"] == "succeeded" and '"state":"already_recorded"' in wrote["result"]
    assert staged == ["speed-to-lead:evt-1"] and len(recorded) == 1 and len(gates) == 1


def test_a_re_run_on_the_second_attempt_keeps_a_recorded_block(monkeypatch):
    """The common re-run: a deploy cancels the handler after it recorded a
    compliance block, and the job is claimed again as attempt 2 (not 3). The
    block stands: no second gate, no draft, no second row."""
    import commands_api

    recorded: list[dict] = [{"intake_event_id": "evt-1", "disposition": "blocked", "command_id": None}]
    gates: list[str] = []
    staged: list[str] = []
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    _stub_contact(monkeypatch, {"phone": "+13025550100", "preferred_channel": "sms"})
    monkeypatch.setattr(speed_to_lead, "_record_response_event", _recorder(recorded))

    async def on_record(ctx, *, intake_event_id, lead_id, contact_id, client_id):
        rows = [r for r in recorded if r["intake_event_id"] == intake_event_id]
        return rows[0]["disposition"] if rows else None

    async def _allowed(ctx, **kwargs):
        gates.append(kwargs["contact"])
        return OutreachDecision(allowed=True, channel="sms", contact="+13025550100", state_code="DE")

    async def _create_command(body, ctx):
        staged.append(body.idempotency_key)
        return {"command": {"id": "cmd-1"}, "created": True}

    monkeypatch.setattr(commands_api, "create_command", _create_command)
    monkeypatch.setattr(speed_to_lead, "_first_response_on_record", on_record)
    monkeypatch.setattr(speed_to_lead, "guard_outreach", _allowed)
    wrote = _through_the_worker(monkeypatch, 2)
    assert wrote["state"] == "succeeded" and '"state":"already_recorded"' in wrote["result"]
    assert gates == [] and staged == [] and len(recorded) == 1


@sync
async def test_the_first_attempt_never_pays_for_the_ledger_lookup(monkeypatch):
    async def never(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("looked up the ledger on a first attempt")

    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    _stub_contact(monkeypatch, {"phone": "+13025550100", "preferred_channel": "sms"})
    monkeypatch.setattr(speed_to_lead, "_record_response_event", _recorder([]))
    monkeypatch.setattr(speed_to_lead, "_first_response_on_record", never)

    async def _allowed(ctx, **kwargs):
        return OutreachDecision(allowed=True, channel="sms", contact="+13025550100", state_code="DE")

    monkeypatch.setattr(speed_to_lead, "guard_outreach", _allowed)
    result = await speed_to_lead._speed_to_lead_job(_payload(), _reporter(attempt_count=1, max_attempts=3))
    assert result["state"] == "staged"


@sync
async def test_a_staged_draft_is_never_metered(monkeypatch):
    """Audit A05. This test used to assert the defect: one lead_engaged row
    the moment a draft was staged, before any human approved it or any
    provider saw it. A staged draft has engaged nobody; the engagement is
    metered at delivery by outreach_lifecycle (see test_outreach_lifecycle)."""
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    recorded: list[dict] = []
    _stub_contact(monkeypatch, {"phone": "+13025550100", "preferred_channel": "sms"})
    monkeypatch.setattr(speed_to_lead, "_record_response_event", _recorder(recorded))

    async def _allowed(ctx, **kwargs):
        return OutreachDecision(
            allowed=True, channel="sms", contact="+13025550100", state_code="DE"
        )

    monkeypatch.setattr(speed_to_lead, "guard_outreach", _allowed)
    _forbid_metering(monkeypatch)

    result = await speed_to_lead._speed_to_lead_job(_payload(), _reporter())

    assert result["state"] == "staged"
    # Staged, never auto-sent: the human-in-the-loop default.
    assert result["requires_approval"] is True
    assert recorded[0]["disposition"] == "staged"
    assert not hasattr(speed_to_lead, "record_usage"), \
        "speed_to_lead must not import a metering call at all"


@sync
async def test_latency_is_measured_from_lead_arrival_not_job_start(monkeypatch):
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    _stub_contact(monkeypatch, {"phone": "+13025550100", "preferred_channel": "sms"})
    monkeypatch.setattr(speed_to_lead, "_record_response_event", _recorder([]))

    async def _allowed(ctx, **kwargs):
        return OutreachDecision(allowed=True, channel="sms", contact="x", state_code="DE")

    monkeypatch.setattr(speed_to_lead, "guard_outreach", _allowed)
    _forbid_metering(monkeypatch)

    old = datetime.now(timezone.utc) - timedelta(seconds=300)
    result = await speed_to_lead._speed_to_lead_job(_payload(created_at=old), _reporter())
    # A queue backlog must show up in the metric, not be hidden by it.
    assert result["latency_seconds"] >= 299


@sync
async def test_disabling_between_enqueue_and_run_records_a_skip(monkeypatch):
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "0")
    recorded: list[dict] = []
    monkeypatch.setattr(speed_to_lead, "_record_response_event", _recorder(recorded))

    result = await speed_to_lead._speed_to_lead_job(_payload(), _reporter())
    assert result["state"] == "disabled"
    assert recorded[0]["disposition"] == "skipped"


@sync
async def test_a_contact_with_no_reachable_channel_is_skipped_not_failed(monkeypatch):
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    recorded: list[dict] = []
    _stub_contact(monkeypatch, {"phone": None, "email": None})
    monkeypatch.setattr(speed_to_lead, "_record_response_event", _recorder(recorded))

    result = await speed_to_lead._speed_to_lead_job(_payload(), _reporter())
    assert result["state"] == "skipped"
    assert result["reason"] == "no_reachable_channel"
    assert recorded[0]["disposition"] == "skipped"


@sync
async def test_a_pool_contact_is_staged_by_the_marked_pipeline_and_an_assigned_one_for_its_agent(monkeypatch):
    """L09-12. The requester a draft is staged as is who the command worker
    holds it to at send time. An assigned contact's draft is the agent's:
    it leaves only while they are an active member who may still reach the
    contact. An unassigned contact's draft is the pipeline's own, under the
    reserved "system:speed-to-lead" marker — never the worker's unmarked
    "speed-to-lead-worker" name, which the worker now refuses."""
    import commands_api
    from tenancy import OUTREACH_PIPELINE_REQUESTERS, Role

    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    monkeypatch.setattr(speed_to_lead, "_record_response_event", _recorder([]))

    async def _allowed(ctx, **kwargs):
        return OutreachDecision(allowed=True, channel="sms", contact="+13025550100", state_code="DE")

    monkeypatch.setattr(speed_to_lead, "guard_outreach", _allowed)
    _forbid_metering(monkeypatch)
    staged_as: list[tuple] = []
    for assigned in ("agent-1@brokerage.test", None):
        _stub_contact(monkeypatch, {"phone": "+13025550100", "preferred_channel": "sms",
                                    "assigned_agent_id": assigned})

        async def _create_command(body, ctx):
            staged_as.append((ctx.agent_id, ctx.role, body.idempotency_key, body.context["source"]))
            return {"command": {"id": "cmd-1"}, "created": True}

        monkeypatch.setattr(commands_api, "create_command", _create_command)
        result = await speed_to_lead._speed_to_lead_job(_payload(), _reporter())
        assert result["state"] == "staged"
    assert staged_as == [
        ("agent-1@brokerage.test", Role.AGENT, "speed-to-lead:evt-1", "speed-to-lead"),
        ("system:speed-to-lead", Role.AGENT, "speed-to-lead:evt-1", "speed-to-lead"),
    ]
    assert staged_as[1][0] in OUTREACH_PIPELINE_REQUESTERS


# ── helpers ─────────────────────────────────────────────────────────────────

def _ctx():
    from tenancy import Role, TenantContext

    return TenantContext(agent_id="test", tenant_id=TENANT_ID, role=Role.BROKER_OWNER)


def _payload(created_at: datetime | None = None) -> dict:
    return {
        "tenant_id": TENANT_ID,
        "contact_id": CONTACT_ID,
        "client_id": None,
        "intake_event_id": "evt-1",
        "lead_id": None,
        "state_code": "DE",
        "lead_created_at": (created_at or datetime.now(timezone.utc)).isoformat(),
        "reason": "intake:test",
    }


def _reporter(**job_fields):
    row = {"tenant_id": TENANT_ID, "id": "job-1", "job_type": speed_to_lead.JOB_TYPE, **job_fields}

    class _R:
        job = row

        async def progress(self, *a, **k):
            return None

    return _R()


def _forbid_metering(monkeypatch) -> None:
    """Any billing write from the staging job fails the test."""
    import billing_usage

    async def _never(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("speed-to-lead staging must not meter usage")

    monkeypatch.setattr(billing_usage, "record_usage", _never)
    monkeypatch.setattr(billing_usage, "record_lead_engagement", _never)


def _recorder(sink: list[dict]):
    async def _record(ctx, **kwargs):
        sink.append(kwargs)

    return _record


def _stub_contact(monkeypatch, overrides: dict) -> None:
    """Replace the tenant_tx + contacts_api round trip with a fixed contact."""
    contact = {
        "id": CONTACT_ID,
        "full_name": "Sam Seller",
        "email": None,
        "phone": None,
        "preferred_channel": "none",
        "timezone": "America/New_York",
        "state_code": "DE",
        "assigned_agent_id": "agent-1",
        "legacy_client_id": None,
        **overrides,
    }

    class _Conn:
        async def fetchrow(self, *a, **k):
            return contact

        async def fetchval(self, sql, *a, **k):
            # _first_response_on_record on a retry: no row for this lead yet.
            assert "FROM lead_response_events" in sql, sql
            return None

        async def execute(self, *a, **k):
            return None

    class _Tx:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(speed_to_lead, "tenant_tx", lambda ctx: _Tx())

    import contacts_api

    async def _contact_json(conn, ctx, row):
        return row

    monkeypatch.setattr(contacts_api, "_contact_json", _contact_json)

    import commands_api

    async def _create_command(body, ctx):
        return {"command": {"id": "cmd-1"}, "created": True}

    monkeypatch.setattr(commands_api, "create_command", _create_command)
