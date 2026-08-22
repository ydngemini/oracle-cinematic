import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
import json
from pathlib import Path
import uuid

import pytest
from pydantic import ValidationError

import ai_chat_agent

import ws_hub
from ai_chat_agent import (
    BASE_SYSTEM_PROMPT,
    _compact_history,
    _foundry_inputs,
    _foundry_tools,
    _tool_config,
)
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
    activity_id = uuid.uuid4()
    action_id = uuid.uuid4()

    class _Conn:
        def __init__(self):
            self.executed = []

        async def fetchval(self, query, *args):
            if "pgp_sym_encrypt" in query:
                return b"ciphertext"
            assert "tenant_id=$2::uuid" in query
            return 1

        async def fetchrow(self, query, *args):
            self.executed.append((query, args))
            if "INSERT INTO client_notes" in query:
                return {"id": note_id, "created_at": datetime.now(timezone.utc)}
            if "INSERT INTO client_activities" in query:
                return {"id": activity_id}
            if "INSERT INTO ai_chat_actions" in query:
                return {"id": action_id,
                        "undo_expires_at": datetime.now(timezone.utc)}
            raise AssertionError(f"unexpected statement: {query[:80]}")

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
    # The note is a mutation, so it must land in the undo ledger. Without this
    # the UI still rendered an "applied, undoable" receipt — with an Undo button
    # that POSTed to .../actions/undefined/undo.
    assert receipt["action_id"] == str(action_id)
    assert receipt["undoable"] is True
    ledger = next(args for query, args in conn.executed
                  if "INSERT INTO ai_chat_actions" in query)
    assert "row_delete" in ledger
    assert str(note_id) in json.dumps(ledger[-1])


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


def test_agent_tool_config_only_advertises_durable_execution_paths():
    config = _tool_config("client")
    assert config is not None
    names = [tool["toolSpec"]["name"] for tool in config["tools"]]
    assert len(names) == len(set(names))
    assert "search_clients" in names
    assert "list_deals" in names
    assert "get_team_pipeline" in names
    assert "list_providers" in names
    # search_listings was previously asserted absent because nothing could
    # execute it. It has a handler now, so the durable form of that assertion is
    # the rule, not the name: a tool is advertised exactly when a handler exists.
    assert "search_listings" in names
    assert "generate_contract" not in names
    # assign_client was a flat refusal ("AI assignment is disabled"). P11 turns
    # it into a ledgered field update that first checks the target is an active
    # member of the workspace — assignee_id is free text, and the refusal was
    # standing in for that check.
    assert "assign_client" in names
    assert all(ai_chat_store.is_agent_tool_available(name) for name in names)


def test_no_advertised_tool_falls_through_to_not_implemented():
    """The allowlist may never run ahead of the execution path.

    Every name offered to the model has to reach a handler; anything that does
    not would answer "not implemented in this execution path" — the model would
    have been told a capability exists and then be refused by the code that was
    supposed to provide it.
    """
    from ai_tools_read import TOOLS_HANDLED

    # Derived from the dispatcher's own source rather than restated here: a
    # hand-kept copy is the thing this test exists to catch.
    store_source = (
        Path(ai_chat_store.__file__).read_text().split("async def _execute_safe_tool", 1)[1]
    )
    from ai_tools_gated import TOOLS_HANDLED as GATED

    handled_elsewhere = set(GATED)
    handled_elsewhere |= set(re.findall(r'tool_name == "([a-z_]+)"', store_source))
    for group in re.findall(r'tool_name in [\{\(]([^}\)]*)[\}\)]', store_source):
        handled_elsewhere |= set(re.findall(r'"([a-z_]+)"', group))

    advertised = {
        tool["toolSpec"]["name"]
        for context in (None, "client", "listing", "lead")
        for tool in (_tool_config(context) or {"tools": []})["tools"]
    }
    unreachable = advertised - TOOLS_HANDLED - handled_elsewhere
    assert not unreachable, f"advertised with no handler: {sorted(unreachable)}"


