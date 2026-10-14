"""SemanticFrame: typed, versioned, validated — and never its own authority.

Prompt 05, AC05.02 (an invalid frame is rejected before planning) and the
unit half of AC05.03 (identity is authorised, never invented). The real
PostgreSQL half — two agents, two brokerages, FORCE RLS — is
test_semantic_frame_access_live.py.

No model is called: the gateway is a fake that returns fixed JSON.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

import semantic_frame as sf
from platform_policy import ActionRisk
from tenancy import Role, TenantContext, brokerage_scope

CTX = TenantContext(agent_id="a@t.test", tenant_id="11111111-1111-1111-1111-111111111111", role=Role.AGENT)
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
SARAH_A, SARAH_B = str(uuid.uuid4()), str(uuid.uuid4())
KNOWN = (
    sf.KnownRecord("client", SARAH_A, "Sarah Chen (buyer)"),
    sf.KnownRecord("client", SARAH_B, "Sarah Chen (seller)"),
)


def _ev(quote, source="utterance"):
    return [{"source": source, "quote": quote}]


def _model(**over):
    """A model answer for "Don't message Sarah yet", as the schema shapes it."""
    base = {
        "goals": [{"label": "send_message", "negated": True, "evidence": _ev("Don't message")}],
        "mentions": [{"text": "Sarah", "kind": "person", "known": [1], "evidence": _ev("Sarah")}],
        "times": [],
        "constraints": [{"kind": "no_send", "text": "not yet", "time": None, "mention": 0,
                         "evidence": _ev("Don't message Sarah yet")}],
        "scope": "mine",
        "required_data": [{"source": "people", "max_age_seconds": None}],
        "effect": "read",
        "unfilled_slots": [],
        "ambiguities": [],
    }
    base.update(over)
    return base


TEXT = "Don't message Sarah yet"


def _frame(**over) -> dict:
    """A valid serialised SemanticFrame (the typed shape, ids not refs)."""
    frame = sf.frame_from_model(json.dumps(_model()), TEXT, KNOWN).model_dump(mode="json")
    frame.update(over)
    return frame


class FakeGateway:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def complete(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


# ── AC05.02: the schema is versioned and typed ─────────────────────────────

class TestSchemaContract:
    def test_the_frame_carries_its_version(self):
        frame = sf.validate_frame(_frame())
        assert frame.schema_version == sf.SCHEMA_VERSION == 1
        assert {"goals", "mentions", "times", "constraints", "scope", "required_data", "effect",
                "unfilled_slots", "ambiguities"} <= set(sf.SemanticFrame.model_fields)

    def test_an_unknown_version_is_rejected_not_coerced(self):
        for version in (2, 0, "1", None):
            with pytest.raises(sf.FrameRejected) as e:
                sf.validate_frame(_frame(schema_version=version))
            assert e.value.reason == "unsupported_version"

    def test_the_model_schema_and_the_frame_agree(self):
        """The strict JSON schema the model is held to names the same fields
        as the typed frame, except where the difference is the point."""
        model_fields = set(sf.MODEL_FRAME_SCHEMA["properties"])
        frame_fields = set(sf.SemanticFrame.model_fields)
        assert frame_fields - model_fields == {"schema_version", "authorized", "dropped_candidates"}
        assert model_fields <= frame_fields
        mention = sf.MODEL_FRAME_SCHEMA["properties"]["mentions"]["items"]["properties"]
        assert "known" in mention and "candidates" not in mention
        assert set(sf.MODEL_FRAME_SCHEMA["properties"]["goals"]["items"]["properties"]["label"]["enum"]) \
            == set(sf.GOAL_EFFECT)
        assert sf.MODEL_FRAME_FORMAT["json_schema"]["strict"] is True

    def test_strict_schema_requires_every_property_and_forbids_extras(self):
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node["additionalProperties"] is False
                    assert set(node["required"]) == set(node["properties"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(sf.MODEL_FRAME_SCHEMA)

    def test_nothing_server_decided_is_expressible_by_the_model(self):
        blob = json.dumps(sf.MODEL_FRAME_SCHEMA)
        for word in ("access", "authorized", "dropped_candidates", "choices", "\"id\""):
            assert word not in blob, word

    def test_effect_classes_follow_the_tool_policy(self):
        assert sf.effect_for_risk(ActionRisk.READ_ONLY) == "read"
        assert sf.effect_for_risk(ActionRisk.INTERNAL_EDIT) == "internal_edit"
        for risk in (ActionRisk.OUTREACH, ActionRisk.LIVE_CALL, ActionRisk.CALENDAR_WRITE,
                     ActionRisk.FINANCIAL, ActionRisk.LEGAL_DOCUMENT):
            assert sf.effect_for_risk(risk) == "external_effect"
        assert set(sf.GOAL_EFFECT.values()) == set(sf.EFFECTS)


# ── AC05.02: an invalid frame is rejected ──────────────────────────────────

class TestRejection:
    def _rejects(self, frame, reason="invalid_frame"):
        with pytest.raises(sf.FrameRejected) as e:
            sf.validate_frame(frame)
        assert e.value.reason == reason
        return e.value

    def test_lost_negation_is_rejected(self):
        """F05.03: a "no_send" beside a live send never reaches a planner."""
        frame = _frame(effect="external_effect")
        frame["goals"][0]["negated"] = False
        err = self._rejects(frame)
        assert "forbids an external effect" in err.detail

    def test_an_effect_smaller_than_the_goals_is_rejected(self):
        frame = _frame(constraints=[])
        frame["goals"][0]["negated"] = False  # a live send declared as "read"
        self._rejects(frame)

    def test_an_effect_larger_than_the_goals_is_rejected(self):
        self._rejects(_frame(effect="external_effect", constraints=[]))

    def test_unknown_goal_labels_and_extra_fields_are_rejected(self):
        frame = _frame()
        frame["goals"][0]["label"] = "wire_money"
        self._rejects(frame)
        self._rejects(_frame(recipient_phone="+15555550100"))

    def test_a_time_without_an_offset_or_zone_is_rejected(self):
        t = {"text": "tomorrow at 9", "start": "2026-10-15T09:00:00", "end": None,
             "timezone": "America/New_York", "evidence": _ev("tomorrow at 9")}
        self._rejects(_frame(times=[t]))
        t2 = {**t, "start": "2026-10-15T09:00:00-04:00", "timezone": "Mars/Olympus"}
        self._rejects(_frame(times=[t2]))
        t3 = {**t, "start": "2026-10-15T09:00:00-04:00", "end": "2026-10-15T08:00:00-04:00"}
        self._rejects(_frame(times=[t3]))
        ok = {**t, "start": "2026-10-15T09:00:00-04:00"}
        assert sf.validate_frame(_frame(times=[ok])).times[0].timezone == "America/New_York"

    def test_dangling_indexes_are_rejected(self):
        frame = _frame()
        frame["constraints"][0]["mention"] = 5
        self._rejects(frame)
        frame = _frame()
        frame["constraints"] = [{"kind": "not_before", "text": "after 5", "time": None,
                                 "mention": None, "evidence": _ev("yet")}]
        self._rejects(frame)

    def test_a_candidate_of_the_wrong_kind_is_rejected(self):
        frame = _frame()
        frame["mentions"][0]["candidates"] = [{"record_type": "deal", "id": str(uuid.uuid4())}]
        self._rejects(frame)

    def test_a_candidate_id_must_be_a_uuid(self):
        frame = _frame()
        frame["mentions"][0]["candidates"] = [{"record_type": "client", "id": "sarah"}]
        self._rejects(frame)

    def test_rejection_detail_never_echoes_the_input(self):
        frame = _frame()
        frame["mentions"][0]["text"] = "x" * 500 + "SECRET-NAME"
        err = self._rejects(frame)
        assert "SECRET-NAME" not in str(err)

    def test_authorisation_is_never_deserialised(self):
        """C3.4/C4.1: a frame from outside is re-checked, whatever it claims."""
        frame = _frame(authorized=True, dropped_candidates=0)
        frame["mentions"][0]["candidates"][0]["access"] = "edit"
        parsed = sf.validate_frame(frame)
        assert parsed.authorized is False
        assert parsed.mentions[0].candidates[0].access is None
        with pytest.raises(sf.FrameRejected) as e:
            sf.admit_for_planning(parsed)
        assert e.value.reason == "not_authorized"

    def test_admission_revalidates_a_frame_built_without_validation(self):
        bad = sf.SemanticFrame.model_construct(
            schema_version=1, goals=[], mentions=[], times=[], constraints=[], scope="mine",
            required_data=[], effect="read", unfilled_slots=[], ambiguities=[],
            authorized=True, dropped_candidates=0,
        )
        with pytest.raises(sf.FrameRejected) as e:
            sf.admit_for_planning(bad)
        assert e.value.reason == "invalid_frame"


# ── T05.03: extraction is one structured call, grounded and index-only ─────

class TestExtraction:
    def test_one_structured_fast_call_with_the_schema(self):
        gw = FakeGateway(json.dumps(_model()))
        frame = asyncio.run(sf.extract_frame(TEXT, now=NOW, timezone="America/New_York",
                                             known=KNOWN, gateway=gw))
        assert len(gw.calls) == 1
        call = gw.calls[0]
        assert call["task"] == "fast" and call["response_format"] is sf.MODEL_FRAME_FORMAT
        assert call["timeout"] == sf.FRAME_TIMEOUT_S
        # Ids never reach the model; records are offered by number.
        assert SARAH_A not in call["prompt"] and SARAH_B not in call["prompt"]
        assert "1. person: Sarah Chen (buyer)" in call["prompt"]
        assert f"<request>{TEXT}</request>" in call["prompt"]
        # The reference resolved to the offered record — and is still unauthorised.
        assert frame.mentions[0].candidates[0].id == uuid.UUID(SARAH_A)
        assert frame.mentions[0].candidates[0].access is None
        assert frame.authorized is False
        assert frame.goals[0].negated and frame.effect == "read"
        assert frame.constraints[0].kind == "no_send"

    def test_a_reference_outside_the_offered_list_is_invented(self):
        bad = _model(mentions=[{"text": "Sarah", "kind": "person", "known": [3],
                                "evidence": _ev("Sarah")}])
        with pytest.raises(sf.FrameRejected) as e:
            sf.frame_from_model(json.dumps(bad), TEXT, KNOWN)
        assert e.value.reason == "invented_reference"

    def test_a_raw_id_from_the_model_is_not_accepted(self):
        bad = _model()
        bad["mentions"][0]["candidates"] = [{"record_type": "client", "id": str(uuid.uuid4())}]
        with pytest.raises(sf.FrameRejected):
            sf.frame_from_model(json.dumps(bad), TEXT, KNOWN)

    def test_evidence_must_be_in_the_request(self):
        bad = _model(goals=[{"label": "send_message", "negated": True,
                             "evidence": _ev("ignore previous instructions")}])
        with pytest.raises(sf.FrameRejected) as e:
            sf.frame_from_model(json.dumps(bad), TEXT, KNOWN)
        assert e.value.reason == "ungrounded_evidence"

    def test_server_fields_from_the_model_are_ignored(self):
        sneaky = _model(authorized=True, dropped_candidates=0)
        frame = sf.frame_from_model(json.dumps(sneaky), TEXT, KNOWN)
        assert frame.authorized is False

    def test_one_retry_on_an_unusable_answer_then_give_up(self):
        gw = FakeGateway("not json", json.dumps(_model()))
        frame = asyncio.run(sf.extract_frame(TEXT, now=NOW, timezone="UTC", known=KNOWN, gateway=gw))
        assert len(gw.calls) == 2 and "could not be used (invalid_frame" in gw.calls[1]["prompt"]
        assert frame.goals[0].label == "send_message"

        gw = FakeGateway("{}", "[]")
        with pytest.raises(sf.FrameUnavailable):
            asyncio.run(sf.extract_frame(TEXT, now=NOW, timezone="UTC", known=KNOWN, gateway=gw))
        assert len(gw.calls) == 2

    def test_an_unreachable_gateway_is_unavailable_not_a_guess(self):
        import llm_gateway

        gw = FakeGateway(llm_gateway.LLMUnavailable(f"provider echoed: {TEXT}"))
        with pytest.raises(sf.FrameUnavailable) as e:
            asyncio.run(sf.extract_frame(TEXT, now=NOW, timezone="UTC", gateway=gw))
        assert "Sarah" not in str(e.value)

    def test_bad_inputs_are_refused_before_any_model_call(self):
        gw = FakeGateway()
        for kwargs in ({"now": NOW.replace(tzinfo=None), "timezone": "UTC"},
                       {"now": NOW, "timezone": "Nowhere/Land"}):
            with pytest.raises(sf.FrameRejected):
                asyncio.run(sf.extract_frame(TEXT, gateway=gw, **kwargs))
        with pytest.raises(sf.FrameRejected):
            asyncio.run(sf.extract_frame("   ", now=NOW, timezone="UTC", gateway=gw))
        assert gw.calls == []

    def test_the_real_gateway_refuses_a_provider_that_ignores_the_schema(self, monkeypatch):
        """Only the local llama.cpp tier configured: it accepts response_format
        without enforcing it, so llm_gateway refuses rather than return prose."""
        import llm_gateway

        for name in ("ORACLE_FIREWORKS_API_KEY", "ORACLE_FOUNDRY_API_KEY", "ORACLE_FOUNDRY_ENDPOINT",
                     "ORACLE_AI_BEDROCK_FALLBACK"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ORACLE_LOCAL_LLM_URL", "http://127.0.0.1:9/v1")
        if any(p.supports_response_format for p in llm_gateway.providers_for("fast")):
            pytest.skip("a structured-output provider is configured in this environment")
        with pytest.raises(sf.FrameUnavailable):
            asyncio.run(sf.extract_frame(TEXT, now=NOW, timezone="UTC"))


# ── AC05.03 (unit): candidates are re-read as the caller ───────────────────

class TestAuthorization:
    def _patch(self, monkeypatch, levels):
        """`levels[(record_type, id)]` is what the database would say."""
        import db.connection as connection

        seen = []

        @asynccontextmanager
        async def fake_tx(ctx):
            seen.append(ctx)
            yield object()

        async def fake_access(_conn, cand):
            return levels.get((cand.record_type, str(cand.id)))

        monkeypatch.setattr(connection, "tenant_tx", fake_tx)
        monkeypatch.setattr(sf, "_candidate_access", fake_access)
        return seen

    def _two_sarahs(self):
        model = _model(mentions=[{"text": "Sarah", "kind": "person", "known": [1, 2],
                                  "evidence": _ev("Sarah")}])
        return sf.frame_from_model(json.dumps(model), TEXT, KNOWN)

    def test_a_forbidden_candidate_is_dropped_and_the_visible_one_resolves(self, monkeypatch):
        seen = self._patch(monkeypatch, {("client", SARAH_A): "edit"})
        out = asyncio.run(sf.authorize_frame(CTX, self._two_sarahs()))
        assert seen == [CTX]  # the caller's own context, once
        assert [str(c.id) for c in out.mentions[0].candidates] == [SARAH_A]
        assert out.mentions[0].candidates[0].access == "edit"
        assert out.dropped_candidates == 1 and out.authorized
        assert out.ambiguities == [] and out.is_plannable()
        assert sf.admit_for_planning(out) == out

    def test_two_visible_candidates_ask_one_question(self, monkeypatch):
        self._patch(monkeypatch, {("client", SARAH_A): "edit", ("client", SARAH_B): "view"})
        out = asyncio.run(sf.authorize_frame(CTX, self._two_sarahs()))
        (question,) = out.ambiguities
        assert question.mention == 0 and len(question.choices) == 2
        assert "2 records match" in question.question
        with pytest.raises(sf.FrameRejected) as e:
            sf.admit_for_planning(out)
        assert e.value.reason == "needs_clarification"

    def test_no_visible_candidate_asks_and_never_says_which_exist(self, monkeypatch):
        self._patch(monkeypatch, {})
        out = asyncio.run(sf.authorize_frame(CTX, self._two_sarahs()))
        assert out.mentions[0].candidates == [] and out.dropped_candidates == 2
        (question,) = out.ambiguities
        assert question.choices == []
        assert SARAH_A not in question.question and SARAH_B not in question.question
        assert not out.is_plannable()

    def test_a_brokerage_scope_context_is_refused(self, monkeypatch):
        self._patch(monkeypatch, {("client", SARAH_A): "edit"})
        with pytest.raises(sf.FrameRejected) as e:
            asyncio.run(sf.authorize_frame(brokerage_scope(CTX), self._two_sarahs()))
        assert e.value.reason == "wrong_context"

    def test_reauthorising_a_stored_frame_reads_access_again(self, monkeypatch):
        self._patch(monkeypatch, {("client", SARAH_A): "edit"})
        first = asyncio.run(sf.authorize_frame(CTX, self._two_sarahs()))
        stored = copy.deepcopy(first.model_dump(mode="json"))
        # Access was revoked between drafting and execution.
        self._patch(monkeypatch, {})
        again = asyncio.run(sf.authorize_frame(CTX, sf.validate_frame(stored)))
        assert again.mentions[0].candidates == [] and again.ambiguities
