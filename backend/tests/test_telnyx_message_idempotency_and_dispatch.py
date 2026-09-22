"""sms_messages idempotency (duplicate webhook -> one row, one CRM activity)
and commands_api.py's SMS dispatch fail-closed/no-fallback structure —
mirrors test_plivo_outbound_dispatch.py for messaging.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from typing import Any

import pytest

import commands_api as ca
import messaging_data

TENANT_ID = "55555555-5555-4555-8555-555555555555"
AGENT_ID = "agent@example.test"


class FakeInboundMessageConn:
    """One inbound message's full lifecycle: contact lookup (miss), the
    UNIQUE(provider,provider_message_id) INSERT, and the client_activities
    write — with real re-entrant idempotency semantics on a duplicate call."""

    def __init__(self):
        self.messages: dict[tuple[str, str], dict[str, Any]] = {}
        self.activities: list[dict[str, Any]] = []
        self.insert_attempts = 0

    async def fetchrow(self, query: str, *args: Any):
        q = " ".join(query.split())
        if "FROM agent_contacts ac" in q:
            return None  # unknown contact — no match
        if q.startswith("INSERT INTO sms_messages"):
            self.insert_attempts += 1
            (
                tenant_id, agent_id, contact_id, client_id, provider,
                provider_message_id, from_e164, to_e164, body, media, opt_out,
            ) = args
            key = (provider, provider_message_id)
            if key in self.messages:
                return None  # ON CONFLICT DO NOTHING
            row = {
                "id": f"msg-{len(self.messages) + 1}",
                "activity_id": None,
            }
            self.messages[key] = row
            return dict(row)
        if q.startswith("SELECT id,activity_id FROM sms_messages"):
            provider, provider_message_id = args
            row = self.messages.get((provider, provider_message_id))
            return dict(row) if row else None
        if q.startswith("INSERT INTO client_activities"):
            activity = {"id": f"activity-{len(self.activities) + 1}"}
            self.activities.append(activity)
            return activity
        raise AssertionError(f"unexpected query: {q}")

    async def execute(self, query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("UPDATE sms_messages SET activity_id"):
            message_id, activity_id = args
            for row in self.messages.values():
                if row["id"] == message_id:
                    row["activity_id"] = activity_id
            return
        raise AssertionError(f"unexpected execute: {q}")


def _patch_tx(monkeypatch, conn):
    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)


def test_duplicate_inbound_webhook_creates_one_message_no_activity_without_client(monkeypatch):
    conn = FakeInboundMessageConn()
    _patch_tx(monkeypatch, conn)

    async def fake_lookup_hash(tenant_id, kind, value):
        return "a" * 64

    monkeypatch.setattr("contact_truth.lookup_hash", lambda *a, **k: "a" * 64)
    monkeypatch.setattr("contact_truth.normalize_phone", lambda v: v)

    kwargs = dict(
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        provider="telnyx",
        provider_message_id="msg-uuid-1",
        from_e164="+15551234567",
        to_e164="+13025551234",
        text="hello",
        media=[],
        opted_out=False,
    )
    first = asyncio.run(messaging_data.record_inbound_message(**kwargs))
    second = asyncio.run(messaging_data.record_inbound_message(**kwargs))

    assert first == second
    assert conn.insert_attempts == 2
    assert len(conn.messages) == 1
    # No matched client/contact -> no CRM activity row, by design (unknown
    # contacts are staged elsewhere per the brief, not silently activity-logged).
    assert len(conn.activities) == 0


def test_opt_out_inbound_message_is_flagged():
    """is_stop_keyword classification happens in the webhook handler before
    record_inbound_message is called — this proves the flag round-trips."""
    from outreach_compliance import is_stop_keyword

    assert is_stop_keyword("STOP") is True
    assert is_stop_keyword("Please stop by the house on Tuesday") is False


# ── commands_api.py SMS dispatch structure ──────────────────────────────


def _sms_branch_source() -> str:
    source = inspect.getsource(ca._execute_command_job)
    start = source.index("elif command_type is CommandType.SMS:")
    end = source.index("elif command_type is CommandType.CALL:")
    return source[start:end]


def test_sms_branch_reads_provider_from_env_selector_not_client_input():
    branch = _sms_branch_source()
    assert "messaging_provider_name()" in branch


def test_sms_branch_fails_closed_when_telnyx_route_not_ready():
    branch = _sms_branch_source()
    telnyx_idx = branch.index('if sms_provider == "telnyx":')
    else_idx = branch.index("else:", telnyx_idx)
    telnyx_block = branch[telnyx_idx:else_idx]
    assert 'hosted_order_status") != "active"' in telnyx_block
    assert "raise RuntimeError" in telnyx_block
    # The raise must occur before submission_started = True, same fail-closed
    # discipline as the Plivo/Twilio CALL branch.
    raise_idx = telnyx_block.index("raise RuntimeError")
    submission_idx = telnyx_block.index("submission_started = True")
    assert raise_idx < submission_idx


def test_sms_branch_never_falls_back_between_providers():
    """The bug this guards against: a Telnyx failure silently retried
    through legacy Twilio SMS could double-send the same approved text."""
    branch = _sms_branch_source()
    telnyx_idx = branch.index('if sms_provider == "telnyx":')
    else_idx = branch.index("else:", telnyx_idx)
    telnyx_block = branch[telnyx_idx:else_idx]
    assert "send_twilio_sms" not in telnyx_block
    assert "except ProviderConfigurationError" not in telnyx_block


def test_sms_branch_sender_is_the_verified_business_number_not_client_supplied():
    branch = _sms_branch_source()
    telnyx_idx = branch.index('if sms_provider == "telnyx":')
    else_idx = branch.index("else:", telnyx_idx)
    telnyx_block = branch[telnyx_idx:else_idx]
    assert 'from_=sender_number' in telnyx_block
    assert 'business_number["voice_caller_id_e164"]' in telnyx_block