def test_foundry_tool_config_uses_the_same_capability_gate():
    client_names = {tool["name"] for tool in _foundry_tools("client")}
    lead_names = {tool["name"] for tool in _foundry_tools("lead")}
    assert {"search_clients", "list_client_tasks", "track_deadlines", "list_providers"} <= client_names
    assert "set_client_stage" in client_names
    assert "move_deal_stage" not in client_names
    assert "move_deal_stage" in lead_names
    assert "search_listings" in client_names
    assert "generate_contract" not in client_names
    # call_contact is offered now, but only with a client selected and only as
    # a request: it stages a command_executions row for a human to approve and
    # reaches no provider. It used to accept a model-supplied phone number.
    assert "call_contact" in client_names
    assert "call_contact" not in {tool["name"] for tool in _foundry_tools(None)}
    assert all(ai_chat_store.is_agent_tool_available(name) for name in client_names | lead_names)


def test_client_search_is_parameterized_tenant_scoped_and_escapes_wildcards(monkeypatch):
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

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-encryption-key")
    monkeypatch.setattr(ai_chat_store, "tenant_tx", tx)
    ctx = TenantContext(
        agent_id="agent-alice",
        tenant_id=str(uuid.uuid4()),
        role=Role.AGENT,
    )
    receipt = asyncio.run(ai_chat_store.execute_safe_tool(
        ctx, ctx.agent_id, str(uuid.uuid4()), "search_clients",
        {"query": "100%_match"}, None, None,
    ))
    assert receipt == {"ok": True, "action_type": "search_clients", "count": 0, "clients": []}
    assert "c.tenant_id=$1::uuid" in conn.query
    assert "ILIKE $2" in conn.query
    assert conn.args[0] == ctx.tenant_id
    assert conn.args[1] == "%100\\%\\_match%"


def test_read_tool_rejects_invalid_record_id_before_querying(monkeypatch):
    class _Conn:
        async def fetchrow(self, *_args):
            raise AssertionError("invalid IDs must not reach the database")

    @asynccontextmanager
    async def tx(_ctx):
        yield _Conn()

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-encryption-key")
    monkeypatch.setattr(ai_chat_store, "tenant_tx", tx)
    ctx = TenantContext(
        agent_id="agent-alice",
        tenant_id=str(uuid.uuid4()),
        role=Role.AGENT,
    )
    receipt = asyncio.run(ai_chat_store.execute_safe_tool(
        ctx, ctx.agent_id, str(uuid.uuid4()), "get_deal_detail",
        {"deal_id": "not-a-uuid"}, None, None,
    ))
    assert receipt == {"ok": False, "error": "deal_id must be a UUID."}


