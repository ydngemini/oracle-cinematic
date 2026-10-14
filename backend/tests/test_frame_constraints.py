"""Stated constraints survive from utterance to tool call and mission step.

Prompt 05: T05.04 (discourse context), T05.05 (parser behind the fast path),
T05.07 (constraints end to end), AC05.04 ("Negation, draft-only and approval
constraints reach 07 tool calls and 08 mission steps unchanged. Evidence:
end-to-end test from utterance to staged command").

No model is called anywhere here: every gateway is a fake (and conftest
installs one with no model at all for every other test). No database either:
`tenant_tx` is replaced where a write would happen, and the writes are
captured and asserted.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

import ai_chat_store
import frame_constraints as fc
import semantic_frame as sf
import semantic_turn
from ai_tool_policy import READ_ONLY_TOOLS, TOOL_RISK
from platform_policy import requires_approval
from tenancy import Role, TenantContext

CTX = TenantContext(agent_id="a@t.test", tenant_id="11111111-1111-1111-1111-111111111111",
                    role=Role.AGENT)
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
SARAH = str(uuid.uuid4())
BOB = str(uuid.uuid4())
ASSISTANT = str(uuid.uuid4())


def _ev(quote, source="utterance"):
    return [{"source": source, "quote": quote}]


def _answer(text, *, goals, constraints=(), mentions=None, times=(), effect="read"):
    """A model answer in MODEL_FRAME_SCHEMA's shape."""
    return json.dumps({
        "goals": list(goals),
        "mentions": mentions if mentions is not None else [
            {"text": "her", "kind": "person", "known": [1], "evidence": _ev("her")}],
        "times": list(times),
        "constraints": list(constraints),
        "scope": "mine",
        "required_data": [],
        "effect": effect,
        "unfilled_slots": [],
        "ambiguities": [],
    })


DONT_MESSAGE = "Don't message her yet"
DONT_MESSAGE_ANSWER = _answer(
    DONT_MESSAGE,
    goals=[{"label": "send_message", "negated": True, "evidence": _ev("Don't message her")}],
    constraints=[{"kind": "no_send", "text": "not yet", "time": None, "mention": 0,
                  "evidence": _ev("Don't message her yet")}],
)
DRAFT_EMAIL = "Draft an email to her"
DRAFT_EMAIL_ANSWER = _answer(
    DRAFT_EMAIL,
    goals=[{"label": "draft_message", "negated": False, "evidence": _ev("Draft an email")}],
    effect="internal_edit",
)
SEND_NOW = "OK, text her now"
SEND_NOW_ANSWER = _answer(
    SEND_NOW,
    goals=[{"label": "send_message", "negated": False, "evidence": _ev("text her now")}],
    effect="external_effect",
)


class FakeGateway:
    def __init__(self, *answers, delay: float = 0.0):
        self.answers = list(answers)
        self.calls = 0
        self.delay = delay

    async def complete(self, prompt, **kwargs):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _c(kind, records=None, *, not_before=None, stated_at=NOW, unresolved=False):
    return fc.ActiveConstraint(kind=kind, records=tuple(records) if records else None,
                               not_before=not_before, origin="t", stated_at=stated_at,
                               unresolved=unresolved)


# ── the switch ──────────────────────────────────────────────────────────────

