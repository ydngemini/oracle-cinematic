"""V1 outbound call path ("use my existing business number") — Task 2.

Four properties matter enough to be worth their own file:

1. **Caller ID is resolved per-agent, never cross-tenant/cross-agent.**
   get_verified_caller_id(ctx) always resolves to the CALLING identity's own
   route — Agent A can never end up with Agent B's verified number.
2. **An agent whose business number is not verified is rejected closed.**
   commands_api.py's CommandType.CALL branch raises before
   `submission_started` is set, so no provider call is ever attempted and the
   command is recorded as a clean 'failed' (never 'reconciliation_required').
3. **All 8 Twilio call states are handled by the status callback webhook**,
   and a duplicate callback for the same CallSid does not duplicate work.
4. **No SMS code path was touched.**
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import pytest
from starlette.datastructures import FormData

import commands_api as ca
import inbound_voice
from tenancy import Role, TenantContext

TENANT_A = "11111111-1111-4111-8111-111111111111"
AGENT_A = "agent-a@example.test"
NUMBER_A = "+13024078981"

TENANT_B = "99999999-9999-4999-8999-999999999999"
AGENT_B = "agent-b@example.test"
NUMBER_B = "+18662805386"

CTX_A = TenantContext(agent_id=AGENT_A, tenant_id=TENANT_A, role=Role.BROKER_OWNER)
CTX_B = TenantContext(agent_id=AGENT_B, tenant_id=TENANT_B, role=Role.BROKER_OWNER)


def _route_row(*, tenant_id, agent_id, number, verified=True):
    return {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "voice_caller_id_e164": number,
        "voice_caller_id_verified": verified,
        "outbound_verification_status": "verified" if verified else "pending",
    }


class FakeRoutesConn:
    """Two agents, two tenants, two routes — the WHERE clause
    get_telephony_route depends on (tenant_id AND agent_id) is the only thing
    under test here."""

    def __init__(self, routes: list[dict[str, Any]]):
        self.routes = routes

    async def fetchrow(self, query: str, *args: Any):
        q = " ".join(query.split())
        assert q.startswith("SELECT") and "FROM telephony_routes" in q
        tenant_id, agent_id = args
        for row in self.routes:
            if row["tenant_id"] == tenant_id and row["agent_id"] == agent_id:
                return dict(row)
        return None


def _patch_tx(monkeypatch, conn):
    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(inbound_voice, "tenant_tx", fake_tx)


# ── get_verified_caller_id: cross-tenant/cross-agent isolation ─────────────


def test_agent_a_gets_its_own_verified_number(monkeypatch):
    conn = FakeRoutesConn(
        [
            _route_row(tenant_id=TENANT_A, agent_id=AGENT_A, number=NUMBER_A),
            _route_row(tenant_id=TENANT_B, agent_id=AGENT_B, number=NUMBER_B),
        ]
    )
    _patch_tx(monkeypatch, conn)

    assert asyncio.run(inbound_voice.get_verified_caller_id(CTX_A)) == NUMBER_A


def test_agent_a_can_never_end_up_with_agent_bs_number(monkeypatch):
    conn = FakeRoutesConn(
        [
            _route_row(tenant_id=TENANT_A, agent_id=AGENT_A, number=NUMBER_A),
            _route_row(tenant_id=TENANT_B, agent_id=AGENT_B, number=NUMBER_B),
        ]
    )
    _patch_tx(monkeypatch, conn)

    result_a = asyncio.run(inbound_voice.get_verified_caller_id(CTX_A))
    result_b = asyncio.run(inbound_voice.get_verified_caller_id(CTX_B))
    assert result_a == NUMBER_A
    assert result_b == NUMBER_B
    assert result_a != result_b


def test_unverified_agent_gets_no_caller_id(monkeypatch):
    conn = FakeRoutesConn(
        [_route_row(tenant_id=TENANT_A, agent_id=AGENT_A, number=NUMBER_A, verified=False)]
    )
    _patch_tx(monkeypatch, conn)

    assert asyncio.run(inbound_voice.get_verified_caller_id(CTX_A)) is None


# ── commands_api CALL branch: fail-closed when unverified ──────────────────


def test_call_branch_rejects_before_any_provider_submission_when_unverified():
    """Structural guarantee: the verified-caller-id check — and its raise —
    must occur BEFORE submission_started is set to True, so an unverified
    agent's rejected call is recorded as 'failed', never
    'reconciliation_required' (which would imply a provider might have
    already placed the call)."""
    source = inspect.getsource(ca._execute_command_job)
    call_branch_start = source.index("elif command_type is CommandType.CALL:")
    next_branch = source.index("else:\n            await reporter.progress(45, \"creating approved calendar event\")")
    call_branch = source[call_branch_start:next_branch]

    verify_idx = call_branch.index("get_verified_caller_id(owner_ctx)")
    raise_idx = call_branch.index('"Call blocked: Connect and verify your business number')
    submission_started_idx = call_branch.index("submission_started = True")

    assert verify_idx < raise_idx < submission_started_idx, (
        "the unverified-number rejection must happen before submission_started "
        "is set, or a real Twilio call could be placed while the command is "
        "later marked reconciliation_required instead of a clean failure"
    )


def test_per_agent_phone_setup_is_resolved_for_the_agent_who_staged_it():
    """The worker runs as "command-worker". Looking the business number or the
    verified caller ID up under THAT identity found nobody's route, so every
    approved call and Telnyx text failed for agents who had set them up
    (killer-demo run, 2026-10-04). They resolve for command["created_by"]."""
    source = inspect.getsource(ca._execute_command_job)
    assert 'agent_id=str(command["created_by"] or "command-worker")' in source
    for lookup in ("get_verified_caller_id", "get_telephony_route"):
        assert f"{lookup}(owner_ctx)" in source, lookup
        assert f"{lookup}(ctx)" not in source, lookup
    # texts: the send-readiness gate (25.1) reads the staging agent's route and number
    import re

    assert re.search(r"messaging_send_readiness\(\s*owner_ctx\b", source)
    assert not re.search(r"messaging_send_readiness\(\s*ctx\b", source)


def test_call_branch_never_falls_back_to_an_unverified_or_default_from_number():
    """The old gap: `if verified_caller_id:` silently fell through to
    whatever from_number was already configured when unverified. That
    conditional must be gone — the branch must raise instead."""
    source = inspect.getsource(ca._execute_command_job)
    call_branch_start = source.index("elif command_type is CommandType.CALL:")
    next_branch = source.index("else:\n            await reporter.progress(45, \"creating approved calendar event\")")
    call_branch = source[call_branch_start:next_branch]

    assert "if verified_caller_id:" not in call_branch
    assert "if not verified_caller_id:" in call_branch
    assert "raise RuntimeError" in call_branch


def test_call_branch_error_message_is_actionable():
    source = inspect.getsource(ca._execute_command_job)
    assert "Connect and verify your business number" in source
    assert "before placing calls" in source


# ── status callback webhook: all 8 Twilio call states ───────────────────────


class FakeStatusRequest:
    def __init__(self, form: dict[str, str]):
        self._form = FormData(form)
        self.headers = {"X-Twilio-Signature": "sig", "host": "internal.invalid"}
        self.query_params: dict[str, str] = {}
        self.url = SimpleNamespace(
            netloc="internal.invalid", scheme="http", query=urlencode({})
        )

    async def form(self):
        return self._form


@pytest.fixture()
def patched_webhook(monkeypatch):
    monkeypatch.setattr(ca, "_validate_twilio_webhook_signature", lambda *a, **k: None)
    updates: list[tuple[str, str]] = []
    cleanups: list[str] = []

    async def fake_accept(provider, call_sid, status, duration=None, *, tenant_id, aliases=()):
        # Stored then applied through record_call_outcome (24.3); the session
        # side of that is what these tests watch.
        assert provider == "twilio" and tenant_id == "brokerage-1"
        if status in {"in-progress", "answered"}:
            updates.append((call_sid, "in-progress"))
        else:
            updates.append((call_sid, "completed"))
            outcomes.append((call_sid, status))
        return 1

    async def fake_brokerage(_sid, _ref=""):
        return "brokerage-1"

    async def fake_cleanup(call_sid: str) -> None:
        cleanups.append(call_sid)

    outcomes.clear()
    monkeypatch.setattr(ca, "accept_call_outcome", fake_accept)
    monkeypatch.setattr(ca, "_twilio_call_brokerage", fake_brokerage)

    import twilio_call_handler

    monkeypatch.setattr(twilio_call_handler, "cleanup_twilio_call", fake_cleanup)
    return updates, cleanups


ALL_EIGHT_STATES = [
    "initiated",
    "ringing",
    "in-progress",
    "completed",
    "busy",
    "no-answer",
    "failed",
    "canceled",
]


def test_all_eight_call_states_are_accepted_without_error(patched_webhook):
    updates, cleanups = patched_webhook
    call_sid = "CA" + "a" * 32
    for status in ALL_EIGHT_STATES:
        response = asyncio.run(
            ca.twilio_status_webhook(
                FakeStatusRequest({"CallSid": call_sid, "CallStatus": status})
            )
        )
        assert response.status_code == 204


outcomes: list = []


def test_outcome_statuses_are_stored_and_terminal_ones_clean_up(patched_webhook):
    """The session mapping itself is record_call_outcome's, proven live in
    test_twilio_call_outcome_live; here: which statuses are stored, and cleanup."""
    updates, cleanups = patched_webhook
    call_sid = "CA" + "b" * 32

    for status in ["initiated", "ringing"]:
        asyncio.run(
            ca.twilio_status_webhook(
                FakeStatusRequest({"CallSid": call_sid, "CallStatus": status})
            )
        )
    assert updates == []   # not yet answered, and nothing to store

    asyncio.run(
        ca.twilio_status_webhook(
            FakeStatusRequest({"CallSid": call_sid, "CallStatus": "in-progress"})
        )
    )
    assert (call_sid, "in-progress") in updates

    for terminal in ["completed", "busy", "no-answer", "failed", "canceled"]:
        updates.clear()
        cleanups.clear()
        sid = f"CA{terminal.replace('-', '')}{'0' * 20}"[:34]
        asyncio.run(
            ca.twilio_status_webhook(
                FakeStatusRequest({"CallSid": sid, "CallStatus": terminal})
            )
        )
        assert (sid, "completed") in updates, terminal
        assert (sid, terminal) in outcomes, terminal       # busy ≠ completed any more
        assert sid in cleanups, terminal


def test_duplicate_status_callback_for_same_callsid_is_idempotent(patched_webhook):
    """Two 'completed' deliveries for the same CallSid converge: the outcome
    is stored under a unique (brokerage, provider, call, status) key and the
    session write is an UPDATE keyed on provider_call_id (no INSERT)."""
    updates, cleanups = patched_webhook
    call_sid = "CA" + "d" * 32

    for _ in range(2):
        response = asyncio.run(
            ca.twilio_status_webhook(
                FakeStatusRequest({"CallSid": call_sid, "CallStatus": "completed"})
            )
        )
        assert response.status_code == 204

    assert updates.count((call_sid, "completed")) == 2
    assert cleanups.count(call_sid) == 2
    import ast
    import pathlib

    text = pathlib.Path(ca.__file__).read_text()
    tree = ast.parse(text)
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "_update_call_session_async")
    source = ast.get_source_segment(text, node)
    assert "UPDATE live_call_sessions" in source
    assert "INSERT" not in source
    assert "COALESCE(ended_at" in source       # duplicates no longer move ended_at
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "accept_call_outcome")
    assert "ON CONFLICT (tenant_id, provider, provider_call_id, call_status)" in ast.get_source_segment(text, node)


def test_status_callback_event_subscription_covers_terminal_states():
    """Twilio's statusCallbackEvent parameter only accepts
    initiated/ringing/answered/completed — busy/no-answer/failed/canceled are
    NOT separate subscribable events, they arrive as CallStatus values on the
    single 'completed' event. Assert the subscription list is exactly the
    valid Twilio set (so a well-meaning 'fix' does not add invalid values
    Twilio would reject), and that the webhook handler above already maps
    every terminal CallStatus it can receive."""
    import command_providers

    source = inspect.getsource(command_providers.place_twilio_call)
    assert 'status_callback_event=["initiated", "ringing", "answered", "completed"]' in source

    for terminal in ["completed", "busy", "failed", "no-answer", "canceled"]:
        assert terminal in ca._TWILIO_TERMINAL_STATUSES
        assert terminal in ca._TWILIO_OUTCOME_STATUSES


# ── No SMS code path touched ────────────────────────────────────────────────


def test_sms_branch_and_mapping_are_unchanged():
    assert ca._SEND_INTERACTION_TYPE == {"EMAIL": "email", "SMS": "sms", "CALL": "call_transcript"}
    source = inspect.getsource(ca._execute_command_job)
    sms_branch_start = source.index("elif command_type is CommandType.SMS:")
    call_branch_start = source.index("elif command_type is CommandType.CALL:")
    sms_branch = source[sms_branch_start:call_branch_start]
    # The SMS branch must still guard, load twilio/acs credentials, and send —
    # untouched shape, no caller-id verification logic bled into it.
    assert "guard_outreach" in sms_branch
    assert "send_twilio_sms" in sms_branch
    assert "get_verified_caller_id" not in sms_branch
