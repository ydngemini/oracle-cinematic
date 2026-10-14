"""Every write path that can receive a payment card number stores, sends and
broadcasts the redacted text instead (PCI DSS SAQ A, ledger L17-07).

Route/function level with fake connections: each test feeds a real card
number (Visa test PAN 4111 1111 1111 1111) through the path and checks every
value the path handed to the database, the model or the live frame. The same
against real PostgreSQL (call notes, inbound text, client note, audit row):
tests/test_pan_redaction_live.py.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

import pan_redaction
from pan_redaction import PAN_PLACEHOLDER
from tenancy import Role, TenantContext

PAN = "4111111111111111"
SAID = "my card is 4111 1111 1111 1111 and the cvv is 123"
TENANT = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="agent@x.test", tenant_id=TENANT, role=Role.AGENT)
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _digits(value: Any) -> str:
    """Every digit in a value, in order, with separators dropped — a stored
    "4111 1111 1111 1111" or "4111-1111-..." still shows up as the PAN."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        value = json.dumps(value, default=str)
    return "".join(ch for ch in value if ch.isdigit())


def _assert_no_pan(*values: Any) -> None:
    for value in values:
        assert PAN not in _digits(value), f"card number stored/sent: {value!r}"


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch):
    """report() schedules an audit-ledger write; record it instead."""
    events = []

    async def fake_audit(surface, cards, codes, tenant_id, target_id):
        events.append((surface, cards, codes))

    monkeypatch.setattr(pan_redaction, "_audit", fake_audit)
    return events


def _tx(conn):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    return tx


async def _drain():
    await asyncio.gather(*list(pan_redaction._pending))


# ── client notes (agent-written; every note seals through seal_body) ───────

def test_an_agent_note_is_sealed_without_the_card(monkeypatch, _no_audit_writes):
    import client_enterprise

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-master")
    plaintexts = []

    class _Conn:
        async def fetchrow(self, query, *args):
            if "pgp_sym_encrypt" in query:
                plaintexts.append(args[0])
                return {"ct": b"sealed:" + args[0].encode()}
            assert "INSERT INTO client_notes" in query
            _assert_no_pan(*args)
            return {"id": uuid.uuid4(), "body": "", "body_ciphertext": args[2], "body_tenant_id": args[0],
                    "author_id": args[3], "pinned": args[4], "created_at": NOW, "updated_at": NOW,
                    "review_state": "approved", "reviewed_by": None, "reviewed_at": None}

        async def fetchval(self, query, ciphertexts, _key):
            return [c.decode().removeprefix("sealed:") for c in ciphertexts]

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(client_enterprise, "tenant_tx", _tx(_Conn()))
    monkeypatch.setattr(client_enterprise, "require_client_edit", nothing)
    monkeypatch.setattr(client_enterprise, "_log_activity", nothing)
    monkeypatch.setattr(client_enterprise, "_queue_client_ai", nothing)

    async def run():
        out = await client_enterprise.create_note(
            str(uuid.uuid4()), client_enterprise.NoteCreate(body=f"Seller said: {SAID}"), ctx=CTX)
        await _drain()
        return out

    out = asyncio.run(run())
    _assert_no_pan(out["body"], *plaintexts)
    assert PAN_PLACEHOLDER in out["body"], "the agent sees what was kept"
    assert _no_audit_writes == [("client_note", 1, 1)]


def test_seal_body_is_the_one_choke_point(monkeypatch):
    import client_note_crypto

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-master")
    seen = []

    class _Conn:
        async def fetchrow(self, _query, value, _key):
            seen.append(value)
            return {"ct": b"x"}

    asyncio.run(client_note_crypto.seal_body(_Conn(), TENANT, SAID, surface="ai_call_note"))
    assert seen == [f"my card is {PAN_PLACEHOLDER} and the cvv is {pan_redaction.CVV_PLACEHOLDER}"]


# ── live call transcripts (negotiation_events + the live frames) ───────────

