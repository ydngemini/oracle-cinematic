"""The tool-execution ledger (ai_tool_operations, migration 0087).

What it buys: a tool round that dies after its mutation commits but before the
model sees the receipt used to be unrecoverable, which is why the standing rule
was "tool rounds never retry". That rule spent every legitimate recovery to
prevent one illegitimate one. These tests pin the properties that make retrying
safe instead.

A note on what is asserted structurally rather than behaviourally. The claim is
taken inside `_execute_safe_tool`'s own transaction, so a test cannot stub the
handler without also stubbing away the ledger. Extracting a seam was tried and
rejected: the dispatcher is 540 lines whose final receipt is deliberately built
AFTER the transaction commits, and splitting it risked changing that ordering in
the most safety-critical function in the AI path. So the claim/handler ordering
is asserted against the source, and the ledger's own decisions are tested
directly against their helpers. That trade is deliberate, not laziness.
"""

import asyncio
import ast
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import ai_chat_store
from ai_chat_store import (
    ToolIdentityMismatch,
    _canonical_arguments_hash,
    _ledger_replay,
    execute_safe_tool,
)
from tenancy import Role, TenantContext

TENANT = "00000000-0000-0000-0000-000000000000"
ASSISTANT = "11111111-1111-1111-1111-111111111111"
BACKEND = Path(__file__).parent.parent


def _ctx() -> TenantContext:
    return TenantContext(agent_id="agent-1", tenant_id=TENANT, role=Role.AGENT)


class _Row(dict):
    """asyncpg rows are mappings; dict is close enough for these lookups."""


class _Conn:
    def __init__(self, row=None):
        self.row = row
        self.inserts = []

    async def fetchrow(self, _sql, *_args):
        return self.row

    async def execute(self, sql, *args):
        if "INSERT INTO ai_tool_operations" in sql:
            self.inserts.append(args)
        return "OK"


# ── identity ────────────────────────────────────────────────────────────────

def test_canonical_hash_ignores_key_order_but_not_the_work():
    a = _canonical_arguments_hash("update_client", {"stage": "won", "id": "x"})
    b = _canonical_arguments_hash("update_client", {"id": "x", "stage": "won"})
    assert a == b, "dict ordering is not a difference in the requested work"

    assert a != _canonical_arguments_hash("update_client", {"id": "x", "stage": "lost"})
    assert a != _canonical_arguments_hash("update_listing", {"stage": "won", "id": "x"})


def test_canonical_hash_survives_unserialisable_arguments():
    """A tool argument that will not serialise must still yield a stable key."""
    class Odd:
        def __repr__(self):  # deterministic
            return "<odd>"

    h1 = _canonical_arguments_hash("update_client", {"x": Odd()})
    h2 = _canonical_arguments_hash("update_client", {"x": Odd()})
    assert h1 == h2 and len(h1) == 64


# ── replay decisions ────────────────────────────────────────────────────────

def test_no_row_means_the_caller_may_execute():
    got = asyncio.run(_ledger_replay(_Conn(None), _ctx(), ASSISTANT, 0, "update_client", "h"))
    assert got is None


def test_a_completed_row_replays_its_stored_receipt():
    row = _Row(tool_name="update_client", arguments_hash="h",
               status="completed", result=json.dumps({"ok": True, "record_id": "r1"}))
    got = asyncio.run(_ledger_replay(_Conn(row), _ctx(), ASSISTANT, 0, "update_client", "h"))
    assert got == {"ok": True, "record_id": "r1", "replayed": True}


def test_a_completed_row_without_a_receipt_still_blocks_re_execution():
    """The receipt is best-effort; the claim is not.

    _ledger_attach_result writes in its own transaction, so a crash between the
    mutation's commit and that write leaves a claim with result NULL. The
    mutation still happened, so this must NOT return None (which would license a
    second execution).
    """
    row = _Row(tool_name="update_client", arguments_hash="h",
               status="completed", result=None)
    got = asyncio.run(_ledger_replay(_Conn(row), _ctx(), ASSISTANT, 0, "update_client", "h"))
    assert got is not None, "a claim without a receipt must still block re-execution"
    assert got["ok"] is True and got["replayed"] is True


