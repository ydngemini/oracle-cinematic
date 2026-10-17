"""A person named in chat who is not yet a known record is looked up as the
caller, so the turn resolves one match or asks about several (prompt 05,
T05.10, AC05.03; ledger L05-10).

The parser can only refer to the records it was shown. "Text Sarah" with no
Sarah on screen used to leave a mention with no candidates — no question,
and the model free to search and guess. Now the fast path's own people search
runs for that mention, as the caller, and authorize_frame keeps or drops the
hits like any other candidate. Pronouns are never searched; a failed, slow or
unrun search changes nothing.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

import frame_constraints as fc
import search_api
import semantic_frame as sf
import semantic_turn
import source_status as ss
from tenancy import Role, TenantContext

CTX = TenantContext(agent_id="a@t.test", tenant_id="11111111-1111-1111-1111-111111111111", role=Role.AGENT)
CHEN, LEE, LEGACY, CONTACT = (str(uuid.uuid4()) for _ in range(4))


def _ev(quote):
    return [{"source": "utterance", "quote": quote}]


def _frame(mention_text="Sarah", known_refs=(), kind="person"):
    model = {
        "goals": [{"label": "send_message", "negated": False, "evidence": _ev("Text")}],
        "mentions": [{"text": mention_text, "kind": kind, "known": list(known_refs), "evidence": _ev(mention_text)}],
        "times": [], "constraints": [], "scope": "mine",
        "required_data": [{"source": "people", "max_age_seconds": None}],
        "effect": "external_effect", "unfilled_slots": [], "ambiguities": [],
    }
    known = (sf.KnownRecord("client", CHEN, "Sarah Chen"),)
    return sf.frame_from_model(json.dumps(model), f"Text {mention_text}", known)


def _people(*hits, status=ss.CHECKED):
    return {"results": list(hits), "counts": {"people": len(hits)},
            "source_status": {"people": {"status": status, "reason": None}}}


def _hit(label, record, score=0.9, sublabel=None):
    return {"kind": "people", "id": record["id"], "label": label, "sublabel": sublabel, "href": "/p/x",
            "score": score, "record": record}


@pytest.fixture
def searches(monkeypatch):
    state = {"answer": _people(), "queries": []}

    async def search(ctx, q, kinds, limit):
        state["queries"].append((ctx, q, tuple(kinds), limit))
        answer = state["answer"]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(search_api, "search", search)
    return state


def lookup(frame, lang=None):
    out = asyncio.run(semantic_turn._lookup_people(CTX, frame, lang=lang))
    return out.frame, out.found


# ── the lookup ──────────────────────────────────────────────────────────────

def test_an_unplaced_person_is_looked_up_and_the_hits_become_candidates(searches):
    searches["answer"] = _people(
        _hit("Sarah Chen", {"type": "client", "id": CHEN}),
        _hit("Sarah Lee", {"type": "client", "id": LEE}),
        _hit("Sarah (contact)", {"type": "contact", "id": CONTACT}),
        _hit("Sarah Chen", {"type": "client", "id": CHEN}),       # a duplicate is one candidate
        _hit("Nobody", {"type": "deal", "id": str(uuid.uuid4())}),  # not a person record
        _hit("Broken", {"type": "client", "id": "not-a-uuid"}),
    )
    out = asyncio.run(semantic_turn._lookup_people(CTX, _frame()))
    frame, found = out.frame, out.found
    assert out.looked_up == {0}
    assert out.capped == {0}, "six hits for a limit of five: the search may have stopped early"
    assert searches["queries"] == [(CTX, "Sarah", ("people",), semantic_turn.LOOKUP_LIMIT)]
    assert [(c.record_type, str(c.id)) for c in frame.mentions[0].candidates] == [
        ("client", CHEN), ("client", LEE), ("contact", CONTACT)]
    assert [(k.record_type, k.id, k.label) for k in found] == [
        ("client", CHEN, "Sarah Chen"), ("client", LEE, "Sarah Lee"), ("contact", CONTACT, "Sarah (contact)")]
    assert all(c.access is None for c in frame.mentions[0].candidates), "authorize_frame decides access, not the search"


@pytest.mark.parametrize("text", ["her", "him", "them", "ella", "él", "Her,", "x", "???"])
def test_a_pronoun_or_a_non_name_is_never_searched(searches, text):
    frame, found = lookup(_frame(text))
    assert searches["queries"] == [] and found == [] and frame.mentions[0].candidates == []


def test_a_mention_the_parser_already_placed_is_not_searched(searches):
    frame, found = lookup(_frame(known_refs=[1]))
    assert searches["queries"] == [] and found == []
    assert [str(c.id) for c in frame.mentions[0].candidates] == [CHEN]
    assert lookup(None) == (None, [])
    assert asyncio.run(semantic_turn._lookup_people(CTX, None)).looked_up == frozenset()


def test_a_failed_slow_or_unrun_search_changes_nothing(searches, monkeypatch):
    searches["answer"] = RuntimeError("search down")
    frame, found = lookup(_frame())
    assert frame.mentions[0].candidates == [] and found == []

    searches["answer"] = _people(_hit("Sarah Chen", {"type": "client", "id": CHEN}), status=ss.UNAVAILABLE)
    frame, found = lookup(_frame())
    assert frame.mentions[0].candidates == [] and found == [], "a leg that did not check is not 'nobody'"

    async def slow(*_a, **_k):
        await asyncio.sleep(30)

    monkeypatch.setattr(search_api, "search", slow)
    monkeypatch.setattr(semantic_turn, "LOOKUP_BUDGET_S", 0.01)
    frame, found = lookup(_frame())
    assert frame.mentions[0].candidates == [] and found == []


def test_only_a_strong_name_match_becomes_a_candidate(searches):
    """Review L05-11: "Bob" must not resolve to "Bobby Lee", nor to someone
    whose email or company merely contains it."""
    searches["answer"] = _people(
        _hit("Bobby Lee", {"type": "client", "id": CHEN}, score=0.4),           # substring
        _hit("Robert Jones", {"type": "client", "id": LEE}, score=0.0),         # email/company only
        _hit("Bob Marsh", {"type": "client", "id": LEGACY}, score=0.85, sublabel="buyer · active"),
    )
    out = asyncio.run(semantic_turn._lookup_people(CTX, _frame("Bob")))
    assert [str(c.id) for c in out.frame.mentions[0].candidates] == [LEGACY]
    assert [(k.label, k.detail) for k in out.found] == [("Bob Marsh", "buyer · active")]
    assert out.capped == frozenset()
    searches["answer"] = _people(_hit("Bobby Lee", {"type": "client", "id": CHEN}, score=0.4))
    out = asyncio.run(semantic_turn._lookup_people(CTX, _frame("Bob")))
    assert out.frame.mentions[0].candidates == [] and out.looked_up == frozenset()


def test_placeholder_labels_are_written_in_the_persons_language(searches):
    searches["answer"] = _people(_hit("Contact", {"type": "contact", "id": CONTACT}),
                                 _hit("Unnamed", {"type": "client", "id": CHEN}))
    _frame_, found = lookup(_frame(), lang="es")
    assert [k.label for k in found] == ["Contacto", "Sin nombre"]


# ── the turn ────────────────────────────────────────────────────────────────

class _Gateway:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def complete(self, prompt, **kwargs):
        self.calls.append(prompt)
        return self.answer


def _bundle(text):
    return {"assistant": {"context_type": None, "context_id": None, "id": "asst"},
            "record": None, "messages": [{"id": "u1", "role": "user", "content": text}], "attachments": []}


def _turn(monkeypatch, searches, grant, *, answer=None, history=(), text="Text Sarah about the showing"):
    import db.connection as connection

    @asynccontextmanager
    async def tx(_ctx):
        yield object()

    async def access(_conn, cand):
        return grant.get(str(cand.id))

    async def persist(*_a, **_k):
        return None

    monkeypatch.setattr(connection, "tenant_tx", tx)
    monkeypatch.setattr(sf, "_candidate_access", access)
    monkeypatch.setattr(semantic_turn, "_persist", persist)
    monkeypatch.delenv("ORACLE_SEMANTIC_FRAME_ENABLED", raising=False)
    answer = answer or json.dumps({
        "goals": [{"label": "send_message", "negated": False, "evidence": _ev("Text Sarah")}],
        "mentions": [{"text": "Sarah", "kind": "person", "known": [], "evidence": _ev("Sarah")}],
        "times": [], "constraints": [], "scope": "mine", "required_data": [],
        "effect": "external_effect", "unfilled_slots": [], "ambiguities": [],
    })
    semantic_turn.GATEWAY = _Gateway(answer)
    bundle = _bundle(text)
    bundle["messages"] = list(history) + bundle["messages"]
    return asyncio.run(semantic_turn.prepare_turn(CTX, bundle, "asst"))


def test_two_lookups_the_caller_can_open_become_the_turns_question(monkeypatch, searches):
    searches["answer"] = _people(_hit("Sarah Chen", {"type": "client", "id": CHEN}),
                                 _hit("Sarah Lee", {"type": "client", "id": LEE}))
    turn = _turn(monkeypatch, searches, {CHEN: "edit", LEE: "view"})
    assert [(c.id, c.label) for c in turn.question.choices] == [(CHEN, "Sarah Chen"), (LEE, "Sarah Lee")]
    assert turn.pending().records == {CHEN, LEE}
    assert '"name": "Sarah Chen"' in turn.question_block()
    assert turn.summary()["needs_clarification"] is True
    stored = json.dumps(turn.summary())
    assert "Sarah" not in stored and CHEN in stored, "ids stored, never the names"


def test_one_lookup_the_caller_can_open_resolves_the_mention_without_asking(monkeypatch, searches):
    # Two hits, but the caller may open only one: that one is the answer and
    # the other is never named.
    searches["answer"] = _people(_hit("Sarah Chen", {"type": "client", "id": CHEN}),
                                 _hit("Sarah Lee", {"type": "client", "id": LEE}))
    turn = _turn(monkeypatch, searches, {CHEN: "edit"})
    assert turn.question is None and turn.pending() is None
    (mention,) = turn.frame.mentions
    assert [(str(c.id), c.access) for c in mention.candidates] == [(CHEN, "edit")]
    assert turn.frame.dropped_candidates == 1 and turn.summary()["needs_clarification"] is False


DONT_TEXT_SARAH = json.dumps({
    "goals": [{"label": "send_message", "negated": True, "evidence": _ev("Don't text Sarah")}],
    "mentions": [{"text": "Sarah", "kind": "person", "known": [], "evidence": _ev("Sarah")}],
    "times": [], "constraints": [{"kind": "do_not_contact", "text": "don't text", "time": None, "mention": 0,
                                  "evidence": _ev("Don't text Sarah")}],
    "scope": "mine", "required_data": [], "effect": "read", "unfilled_slots": [], "ambiguities": [],
})


def test_a_lookup_never_narrows_a_restriction(monkeypatch, searches):
    """Review L05-11: a restriction on a name the parser could not place used
    to hold everyone; scoping it to whatever the capped search returned let it
    miss the person actually meant. It stays unscoped."""
    searches["answer"] = _people(_hit("Sarah Chen", {"type": "client", "id": CHEN}))
    turn = _turn(monkeypatch, searches, {CHEN: "edit"}, answer=DONT_TEXT_SARAH,
                 text="Don't text Sarah about the showing")
    (c,) = [c for c in turn.active if c.kind == "do_not_contact"]
    assert c.records is None and c.unresolved is True


def test_a_turn_still_asking_lifts_nothing(monkeypatch, searches):
    """Review L05-11 (confirmed): "OK, text Sarah now" with two Sarahs found by
    the lookup must not lift a do-not-contact on either."""
    standing = [fc.ActiveConstraint(kind="do_not_contact", records=(CHEN,), origin="earlier",
                                    stated_at=datetime.now(timezone.utc))]
    earlier = {"id": "a0", "role": "assistant", "content": "Noted.",
               "metadata": {"semantic_frame": {"v": 1, "lang": "en", "mentions": [], "active": fc.dump(standing)}}}
    searches["answer"] = _people(_hit("Sarah Chen", {"type": "client", "id": CHEN}),
                                 _hit("Sarah Lee", {"type": "client", "id": LEE}))
    turn = _turn(monkeypatch, searches, {CHEN: "edit", LEE: "edit"}, history=[earlier])
    assert turn.question is not None
    assert any(c.kind == "do_not_contact" and c.records == (CHEN,) for c in turn.active)


def test_a_mission_objective_is_never_searched(monkeypatch, searches):
    """Missions have no question step, so a lookup there could only narrow
    their constraints (review L05-11)."""
    monkeypatch.delenv("ORACLE_SEMANTIC_FRAME_ENABLED", raising=False)
    semantic_turn.GATEWAY = _Gateway(DONT_TEXT_SARAH)
    out = asyncio.run(semantic_turn.mission_constraints(CTX, "Reactivate my sphere but don't text Sarah"))
    assert searches["queries"] == []
    (c,) = [c for c in fc.load(out["semantic_frame"]["active"]) if c.kind == "do_not_contact"]
    assert c.records is None and c.unresolved is True
