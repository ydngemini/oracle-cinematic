"""Product defects the killer-demo run (Mission 4 Part II, 2026-10-04) found.

Each was a break in the real path "agent asks Neoh about a property → Neoh
finds the buyer → proposes a text/call → agent approves → it happens → it is
on the client's timeline". None is demo-specific code: every fix here is a
path any brokerage uses.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

TENANT = "11111111-1111-4111-8111-111111111111"
CLIENT = "22222222-2222-4222-8222-222222222222"


class RecordingConn:
    def __init__(self, fetchrow_results=None, fetchval_results=None):
        self.executed: list[tuple[str, tuple]] = []
        self.fetchrow_results = list(fetchrow_results or [])
        self.fetchval_results = list(fetchval_results or [])

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))
        return "INSERT 0 1"

    async def fetchrow(self, sql, *args):
        return self.fetchrow_results.pop(0) if self.fetchrow_results else None

    async def fetchval(self, sql, *args):
        return self.fetchval_results.pop(0) if self.fetchval_results else None

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield self
        return _tx()


def _patch_tx(monkeypatch, module, conn):
    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(module, "tenant_tx", fake_tx)


# ── approved sends reach the client's timeline ──────────────────────────────

def _bookkeep(monkeypatch, command_type, provider):
    import commands_api as ca
    from tenancy import Role, TenantContext

    conn = RecordingConn()
    _patch_tx(monkeypatch, ca, conn)
    ctx = TenantContext(agent_id="command-worker", tenant_id=TENANT, role=Role.PLATFORM_ADMIN)
    asyncio.run(ca._record_send_bookkeeping(
        ctx,
        command_id="cmd-1",
        command_type=command_type,
        target={"client_id": CLIENT, "phone": "+13024078981"},
        draft={"body": "hello", "reason": "showing"},
        provider_result=SimpleNamespace(provider=provider, reference="ref-1"),
        actor="jordan@northstar.example.test",
    ))
    return [args for sql, args in conn.executed if "INSERT INTO client_activities" in sql]


def test_an_approved_call_appears_on_the_client_timeline(monkeypatch):
    import commands_api as ca

    rows = _bookkeep(monkeypatch, ca.CommandType.CALL, "plivo")
    assert len(rows) == 1
    tenant, client, summary, meta, actor, command_id = rows[0]
    assert client == CLIENT and command_id == "cmd-1"
    assert summary == "Call placed (approved in Neoh)"
    assert '"channel": "call"' in meta and '"provider_reference": "ref-1"' in meta
    # The person who approved it, not the job runner.
    assert actor == "jordan@northstar.example.test"


def test_timeline_wording_claims_no_more_than_the_provider_said():
    import commands_api as ca

    for text in ca._SEND_ACTIVITY_SUMMARY.values():
        assert "delivered" not in text.lower() and "answered" not in text.lower()
        assert "connected" not in text.lower()


def test_a_twilio_text_appears_but_a_telnyx_text_is_not_doubled(monkeypatch):
    import commands_api as ca

    assert len(_bookkeep(monkeypatch, ca.CommandType.SMS, "twilio_sms")) == 1
    # record_outbound_message already wrote "Text message sent" for Telnyx.
    assert _bookkeep(monkeypatch, ca.CommandType.SMS, "telnyx") == []


def test_a_retried_worker_does_not_write_the_activity_twice():
    import inspect

    import commands_api as ca

    helper = inspect.getsource(ca._record_send_bookkeeping)
    assert "meta->>'command_id' = $6" in helper


# ── a buyer can be texted/called: state + timezone come from the contact ────

def test_a_buyer_without_a_listed_property_gets_state_and_timezone_from_their_contact(monkeypatch):
    import ai_tools_gated as g
    from tenancy import Role, TenantContext

    ctx = TenantContext(agent_id="jordan@northstar.example.test", tenant_id=TENANT, role=Role.BROKER_OWNER)
    conn = RecordingConn(
        fetchrow_results=[
            {"id": CLIENT, "full_name": "Sarah Johnson", "email": "s@example.test",
             "phone": "+13024078981"},
            {"state_code": "DE", "timezone": "America/New_York"},
        ],
        fetchval_results=[None],  # no lead where she is the seller
    )
    contact, error = asyncio.run(g._outreach_target(conn, ctx, CLIENT))
    assert error is None
    assert contact["state_code"] == "DE"
    assert contact["timezone"] == "America/New_York"


def test_a_sellers_listed_property_still_decides_the_state(monkeypatch):
    import ai_tools_gated as g
    from tenancy import Role, TenantContext

    ctx = TenantContext(agent_id="a", tenant_id=TENANT, role=Role.BROKER_OWNER)
    conn = RecordingConn(
        fetchrow_results=[
            {"id": CLIENT, "full_name": "S", "email": "", "phone": "+13025550142"},
            {"state_code": "PA", "timezone": "UTC"},
        ],
        fetchval_results=["de"],
    )
    contact, _ = asyncio.run(g._outreach_target(conn, ctx, CLIENT))
    assert contact["state_code"] == "DE"
    assert contact["timezone"] == ""  # the UTC default is "unknown", not a place


def test_no_state_anywhere_is_still_refused():
    import ai_tools_gated as g
    from tenancy import Role, TenantContext

    ctx = TenantContext(agent_id="a", tenant_id=TENANT, role=Role.BROKER_OWNER)
    conn = RecordingConn(
        fetchrow_results=[{"id": CLIENT, "full_name": "S", "email": "", "phone": "+13025550142"}, None],
        fetchval_results=[None],
    )
    contact, _ = asyncio.run(g._outreach_target(conn, ctx, CLIENT))
    assert contact["state_code"] == ""


def test_voice_quiet_hours_use_the_contacts_timezone():
    import inspect

    import commands_api as ca

    source = inspect.getsource(ca._execute_command_job)
    call = source[source.index("elif command_type is CommandType.CALL:"):]
    gate = call[call.index("guard_outreach("):call.index("if not decision.allowed:")]
    assert 'tz_name=target.get("timezone")' in gate


# ── a multi-step hosted turn finishes with an answer ───────────────────────

def test_a_spent_tool_budget_ends_with_an_answer_not_an_error(monkeypatch):
    """read client → read listing → stage text → … then answer. When the rounds
    run out, the model is asked once, without tools, to answer from what the
    tools already returned — the staged text is not left orphaned next to
    "Neoh couldn't complete that response"."""
    from types import SimpleNamespace

    import ai_chat_agent

    async def fake_execute(*_args, **_kwargs):
        return {"ok": True, "command_id": "cmd-1"}

    seen = []

    async def fake_chat(payload, **_kwargs):
        seen.append(bool(payload.get("tools")))
        if payload.get("tools"):
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{len(seen)}", "type": "function",
                 "function": {"name": "get_client_detail", "arguments": "{}"}}]}}]}
        return {"choices": [{"message": {"role": "assistant", "content": "Text staged for approval."}}]}

    monkeypatch.setattr(ai_chat_agent, "execute_safe_tool", fake_execute)
    monkeypatch.setattr(ai_chat_agent, "_local_chat", fake_chat)
    monkeypatch.setattr(ai_chat_agent, "_local_tools", lambda _ctx: [{"type": "function"}])
    bundle = {"attachments": [], "record": None,
              "assistant": {"context_type": "client", "context_id": "c-1"},
              "messages": [{"role": "user", "content": "text her"}]}
    text, _ = asyncio.run(ai_chat_agent._local_fallback(
        SimpleNamespace(agent_id="a"), bundle, "system", "asst", applied=[],
        max_rounds=ai_chat_agent._HOSTED_TOOL_ROUNDS))
    assert text == "Text staged for approval."
    assert seen == [True] * ai_chat_agent._HOSTED_TOOL_ROUNDS + [False]


def test_a_reply_cannot_claim_a_staged_text_no_tool_staged():
    import ai_chat_agent as a

    claim = "I've drafted the text to Sarah. It's staged for your approval."
    assert a._guard_unbacked_claims(claim, []).endswith(a.UNBACKED_CLAIM_NOTE)
    assert a._guard_unbacked_claims(claim, ["cmd-1"]) == claim
    plain = "Call Sarah Johnson first; she fits on budget, area and bedrooms."
    assert a._guard_unbacked_claims(plain, []) == plain


def test_the_guard_sees_what_the_tools_actually_staged(monkeypatch):
    from types import SimpleNamespace

    import ai_chat_agent

    calls = {"n": 0}

    async def fake_execute(*_args, **_kwargs):
        return {"ok": True, "command_id": "cmd-9", "approval_id": "ap-9", "sent": False}

    async def fake_chat(payload, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "draft_sms", "arguments": '{"client_id":"x","body":"hi"}'}}]}}]}
        return {"choices": [{"message": {"role": "assistant",
                                         "content": "Staged for your approval."}}]}

    monkeypatch.setattr(ai_chat_agent, "execute_safe_tool", fake_execute)
    monkeypatch.setattr(ai_chat_agent, "_local_chat", fake_chat)
    monkeypatch.setattr(ai_chat_agent, "_local_tools", lambda _ctx: [{"type": "function"}])
    bundle = {"attachments": [], "record": None,
              "assistant": {"context_type": "client", "context_id": "c-1"},
              "messages": [{"role": "user", "content": "text her"}]}
    text, _ = asyncio.run(ai_chat_agent._local_fallback(
        SimpleNamespace(agent_id="a"), bundle, "system", "asst", applied=[], max_rounds=6))
    assert text == "Staged for your approval."


def test_hosted_tiers_get_more_rounds_than_the_local_model():
    import inspect

    import ai_chat_agent

    assert ai_chat_agent._HOSTED_TOOL_ROUNDS > ai_chat_agent._LOCAL_TOOL_ROUNDS
    src = inspect.getsource(ai_chat_agent)
    assert src.count("max_rounds=_HOSTED_TOOL_ROUNDS") == 2  # gateway + direct Fireworks


def test_the_persona_forbids_markdown_and_column_names():
    import neoh_persona

    prompt = neoh_persona.build_system_prompt(compact=True)
    assert "No markdown" in prompt and "column" in prompt


# ── the carrier's word on a call reaches the timeline ──────────────────────

@pytest.mark.parametrize("status, duration, summary", [
    ("completed", 41, "Call answered and ended (0:41)"),
    ("completed", 0, "Call not answered"),
    ("no-answer", None, "Call not answered"),
    ("busy", None, "Call not connected — line busy"),
])
def test_a_finished_call_is_written_in_the_carriers_words(monkeypatch, status, duration, summary):
    import commands_api as ca

    conn = RecordingConn()
    _patch_tx(monkeypatch, ca, conn)
    asyncio.run(ca.record_call_outcome("call-uuid-1", status, duration))
    session = [a for sql, a in conn.executed if "UPDATE live_call_sessions" in sql]
    assert session and session[0][1] == "completed" and session[0][2] == status
    rows = [a for sql, a in conn.executed if "INSERT INTO client_activities" in sql]
    assert rows and rows[0][1] == summary


def test_ringing_writes_nothing_and_answered_only_starts_the_session(monkeypatch):
    import commands_api as ca

    conn = RecordingConn()
    _patch_tx(monkeypatch, ca, conn)
    asyncio.run(ca.record_call_outcome("call-uuid-1", "ringing"))
    assert conn.executed == []
    asyncio.run(ca.record_call_outcome("call-uuid-1", "in-progress"))
    assert len(conn.executed) == 1 and "UPDATE live_call_sessions" in conn.executed[0][0]


def test_plivo_outbound_status_records_the_outcome():
    import inspect

    import telephony_api

    src = inspect.getsource(telephony_api.plivo_outbound_status)
    assert src.index("validate_plivo_signature(") < src.index("record_call_outcome(")


# ── buyer matching for the brokerage's OWN property ────────────────────────

LEAD = "33333333-3333-4333-8333-333333333333"
LISTING = "44444444-4444-4444-8444-444444444444"
SARAH = "55555555-5555-4555-8555-555555555555"
MARCUS = "66666666-6666-4666-8666-666666666666"


class MatchConn:
    """Answers the matcher's queries from a tiny in-memory book."""

    def __init__(self, *, clients=None, showings=None):
        self.clients = clients if clients is not None else [
            {"id": SARAH, "full_name": "Sarah Johnson", "stage": "active", "lead_score": 80,
             "last_contacted_at": None,
             "preferences": {"budget_max": 525000, "target_cities": ["Wilmington"],
                             "beds_min": 3, "must_haves": ["updated kitchen", "home office"]}},
            {"id": MARCUS, "full_name": "Marcus Lee", "stage": "active", "lead_score": 60,
             "last_contacted_at": None,
             "preferences": {"budget_max": 380000, "target_cities": ["Newark"], "beds_min": 2}},
        ]
        self.showings = showings if showings is not None else [
            {"client_id": SARAH, "shown_at": None, "outcome": "interested",
             "feedback": "Loved the kitchen; the office was too small.",
             "address": "48 Elm Court, Wilmington, DE 19803"},
        ]

    async def fetchrow(self, sql, *args):
        if "FROM leads ld" in sql:
            return {"lead_id": LEAD, "listing_id": LISTING,
                    "address": "123 Main Street, Wilmington, DE 19801", "price": 499000,
                    "status": "active", "beds": 3, "baths": 2, "sqft": 1850, "state": "DE",
                    "payload": {"features": ["Updated kitchen (2024)", "Den / home office"]}}
        return None

    async def fetch(self, sql, *args):
        if "FROM clients" in sql and "archived_at IS NULL" in sql:
            return self.clients
        if "FROM showings" in sql:
            return self.showings
        if "FROM clients" in sql:
            return [{"id": c["id"], "preferences": c["preferences"]} for c in self.clients]
        if "FROM listings" in sql:
            return [{"listing_id": LISTING, "lead_id": LEAD}]
        return []

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield self
        return _tx()