def test_a_failed_row_permits_a_retry():
    """'failed' is only ever written after the mutation's transaction rolled back."""
    row = _Row(tool_name="update_client", arguments_hash="h", status="failed", result=None)
    got = asyncio.run(_ledger_replay(_Conn(row), _ctx(), ASSISTANT, 0, "update_client", "h"))
    assert got is None, "nothing committed, so there is nothing to protect"


def test_same_index_different_work_is_refused_not_executed():
    """Divergence is neither a duplicate nor a new operation."""
    row = _Row(tool_name="update_client", arguments_hash="hash-of-A",
               status="completed", result=None)
    with pytest.raises(ToolIdentityMismatch):
        asyncio.run(_ledger_replay(
            _Conn(row), _ctx(), ASSISTANT, 0, "update_client", "hash-of-B",
        ))
    with pytest.raises(ToolIdentityMismatch):
        asyncio.run(_ledger_replay(
            _Conn(row), _ctx(), ASSISTANT, 0, "delete_everything", "hash-of-A",
        ))


# ── enrolment ───────────────────────────────────────────────────────────────

def test_read_only_tools_and_missing_call_index_are_never_enrolled(monkeypatch):
    """~101 of 122 tools are reads; enrolling them costs ~100 inserts a turn."""
    seen = []

    async def fake_dispatch(*_a, **_k):
        return {"ok": True}

    async def spy(*args):
        seen.append(args)

    monkeypatch.setattr(ai_chat_store, "_execute_safe_tool", fake_dispatch)
    monkeypatch.setattr(ai_chat_store, "_ledger_attach_result", spy)

    asyncio.run(execute_safe_tool(
        _ctx(), "a", ASSISTANT, "search_clients", {"q": "x"}, None, None, 0,
    ))
    assert seen == [], "a read-only tool must not take an operation identity"

    asyncio.run(execute_safe_tool(
        _ctx(), "a", ASSISTANT, "update_client", {"id": "c1"}, "client", "c1",
    ))
    assert seen == [], "no call_index means no ledger participation"

    asyncio.run(execute_safe_tool(
        _ctx(), "a", ASSISTANT, "update_client", {"id": "c1"}, "client", "c1", 0,
    ))
    assert len(seen) == 1, "an effectful tool with a call_index must be recorded"


def test_a_replayed_receipt_is_not_written_back_over_the_original(monkeypatch):
    seen = []

    async def replayed(*_a, **_k):
        return {"ok": True, "replayed": True}

    async def spy(*args):
        seen.append(args)

    monkeypatch.setattr(ai_chat_store, "_execute_safe_tool", replayed)
    monkeypatch.setattr(ai_chat_store, "_ledger_attach_result", spy)
    asyncio.run(execute_safe_tool(
        _ctx(), "a", ASSISTANT, "update_client", {"id": "c1"}, "client", "c1", 0,
    ))
    assert seen == [], "a replay must not overwrite the first execution's record"


# ── failure recording ───────────────────────────────────────────────────────

@pytest.mark.parametrize("exc,code", [
    (RuntimeError("boom"), "TOOL_HANDLER_ERROR"),
    (ValueError("bad input"), "INVALID_TOOL_INPUT"),
    (ToolIdentityMismatch("diverged"), "TOOL_CALL_IDENTITY_MISMATCH"),
])
def test_every_failure_path_records_that_an_attempt_did_not_commit(monkeypatch, exc, code):
    """Absence cannot distinguish "never started" from "started and died"."""
    recorded = {}

    async def boom(*_a, **_k):
        raise exc

    async def fake_failure(_ctx, _uid, assistant_id, call_index, tool_name, _h, error_code):
        recorded.update(assistant=assistant_id, index=call_index,
                        tool=tool_name, code=error_code)

    monkeypatch.setattr(ai_chat_store, "_execute_safe_tool", boom)
    monkeypatch.setattr(ai_chat_store, "_ledger_record_failure", fake_failure)
    receipt = asyncio.run(execute_safe_tool(
        _ctx(), "a", ASSISTANT, "update_client", {"id": "c1"}, "client", "c1", 3,
    ))

    assert receipt["ok"] is False
    assert recorded == {"assistant": ASSISTANT, "index": 3,
                        "tool": "update_client", "code": code}