def test_call_telemetry_stores_no_card(monkeypatch):
    import commands_api
    import record_access
    import ws_hub

    inserts, frames = [], []

    class _Conn:
        async def fetchrow(self, query, *args):
            if "FROM live_call_sessions" in query:
                return {"consent_recorded": True, "client_id": None, "contact_id": None}
            assert "INSERT INTO negotiation_events" in query
            inserts.append(args)
            return {"id": 7, "created_at": NOW}

    async def allowed(*_a, **_k):
        return None

    async def push(_tenant, frame, **_k):
        frames.append(frame)

    monkeypatch.setattr(commands_api, "require_feature", lambda _f: None)
    monkeypatch.setattr(commands_api, "tenant_tx", _tx(_Conn()))
    monkeypatch.setattr(record_access, "require_linked_edit", allowed)
    monkeypatch.setattr(ws_hub, "broadcast_record", push)
    body = commands_api.CallTelemetry(transcript_excerpt=f"They countered at 250000. {SAID}",
                                      counter_offer=250000, arv=400000, rehab=50000)
    asyncio.run(commands_api.call_telemetry(str(uuid.uuid4()), body, ctx=CTX))
    assert inserts and PAN_PLACEHOLDER in inserts[0][2]
    _assert_no_pan(*inserts[0], *frames)


def test_voice_telemetry_stores_and_streams_no_card_and_never_reads_it_as_an_offer(monkeypatch):
    import record_access
    import voice_intel

    inserts, frames = [], []

    class _Conn:
        async def fetchrow(self, query, *args):
            if "FROM live_call_sessions AS session" in query:
                return {"id": args[0], "client_id": None, "contact_id": None, "lead_id": None,
                        "consent_recorded": True, "transcript_status": "active",
                        "underwriting": None, "payload": None}
            assert "INSERT INTO negotiation_events" in query
            inserts.append(args)
            return {"id": 9, "created_at": NOW}

        async def execute(self, *_a):
            return "UPDATE 1"

    async def allowed(*_a, **_k):
        return None

    async def push(_ctx, _session, frame, **_k):
        frames.append(frame)

    monkeypatch.setattr(voice_intel, "tenant_tx", _tx(_Conn()))
    monkeypatch.setattr(record_access, "require_linked_edit", allowed)
    monkeypatch.setattr(voice_intel, "_broadcast_voice", push)
    body = voice_intel.VoiceTelemetryRequest(session_id=uuid.uuid4(), transcript_chunk=SAID,
                                             speaker="CLIENT", is_final=True)
    frame = asyncio.run(voice_intel.ingest_voice_telemetry(body, ctx=CTX))
    assert frame["transcript"]["text"].startswith(f"my card is {PAN_PLACEHOLDER}")
    assert frame["counter_offer"] is None, "a card number must never parse as a counter offer"
    _assert_no_pan(*inserts[0], *frames)


# ── texts ──────────────────────────────────────────────────────────────────

class _SmsConn:
    def __init__(self):
        self.inserts = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if "FROM agent_contacts" in q:
            return None
        if q.startswith("INSERT INTO sms_messages"):
            self.inserts.append(args)
            return {"id": "m-1", "activity_id": None}
        raise AssertionError(q)

    async def execute(self, *_a):
        return "UPDATE 1"


def test_an_inbound_text_is_stored_without_the_card(monkeypatch):
    import messaging_data

    conn = _SmsConn()
    monkeypatch.setattr(messaging_data, "tenant_tx", _tx(conn))
    monkeypatch.setattr("contact_truth.lookup_hash", lambda *a, **k: "a" * 64)
    monkeypatch.setattr("contact_truth.normalize_phone", lambda v: v)
    asyncio.run(messaging_data.record_inbound_message(
        tenant_id=TENANT, agent_id="agent@x.test", provider="telnyx", provider_message_id="m1",
        from_e164="+13025550100", to_e164="+13025550199", text=SAID, media=[], opted_out=False))
    body = conn.inserts[0][8]
    assert body.startswith(f"my card is {PAN_PLACEHOLDER}")
    _assert_no_pan(*conn.inserts[0])
    assert "+13025550100" in conn.inserts[0], "phone numbers are untouched"