def _ctx():
    from tenancy import Role, TenantContext
    return TenantContext(agent_id="jordan@northstar.example.test", tenant_id=TENANT,
                         role=Role.BROKER_OWNER)


def test_address_parsing_reads_city_state_zip():
    import buyer_matching as bm

    assert bm.parse_us_address("123 Main Street, Wilmington, DE 19801") == {
        "street": "123 Main Street", "city": "Wilmington", "state": "DE", "zip_code": "19801"}
    assert bm.parse_us_address("no commas here") == {}


def test_the_open_property_finds_the_buyer_with_evidence():
    import buyer_matching as bm

    result = asyncio.run(bm.property_buyer_matches(MatchConn(), _ctx(), lead_id=LEAD))
    assert result["property"]["city"] == "Wilmington"
    assert [m["name"] for m in result["matches"]] == ["Sarah Johnson"]  # Marcus conflicts
    sarah = result["matches"][0]
    assert sarah["verdict"] == "strong" and sarah["matched_signals"] == 3
    assert any("525,000" in c for c in sarah["matched_criteria"])
    assert any("Wilmington" in c for c in sarah["matched_criteria"])
    assert any("3+ bedrooms" in c for c in sarah["matched_criteria"])
    assert sarah["stated_needs"] == ["updated kitchen", "home office"]
    assert sarah["recent_showings"][0]["address"].startswith("48 Elm Court")
    assert "no model score" in result["method"].lower()