def test_client_detail_reads_all_children_on_the_same_tenant_connection(monkeypatch):
    client_id = str(uuid.uuid4())

    class _Conn:
        def __init__(self):
            self.calls = []

        async def fetchrow(self, query, *args):
            self.calls.append(("fetchrow", query, args))
            return {
                "id": uuid.UUID(client_id), "full_name": "Avery Seller", "email": "avery@example.test",
                "phone": None, "client_type": "seller", "stage": "lead", "lead_score": 75,
                "assignee_id": None, "company": None, "preferences": {}, "source": "manual",
                "last_contacted_at": None, "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }

        async def fetch(self, query, *args):
            self.calls.append(("fetch", query, args))
            return [{"tag": "priority"}] if "client_tags" in query else []

        async def fetchval(self, query, *args):
            self.calls.append(("fetchval", query, args))
            return 2

    conn = _Conn()

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-encryption-key")
    monkeypatch.setattr(ai_chat_store, "tenant_tx", tx)
    ctx = TenantContext(agent_id="agent-alice", tenant_id=str(uuid.uuid4()), role=Role.AGENT)
    receipt = asyncio.run(ai_chat_store.execute_safe_tool(
        ctx, ctx.agent_id, str(uuid.uuid4()), "get_client_detail",
        {"client_id": client_id}, None, None,
    ))
    assert receipt["ok"] is True
    assert receipt["client"]["tags"] == ["priority"]
    assert receipt["client"]["open_task_count"] == 2
    assert all(args[-1] == ctx.tenant_id for _method, _query, args in conn.calls)


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


# ── Local model tool calling ─────────────────────────────────────────────────
def test_local_tools_use_chat_completions_shape_and_the_same_gate():
    """The local model gets the hosted policy, in the dialect llama.cpp speaks."""
    import ai_chat_agent

    tools = ai_chat_agent._local_tools("client")
    assert tools, "expected at least one gated tool"
    for tool in tools:
        assert tool["type"] == "function"
        # Chat Completions nests under "function"; the Responses API inlines it.
        assert set(tool["function"]) >= {"name", "description", "parameters"}
        assert tool["function"]["parameters"]["type"] == "object"

    local_names = {tool["function"]["name"] for tool in tools}
    assert local_names == {tool["name"] for tool in ai_chat_agent._foundry_tools("client")}

    # A listing context must not be offered client mutations, same as Foundry.
    listing_names = {
        tool["function"]["name"] for tool in ai_chat_agent._local_tools("listing")
    }
    assert "update_client" not in listing_names
    assert "update_listing" in listing_names or not listing_names


def test_local_fallback_runs_a_tool_loop_through_the_safe_executor(monkeypatch):
    import ai_chat_agent

    executed = []

    async def fake_execute(ctx, agent_id, assistant_id, name, args, ctype, cid):
        executed.append((name, args, ctype, cid))
        # An applied mutation carries its ai_chat_actions row; a receipt without
        # one is not broadcast as an applied record change.
        return {"ok": True, "action_id": "act-1", "undoable": True,
                "summary": "note added"}

    responses = [
        {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "add_client_note", "arguments": '{"note": "prefers weekends"}',
            }}],
        }}]},
        {"choices": [{"message": {"role": "assistant", "content": "Saved the note."}}]},
    ]
    sent = []

    async def fake_chat(payload, **_kwargs):
        sent.append(payload)
        return responses[len(sent) - 1]

    monkeypatch.setattr(ai_chat_agent, "execute_safe_tool", fake_execute)
    monkeypatch.setattr(ai_chat_agent, "_local_chat", fake_chat)

    ctx = SimpleNamespace(agent_id="agent@example.test")
    bundle = {
        "attachments": [], "record": None,
        "assistant": {"context_type": "client", "context_id": "c-1"},
        "messages": [{"role": "user", "content": "note that they prefer weekends"}],
    }
    text, actions = asyncio.run(
        ai_chat_agent._local_fallback(ctx, bundle, "system", "asst-1")
    )

    assert text == "Saved the note."
    assert len(actions) == 1
    # The tool ran through execute_safe_tool, carrying the anchor context.
    assert executed == [("add_client_note", {"note": "prefers weekends"}, "client", "c-1")]
    # The tool result was fed back so the model could answer from the receipt.
    assert sent[1]["messages"][-1]["role"] == "tool"
    assert sent[1]["messages"][-1]["tool_call_id"] == "call_1"


def test_local_fallback_reports_malformed_tool_arguments_instead_of_guessing(monkeypatch):
    """A small model emitting broken JSON must not call the tool with no args."""
    import ai_chat_agent

    async def must_not_execute(*_args, **_kwargs):
        raise AssertionError("tool must not run with unparseable arguments")

    responses = [
        {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "add_client_note", "arguments": "{not json",
            }}],
        }}]},
        {"choices": [{"message": {"role": "assistant", "content": "Sorry, retrying."}}]},
    ]
    sent = []

    async def fake_chat(payload, **_kwargs):
        sent.append(payload)
        return responses[len(sent) - 1]

    monkeypatch.setattr(ai_chat_agent, "execute_safe_tool", must_not_execute)
    monkeypatch.setattr(ai_chat_agent, "_local_chat", fake_chat)

    ctx = SimpleNamespace(agent_id="agent@example.test")
    bundle = {
        "attachments": [], "record": None,
        "assistant": {"context_type": "client", "context_id": "c-1"},
        "messages": [{"role": "user", "content": "add a note"}],
    }
    text, actions = asyncio.run(
        ai_chat_agent._local_fallback(ctx, bundle, "system", "asst-1")
    )
    assert actions == []
    assert "not valid JSON" in sent[1]["messages"][-1]["content"]
    assert text == "Sorry, retrying."


def test_local_fallback_retries_without_tools_when_the_server_rejects_them(monkeypatch):
    """An older llama-server without --jinja 400s on `tools`; still answer."""
    import httpx
    import ai_chat_agent

    attempts = []

    async def fake_chat(payload, **_kwargs):
        attempts.append("tools" in payload)
        if "tools" in payload:
            raise httpx.HTTPStatusError(
                "bad request",
                request=httpx.Request("POST", "http://local/v1/chat/completions"),
                response=httpx.Response(400),
            )
        return {"choices": [{"message": {"content": "Plain answer."}}]}

    monkeypatch.setattr(ai_chat_agent, "_local_chat", fake_chat)

    ctx = SimpleNamespace(agent_id="agent@example.test")
    bundle = {
        "attachments": [], "record": None,
        "assistant": {"context_type": "client", "context_id": "c-1"},
        "messages": [{"role": "user", "content": "hello"}],
    }
    text, actions = asyncio.run(
        ai_chat_agent._local_fallback(ctx, bundle, "system", "asst-1")
    )
    assert attempts == [True, False]
    assert text == "Plain answer."
    assert actions == []