class TestSwitch:
    @pytest.mark.parametrize("value", [None, "", "1", "true", "on", "yes", "ture"])
    def test_on_by_default_and_on_for_anything_not_recognisably_off(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv(fc.ENV_FLAG, raising=False)
        else:
            monkeypatch.setenv(fc.ENV_FLAG, value)
        assert fc.enabled()

    @pytest.mark.parametrize("value", ["0", "false", "off", "no", " OFF "])
    def test_off_values(self, monkeypatch, value):
        monkeypatch.setenv(fc.ENV_FLAG, value)
        assert not fc.enabled()


# ── from a frame ────────────────────────────────────────────────────────────

def _frame(answer, text, known, *, access=None):
    frame = sf.frame_from_model(answer, text, known)
    if access is None:
        return frame
    mentions = []
    for m in frame.mentions:
        kept = [c.model_copy(update={"access": access[str(c.id)]})
                for c in m.candidates if access.get(str(c.id))]
        mentions.append(m.model_copy(update={"candidates": kept}))
    return frame.model_copy(update={"mentions": mentions, "authorized": True})


KNOWN = (sf.KnownRecord("client", SARAH, "Sarah Chen"),)


class TestFromFrame:
    def test_dont_message_her_is_a_no_send_scoped_to_her(self):
        frame = _frame(DONT_MESSAGE_ANSWER, DONT_MESSAGE, KNOWN, access={SARAH: "edit"})
        [c] = fc.from_frame(frame, origin="a", stated_at=NOW)
        assert c.kind == "no_send" and c.records == (SARAH,) and not c.unresolved

    def test_an_unauthorised_frame_never_narrows_a_restriction(self):
        frame = _frame(DONT_MESSAGE_ANSWER, DONT_MESSAGE, KNOWN)  # authorize never ran
        [c] = fc.from_frame(frame, origin="a", stated_at=NOW)
        assert c.records is None and c.unresolved

    def test_a_person_nobody_could_resolve_means_everyone(self):
        frame = _frame(DONT_MESSAGE_ANSWER, DONT_MESSAGE, KNOWN, access={})
        [c] = fc.from_frame(frame, origin="a", stated_at=NOW)
        assert c.records is None and c.unresolved
        assert fc.check_tool([c], "draft_sms", targets=frozenset({BOB}), now=NOW)

    def test_two_sarahs_protect_both(self):
        known = KNOWN + (sf.KnownRecord("client", BOB, "Sarah Chen (seller)"),)
        answer = json.loads(DONT_MESSAGE_ANSWER)
        answer["mentions"][0]["known"] = [1, 2]
        frame = _frame(json.dumps(answer), DONT_MESSAGE, known, access={SARAH: "edit", BOB: "view"})
        [c] = fc.from_frame(frame, origin="a", stated_at=NOW)
        assert set(c.records) == {SARAH, BOB}

    def test_a_negated_call_without_its_constraint_still_means_dont(self):
        text = "Don't call her"
        answer = _answer(text, goals=[{"label": "call", "negated": True,
                                       "evidence": _ev("Don't call her")}])
        frame = _frame(answer, text, KNOWN, access={SARAH: "edit"})
        [c] = fc.from_frame(frame, origin="a", stated_at=NOW)
        assert c.kind == "no_send" and c.records is None

    def test_not_before_carries_its_time(self):
        text = "Update her stage, but not before Monday 9am"
        answer = _answer(
            text,
            goals=[{"label": "update_record", "negated": False, "evidence": _ev("Update her stage")}],
            constraints=[{"kind": "not_before", "text": "Monday", "time": 0, "mention": None,
                          "evidence": _ev("not before Monday 9am")}],
            times=[{"text": "Monday 9am", "start": "2026-10-19T09:00:00-04:00", "end": None,
                    "timezone": "America/New_York", "evidence": _ev("Monday 9am")}],
            effect="internal_edit",
        )
        frame = _frame(answer, text, KNOWN, access={SARAH: "edit"})
        [c] = fc.from_frame(frame, origin="a", stated_at=NOW)
        assert c.kind == "not_before"
        assert c.not_before == datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc)


# ── the tool boundary: one rule table, every tool ───────────────────────────

CONTACT_TOOLS = {n for n, r in TOOL_RISK.items() if r in fc.CONTACT_RISKS}
GATED = {n for n, r in TOOL_RISK.items() if requires_approval(r)}
INTERNAL = {n for n in TOOL_RISK if n not in READ_ONLY_TOOLS and n not in GATED}


