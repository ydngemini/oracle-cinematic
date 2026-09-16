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

    verify_idx = call_branch.index("get_verified_caller_id(ctx)")
    raise_idx = call_branch.index('"Call blocked: Connect and verify your business number')
    submission_started_idx = call_branch.index("submission_started = True")

    assert verify_idx < raise_idx < submission_started_idx, (
        "the unverified-number rejection must happen before submission_started "
        "is set, or a real Twilio call could be placed while the command is "
        "later marked reconciliation_required instead of a clean failure"
    )


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

    def fake_update(call_sid: str, status: str) -> None:
        updates.append((call_sid, status))

    async def fake_cleanup(call_sid: str) -> None:
        cleanups.append(call_sid)

    monkeypatch.setattr(ca, "_update_call_session", fake_update)

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


def test_active_states_map_to_in_progress_and_terminal_states_map_to_completed(patched_webhook):
    updates, cleanups = patched_webhook
    call_sid = "CA" + "b" * 32

    for status in ["initiated", "ringing"]:
        asyncio.run(
            ca.twilio_status_webhook(
                FakeStatusRequest({"CallSid": call_sid, "CallStatus": status})
            )
        )
    assert ("in-progress" in [u[1] for u in updates]) is False  # not yet answered

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
        assert sid in cleanups, terminal


def test_duplicate_status_callback_for_same_callsid_is_idempotent(patched_webhook):
    """Two 'completed' deliveries for the same CallSid must not be treated
    differently from one — the underlying write is an UPDATE keyed on
    provider_call_id (no INSERT), so replays converge rather than
    duplicating a call record or CRM activity."""
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
    # _update_call_session_async is a plain UPDATE ... WHERE provider_call_id=$1
    # (no INSERT), so replaying it never creates a second live_call_sessions
    # row or a second CRM write — verified structurally here since the fake
    # webhook layer only proves "called twice", not "row count", which is
    # covered by _update_call_session_async's own UPDATE-only shape below.
    source = inspect.getsource(ca._update_call_session_async)
    assert "UPDATE live_call_sessions" in source
    assert "INSERT" not in source


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

    handler_source = inspect.getsource(ca.twilio_status_webhook)
    for terminal in ["completed", "busy", "failed", "no-answer", "canceled"]:
        assert f'"{terminal}"' in handler_source


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
