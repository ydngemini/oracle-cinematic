"""The gate between a plan and a person's phone.

The rules that matter here are the ones a future author might reasonably think
are negotiable, and are not: a mission grant never bypasses compliance, a
channel nobody granted never releases itself, and a deployment with no
credentials stages nothing no matter what any dial or grant says.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from missions import policy
from tenancy import Role, TenantContext

CTX = TenantContext(
    agent_id="a@t.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)
NOW = datetime(2026, 9, 4, 15, 0, tzinfo=timezone.utc)


def mission(**overrides):
    base = {
        "id": "00000000-0000-0000-0000-0000000000aa",
        "status": "active",
        "mode": "live",
        "allowed_channels": ["sms", "email"],
        "auto_channels": [],
        "consent_at": None,
        "budget_cents": 0,
        "deadline": None,
    }
    return {**base, **overrides}


@dataclass
class _Decision:
    allowed: bool = True
    blockers: tuple = ()
    required_disclosures: tuple = ()


@pytest.fixture
def gate(monkeypatch):
    """Everything permissive by default; each test tightens one thing."""
    calls = {"may_act": [], "guard": []}

    async def may_act(_ctx, category, *, reversible):
        calls["may_act"].append((category, reversible))
        return False, f"{category} is set to 'assist' — prepared, not sent."

    async def guard(_ctx, **kwargs):
        calls["guard"].append(kwargs)
        return _Decision(required_disclosures=("This is an AI assistant.",))

    async def ready(_ctx, channels):
        return True, []

    async def spend(_ctx, _mission_id):
        return 0

    async def monthly(_ctx):
        return 0, 0

    import autonomy
    import outreach_compliance

    monkeypatch.setattr(autonomy, "may_act", may_act)
    monkeypatch.setattr(outreach_compliance, "guard_outreach", guard)
    monkeypatch.setattr(policy, "outbound_ready", ready)
    monkeypatch.setattr(policy, "spend_so_far", spend)
    monkeypatch.setattr(policy, "monthly_spend", monthly)
    return calls


def evaluate(m, action=None, **kwargs):
    return asyncio.run(policy.evaluate_action(
        CTX, m, action or {"channel": "sms"},
        contact="+13025551234", state_code="DE", now=NOW, **kwargs,
    ))


class TestReleaseAuthority:
    def test_a_channel_nobody_granted_is_staged_not_released(self, gate):
        """The healthy default: it goes in the approval queue."""
        verdict = evaluate(mission())
        assert verdict.may_stage is True
        assert verdict.may_release is False
        assert "no grant covers this channel" in verdict.reason

    def test_a_consented_grant_releases_that_channel(self, gate):
        verdict = evaluate(mission(auto_channels=["sms"], consent_at="2026-09-01T00:00:00Z"))
        assert verdict.may_release is True
        assert "mission grant" in verdict.release_authority

    def test_a_grant_on_one_channel_does_not_release_another(self, gate):
        verdict = evaluate(
            mission(auto_channels=["sms"], consent_at="2026-09-01T00:00:00Z"),
            action={"channel": "email"},
        )
        assert verdict.may_stage is True
        assert verdict.may_release is False

    def test_the_standing_dial_is_asked_and_recorded_even_though_it_refuses(self, gate):
        """First real caller of may_act(). The dial is not the release
        authority for a mission, but an audit must see what it said."""
        verdict = evaluate(mission(auto_channels=["sms"], consent_at="2026-09-01T00:00:00Z"))
        assert gate["may_act"] == [("texts", False)], "the dial was not consulted"
        assert verdict.dial_allowed is False
        assert "assist" in verdict.dial_reason
        # Released anyway — by the grant, and the verdict says so.
        assert verdict.may_release is True
        assert verdict.release_authority.startswith("mission grant")

    def test_an_outbound_action_is_never_treated_as_reversible(self, gate):
        """may_act refuses irreversible actions even on autopilot, so passing
        reversible=True for a text would be asking the wrong question."""
        evaluate(mission(), action={"channel": "email"})
        assert gate["may_act"] == [("emails", False)]


class TestComplianceIsNotNegotiable:
    def test_a_grant_never_bypasses_the_outreach_gate(self, gate, monkeypatch):
        """A mission may be granted autopilot calls and this still blocks the
        specific contact who never gave express written consent (FCC 24-17)."""
        import outreach_compliance

        async def refuse(_ctx, **_kwargs):
            return _Decision(allowed=False, blockers=("no express written consent on file",))

        monkeypatch.setattr(outreach_compliance, "guard_outreach", refuse)
        verdict = evaluate(mission(
            allowed_channels=["voice"], auto_channels=["voice"],
            consent_at="2026-09-01T00:00:00Z",
        ), action={"channel": "voice"})

        assert verdict.may_stage is False and verdict.may_release is False
        assert "compliance" in verdict.blocked_reason
        assert "express written consent" in verdict.blocked_reason

    def test_required_disclosures_are_carried_to_the_caller(self, gate):
        verdict = evaluate(mission())
        assert "This is an AI assistant." in verdict.disclosures

    def test_the_gate_is_asked_before_anything_is_staged(self, gate):
        evaluate(mission())
        assert gate["guard"], "guard_outreach was not consulted"
        assert gate["guard"][0]["log"] is False, "planning must not write an attempt log"


class TestDormancy:
    def test_no_credential_means_nothing_is_staged(self, gate, monkeypatch):
        """A channel with nothing to send on stages nothing.

        NOT the off switch, though — see the class docstring below and
        `outbound_ready`. Measured on the local stack: zero credential rows,
        but Twilio in the environment, so sms and voice report ready. The
        off switch is Feature.MISSIONS.
        """
        async def unready(_ctx, channels):
            return False, ["sms (needs one of: twilio, $TWILIO_ACCOUNT_SID)"]

        monkeypatch.setattr(policy, "outbound_ready", unready)
        verdict = evaluate(mission(auto_channels=["sms"], consent_at="2026-09-01T00:00:00Z"))
        assert verdict.may_stage is False
        assert "no credential" in verdict.blocked_reason
        assert verdict.missing

    def test_env_configured_providers_count_as_ready(self, monkeypatch):
        """The finding that falsified this feature's stated safety premise.

        The plan said: no credential rows, therefore dormant, therefore no
        feature flag needed. The local stack has zero rows AND a live
        TWILIO_ACCOUNT_SID, so sms reports ready. Reporting otherwise would be
        the more dangerous lie — the existing send paths really do use it — so
        this function stays honest and dormancy moves to Feature.MISSIONS.
        """
        monkeypatch.setattr(policy, "CHANNEL_PROVIDERS", {"sms": ("twilio",)})
        monkeypatch.setattr(policy, "CHANNEL_ENV", {"sms": ("TWILIO_ACCOUNT_SID",)})
        # No credential ROWS: _channel_ready is asked with an empty live set.
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
        assert policy._channel_ready("sms", live=set()) is True

        monkeypatch.delenv("TWILIO_ACCOUNT_SID")
        assert policy._channel_ready("sms", live=set()) is False
        assert policy._channel_ready("sms", live={"twilio"}) is True

    def test_the_whole_gate_runs_even_when_it_cannot_send(self, gate, monkeypatch):
        """Everything above step 5 is computed and recorded, so shadow mode and
        a dormant deployment exercise the identical path. That is what makes
        `would_have_done` worth trusting."""
        async def unready(_ctx, channels):
            return False, ["sms (needs one of: twilio)"]

        monkeypatch.setattr(policy, "outbound_ready", unready)
        verdict = evaluate(mission(auto_channels=["sms"], consent_at="2026-09-01T00:00:00Z"))
        assert gate["may_act"], "the dial was skipped when there was no credential"
        assert gate["guard"], "compliance was skipped when there was no credential"
        assert verdict.release_authority.startswith("mission grant")

    def test_shadow_mode_records_but_does_not_stage(self, gate):
        verdict = evaluate(mission(mode="shadow", status="shadow"))
        assert verdict.may_stage is False
        assert "shadow" in verdict.blocked_reason
        assert gate["guard"], "shadow must still run the full evaluation"


class TestBudget:
    def test_a_mission_that_has_spent_its_budget_stops(self, gate, monkeypatch):
        async def spent(_ctx, _mission_id):
            return 500

        monkeypatch.setattr(policy, "spend_so_far", spent)
        verdict = evaluate(mission(budget_cents=500))
        assert verdict.may_stage is False
        assert "budget is spent" in verdict.blocked_reason

    def test_the_tenant_ceiling_stops_a_mission_inside_its_own_budget(self, gate, monkeypatch):
        async def monthly(_ctx):
            return 10_000, 10_000

        monkeypatch.setattr(policy, "monthly_spend", monthly)
        verdict = evaluate(mission(budget_cents=0))
        assert verdict.may_stage is False
        assert "brokerage's monthly" in verdict.blocked_reason

    def test_no_cap_means_no_ceiling_not_a_zero_ceiling(self, gate):
        assert evaluate(mission(budget_cents=0)).may_stage is True


class TestMissionState:
    @pytest.mark.parametrize("status", ["draft", "paused", "completed", "cancelled", "failed"])
    def test_a_mission_that_is_not_running_does_nothing(self, gate, status):
        verdict = evaluate(mission(status=status))
        assert verdict.may_stage is False
        assert status in verdict.blocked_reason

    def test_a_channel_outside_allowed_channels_is_refused(self, gate):
        verdict = evaluate(mission(), action={"channel": "voice"})
        assert verdict.may_stage is False
        assert "not a channel this mission may use" in verdict.blocked_reason

    def test_a_passed_deadline_stops_the_mission(self, gate):
        verdict = evaluate(mission(deadline=(NOW - timedelta(days=1)).isoformat()))
        assert verdict.may_stage is False
        assert "deadline has passed" in verdict.blocked_reason


class TestShape:
    def test_every_refusal_carries_a_reason_the_schema_requires(self, gate):
        """mission_actions CHECKs that a withheld action has blocked_reason."""
        for m in (mission(status="paused"), mission(mode="shadow")):
            verdict = evaluate(m)
            assert verdict.may_stage is False
            assert verdict.blocked_reason, "the schema will reject this row"

    def test_the_gate_never_sends_anything_itself(self):
        source = inspect.getsource(policy)
        for forbidden in ("stage_command", "release_command", "enqueue_job", "send("):
            assert forbidden not in source, f"policy must only decide, found {forbidden}"