def test_read_only_tool_results_are_not_broadcast_as_record_changes(monkeypatch):
    """Reads return ok=True too, but the UI renders `actions` as applied, undoable
    record changes — a search must not show a green "Record updated" receipt with
    an Undo button that has no action_id to undo."""
    import ai_chat_agent

    async def fake_execute(ctx, agent_id, assistant_id, name, args, ctype, cid):
        return {"ok": True, "action_type": name, "clients": [{"id": "c-1"}]}

    responses = [
        {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "search_clients", "arguments": '{"query": "Smith"}',
            }}],
        }}]},
        {"choices": [{"message": {"role": "assistant", "content": "Found one Smith."}}]},
    ]
    sent = []

    async def fake_chat(payload, **_kwargs):
        sent.append(payload)
        return responses[len(sent) - 1]

    monkeypatch.setattr(ai_chat_agent, "execute_safe_tool", fake_execute)
    monkeypatch.setattr(ai_chat_agent, "_local_chat", fake_chat)

    ctx = SimpleNamespace(agent_id="agent@example.test")
    bundle = {
        "attachments": [], "record": None,
        "assistant": {"context_type": "client", "context_id": "c-1"},
        "messages": [{"role": "user", "content": "find my clients named Smith"}],
    }
    text, actions = asyncio.run(
        ai_chat_agent._local_fallback(ctx, bundle, "system", "asst-1")
    )

    assert text == "Found one Smith."
    assert actions == []
    # The model still sees the result — only the broadcast is filtered.
    assert sent[1]["messages"][-1]["role"] == "tool"


def test_local_fallback_keeps_receipts_for_writes_that_already_committed(monkeypatch):
    """Exhausting the tool-call budget must not discard writes that landed, or the
    user is told nothing happened and their retry applies the change twice."""
    import ai_chat_agent

    async def fake_execute(ctx, agent_id, assistant_id, name, args, ctype, cid):
        return {"ok": True, "action_id": "a-1", "summary": "stage moved"}

    async def fake_chat(payload, **_kwargs):
        return {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_n", "type": "function", "function": {
                "name": "set_client_stage", "arguments": '{"stage": "under_contract"}',
            }}],
        }}]}

    monkeypatch.setattr(ai_chat_agent, "execute_safe_tool", fake_execute)
    monkeypatch.setattr(ai_chat_agent, "_local_chat", fake_chat)

    ctx = SimpleNamespace(agent_id="agent@example.test")
    bundle = {
        "attachments": [], "record": None,
        "assistant": {"context_type": "client", "context_id": "c-1"},
        "messages": [{"role": "user", "content": "move them to under contract"}],
    }
    applied: list[dict] = []
    with pytest.raises(RuntimeError):
        asyncio.run(
            ai_chat_agent._local_fallback(
                ctx, bundle, "system", "asst-1", applied=applied
            )
        )
    assert len(applied) == ai_chat_agent._LOCAL_TOOL_ROUNDS
    assert applied[0]["action_id"] == "a-1"


