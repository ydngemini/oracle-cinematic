"""Fixed inputs for every successful fast-path answer and command fallback
draft (L05-05). Imported by test_fast_path_language.py.

`english_outputs()` is what the English path produced at da163ba, before the
answers were routed through the en/es catalogue. Its result is frozen in
`fast_path_en_golden.json`; the test holds today's English to it byte for
byte. Only English inputs are listed here, and only APIs that existed before
the change are called, so the golden file could be produced from the old code.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import belief_store
import intent_states
import neoh_intents as intents
import neoh_render as render
import source_status as ss
from tenancy import Role, TenantContext

GOLDEN = Path(__file__).with_name("fast_path_en_golden.json")

CTX = TenantContext(
    agent_id="a@t.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)

CLIENT_ID = "22222222-2222-2222-2222-222222222222"
DEAL_ID = "33333333-3333-3333-3333-333333333333"
PROP_ID = "44444444-4444-4444-4444-444444444444"


# ── intent pictures: the five verdicts intent_states can reach ──────────────

def _reading(score, state="observed"):
    return intent_states.IntentReading(score=score, evidence_state=state, basis="test")


def intent_picture(verdict: str) -> dict[str, Any]:
    declared, observed = {
        "cannot_compare": (_reading(0.4), _reading(None, "unobserved")),
        "behaviour_only": (_reading(None), _reading(0.55)),
        "behaviour_ahead": (_reading(0.3), _reading(0.82)),
        "behaviour_behind": (_reading(0.8), _reading(0.35)),
        "consistent": (_reading(0.6), _reading(0.66)),
    }[verdict]
    return {
        "declared": declared.__dict__,
        "observed": observed.__dict__,
        "latent": intent_states._reconcile(declared, observed),
        "disputes": [],
    }


def _belief(value, age, kind, learned):
    return {"value": value, "age_days": age, "source": {"kind": kind},
            "learned_at": learned, "confidence": 0.7, "status": "stated"}


def dispute() -> dict[str, Any]:
    older = _belief("6 months", 40, "call_note", "2026-09-01T00:00:00+00:00")
    newer = _belief("30 days", 3, "web_form", "2026-10-10T00:00:00+00:00")
    ranked = [newer, older]
    return {"predicate": "move_timeline", "leading": newer, "challenged": [older],
            "question": belief_store._dispute_question("move_timeline", ranked)}


RECORD = {"id": CLIENT_ID, "full_name": "Sarah Johnson", "client_type": "buyer",
          "stage": "active", "email": "s@x.test", "phone": "+15550100", "company": None}
TIMELINE = [
    {"label": "Called about the offer", "at": "2026-10-10T15:00:00+00:00", "direction": "outbound", "done": True},
    {"label": "sms", "at": "2026-10-09T15:00:00+00:00", "direction": "inbound", "done": True},
]


# ── the call queue ─────────────────────────────────────────────────────────

def _opp(subject, value, action="call"):
    return {"subject": subject, "subject_id": CLIENT_ID, "subject_type": "client",
            "headline": "Contract expires in 3 days", "why": "Because.",
            "recommended_action": "Confirm the closing timeline with all parties today.",
            "action_type": action, "confidence": 0.7, "kind": "contract_expiry",
            "economics": {"expected_value": value}}


def briefing(n: int, **extra) -> dict[str, Any]:
    names = ["Sarah Johnson", "Marcus Lee", "Dana Ortiz", "Ana Ruiz"]
    return {
        "attention": {
            "opportunities": [_opp(names[i], 1000 - i) for i in range(n)],
            "portfolio": {"total": 12500.0, "caveat": "Uncalibrated priors.", "calibrated": False},
        },
        **extra,
    }


# ── a shortlist and a deal ─────────────────────────────────────────────────

CANDIDATES = [
    {"id": PROP_ID, "address": "12 Oak St", "price": 425000, "beds": 3, "baths": 2,
     "match_reason": "inside the stated budget"},
    {"property_id": PROP_ID, "address": None, "list_price": "389000"},
    {"address": "9 Elm Ave"},
    {"address": "4th one, never shown"},
]


def milestones(kind: str) -> list[dict[str, Any]]:
    done = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return {
        "none": [],
        "all_done": [{"milestone_type": "inspection", "title": "Inspection", "status": "completed",
                      "due_at": "2026-10-01", "completed_at": done}],
        "titled": [
            {"milestone_type": "appraisal", "title": "appraisal report due", "status": "pending",
             "due_at": "2026-10-20", "completed_at": None},
            {"milestone_type": "inspection", "title": "Inspection", "status": "completed",
             "due_at": "2026-10-01", "completed_at": done},
        ],
        "typed": [{"milestone_type": "loan_commitment", "title": None, "status": "pending",
                   "due_at": "2026-10-25", "completed_at": None}],
        "bare": [{"milestone_type": None, "title": None, "status": "pending",
                  "due_at": None, "completed_at": None}],
    }[kind]


def transaction(address: str | None = "123 Main St") -> dict[str, Any]:
    return {"id": DEAL_ID, "property_address": address, "status": "under_contract"}


# ── the ask path, end to end with the data access mocked ───────────────────

def hit(kind, label, id_=CLIENT_ID, sublabel=None):
    return {"kind": kind, "id": id_, "label": label, "sublabel": sublabel}


@contextlib.contextmanager
def data(*, people=(), properties=(), deals=(), record=RECORD, intent=None, timeline=TIMELINE,
         candidates=CANDIDATES, lookup=None, deal=None, deal_milestones=(), brief=None):
    lookup = {"status": "checked"} if lookup is None else lookup
    legs = {"people": list(people), "properties": list(properties), "deals": list(deals)}

    async def search(_ctx, _query, kind):
        return legs[kind], ss.checked(matched=len(legs[kind]))

    async def person_detail(_ctx, _id, _lang="en"):
        return (dict(record) if record else None), intent, list(timeline)

    async def candidates_for(_ctx, _id):
        return list(candidates), dict(lookup)

    async def deal_detail(_ctx, _id):
        return (dict(deal) if deal else None), list(deal_milestones)

    async def the_briefing(_ctx, **_kw):
        return brief if brief is not None else briefing(3)

    with mock.patch.object(intents, "_search", search), \
            mock.patch.object(intents, "_person_detail", person_detail), \
            mock.patch.object(intents, "_candidates_for", candidates_for), \
            mock.patch.object(intents, "_deal_detail", deal_detail), \
            mock.patch("command_center.briefing", the_briefing):
        yield


def ask(text: str, **setup) -> dict[str, Any]:
    with data(**setup):
        return asyncio.run(intents.ask(CTX, text))


#: Each fast-path intent's English question and its Spanish twin, with the
#: data the resolver will find. Every one reaches a SUCCESS answer.
ASK_TWINS: list[tuple[str, str, str, dict[str, Any]]] = [
    ("who_to_call.one", "who should I call", "¿A quién debo llamar?", {"brief": briefing(1)}),
    ("who_to_call.many", "my call list", "mi lista de llamadas", {"brief": briefing(3)}),
    ("show_person.card", "show Sarah Johnson", "muéstrame a Sarah Johnson",
     {"people": [hit("people", "Sarah Johnson")], "intent": intent_picture("behaviour_ahead")}),
    ("show_person.no_intent", "who is Sarah Johnson", "¿quién es Sarah Johnson?",
     {"people": [hit("people", "Sarah Johnson")], "record": {**RECORD, "full_name": None}, "timeline": []}),
    ("show_person.dispute", "tell me about Sarah Johnson", "háblame de Sarah Johnson",
     {"people": [hit("people", "Sarah Johnson")],
      "intent": {**intent_picture("consistent"), "disputes": [dispute(), {"predicate": "budget"}]}}),
    ("show_person.choices", "show Sarah", "muéstrame a Sarah",
     {"people": [hit("people", "Sarah Johnson"), hit("people", None, "55555555-5555-5555-5555-555555555555")]}),
    ("show_property.one", "show 412 Delaware Ave", "muéstrame 412 Delaware Ave",
     {"properties": [hit("properties", "412 Delaware Ave", PROP_ID, "Wilmington, DE")]}),
    ("properties_for.shortlist", "find properties for Sarah", "busca propiedades para Sarah",
     {"people": [hit("people", "Sarah Johnson")]}),
    ("deal_blocker.open", "what's holding 123 Main up", "¿qué está frenando 123 Main?",
     {"deals": [hit("deals", "123 Main St", DEAL_ID)], "deal": transaction(),
      "deal_milestones": milestones("titled")}),
    ("deal_blocker.typed", "what is blocking 123 Main", "¿qué está bloqueando 123 Main?",
     {"deals": [hit("deals", "123 Main St", DEAL_ID)], "deal": transaction(None),
      "deal_milestones": milestones("typed")}),
    ("deal_blocker.done", "what's stuck on 123 Main", "¿por qué está atascado 123 Main?",
     {"deals": [hit("deals", "123 Main St", DEAL_ID)], "deal": transaction(),
      "deal_milestones": milestones("all_done")}),
    ("deal_blocker.empty", "what's holding 123 Main up", "¿qué está frenando 123 Main?",
     {"deals": [hit("deals", "123 Main St", DEAL_ID)], "deal": transaction(),
      "deal_milestones": milestones("none")}),
]


def render_cases() -> dict[str, Any]:
    """The renderers called directly, for branches the ask path reaches rarely."""
    out: dict[str, Any] = {}
    for verdict in ("cannot_compare", "behaviour_only", "behaviour_ahead", "behaviour_behind", "consistent"):
        out[f"person.{verdict}"] = render.person(RECORD, intent_picture(verdict), TIMELINE)
    out["person.bare"] = render.person({"id": CLIENT_ID}, None, [])
    out["deal.bare"] = render.deal_blocker(transaction(), milestones("bare"))
    out["shortlist.one"] = render.properties_for("Sarah Johnson", CANDIDATES[:1], {"status": "checked"})
    out["queue.empty_portfolio"] = render.who_to_call({"attention": {"portfolio": {"total": 0}}})
    return out


def draft_cases() -> dict[str, Any]:
    import commands_api as c

    client = {"id": CLIENT_ID, "full_name": "Sarah Johnson"}
    prop = {"id": PROP_ID, "address": "12 Oak St", "payload": {"state_code": "de"}}
    profile = {"name": "Alex Agent", "signature": "Alex Agent, Neoh Realty", "phone_number": "+15550101"}
    bare = {}
    out: dict[str, Any] = {}
    for key, intent, text in (
        ("email", c.ParsedIntent.EMAIL, "Email Sarah about the offer of $425,000"),
        ("sms", c.ParsedIntent.SMS, "Text Sarah about the offer"),
        ("call", c.ParsedIntent.CALL, "Call Sarah about the offer"),
        ("calendar", c.ParsedIntent.CALENDAR, "Schedule a meeting with Sarah"),
        ("contract", c.ParsedIntent.CONTRACT, "Draft an assignment contract for Sarah"),
        ("mao", c.ParsedIntent.MAO_CALC, "Calculate the MAO for this property"),
    ):
        out[f"draft.{key}.full"] = c._fallback_draft(intent, text, profile=profile, client=client,
                                                     property_context=prop)
        out[f"draft.{key}.bare"] = c._fallback_draft(intent, text, profile=bare, client=None,
                                                     property_context=None)
    return out


def stable(value: Any) -> Any:
    """Drop the wall-clock `checked_at` a source status carries: it is the
    one field that differs between two runs of the same code."""
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k != "checked_at"}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


def english_outputs() -> dict[str, Any]:
    out = {f"ask.{key}": ask(english, **setup) for key, english, _es, setup in ASK_TWINS}
    out.update(render_cases())
    out.update(draft_cases())
    # One JSON round trip, so datetimes compare the way the golden file stores them.
    return stable(json.loads(json.dumps(out, default=str, sort_keys=True)))


if __name__ == "__main__":  # regenerate ONLY from the pre-change code
    GOLDEN.write_text(json.dumps(english_outputs(), indent=1, sort_keys=True, ensure_ascii=False) + "\n")