def test_an_outbound_text_log_row_holds_no_card(monkeypatch):
    import messaging_data

    conn = _SmsConn()
    monkeypatch.setattr(messaging_data, "tenant_tx", _tx(conn))
    asyncio.run(messaging_data.record_outbound_message(
        tenant_id=TENANT, agent_id="agent@x.test", contact_id=None, client_id=None, provider="telnyx",
        provider_message_id="m2", from_e164="+13025550199", to_e164="+13025550100", body=SAID))
    _assert_no_pan(*conn.inserts[0])


def test_an_opt_out_kept_as_proof_holds_no_card(monkeypatch):
    import outreach_compliance

    executed = []

    class _Conn:
        async def execute(self, query, *args):
            executed.append(args)

    monkeypatch.setattr(outreach_compliance, "tenant_tx", _tx(_Conn()))
    asyncio.run(outreach_compliance.ConsentLedger.suppress(
        CTX, contact="+13025550100", channel="*", reason="stop_keyword", source_text=f"STOP {PAN}"))
    assert executed[0][4] == f"STOP {PAN_PLACEHOLDER}"


# ── CRM messages and email ─────────────────────────────────────────────────

def test_a_logged_email_and_its_outbox_row_hold_no_card(monkeypatch):
    import commercial_email

    async def _has_address(_tenant_id):
        return None

    monkeypatch.setattr(commercial_email, "require_postal_address", _has_address)
    import crm
    import email_outbox
    from tests.test_crm_workflows import CLIENT_ID, _MessageConn, _fake_tenant_tx

    conn = _MessageConn()

    async def allow(*_a, **_k):
        return None

    async def enqueue(*_a, **_k):
        return None

    monkeypatch.setattr(crm, "tenant_tx", _fake_tenant_tx(conn))
    monkeypatch.setattr(crm, "enforce_outreach", allow)
    monkeypatch.setattr(email_outbox, "enqueue_delivery", enqueue)
    asyncio.run(crm.send_message(
        CLIENT_ID, crm.MessageCreate(channel="email", subject=f"Card {PAN}", body=SAID), CTX))
    for query, args in conn.fetchrow_calls:
        if "INSERT INTO interaction_logs" in query or "INSERT INTO email_outbox" in query:
            _assert_no_pan(*args)
    outbox = next(a for q, a in conn.fetchrow_calls if "INSERT INTO email_outbox" in q)
    assert PAN_PLACEHOLDER in outbox[5] and outbox[4] == f"Card {PAN_PLACEHOLDER}"


@pytest.mark.parametrize("channel", ["note", "sms"])
def test_a_logged_note_or_text_holds_no_card(monkeypatch, channel):
    import crm
    from tests.test_crm_workflows import CLIENT_ID, _MessageConn, _fake_tenant_tx

    conn = _MessageConn()
    monkeypatch.setattr(crm, "tenant_tx", _fake_tenant_tx(conn))
    asyncio.run(crm.send_message(CLIENT_ID, crm.MessageCreate(channel=channel, body=SAID), CTX))
    _assert_no_pan(*conn.interaction_args)


# ── outreach drafts: removed before the approval hash ──────────────────────

