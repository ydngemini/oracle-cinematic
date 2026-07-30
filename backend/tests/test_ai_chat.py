import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

import pytest
from pydantic import ValidationError

import ws_hub
from ai_chat_agent import BASE_SYSTEM_PROMPT, _compact_history, _foundry_inputs
from ai_chat_api import _clamd_scan_sync, _safe_filename, _sniff_type
from ai_chat_models import ChatSendFrame
import ai_chat_store
import rate_limiter
from platform_policy import ActionRisk, requires_approval
from tenancy import Role, TenantContext


def test_chat_frame_requires_content_or_record_bound_attachment():
    with pytest.raises(ValidationError):
        ChatSendFrame.model_validate({
            "type": "AI_CHAT_SEND", "version": 1,
            "request_id": str(uuid.uuid4()), "content": "",
        })
    with pytest.raises(ValidationError):
        ChatSendFrame.model_validate({
            "type": "AI_CHAT_SEND", "version": 1,
            "request_id": str(uuid.uuid4()), "content": "",
            "attachment_ids": [str(uuid.uuid4())],
        })


def test_chat_frame_accepts_versioned_record_context():
    frame = ChatSendFrame.model_validate({
        "type": "AI_CHAT_SEND", "version": 1,
        "request_id": str(uuid.uuid4()), "content": "  review this  ",
        "context": {"type": "client", "id": str(uuid.uuid4())},
    })
    assert frame.content == "review this"