def test_the_assistant_tool_answers_who_to_call_about_the_open_property():
    import ai_chat_store
    import ai_tools_read

    assert ai_chat_store.is_agent_tool_available("suggest_client_matches")
    out = asyncio.run(ai_tools_read.execute(MatchConn(), _ctx(), "suggest_client_matches",
                                            {"lead_id": LEAD}))
    assert out["ok"] is True
    assert out["matches"][0]["name"] == "Sarah Johnson"


def test_the_tool_refuses_garbage_ids():
    import ai_tools_read

    out = asyncio.run(ai_tools_read.execute(MatchConn(), _ctx(), "suggest_client_matches",
                                            {"lead_id": "123 Main"}))
    assert out["ok"] is False


def test_home_surfaces_the_listing_buyer_match_with_evidence():
    import opportunity_engine as oe

    cards = asyncio.run(oe._listing_buyer_opportunities(MatchConn(), _ctx()))
    assert len(cards) == 1
    card = cards[0]
    assert card.subject == "123 Main Street, Wilmington"
    assert card.headline == "May fit Sarah Johnson"
    assert card.subject_type == "lead" and card.subject_id == LEAD
    assert card.confidence >= oe.MIN_CONFIDENCE and card.confidence < 1
    labels = {e.label for e in card.evidence}
    assert {"Fits", "Wants", "Was shown"} <= labels
    assert "%" not in card.why  # no invented probability in the words


def test_home_says_nothing_when_no_buyer_strongly_fits():
    import opportunity_engine as oe

    conn = MatchConn(clients=[{"id": MARCUS, "full_name": "Marcus Lee", "stage": "active",
                               "lead_score": 60, "last_contacted_at": None,
                               "preferences": {"target_cities": ["Wilmington"]}}])
    assert asyncio.run(oe._listing_buyer_opportunities(conn, _ctx())) == []


def test_intent_cards_show_the_models_actual_recommendation():
    import inspect

    import opportunity_engine as oe

    src = inspect.getsource(oe._intent_model_opportunities)
    assert 'first.get("title")' in src


@pytest.mark.parametrize("value", ["America/New_York"])
def test_timezone_key_is_accepted_by_the_command_validator(value):
    import commands_api as ca

    ca._validate_command_payload(
        ca.CommandType.CALL,
        {"phone": "+13024078981", "client_id": CLIENT, "state_code": "DE", "timezone": value},
        {"reason": "showing"},
    )