def test_recording_a_failure_never_changes_what_the_tool_reports(monkeypatch):
    """The ledger must not become an availability dependency.

    If the failure write itself fails, the caller still receives the original
    execution failure rather than a second, unrelated one.
    """
    @asynccontextmanager
    async def exploding_tx(_ctx):
        raise RuntimeError("database unreachable")
        yield  # pragma: no cover

    async def boom(*_a, **_k):
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(ai_chat_store, "tenant_tx", exploding_tx)
    monkeypatch.setattr(ai_chat_store, "_execute_safe_tool", boom)
    receipt = asyncio.run(execute_safe_tool(
        _ctx(), "a", ASSISTANT, "update_client", {"id": "c1"}, "client", "c1", 0,
    ))
    assert receipt["ok"] is False
    assert "could not be persisted" in receipt["error"]


# ── structural invariants ───────────────────────────────────────────────────

def test_the_claim_is_taken_before_the_dispatch_chain():
    """The unique index is a mutex only if it is acquired before the mutation.

    Moved after, two concurrent duplicates would both write and only then
    discover each other — the exact double-write this table exists to prevent.
    """
    src = (BACKEND / "ai_chat_store.py").read_text()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_execute_safe_tool")
    body = "\n".join(src.split("\n")[fn.lineno - 1:fn.end_lineno])
    claim = body.index("_ledger_claim(")
    first_handler = body.index("if tool_name in {")
    assert claim < first_handler, "the claim must precede every tool handler"


def test_call_index_is_monotonic_across_rounds_not_per_round():
    """A per-round counter makes round 2's call 0 collide with round 1's.

    Invisible until a turn actually runs two rounds, and then it replays the
    wrong receipt — so it is asserted here rather than left to production.
    """
    src = (BACKEND / "ai_chat_agent.py").read_text()
    for loop in ("for round_index in range(2):", "for _ in range(_LOCAL_TOOL_ROUNDS):"):
        head = src.index(loop)
        assert "call_index = 0" in src[max(0, head - 500):head], (
            f"call_index must be initialised outside `{loop}`"
        )
        assert "call_index = 0" not in src[head:head + 1500], (
            f"call_index must not reset inside `{loop}`"
        )


def test_migration_0087_grants_the_table_and_forces_rls():
    """0003 revokes PUBLIC: a table with no GRANT is invisible to the app role.

    0050 shipped a function without one and ~25 state harvests died on the write
    after paying for their fetch. Same class of omission, asserted here.
    """
    sql = (BACKEND / "db" / "migrations" / "0087_ai_tool_operations.sql").read_text()
    assert "GRANT SELECT, INSERT, UPDATE ON ai_tool_operations TO oracle_app;" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "DELETE" not in sql.split("GRANT")[-1], "an execution record is history"
    assert "UNIQUE (tenant_id, assistant_id, call_index)" in sql


def test_every_rls_helper_a_policy_calls_is_granted_to_the_app_role():
    """The omission that hid for three migrations, generalised.

    0008 granted app_current_tenant, app_current_role, app_is_platform_admin and
    app_has_listing_grant — and missed app_current_agent, which five tables'
    policies call. 0003 revokes PUBLIC, so that left it owner-only and
    `SELECT count(*) FROM ai_chat_actions` as a normal broker raised
    "permission denied for function app_current_agent". Migration 0088 fixes it.

    It hid because the policies are `app_is_platform_admin() OR (...)`: when the
    first operand is true the second need never be evaluated, so admin paths
    worked and only real brokers broke — and Postgres does not guarantee OR
    operand order, so which sessions broke was not even stable.

    This asserts the general rule rather than the one function: any app_* helper
    named in a policy must carry an explicit grant somewhere in the migrations.
    """
    migrations = sorted((BACKEND / "db" / "migrations").glob("*.sql"))
    corpus = "\n".join(m.read_text() for m in migrations)

    called = set()
    for text in (m.read_text() for m in migrations):
        for chunk in text.split("CREATE POLICY")[1:]:
            body = chunk.split(";")[0]
            for name in ("app_current_agent", "app_current_tenant", "app_current_role",
                         "app_is_platform_admin", "app_has_listing_grant"):
                if f"{name}()" in body:
                    called.add(name)

    assert "app_current_agent" in called, "expected the policies to call it"
    ungranted = [
        n for n in sorted(called)
        if f"GRANT EXECUTE ON FUNCTION {n}()" not in corpus
    ]
    assert ungranted == [], (
        "these RLS helpers are called by a policy but never granted to "
        f"oracle_app, so the policy raises for any role that evaluates them: {ungranted}"
    )
