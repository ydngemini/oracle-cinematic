"""The chat turn asks the frame's one precise question instead of picking a
record (prompt 05, T05.10, AC05.03; ledger L05-08).

authorize_frame already leaves an Ambiguity with the authorised choices when
several records the caller can open match a name (L05-01). This pins what
happens next: the turn carries that question with each choice labelled from
the list the parser was shown, the model is told to ask and name the choices,
and the tool chokepoint refuses acting on any of them — or creating a new
person instead — until the person answers. Nothing is stored beyond the
boolean the summary already kept (C5.4).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

import ai_chat_store
import frame_constraints as fc
import semantic_frame as sf
import semantic_turn
from tenancy import Role, TenantContext

CTX = TenantContext(agent_id="a@t.test", tenant_id="11111111-1111-1111-1111-111111111111", role=Role.AGENT)
SARAH_A, SARAH_B, SOMEONE = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
KNOWN = (
    sf.KnownRecord("client", SARAH_A, "Sarah Chen (buyer)"),
    sf.KnownRecord("client", SARAH_B, "Sarah Chen (seller)"),
)
TEXT = "Text Sarah about the showing"


def _ev(quote):
    return [{"source": "utterance", "quote": quote}]


def _frame(known_refs, grant):
    """A frame the parser could return for TEXT, authorised with `grant`
    ({(type, id): access}) — the same seam test_semantic_frame uses."""
    model = {
        "goals": [{"label": "send_message", "negated": False, "evidence": _ev("Text Sarah")}],
        "mentions": [{"text": "Sarah", "kind": "person", "known": known_refs, "evidence": _ev("Sarah")}],
        "times": [], "constraints": [], "scope": "mine",
        "required_data": [{"source": "people", "max_age_seconds": None}],
        "effect": "external_effect", "unfilled_slots": [], "ambiguities": [],
    }
    frame = sf.frame_from_model(json.dumps(model), TEXT, KNOWN)

    async def access(_conn, cand):
        return grant.get((cand.record_type, str(cand.id)))

    return frame, access


def _authorised(monkeypatch, known_refs, grant):
    from contextlib import asynccontextmanager

    import db.connection as connection

    @asynccontextmanager
    async def tx(_ctx):
        yield object()

    frame, access = _frame(known_refs, grant)
    monkeypatch.setattr(connection, "tenant_tx", tx)
    monkeypatch.setattr(sf, "_candidate_access", access)
    return asyncio.run(sf.authorize_frame(CTX, frame))


# ── the question ────────────────────────────────────────────────────────────

def test_two_records_the_caller_can_open_become_one_labelled_question(monkeypatch):
    frame = _authorised(monkeypatch, [1, 2], {("client", SARAH_A): "edit", ("client", SARAH_B): "view"})
    q = semantic_turn.question_from(frame, KNOWN)
    assert q.text == "Sarah"
    assert [(c.id, c.label) for c in q.choices] == [(SARAH_A, "Sarah Chen (buyer)"), (SARAH_B, "Sarah Chen (seller)")]
    assert q.pending() == fc.Pending("Sarah", frozenset({SARAH_A, SARAH_B}))


def test_one_authorised_record_is_an_answer_not_a_question(monkeypatch):
    frame = _authorised(monkeypatch, [1, 2], {("client", SARAH_A): "edit"})
    assert semantic_turn.question_from(frame, KNOWN) is None
    assert semantic_turn.question_from(None, KNOWN) is None


def test_nobody_the_caller_can_open_is_still_a_question_and_never_names_the_dropped(monkeypatch):
    frame = _authorised(monkeypatch, [1, 2], {})
    q = semantic_turn.question_from(frame, KNOWN)
    assert q.text == "Sarah" and q.choices == () and q.pending() is None
    block = semantic_turn.TurnFrame(status="parsed", frame=frame, question=q).question_block()
    assert "never invent a record" in block
    assert "Sarah Chen" not in block and SARAH_A not in block


def test_the_block_names_the_choices_and_the_rule_in_the_persons_language(monkeypatch):
    frame = _authorised(monkeypatch, [1, 2], {("client", SARAH_A): "edit", ("client", SARAH_B): "edit"})
    q = semantic_turn.question_from(frame, KNOWN)
    en = semantic_turn.TurnFrame(status="parsed", frame=frame, question=q, lang="en").question_block()
    assert en.startswith("## ASK FIRST — ENFORCED BY THE SERVER")
    assert "2 records match “Sarah”. Ask which one" in en
    assert "1) Sarah Chen (buyer) (client)" in en and "2) Sarah Chen (seller) (client)" in en
    assert "Do not pick one" in en and "refuses tool calls" in en
    es = semantic_turn.TurnFrame(status="parsed", frame=frame, question=q, lang="es").question_block()
    assert "2 registros coinciden con “Sarah”" in es and "No elijas una" in es and "writing in Spanish" in es
    assert semantic_turn.TurnFrame(status="parsed").question_block() == ""


# ── the chokepoint ──────────────────────────────────────────────────────────

PENDING = fc.Pending("Sarah", frozenset({SARAH_A, SARAH_B}))


@pytest.mark.parametrize("tool, target, refused", [
    ("draft_sms", SARAH_A, True),            # contacting one of them is picking
    ("add_client_note", SARAH_B, True),      # so is editing one of them
    ("draft_sms", SOMEONE, False),           # another record was never in question
    ("get_client", SARAH_A, False),          # reads help the person choose
    ("create_client", None, True),           # inventing a third Sarah is the guess itself
])
def test_the_boundary_refuses_acting_on_a_record_still_in_question(tool, target, refused):
    targets = frozenset({target}) if target else frozenset()
    out = fc.check_pending(PENDING, tool, targets=targets, lang="en")
    if not refused:
        assert out is None
        return
    assert out.code == "FRAME_CLARIFY" and out.kind == "clarify"
    assert "Sarah" in out.message and SARAH_A not in out.message
    if tool == "create_client":
        assert "nothing was created" in out.message
    else:
        assert "did not act on either record" in out.message
    assert fc.check_pending(None, tool, targets=targets) is None


def test_the_refusal_is_written_in_the_persons_language():
    out = fc.check_pending(PENDING, "draft_sms", targets=frozenset({SARAH_A}), lang="es")
    assert out.message.startswith("Pregunté a qué “Sarah” te referías") and out.code == "FRAME_CLARIFY"


def test_execute_safe_tool_refuses_from_the_bound_question_before_the_dispatcher(monkeypatch):
    recorded = []

    async def must_not_dispatch(*_a, **_k):
        raise AssertionError("a record still in question must not reach the dispatcher")

    async def record_failure(ctx, user_id, assistant_id, call_index, tool_name, arguments_hash, code):
        recorded.append((tool_name, code))

    monkeypatch.delenv(fc.ENV_FLAG, raising=False)
    monkeypatch.setattr(ai_chat_store, "_execute_safe_tool", must_not_dispatch)
    monkeypatch.setattr(ai_chat_store, "_ledger_record_failure", record_failure)
    import tool_offer
    tool_offer.remember(CTX, "turn-q", tool_offer.for_actor(CTX, "client"))

    async def run():
        semantic_turn.bind("turn-q", [], "en", pending=PENDING)
        refused = await ai_chat_store.execute_safe_tool(
            CTX, CTX.agent_id, "turn-q", "draft_sms", {"client_id": SARAH_A, "body": "hi"}, "client", SARAH_A, 0)
        other_turn = await ai_chat_store.execute_safe_tool(
            CTX, CTX.agent_id, "turn-other", "get_client", {"client_id": SARAH_A}, None, None)
        return refused, other_turn

    async def plain_read(*_a, **_k):
        return {"ok": True}

    refused, _ = asyncio.run(run())
    assert refused == {"ok": False, "refused": True, "constraint": "clarify",
                       "error": "I asked which “Sarah” you meant and have not had an answer yet, so I did not act on either record."}
    assert recorded == [("draft_sms", "FRAME_CLARIFY")], "the refusal is ledgered as a failed attempt"

    # Another turn, or the switch off, is exactly the previous path.
    monkeypatch.setattr(ai_chat_store, "_execute_safe_tool", plain_read)

    async def read_elsewhere():
        semantic_turn.bind("turn-q", [], "en", pending=PENDING)
        return await ai_chat_store.execute_safe_tool(
            CTX, CTX.agent_id, "turn-other", "add_client_note", {"client_id": SARAH_A, "note": "x"}, "client", SARAH_A)

    assert asyncio.run(read_elsewhere()) == {"ok": True}
    monkeypatch.setenv(fc.ENV_FLAG, "off")

    async def switched_off():
        semantic_turn.bind("turn-q", [], "en", pending=PENDING)
        return await ai_chat_store.execute_safe_tool(
            CTX, CTX.agent_id, "turn-q", "add_client_note", {"client_id": SARAH_A, "note": "x"}, "client", SARAH_A)

    assert asyncio.run(switched_off()) == {"ok": True}


def test_a_turn_without_a_question_binds_nothing_pending():
    async def run():
        semantic_turn.bind("turn-none", [], "en")
        return semantic_turn.pending_for("turn-none"), semantic_turn.pending_for("someone-else")

    assert asyncio.run(run()) == (None, None)
