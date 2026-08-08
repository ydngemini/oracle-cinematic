"""Speed-to-lead: channel resolution, the compliance-block outcome, and metering.

The behaviours worth pinning here are the ones where a plausible-looking
implementation would be wrong in a way that costs money or breaks the law:

  * a compliance block must SUCCEED the job and leave a counted ledger row,
    not fail/retry (retrying a TCPA denial is the abuse the gate prevents);
  * the feature must be OFF by default;
  * usage must be metered on engagement, never on a blocked attempt;
  * a stated channel preference the contact cannot actually receive must not
    produce a message staged to nowhere.
"""

from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timedelta, timezone

import pytest

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
    assert captured["max_attempts"] == 1
    assert captured.get("scheduled_at") is None


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

    async def _never_meters(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("blocked leads must not be metered as engaged")

    monkeypatch.setattr(speed_to_lead, "record_usage", _never_meters)

    result = await speed_to_lead._speed_to_lead_job(_payload(), _reporter())

    assert result["state"] == "blocked"
    assert result["reason"] == "outside_calling_window"
    assert len(recorded) == 1
    assert recorded[0]["disposition"] == "blocked"
    assert recorded[0]["blocked_reason"] == "outside_calling_window"


@sync
async def test_a_staged_response_is_metered_once_on_engagement(monkeypatch):
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    recorded: list[dict] = []
    metered: list[dict] = []
    _stub_contact(monkeypatch, {"phone": "+13025550100", "preferred_channel": "sms"})
    monkeypatch.setattr(speed_to_lead, "_record_response_event", _recorder(recorded))

    async def _allowed(ctx, **kwargs):
        return OutreachDecision(
            allowed=True, channel="sms", contact="+13025550100", state_code="DE"
        )

    monkeypatch.setattr(speed_to_lead, "guard_outreach", _allowed)

    async def _meter(ctx, **kwargs):
        metered.append(kwargs)
        return True

    monkeypatch.setattr(speed_to_lead, "record_usage", _meter)

    result = await speed_to_lead._speed_to_lead_job(_payload(), _reporter())

    assert result["state"] == "staged"
    # Staged, never auto-sent: the human-in-the-loop default.
    assert result["requires_approval"] is True
    assert recorded[0]["disposition"] == "staged"
    assert len(metered) == 1
    assert metered[0]["metric"] == "lead_engaged"
    assert metered[0]["idempotency_key"] == "lead-engaged:evt-1"


@sync
async def test_latency_is_measured_from_lead_arrival_not_job_start(monkeypatch):
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    _stub_contact(monkeypatch, {"phone": "+13025550100", "preferred_channel": "sms"})
    monkeypatch.setattr(speed_to_lead, "_record_response_event", _recorder([]))

    async def _allowed(ctx, **kwargs):
        return OutreachDecision(allowed=True, channel="sms", contact="x", state_code="DE")

    monkeypatch.setattr(speed_to_lead, "guard_outreach", _allowed)

    async def _meter(*a, **k):
        return True

    monkeypatch.setattr(speed_to_lead, "record_usage", _meter)

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


def _reporter():
    class _R:
        job = {"tenant_id": TENANT_ID, "id": "job-1", "job_type": speed_to_lead.JOB_TYPE}

        async def progress(self, *a, **k):
            return None

    return _R()


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