def test_voice_reply_abandons_a_slow_tier_inside_the_live_call_budget(monkeypatch):
    """A phone caller is waiting and Twilio will not.

    The per-tier timeouts are sized for a chat box (120s). On a <Gather> action
    request Twilio gives up around 15s, plays its own error over the caller and
    drops the call — so a slow tier has to be abandoned long before its own
    timeout fires, and the remaining tiers skipped rather than stacked on top.
    """
    import time
    import ai_chat_agent

    foundry_calls = []

    async def never_answers(payload, **_kwargs):
        await asyncio.sleep(30)

    def fake_foundry(*args, **kwargs):
        foundry_calls.append(args)
        return SimpleNamespace(output_text="late answer")

    # This test is about the pre-gateway ladder, which _generate_voice_reply
    # now uses only when no gateway provider is configured. Stating that here
    # keeps the subject fixed — otherwise a developer with Foundry credentials
    # in .env silently exercises a different code path than CI does.
    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda: [])
    monkeypatch.setattr(ai_chat_agent, "FIREWORKS_ENABLED", True)
    monkeypatch.setattr(ai_chat_agent, "FIREWORKS_API_KEY", "fw-test-key")
    monkeypatch.setattr(ai_chat_agent, "VOICE_REPLY_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(ai_chat_agent, "_local_chat", never_answers)
    monkeypatch.setattr(ai_chat_agent, "_foundry_response", fake_foundry)

    started = time.monotonic()
    reply = asyncio.run(ai_chat_agent._generate_voice_reply("+15555550123", "hello?"))
    elapsed = time.monotonic() - started

    assert elapsed < 3, f"voice reply overran the live-call budget ({elapsed:.1f}s)"
    assert reply == ai_chat_agent._VOICE_STALL_LINE
    # Budget already spent: a second tier here would only push the caller further
    # past the point where Twilio has hung up on them.
    assert foundry_calls == []


# ---------------------------------------------------------------------------
# Voice replies through the gateway (P1 remainder)
# ---------------------------------------------------------------------------

def _voice_provider():
    import llm_gateway

    return llm_gateway.Provider(name="fireworks", model="fireworks_ai/m")


def test_voice_reply_gives_the_gateway_the_whole_call_budget(monkeypatch):
    """Twilio abandons a <Gather> around 15s and talks over the caller. Two
    fallbacks each given a fresh 8s would spend 16 — so the budget bounds the
    whole call, which is what complete() implements."""
    import llm_gateway

    captured = {}

    async def _complete(prompt, **kwargs):
        captured.update(kwargs)
        return "Sure, I can help with that."

    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda: [_voice_provider()])
    monkeypatch.setattr(llm_gateway, "complete", _complete)

    reply = asyncio.run(ai_chat_agent._generate_voice_reply("+15555550123", "hello?"))

    assert reply == "Sure, I can help with that."
    assert captured["timeout"] == ai_chat_agent.VOICE_REPLY_BUDGET_SECONDS
    # The latency ladder, not the analysis one — a caller is on the line.
    assert captured["task"] == "fast"


def test_voice_reply_returns_the_stall_line_rather_than_raising(monkeypatch):
    """A live call never gets a traceback. Every failure is a spoken sentence."""
    import llm_gateway

    async def _boom(prompt, **kwargs):
        raise llm_gateway.LLMUnavailable("every provider failed")

    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda: [_voice_provider()])
    monkeypatch.setattr(llm_gateway, "complete", _boom)

    reply = asyncio.run(ai_chat_agent._generate_voice_reply("+15555550123", "hello?"))
    assert reply == ai_chat_agent._VOICE_STALL_LINE


def test_voice_reply_is_capped_so_the_caller_is_not_read_a_monologue(monkeypatch):
    import llm_gateway

    async def _long(prompt, **kwargs):
        return "word " * 400

    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda: [_voice_provider()])
    monkeypatch.setattr(llm_gateway, "complete", _long)

    reply = asyncio.run(ai_chat_agent._generate_voice_reply("+15555550123", "hello?"))
    assert len(reply) <= 300


def test_a_blank_completion_becomes_the_stall_line(monkeypatch):
    import llm_gateway

    async def _blank(prompt, **kwargs):
        return "   "

    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda: [_voice_provider()])
    monkeypatch.setattr(llm_gateway, "complete", _blank)

    reply = asyncio.run(ai_chat_agent._generate_voice_reply("+15555550123", "hello?"))
    assert reply == ai_chat_agent._VOICE_STALL_LINE


def test_without_a_gateway_the_original_ladder_still_answers_callers(monkeypatch):
    """A deployment that has not installed litellm must not answer every turn
    with a stall line — the pre-gateway ladder is kept for exactly that."""
    called = {}

    async def _direct(caller_id, speech_text):
        called["used"] = True
        return "answered by the direct path"

    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda: [])
    monkeypatch.setattr(ai_chat_agent, "_voice_reply_direct", _direct)

    reply = asyncio.run(ai_chat_agent._generate_voice_reply("+15555550123", "hello?"))
    assert called.get("used") is True
    assert reply == "answered by the direct path"