def test_an_outreach_draft_is_cleaned_before_it_is_approved(monkeypatch):
    import commercial_email

    async def _has_address(_tenant_id):
        return None

    monkeypatch.setattr(commercial_email, "require_postal_address", _has_address)
    import commands_api

    captured = {}

    class Stop(Exception):
        pass

    class _Conn:
        async def fetchrow(self, query, *args):
            assert "FROM command_executions" in query
            return None

    async def editable(*_a, **_k):
        return None

    async def approval(_ctx, **kwargs):
        captured.update(kwargs)
        raise Stop

    monkeypatch.setattr(commands_api, "require_feature", lambda _f: None)
    monkeypatch.setattr(commands_api, "tenant_tx", _tx(_Conn()))
    monkeypatch.setattr(commands_api, "_require_target_editable", editable)
    monkeypatch.setattr(commands_api, "create_approval", approval)
    with pytest.raises(Stop):
        asyncio.run(commands_api.stage_command(
            CTX, command_type=commands_api.CommandType.EMAIL,
            target={"email": "client@example.test"},
            draft={"subject": "Deposit", "body": f"Please pay with {PAN}, exp 12/27"},
            context={}, idempotency_key="pan-draft-0001", created_by=CTX.agent_id))
    _assert_no_pan(captured["draft_payload"])
    assert PAN_PLACEHOLDER in json.dumps(captured["draft_payload"])


# ── AI chat ────────────────────────────────────────────────────────────────

def test_a_chat_message_is_stored_and_shown_to_the_model_without_the_card(monkeypatch):
    import ai_chat_store

    encrypted = []

    class _Conn:
        async def execute(self, *_a):
            return "SELECT 1"

        async def fetchrow(self, query, *args):
            if "INSERT INTO ai_chat_messages" in query:
                return {"id": uuid.uuid4(), "created_at": NOW}
            return None

        async def fetchval(self, *_a):
            return 0

    async def encrypt(_conn, value, _key):
        encrypted.append(value)
        return b"ct"

    @asynccontextmanager
    async def no_limiter():
        yield None

    monkeypatch.setattr(ai_chat_store, "tenant_tx", _tx(_Conn()))
    monkeypatch.setattr(ai_chat_store, "_encrypt_text", encrypt)
    monkeypatch.setattr(ai_chat_store, "tenant_key", lambda _c: "k")
    monkeypatch.setattr(ai_chat_store, "distributed_rate_limiter", no_limiter)
    asyncio.run(ai_chat_store.create_chat_request(
        CTX, request_id=str(uuid.uuid4()), content=f"note for my client: {SAID}",
        context=None, attachment_ids=[]))
    assert encrypted and PAN_PLACEHOLDER in encrypted[0]
    _assert_no_pan(*encrypted)


def test_an_assistant_reply_is_stored_without_a_card(monkeypatch):
    import ai_chat_store

    encrypted = []

    class _Conn:
        async def execute(self, *_a):
            return "UPDATE 1"

    async def encrypt(_conn, value, _key):
        encrypted.append(value)
        return b"ct"

    monkeypatch.setattr(ai_chat_store, "tenant_tx", _tx(_Conn()))
    monkeypatch.setattr(ai_chat_store, "_encrypt_text", encrypt)
    monkeypatch.setattr(ai_chat_store, "tenant_key", lambda _c: "k")
    asyncio.run(ai_chat_store.update_assistant(CTX, str(uuid.uuid4()), content=f"You said {PAN}.",
                                               status_value="completed"))
    assert encrypted == [f"You said {PAN_PLACEHOLDER}."]


def test_chat_memory_turns_hold_no_card(monkeypatch):
    import db.connection
    from memory_core.session_manager import SessionManager

    executed = []

    class _Conn:
        async def execute(self, query, *args):
            executed.append(args)

    monkeypatch.setattr(db.connection, "tenant_tx", _tx(_Conn()))
    asyncio.run(SessionManager(CTX).record_interaction(CTX.agent_id, "user", SAID))
    _assert_no_pan(*executed[0])


# ── voice calls ────────────────────────────────────────────────────────────

def test_caller_speech_reaches_the_model_without_the_card(monkeypatch):
    import ai_chat_agent
    import llm_gateway

    sent = []

    async def complete(prompt, **kwargs):
        sent.append((prompt, kwargs.get("system", "")))
        return "Thanks."

    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda: ["fireworks"])
    monkeypatch.setattr(llm_gateway, "complete", complete)
    assert asyncio.run(ai_chat_agent._generate_voice_reply("+13025550100", SAID)) == "Thanks."
    _assert_no_pan(*sent[0])
    assert pan_redaction.PAYMENT_CARD_RULE in sent[0][1]