class TestToolRules:
    T = frozenset({SARAH})

    def _refused(self, constraint, tool, targets=None, now=NOW):
        return fc.check_tool([constraint], tool, targets=self.T if targets is None else targets,
                             now=now)

    def test_the_sets_are_what_the_tool_policy_says(self):
        assert {"draft_email", "draft_sms", "call_contact", "schedule_event",
                "share_property_tour"} == CONTACT_TOOLS
        assert {"update_client", "add_client_note", "move_deal_stage"} <= INTERNAL

    @pytest.mark.parametrize("tool", sorted(TOOL_RISK))
    def test_every_tool_under_every_constraint(self, tool):
        """The whole table, so a new tool cannot fall between the rules."""
        later = NOW + timedelta(days=1)
        expected = {
            "no_send": tool in CONTACT_TOOLS,
            "do_not_contact": tool in CONTACT_TOOLS,
            "draft_only": tool in GATED and tool not in fc.DRAFT_TOOLS,
            "approval_required": tool in INTERNAL,
            "not_before": tool not in READ_ONLY_TOOLS,
        }
        for kind, refuse in expected.items():
            c = _c(kind, not_before=later if kind == "not_before" else None)
            got = self._refused(c, tool)
            assert (got is not None) == refuse, (kind, tool)
            if got is not None:
                receipt = got.receipt()
                assert receipt["ok"] is False and receipt["refused"] is True
                assert receipt["constraint"] == kind and got.code.startswith("FRAME_")

    def test_read_only_tools_are_never_refused(self):
        for tool in READ_ONLY_TOOLS:
            for kind in fc.KINDS:
                c = _c(kind, not_before=NOW + timedelta(days=1) if kind == "not_before" else None)
                assert self._refused(c, tool) is None

    def test_a_scoped_constraint_leaves_other_people_alone(self):
        c = _c("no_send", [SARAH])
        assert self._refused(c, "draft_sms", frozenset({SARAH}))
        assert self._refused(c, "draft_sms", frozenset({BOB})) is None

    def test_a_call_whose_target_is_unknown_counts_as_the_person(self):
        assert self._refused(_c("do_not_contact", [SARAH]), "draft_sms", frozenset())

    def test_not_before_ends_when_the_time_passes(self):
        c = _c("not_before", not_before=NOW + timedelta(hours=1))
        assert self._refused(c, "update_client")
        assert self._refused(c, "update_client", now=NOW + timedelta(hours=2)) is None

    def test_targets_include_the_selected_record_and_named_ids(self):
        t = fc.tool_targets({"client_id": BOB, "body": "hi"}, "client", SARAH)
        assert t == frozenset({SARAH, BOB})
        assert fc.tool_targets("not a dict", None, None) == frozenset()

    def test_refusal_text_names_nobody(self):
        for kind in fc.KINDS:
            c = _c(kind, [SARAH], not_before=NOW + timedelta(days=1))
            for tool in ("draft_sms", "update_client", "call_contact"):
                r = self._refused(c, tool)
                if r is not None:
                    assert SARAH not in r.message


# ── discourse: amend, never stack ───────────────────────────────────────────

class TestDiscourse:
    def test_a_chat_constraint_expires(self):
        old = _c("no_send", stated_at=NOW - fc.DISCOURSE_TTL - timedelta(seconds=1))
        assert fc.merge([old], [], lifted_for=None, now=NOW) == []
        fresh = _c("no_send", stated_at=NOW - timedelta(minutes=5))
        assert fc.merge([fresh], [], lifted_for=None, now=NOW) == [fresh]

    def test_a_passed_not_before_is_dropped(self):
        c = _c("not_before", not_before=NOW - timedelta(minutes=1))
        assert fc.merge([c], [], lifted_for=None, now=NOW) == []

    def test_an_explicit_request_lifts_only_what_it_contradicts(self):
        her = _c("no_send", [SARAH])
        bob = _c("do_not_contact", [BOB])
        approval = _c("approval_required")
        out = fc.merge([her, bob, approval], [], lifted_for=frozenset({SARAH}), now=NOW)
        assert out == [bob, approval]

    def test_a_request_naming_nobody_lifts_only_unscoped_blocks(self):
        everyone = _c("no_send")
        her = _c("no_send", [SARAH])
        assert fc.merge([everyone, her], [], lifted_for=frozenset(), now=NOW) == [her]

    def test_duplicates_collapse_to_the_newest(self):
        a = _c("no_send", [SARAH], stated_at=NOW - timedelta(minutes=3))
        b = _c("no_send", [SARAH], stated_at=NOW)
        assert fc.merge([a], [b], lifted_for=None, now=NOW) == [b]

    def test_unreadable_storage_fails_closed(self):
        out = fc.load([{"kind": "no_send", "bogus": 1}])
        assert {c.kind for c in out} == {"no_send", "approval_required"}
        assert all(c.records is None for c in out)
        assert fc.load(None) == []

    def test_roundtrip(self):
        items = [_c("no_send", [SARAH]), _c("not_before", not_before=NOW + timedelta(days=1))]
        assert fc.load(json.loads(json.dumps(fc.dump(items)))) == items