def test_attachment_signature_and_filename_guards():
    assert _sniff_type(b"%PDF-1.7\n") == "application/pdf"
    assert _sniff_type(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert _sniff_type(b"not a supported file") is None
    assert _safe_filename("../../deal\n.pdf") == "deal.pdf"
    with pytest.raises(ValueError, match="malware"):
        _clamd_scan_sync(b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE")


def test_internal_edits_are_distinct_but_do_not_bypass_high_risk_policy():
    assert not requires_approval(ActionRisk.INTERNAL_EDIT)
    assert requires_approval(ActionRisk.OUTREACH)
    assert requires_approval(ActionRisk.FINANCIAL)
    assert requires_approval(ActionRisk.LEGAL_DOCUMENT)
    assert "Never delete or archive" in BASE_SYSTEM_PROMPT


def test_history_compaction_keeps_newest_turns_within_budget():
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": "x" * 100}
        for index in range(10)
    ]
    compact = _compact_history(history, max_chars=250)
    assert len(compact) == 2
    assert sum(len(turn["content"]) for turn in compact) <= 250


def test_foundry_input_is_stateless_and_marks_server_context_as_untrusted():
    bundle = {
        "record": {"id": "record-1", "address": "10 Main St"},
        "messages": [{"role": "user", "content": "Review this deal"}],
        "attachments": [{
            "filename": "terms.pdf", "media_type": "application/pdf",
            "data": b"%PDF", "extracted_text": "Purchase price is 100000",
        }],
    }
    items = _foundry_inputs(bundle, "Target MAO rule: 70%")
    assert "untrusted data" in items[0]["content"]
    assert "10 Main St" in items[0]["content"]
    assert items[-1]["content"][0] == {"type": "input_text", "text": "Review this deal"}
    assert "Purchase price is 100000" in items[-1]["content"][1]["text"]


class _Socket:
    def __init__(self):
        self.frames = []

    async def send_text(self, value):
        self.frames.append(json.loads(value))


def test_private_hub_delivery_never_reaches_another_agent():
    tenant = str(uuid.uuid4())
    alice = _Socket()
    bob = _Socket()
    ws_hub.register(tenant, alice, "alice")
    ws_hub.register(tenant, bob, "bob")
    try:
        delivered = asyncio.run(ws_hub.broadcast_user(tenant, "alice", {"type": "AI_CHAT_DELTA"}))
        assert delivered == 1
        assert alice.frames == [{"type": "AI_CHAT_DELTA"}]
        assert bob.frames == []
    finally:
        ws_hub.unregister(tenant, alice, "alice")
        ws_hub.unregister(tenant, bob, "bob")


def test_private_attachment_migration_and_queries_require_agent_owner(monkeypatch):
    migration = (
        Path(__file__).resolve().parents[1]
        / "db"
        / "migrations"
        / "0036_private_ai_chat.sql"
    ).read_text(encoding="utf-8")
    normalized_migration = " ".join(migration.split())
    assert "owner_agent_id text NOT NULL DEFAULT app_current_agent()" in normalized_migration
    assert "tenant_id = app_current_tenant()" in migration
    assert "owner_agent_id = app_current_agent()" in migration
    forward_migration = (
        Path(__file__).resolve().parents[1]
        / "db"
        / "migrations"
        / "0046_azure_security_forward_fixes.sql"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS owner_agent_id text" in forward_migration
    assert "SET owner_agent_id = created_by" in forward_migration
    assert "owner_agent_id = app_current_agent()" in forward_migration

    class _Conn:
        def __init__(self):
            self.query = ""
            self.args = ()

        async def fetch(self, query, *args):
            self.query = query
            self.args = args
            return []

    conn = _Conn()

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    async def record(*_args):
        return {"label": "Selected client"}

    monkeypatch.setattr(ai_chat_store, "tenant_tx", tx)
    monkeypatch.setattr(ai_chat_store, "resolve_record", record)
    ctx = TenantContext(
        agent_id="agent-alice",
        tenant_id=str(uuid.uuid4()),
        role=Role.AGENT,
    )
    asyncio.run(ai_chat_store.list_attachments(ctx, "client", str(uuid.uuid4())))
    assert "owner_agent_id=$2" in conn.query
    assert conn.args[1] == "agent-alice"


def test_personal_ai_note_reports_success_only_after_insert(monkeypatch):
    client_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    note_id = uuid.uuid4()

    class _Conn:
        def __init__(self):
            self.executed = []

        async def fetchval(self, query, *args):
            assert "tenant_id=$2::uuid" in query
            return 1

        async def fetchrow(self, query, *args):
            assert "INSERT INTO client_notes" in query
            return {"id": note_id, "created_at": datetime.now(timezone.utc)}

        async def execute(self, query, *args):
            self.executed.append((query, args))
            return "INSERT 0 1"

    conn = _Conn()

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-encryption-key")
    monkeypatch.setattr(ai_chat_store, "tenant_tx", tx)
    ctx = TenantContext(
        agent_id="agent-alice",
        tenant_id=str(uuid.uuid4()),
        role=Role.AGENT,
    )
    receipt = asyncio.run(ai_chat_store.execute_safe_tool(
        ctx,
        ctx.agent_id,
        message_id,
        "add_client_note",
        {"client_id": client_id, "note": "Seller requested a Friday callback."},
        "client",
        client_id,
    ))
    assert receipt["ok"] is True
    assert receipt["note_id"] == str(note_id)
    assert any("INSERT INTO client_activities" in query for query, _ in conn.executed)


def test_contract_tool_fails_closed_until_controlled_workflow_exists(monkeypatch):
    @asynccontextmanager
    async def tx(_ctx):
        yield object()

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-encryption-key")
    monkeypatch.setattr(ai_chat_store, "tenant_tx", tx)
    ctx = TenantContext(
        agent_id="agent-alice",
        tenant_id=str(uuid.uuid4()),
        role=Role.AGENT,
    )
    receipt = asyncio.run(ai_chat_store.execute_safe_tool(
        ctx,
        ctx.agent_id,
        str(uuid.uuid4()),
        "generate_contract",
        {"template_id": str(uuid.uuid4()), "deal_id": str(uuid.uuid4())},
        None,
        None,
    ))
    assert receipt["ok"] is False
    assert "attorney review" in receipt["error"]


def test_redis_lock_is_initialized_before_first_context_use():
    assert rate_limiter._redis_lock is not None


def test_rejected_redis_concurrency_reservation_is_released():
    class _Redis:
        def __init__(self):
            self.decrements = 0

        async def incr(self, _key):
            return 3

        async def decr(self, _key):
            self.decrements += 1
            return 2

        async def delete(self, _key):
            raise AssertionError("a nonzero counter must not be deleted")

    redis = _Redis()
    limiter = rate_limiter.DistributedRateLimiter(redis)
    ctx = TenantContext(
        agent_id="agent-alice",
        tenant_id=str(uuid.uuid4()),
        role=Role.AGENT,
    )
    allowed, current = asyncio.run(
        limiter.check_concurrency_limit(ctx, max_active=2)
    )
    assert allowed is False
    assert current == 3
    assert redis.decrements == 1


def test_acs_migration_preserves_supported_twilio_credentials():
    migration = (
        Path(__file__).resolve().parents[1]
        / "db"
        / "migrations"
        / "0043_acs_provider.sql"
    ).read_text(encoding="utf-8")
    assert "DELETE FROM provider_credentials" not in migration
    assert "'twilio'" in migration
    assert "'acs'" in migration
    forward_migration = (
        Path(__file__).resolve().parents[1]
        / "db"
        / "migrations"
        / "0046_azure_security_forward_fixes.sql"
    ).read_text(encoding="utf-8")
    assert "DELETE FROM provider_credentials" not in forward_migration
    assert "'twilio'" in forward_migration
    assert "'acs'" in forward_migration