def test_an_inbound_call_transcript_keeps_no_card(_no_audit_writes):
    import inbound_voice

    turns = [{"role": "assistant", "text": "How can I help?"},
             {"role": "caller", "text": "four one one one one one one one one one one one one one one one"},
             {"role": "caller", "text": "My number is 302 555 0100"}]

    async def run():
        out = inbound_voice._clean_transcript(turns, tenant_id=TENANT)
        await _drain()
        return out

    cleaned = asyncio.run(run())
    assert cleaned[1]["text"] == PAN_PLACEHOLDER
    assert cleaned[2]["text"] == "My number is 302 555 0100"
    assert _no_audit_writes == [("inbound_call_transcript", 1, 0)]


def test_a_contact_intake_keeps_no_card(monkeypatch):
    import contacts_api
    from contacts_api import IntakeSubmission
    from tests.test_contacts_api import CONTACT_ID, CTX as INTAKE_CTX, _IntakeConn, _fake_tenant_tx

    sealed = []

    async def seal(_conn, _tenant, payload):
        sealed.append(payload)
        return b"sealed"

    monkeypatch.setattr(contacts_api, "tenant_tx", _fake_tenant_tx(_IntakeConn()))
    monkeypatch.setattr(contacts_api, "seal_json", seal)
    asyncio.run(contacts_api.create_intake(
        CONTACT_ID,
        IntakeSubmission(persona="seller", answers=["12 Elm St, Dover, DE", "Within 60 days", SAID],
                         transcript=f"Q: anything else?\nA: {SAID}"),
        INTAKE_CTX))
    _assert_no_pan(*sealed)
    assert "12 Elm St, Dover, DE" in json.dumps(sealed)


def test_portal_and_perception_payloads_hold_no_card(monkeypatch):
    import client_portal
    import intelligence_os_api

    assert client_portal._clean_portal_payload({"section": SAID})["section"].startswith(
        f"my card is {PAN_PLACEHOLDER}")
    executed = []

    class _Conn:
        async def execute(self, query, *args):
            executed.append(args)

    monkeypatch.setattr(intelligence_os_api, "tenant_tx", _tx(_Conn()))
    batch = intelligence_os_api.PerceptionBatch(events=[intelligence_os_api.PerceptionEvent(
        client_id=str(uuid.uuid4()), interaction_type="search", payload={"q": SAID, "n": 3})])
    asyncio.run(intelligence_os_api.capture_events(batch, ctx=CTX))
    _assert_no_pan(*executed[0])
    assert json.loads(executed[0][4])["n"] == 3


# ── AI call notes taken live (ElevenLabs save_note) ────────────────────────

from tests.test_elevenlabs_realtime import _results, _run, _tool, notes_backend  # noqa: E402,F401


def test_save_note_never_keeps_a_card_and_tells_the_agent_why(monkeypatch, notes_backend, _no_audit_writes):
    import elevenlabs_realtime as el

    _, agent, _, bridge = _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="4111 1111 1111 1111"),
        _tool(el.TOOL_SAVE_NOTE, "t3", note=f"Wants to pay the deposit by card {PAN} cvv 123"),
        _tool(el.TOOL_SAVE_NOTE, "t4", note="Prefers afternoon showings"),
    ], state={"direction": "outbound", "reference": "ref-pan"})
    results = _results(agent)
    assert results["t2"]["is_error"] and "nothing was saved" in results["t2"]["result"]
    assert not results["t3"]["is_error"] and "secure payment link" in results["t3"]["result"]
    assert results["t4"]["result"] == "Saved for their agent."
    delivered = notes_backend["delivered"][0][1]
    assert delivered == [f"Wants to pay the deposit by card {PAN_PLACEHOLDER} cvv "
                         f"{pan_redaction.CVV_PLACEHOLDER}", "Prefers afternoon showings"]
    _assert_no_pan(*bridge.notes, *delivered)
    assert ("ai_call_save_note", 1, 0) in _no_audit_writes