# ── the chat turn: parser tier, storage, chokepoint ─────────────────────────

class _Tx:
    """A tenant_tx stand-in that records every statement."""

    def __init__(self, fail=False):
        self.statements = []
        self.fail = fail

    def __call__(self, ctx):
        tx = self

        @asynccontextmanager
        async def _cm():
            if tx.fail:
                raise ConnectionError("database down")

            class Conn:
                async def execute(self, sql, *args):
                    tx.statements.append((" ".join(sql.split()), args))
                    return "OK"

                async def fetchrow(self, *_a):
                    return None

            yield Conn()
        return _cm()


def _bundle(text, *, history=()):
    return {
        "assistant": {"context_type": "client", "context_id": SARAH, "id": ASSISTANT},
        "record": {"label": "Sarah Chen", "full_name": "Sarah Chen"},
        "messages": list(history) + [{"id": str(uuid.uuid4()), "role": "user", "content": text}],
        "attachments": [],
    }


@pytest.fixture
def chat(monkeypatch):
    """Authorise every offered candidate as 'edit'; capture DB writes."""
    import db.connection as connection

    tx = _Tx()
    monkeypatch.setattr(connection, "tenant_tx", tx)
    monkeypatch.setattr(ai_chat_store, "tenant_tx", tx)

    async def visible(_conn, cand):
        return "edit"

    monkeypatch.setattr(sf, "_candidate_access", visible)
    monkeypatch.delenv(fc.ENV_FLAG, raising=False)
    return tx


def _stored(tx):
    persisted = [a for s, a in tx.statements if s.startswith("UPDATE ai_chat_messages")]
    return json.loads(persisted[-1][1]) if persisted else None


