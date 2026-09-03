"""The planner is the one place a model decides something. These are the rules
that make that safe.

The model sequences a list it was given. It cannot add anyone — the schema has
an integer where a name would go — and code refuses anything the schema could
not. Two unusable answers raise rather than fabricate, because a made-up list
of who to phone is worse than an empty screen: the empty screen is obviously
empty.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from missions import planner
from tenancy import Role, TenantContext

CTX = TenantContext(
    agent_id="a@t.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)

MISSION = {
    "objective_kind": "listings_won",
    "objective_text": "Win three listings in Newark before the end of the quarter.",
    "allowed_channels": ["sms", "email"],
}

CANDIDATES = [
    {"id": "cand-0", "subject_type": "client", "subject_id": "c0", "label": "Sarah Chen", "score": 0.9},
    {"id": "cand-1", "subject_type": "client", "subject_id": "c1", "label": "Marcus Reed", "score": 0.7},
]


class _Gateway:
    """Returns queued answers; records what it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    async def complete(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self.answers.pop(0)


def _plan(**overrides):
    return json.dumps({"steps": [], "reasoning": "", **overrides})


# propose_plan imports llm_gateway inside the function, so substituting the
# module is the seam. The fixture puts the real one back so a later test in the
# same session is not handed this stub.
@pytest.fixture(autouse=True)
def _restore_real_gateway():
    real = sys.modules.get("llm_gateway")
    yield
    if real is not None:
        sys.modules["llm_gateway"] = real
    else:
        sys.modules.pop("llm_gateway", None)


def _run(gateway, mission=MISSION, candidates=CANDIDATES):
    fake = types.ModuleType("llm_gateway")
    fake.complete = gateway.complete
    sys.modules["llm_gateway"] = fake
    return asyncio.run(planner.propose_plan(CTX, mission, candidates))


class TestContainment:
    def test_the_model_refers_to_people_by_index_so_it_cannot_invent_one(self):
        """The schema has an integer where a name would go. This is the
        structural guarantee, not a validation afterthought."""
        step = planner.PLAN_SCHEMA["json_schema"]["schema"]["properties"]["steps"]["items"]
        assert step["properties"]["candidate"] == {"type": "integer", "minimum": 0}
        assert "name" not in step["properties"]
        assert "subject" not in step["properties"]
        assert step["additionalProperties"] is False

    def test_an_index_pointing_at_nobody_is_dropped_with_a_reason(self):
        gateway = _Gateway(_plan(steps=[
            {"candidate": 0, "channel": "sms", "day_offset": 0, "intent": "call her"},
            {"candidate": 99, "channel": "sms", "day_offset": 1, "intent": "invented"},
        ]))
        steps, dropped, _ = _run(gateway)
        assert [s["candidate_id"] for s in steps] == ["cand-0"]
        assert len(dropped) == 1
        assert "not in the list" in dropped[0]["reason"]

    def test_a_channel_the_mission_may_not_use_is_dropped(self):
        """The mission allows sms and email. A voice step is refused by code
        even though the schema permits the word."""
        gateway = _Gateway(_plan(steps=[
            {"candidate": 0, "channel": "voice", "day_offset": 0, "intent": "ring her"},
        ]))
        steps, dropped, _ = _run(gateway)
        assert steps == []
        assert "not allowed to use" in dropped[0]["reason"]

    def test_two_contacts_with_one_person_on_one_day_is_refused(self):
        gateway = _Gateway(_plan(steps=[
            {"candidate": 0, "channel": "sms", "day_offset": 3, "intent": "first"},
            {"candidate": 0, "channel": "email", "day_offset": 3, "intent": "second"},
        ]))
        steps, dropped, _ = _run(gateway)
        assert len(steps) == 1
        assert "same person on the same day" in dropped[0]["reason"]

    def test_the_plan_is_always_a_subset_never_a_superset(self):
        gateway = _Gateway(_plan(steps=[
            {"candidate": 0, "channel": "sms", "day_offset": 0, "intent": "a"},
            {"candidate": 5, "channel": "sms", "day_offset": 0, "intent": "b"},
            {"candidate": 1, "channel": "fax", "day_offset": 0, "intent": "c"},
        ]))
        steps, dropped, _ = _run(gateway)
        assert len(steps) + len(dropped) == 3
        assert all(s["candidate_id"] in {"cand-0", "cand-1"} for s in steps)


class TestFailure:
    def test_an_unusable_answer_is_retried_once_with_the_error(self):
        gateway = _Gateway("not json at all", _plan(steps=[
            {"candidate": 0, "channel": "sms", "day_offset": 0, "intent": "ok"},
        ]))
        steps, _dropped, _ = _run(gateway)
        assert len(steps) == 1
        assert len(gateway.calls) == 2
        assert "could not be used" in gateway.calls[1]["prompt"]

    def test_two_failures_raise_rather_than_fabricate(self):
        """There is no fallback plan. A made-up sequence of who to phone is
        worse than nothing, because nothing is obviously nothing."""
        gateway = _Gateway("garbage", "still garbage")
        with pytest.raises(planner.PlanUnavailable):
            _run(gateway)
        assert len(gateway.calls) == 2

    def test_no_candidates_means_no_call_at_all(self):
        gateway = _Gateway()
        steps, dropped, reasoning = _run(gateway, candidates=[])
        assert steps == [] and dropped == []
        assert "No candidates" in reasoning
        assert gateway.calls == [], "a model was asked to plan for nobody"

    def test_a_mission_with_no_allowed_channels_never_asks(self):
        gateway = _Gateway()
        steps, _dropped, reasoning = _run(gateway, mission={**MISSION, "allowed_channels": []})
        assert steps == []
        assert "no channels" in reasoning
        assert gateway.calls == []


class TestRequest:
    def test_it_demands_structured_output(self):
        """Without this the gateway may return prose, and prose that parses as
        a plan is the failure mode the whole schema exists to prevent."""
        gateway = _Gateway(_plan())
        _run(gateway)
        assert gateway.calls[0]["response_format"] is planner.PLAN_SCHEMA

    def test_the_candidate_list_is_capped(self):
        many = [dict(CANDIDATES[0], id=f"c{i}") for i in range(planner.MAX_CANDIDATES + 20)]
        gateway = _Gateway(_plan())
        _run(gateway, candidates=many)
        prompt = gateway.calls[0]["prompt"]
        assert f"  {planner.MAX_CANDIDATES - 1}. " in prompt
        assert f"  {planner.MAX_CANDIDATES}. " not in prompt

    def test_the_objective_reaches_the_model_verbatim(self):
        gateway = _Gateway(_plan())
        _run(gateway)
        assert MISSION["objective_text"] in gateway.calls[0]["prompt"]