class TestTurn:
    def test_the_frame_is_parsed_authorised_and_stored_without_words(self, chat, monkeypatch):
        monkeypatch.setattr(semantic_turn, "GATEWAY", FakeGateway(DONT_MESSAGE_ANSWER))
        turn = asyncio.run(semantic_turn.prepare_turn(CTX, _bundle(DONT_MESSAGE), ASSISTANT, now=NOW))
        assert turn.status == "parsed" and turn.frame.authorized
        assert [c.kind for c in turn.active] == ["no_send"]
        stored = _stored(chat)
        assert stored["active"][0]["records"] == [SARAH]
        blob = json.dumps(stored)
        assert "Sarah" not in blob and "message" not in blob.replace("send_message", "")
        assert "STATED CONSTRAINTS" in turn.prompt_block() and "Sarah" not in turn.prompt_block()

    @pytest.mark.parametrize("failure,status", [
        (sf.FrameUnavailable("x"), "unavailable"),
        (RuntimeError("provider exploded"), "unavailable"),
        ("not json", "unavailable"),  # rejected twice -> FrameUnavailable
    ])
    def test_a_parser_failure_is_a_frame_less_turn(self, chat, monkeypatch, failure, status):
        answers = [failure, failure] if isinstance(failure, str) else [failure]
        monkeypatch.setattr(semantic_turn, "GATEWAY", FakeGateway(*answers))
        turn = asyncio.run(semantic_turn.prepare_turn(
            CTX, _bundle("What did she say about the kitchen?"), ASSISTANT, now=NOW))
        assert turn.status == status and turn.frame is None and turn.active == []
        assert _stored(chat) is None  # nothing to store, no write

    @pytest.mark.parametrize("failure", [sf.FrameUnavailable("x"), "not json"])
    def test_a_parser_failure_still_keeps_a_stated_restriction(self, chat, monkeypatch, failure):
        """L05-04: the deterministic backstop reads "Don't message her yet"
        when the parser cannot (before L05-04 this turn was frame-less)."""
        answers = [failure, failure] if isinstance(failure, str) else [failure]
        monkeypatch.setattr(semantic_turn, "GATEWAY", FakeGateway(*answers))
        turn = asyncio.run(semantic_turn.prepare_turn(CTX, _bundle(DONT_MESSAGE), ASSISTANT, now=NOW))
        assert turn.status == "unavailable" and turn.frame is None
        assert [c.kind for c in turn.active] == ["no_send"] and turn.backstop == ["no_send"]
        assert _stored(chat)["backstop"] == ["no_send"]

    def test_the_parser_is_bounded_by_its_budget(self, chat, monkeypatch):
        monkeypatch.setattr(semantic_turn, "FRAME_BUDGET_S", 0.2)
        monkeypatch.setattr(semantic_turn, "GATEWAY", FakeGateway(DONT_MESSAGE_ANSWER, delay=5))
        started = time.perf_counter()
        turn = asyncio.run(semantic_turn.prepare_turn(CTX, _bundle(DONT_MESSAGE), ASSISTANT, now=NOW))
        assert time.perf_counter() - started < 1.0
        # Timed out: the parser's frame is lost, the backstop's no_send is not.
        assert turn.status == "timed_out" and [c.kind for c in turn.active] == ["no_send"]

    def test_constraints_from_earlier_turns_survive_a_parser_outage(self, chat, monkeypatch):
        earlier = {"id": str(uuid.uuid4()), "role": "assistant", "content": "ok",
                   "metadata": {"semantic_frame": {"active": fc.dump([_c("no_send", [SARAH],
                                stated_at=NOW - timedelta(minutes=2))])}}}
        monkeypatch.setattr(semantic_turn, "GATEWAY", FakeGateway(sf.FrameUnavailable("down")))
        turn = asyncio.run(semantic_turn.prepare_turn(
            CTX, _bundle("draft a text to her", history=[earlier]), ASSISTANT, now=NOW))
        assert turn.status == "unavailable" and [c.kind for c in turn.active] == ["no_send"]
        assert _stored(chat)["active"][0]["kind"] == "no_send"

    def test_constraints_that_cannot_be_stored_fail_the_turn(self, monkeypatch):
        import db.connection as connection

        monkeypatch.delenv(fc.ENV_FLAG, raising=False)
        monkeypatch.setattr(connection, "tenant_tx", _Tx(fail=True))
        monkeypatch.setattr(sf, "authorize_frame", _authorize_all)
        monkeypatch.setattr(semantic_turn, "GATEWAY", FakeGateway(DONT_MESSAGE_ANSWER))
        with pytest.raises(RuntimeError, match="could not be stored"):
            asyncio.run(semantic_turn.prepare_turn(CTX, _bundle(DONT_MESSAGE), ASSISTANT, now=NOW))

    def test_switched_off_there_is_no_call_no_write_no_binding(self, chat, monkeypatch):
        monkeypatch.setenv(fc.ENV_FLAG, "0")
        gw = FakeGateway(DONT_MESSAGE_ANSWER)
        monkeypatch.setattr(semantic_turn, "GATEWAY", gw)
        assert asyncio.run(semantic_turn.prepare_turn(CTX, _bundle(DONT_MESSAGE), ASSISTANT)) is None
        assert gw.calls == 0 and chat.statements == []
        semantic_turn.bind(ASSISTANT, [_c("no_send")])
        assert semantic_turn.constraints_for(ASSISTANT) == ()
        assert asyncio.run(semantic_turn.mission_constraints(CTX, "Reactivate my sphere")) == {}

    def test_added_latency_with_an_instant_parser(self, chat, monkeypatch):
        """Measured with the fake gateway only: the cost of the tier itself
        (parse, validate, authorise, merge, store), not of any model."""
        runs = []
        for _ in range(50):
            monkeypatch.setattr(semantic_turn, "GATEWAY", FakeGateway(DONT_MESSAGE_ANSWER))
            started = time.perf_counter()
            asyncio.run(semantic_turn.prepare_turn(CTX, _bundle(DONT_MESSAGE), ASSISTANT, now=NOW))
            runs.append((time.perf_counter() - started) * 1000)
        runs.sort()
        p50, p95 = runs[len(runs) // 2], runs[int(len(runs) * 0.95)]
        print(f"\nsemantic tier overhead, fake gateway: p50={p50:.2f} ms p95={p95:.2f} ms (n=50)")
        assert p95 < 250


async def _authorize_all(ctx, frame):
    mentions = [m.model_copy(update={"candidates": [
        c.model_copy(update={"access": "edit"}) for c in m.candidates]}) for m in frame.mentions]
    return frame.model_copy(update={"mentions": mentions, "authorized": True})


# ── AC05.04 end to end: utterance -> chat turn -> tool chokepoint ───────────

class _Provider:
    name = "fake"
    model = "fake-model"


def _tool_call(name, args):
    return {"choices": [{"message": {"content": "", "tool_calls": [{
        "id": f"call-{name}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def _final(text):
    return {"choices": [{"message": {"content": text}}]}


@pytest.fixture
def turn_env(chat, monkeypatch):
    """Drive the real _generate -> _local_fallback -> execute_safe_tool path
    with a scripted model. Dispatch past the chokepoint is captured, not run."""
    import ai_chat_agent
    import llm_gateway

    dispatched = []
    script = []

    async def fake_round(messages, tools, **_kw):
        script_item = script.pop(0)
        return script_item

    async def fake_dispatch(ctx, user_id, message_id, tool_name, tool_input,
                            context_type, context_id, call_index=None):
        dispatched.append(tool_name)
        if tool_name == "draft_sms":
            return {"ok": True, "action_type": "draft_sms", "command_id": "cmd-1",
                    "approval_id": "ap-1", "state": "awaiting_approval", "sent": False}
        return {"ok": True, "action_type": tool_name}

    class FakeMemory:
        def __init__(self, _ctx): pass
        async def inject_jit_prompt(self, _agent, base): return base

    async def no_brokerage(_ctx):
        return None

    monkeypatch.setattr(llm_gateway, "tool_round", fake_round)
    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda: [_Provider()])
    monkeypatch.setattr(ai_chat_agent, "_resolve_brokerage_context", no_brokerage)
    monkeypatch.setattr(ai_chat_agent, "SessionManager", FakeMemory)
    monkeypatch.setattr(ai_chat_agent, "offered_tool_names",
                        lambda _ct: frozenset(TOOL_RISK))
    monkeypatch.setattr(ai_chat_store, "_execute_safe_tool", fake_dispatch)
    return {"agent": ai_chat_agent, "script": script, "dispatched": dispatched}


def _run_turn(env, text, model_answer, tool, args, *, history=()):
    env["script"][:] = [_tool_call(tool, args), _final("done")]
    gw = FakeGateway(model_answer)
    semantic_turn.GATEWAY = gw
    bundle = _bundle(text, history=history)
    assistant = str(uuid.uuid4())
    text_out, actions, _model = asyncio.run(env["agent"]._generate(CTX, bundle, assistant))
    return assistant, gw


def _ledger_refusals(tx):
    return [a for s, a in tx.statements
            if s.startswith("INSERT INTO ai_tool_operations") and "'failed'" in s]


class TestEndToEnd:
    def test_dont_message_her_refuses_the_draft_and_records_why(self, turn_env, chat):
        assistant, gw = _run_turn(turn_env, DONT_MESSAGE, DONT_MESSAGE_ANSWER,
                                  "draft_sms", {"client_id": SARAH, "body": "Hi Sarah"})
        assert gw.calls == 1
        assert turn_env["dispatched"] == []          # never reached the dispatcher
        [row] = _ledger_refusals(chat)
        assert row[2] == assistant and row[4] == "draft_sms" and row[6] == "FRAME_NO_SEND"

    def test_an_internal_note_is_still_allowed(self, turn_env, chat):
        _run_turn(turn_env, DONT_MESSAGE, DONT_MESSAGE_ANSWER,
                  "add_client_note", {"client_id": SARAH, "note": "wait"})
        assert turn_env["dispatched"] == ["add_client_note"]
        assert _ledger_refusals(chat) == []

    def test_the_constraint_survives_into_the_next_turn_and_a_lift_ends_it(self, turn_env, chat):
        _run_turn(turn_env, DONT_MESSAGE, DONT_MESSAGE_ANSWER,
                  "draft_sms", {"client_id": SARAH, "body": "Hi"})
        first = _stored(chat)
        prior = [{"id": str(uuid.uuid4()), "role": "user", "content": DONT_MESSAGE},
                 {"id": str(uuid.uuid4()), "role": "assistant", "content": "Understood.",
                  "metadata": {"semantic_frame": first}}]

        # "Draft an email to her" does not contradict "don't message her yet":
        # it is still refused — a later request never quietly stacks.
        _run_turn(turn_env, DRAFT_EMAIL, DRAFT_EMAIL_ANSWER,
                  "draft_email", {"client_id": SARAH, "subject": "Hi", "body": "Hi"},
                  history=prior)
        assert turn_env["dispatched"] == []
        second = _stored(chat)
        assert [c["kind"] for c in second["active"]] == ["no_send"]

        prior += [{"id": str(uuid.uuid4()), "role": "user", "content": DRAFT_EMAIL},
                  {"id": str(uuid.uuid4()), "role": "assistant", "content": "Held.",
                   "metadata": {"semantic_frame": second}}]
        # "OK, text her now" is the explicit contradiction: it amends the frame.
        _run_turn(turn_env, SEND_NOW, SEND_NOW_ANSWER,
                  "draft_sms", {"client_id": SARAH, "body": "Hi"}, history=prior)
        assert turn_env["dispatched"] == ["draft_sms"]   # staged for approval, not sent
        assert _stored(chat)["active"] == []

    def test_switched_off_the_same_turn_behaves_as_before(self, turn_env, chat, monkeypatch):
        monkeypatch.setenv(fc.ENV_FLAG, "off")
        assistant, gw = _run_turn(turn_env, DONT_MESSAGE, DONT_MESSAGE_ANSWER,
                                  "draft_sms", {"client_id": SARAH, "body": "Hi"})
        assert gw.calls == 0
        assert turn_env["dispatched"] == ["draft_sms"]
        assert _stored(chat) is None and _ledger_refusals(chat) == []

    def test_no_provider_and_recovery_mode_do_not_break_chat(self, turn_env, chat, monkeypatch):
        monkeypatch.setenv("ORACLE_RECOVERY_MODE", "1")
        turn_env["script"][:] = [_final("Here is what I found.")]
        semantic_turn.GATEWAY = None   # the real llm_gateway, with nothing configured
        for name in ("ORACLE_FIREWORKS_API_KEY", "FIREWORKS_API_KEY", "ORACLE_FOUNDRY_API_KEY",
                     "ORACLE_FOUNDRY_ENDPOINT", "ORACLE_AI_BEDROCK_FALLBACK",
                     "AZURE_OPENAI_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        import llm_gateway
        if any(p.supports_response_format for p in llm_gateway.providers_for("fast")):
            pytest.skip("a structured-output provider is configured in this environment")
        text, actions, _ = asyncio.run(turn_env["agent"]._generate(
            CTX, _bundle("what's new with her?"), str(uuid.uuid4())))
        assert text == "Here is what I found." and actions == []


# ── AC05.04 end to end: objective -> mission row -> step verdict ────────────

class TestMissions:
    def _mission(self, monkeypatch, objective, answer, **row):
        monkeypatch.delenv(fc.ENV_FLAG, raising=False)
        monkeypatch.setattr(semantic_turn, "GATEWAY", FakeGateway(answer))
        stated = asyncio.run(semantic_turn.mission_constraints(CTX, objective, now=NOW))
        base = {"id": str(uuid.uuid4()), "status": "active", "mode": "live",
                "allowed_channels": ["email", "sms", "voice"],
                "auto_channels": ["email", "sms", "voice"],
                "consent_at": NOW.isoformat(), "budget_cents": 0,
                "constraints": json.dumps(stated)}
        base.update(row)
        return stated, base

    @pytest.fixture
    def gate(self, monkeypatch):
        import autonomy
        import outreach_compliance
        from missions import policy

        async def may_act(*_a, **_k):
            return False, "dial pinned to assist"

        class Decision:
            allowed = True
            blockers = ()
            required_disclosures = ()

        async def guard(*_a, **_k):
            return Decision()

        async def ready(*_a, **_k):
            return True, []

        async def zero(*_a, **_k):
            return 0

        async def monthly(*_a, **_k):
            return 0, 0

        monkeypatch.setattr(autonomy, "may_act", may_act)
        monkeypatch.setattr(outreach_compliance, "guard_outreach", guard)
        monkeypatch.setattr(policy, "outbound_ready", ready)
        monkeypatch.setattr(policy, "spend_so_far", zero)
        monkeypatch.setattr(policy, "monthly_spend", monthly)

        def run(mission, channel, subject=None, now=NOW):
            return asyncio.run(policy.evaluate_action(
                CTX, mission, {"channel": channel}, contact="+15555550100",
                state_code="DE", now=now, subject_id=subject))
        return run

    OBJECTIVE = "Reactivate my sphere, drafts only, nothing before Monday 9am"
    ANSWER = json.dumps({
        "goals": [{"label": "draft_message", "negated": False, "evidence": _ev("Reactivate my sphere")}],
        "mentions": [],
        "times": [{"text": "Monday 9am", "start": "2026-10-19T09:00:00-04:00", "end": None,
                   "timezone": "America/New_York", "evidence": _ev("Monday 9am")}],
        "constraints": [
            {"kind": "draft_only", "text": "drafts only", "time": None, "mention": None,
             "evidence": _ev("drafts only")},
            {"kind": "not_before", "text": "Monday", "time": 0, "mention": None,
             "evidence": _ev("nothing before Monday 9am")}],
        "scope": "mine", "required_data": [], "effect": "internal_edit",
        "unfilled_slots": [], "ambiguities": [],
    })

    def test_a_granted_mission_defers_then_stages_but_never_self_releases(self, monkeypatch, gate):
        stated, mission = self._mission(monkeypatch, self.OBJECTIVE, self.ANSWER)
        assert stated["semantic_frame"]["status"] == "parsed"
        assert {c["kind"] for c in stated["semantic_frame"]["active"]} == {"draft_only", "not_before"}

        before = gate(mission, "sms")
        assert not before.may_stage and before.defer_until == datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc)

        after = gate(mission, "sms", now=datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc))
        assert after.may_stage and not after.may_release   # the grant would have released it
        assert "drafts only" in after.reason and after.constraint == "draft_only"

        call = gate(mission, "voice", now=datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc))
        assert not call.may_stage and "a call is not a draft" in call.blocked_reason

    def test_without_stated_constraints_the_grant_releases_as_before(self, monkeypatch, gate):
        _, mission = self._mission(monkeypatch, "x", "", constraints="{}")
        verdict = gate(mission, "sms")
        assert verdict.may_stage and verdict.may_release

    def test_do_not_contact_withholds_that_person_only(self, monkeypatch, gate):
        mission = {"id": "m", "status": "active", "mode": "live", "allowed_channels": ["sms"],
                   "auto_channels": [], "budget_cents": 0,
                   "constraints": {"semantic_frame": {"active": fc.dump([_c("do_not_contact", [BOB])])}}}
        monkeypatch.delenv(fc.ENV_FLAG, raising=False)
        held = gate(mission, "sms", subject=BOB)
        assert not held.may_stage and held.blocked_reason.startswith("stated constraint")
        assert gate(mission, "sms", subject=SARAH).may_stage
        assert not gate(mission, "sms", subject=None).may_stage   # unknown = maybe Bob

    def test_switched_off_stored_constraints_are_not_read(self, monkeypatch, gate):
        mission = {"id": "m", "status": "active", "mode": "live", "allowed_channels": ["sms"],
                   "auto_channels": [], "budget_cents": 0,
                   "constraints": {"semantic_frame": {"active": fc.dump([_c("no_send")])}}}
        monkeypatch.setenv(fc.ENV_FLAG, "0")
        assert gate(mission, "sms").may_stage

    def test_a_garbled_mission_constraint_fails_closed(self, monkeypatch, gate):
        mission = {"id": "m", "status": "active", "mode": "live", "allowed_channels": ["sms"],
                   "auto_channels": [], "budget_cents": 0,
                   "constraints": {"semantic_frame": "garbage"}}
        monkeypatch.delenv(fc.ENV_FLAG, raising=False)
        assert not gate(mission, "sms").may_stage

    def test_the_executor_moves_a_deferred_step_and_journals_once(self):
        import inspect

        from missions import executor

        src = inspect.getsource(executor._work_one)
        assert src.index("defer_until") < src.index("if not verdict.may_stage")
        assert "subject_id=" in src
        defer_src = inspect.getsource(executor._defer)
        assert "state = 'planned'" in defer_src and "due_at" in defer_src
