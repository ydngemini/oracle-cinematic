"""The invariants that guard every future tool batch.

P4 adds fifteen read-only tools. The point of the batch is not the fifteen — it
is that the five artifacts each tool needs (spec, risk class, handler, gate,
allowlist entry) stay in step, so the next batch cannot ship a tool that is
offered but unimplemented, or gated but acting, or quietly able to grant itself
the consent that the TCPA gate depends on.

Each test here states one of those properties. They are deliberately written
against the rule rather than against today's tool names, so adding a tool that
violates one fails the suite instead of silently widening the surface.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

import ai_chat_agent
import ai_chat_store
import ai_tool_policy
import ai_tools_read
from platform_policy import ActionRisk, requires_approval
from tenancy import Role, TenantContext


BACKEND = Path(__file__).resolve().parent.parent
CTX = TenantContext(
    agent_id="agent@tenant.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)


# ---------------------------------------------------------------------------
# 1. Every advertised name has a handler and a risk class
# ---------------------------------------------------------------------------

def test_every_catalog_tool_has_a_risk_class():
    missing = sorted(set(ai_chat_agent.TOOLS) - set(ai_tool_policy.TOOL_RISK))
    assert not missing, (
        f"tools with no risk class: {missing}. A tool the model can be offered "
        f"without a decision about what it may do is a permission granted by "
        f"omission."
    )


def test_risk_lookup_refuses_an_unclassified_name():
    """Defaulting an unknown tool to READ_ONLY would ship the omission."""
    with pytest.raises(KeyError):
        ai_tool_policy.risk_for("a_tool_that_was_never_classified")


def test_the_two_read_only_sets_are_now_one():
    """They used to be separate literals and had drifted.

    ai_chat_agent's copy decides _is_record_change; ai_chat_store's decides
    whether a tool needs a selected record. Two answers to "is this a mutation?"
    is how get_transaction_workflow ended up read-only in one and not the other.
    """
    assert set(ai_chat_agent._READ_ONLY_TOOLS) == set(ai_chat_store._READ_ONLY_TOOLS)
    assert set(ai_chat_agent._READ_ONLY_TOOLS) == set(ai_tool_policy.READ_ONLY_TOOLS)


def test_no_read_only_tool_is_broadcast_as_an_applied_record_change():
    """A read has no action_id, so a receipt for one renders an Undo button
    that POSTs to .../actions/undefined/undo."""
    for name in ai_tool_policy.READ_ONLY_TOOLS:
        assert ai_chat_agent._is_record_change(name, {"ok": True}) is False, name


def test_call_contact_is_not_classified_read_only():
    """It creates a LIVE_CALL approval. It sat in the read-only set for as long
    as nothing consumed that set as a risk oracle."""
    assert ai_tool_policy.risk_for("call_contact") is ActionRisk.LIVE_CALL
    assert "call_contact" not in ai_tool_policy.READ_ONLY_TOOLS
    assert "call_contact" not in ai_chat_store._READ_ONLY_TOOLS


def test_every_read_tool_handler_is_classified_read_only():
    for name in ai_tools_read.TOOLS_HANDLED:
        assert ai_tool_policy.risk_for(name) is ActionRisk.READ_ONLY, name


def test_read_only_handlers_contain_no_write_statements():
    """Static, because a handler that writes only on an unusual branch would
    pass every behavioural test that never took that branch."""
    source = (BACKEND / "ai_tools_read.py").read_text().upper()
    for statement in ("INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE"):
        assert statement not in source, (
            f"{statement.strip()} appears in a module of read-only handlers"
        )


# ---------------------------------------------------------------------------
# 2. A gated tool requests; it does not act
# ---------------------------------------------------------------------------

def test_gated_tools_are_exactly_the_ones_platform_policy_says_they_are():
    for name, risk in ai_tool_policy.TOOL_RISK.items():
        assert (name in ai_tool_policy.GATED_TOOLS) is requires_approval(risk), name


def test_a_gated_tool_stages_a_command_and_reaches_no_provider(monkeypatch):
    """The shape every gated tool copies: build the request, return its ids,
    and let the human decision path do the sending.

    call_contact used to call create_approval directly, which produced a pending
    approval with no command behind it — and decide_approval only records a
    decision, so approving it did nothing at all while the receipt said "an
    admin must approve before the call is placed".
    """
    import commands_api

    staged_calls: list = []
    provider_calls: list = []

    async def _stage_command(ctx, **kwargs):
        staged_calls.append(kwargs)
        return {
            "command": {"id": "cccccccc-0000-0000-0000-000000000000",
                        "state": "awaiting_approval"},
            "approval": {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
            "created": True,
        }

    monkeypatch.setattr(commands_api, "stage_command", _stage_command)
    for sender in ("send_twilio_sms", "send_acs_sms"):
        if hasattr(commands_api, sender):
            monkeypatch.setattr(
                commands_api, sender,
                lambda *a, **k: provider_calls.append(a) or {},
            )

    class _Conn:
        async def fetchrow(self, query, *args):
            if "FROM clients" in query:
                return {"id": "c1", "full_name": "Dana Reed",
                        "email": "dana@example.test", "phone": "302-555-0134"}
            return None

        async def fetchval(self, query, *args):
            return "DE" if "FROM leads" in query else None

        async def fetch(self, *a, **k):
            return []

        async def execute(self, *a, **k):
            raise AssertionError("a gated tool wrote to the database")

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _tx(_ctx):
        yield _Conn()

    monkeypatch.setattr(ai_chat_store, "tenant_tx", _tx)
    monkeypatch.setattr(ai_chat_store, "tenant_key", lambda ctx: "k" * 32)

    client_id = "22222222-2222-2222-2222-222222222222"
    result = asyncio.run(ai_chat_store._execute_safe_tool(
        CTX, "user-1", "33333333-3333-3333-3333-333333333333", "call_contact",
        {"client_id": client_id, "reason": "Following up on the listing"},
        "client", client_id,
    ))

    assert result["ok"] is True
    assert result["sent"] is False
    assert result["approval_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert result["state"] == "awaiting_approval"
    assert not provider_calls, "a gated tool reached a provider"
    assert staged_calls[0]["command_type"] is commands_api.CommandType.CALL
    # The number came from the record, not from the model.
    assert staged_calls[0]["target"]["phone"] == "+13025550134"


def test_a_gated_tool_will_not_take_a_phone_number_from_the_model():
    """call_contact once accepted a model-supplied `phone`. One fabricated digit
    dials a stranger, so the schema no longer offers the field at all."""
    import ai_chat_agent as agent

    for name in ("call_contact", "draft_sms", "draft_email"):
        schema = agent.TOOLS[name]["toolSpec"]["inputSchema"]["json"]
        assert "phone" not in schema["properties"], name
        assert "email" not in schema["properties"], name
        assert "client_id" in schema["required"], name
        assert schema["additionalProperties"] is False, name


def test_outreach_refuses_without_a_state_because_the_rules_are_per_state(monkeypatch):
    """guard_outreach applies quiet hours and consent by state. Staging without
    one would either fail later or, worse, apply the wrong state's law."""
    class _Conn:
        async def fetchrow(self, query, *args):
            if "FROM clients" in query:
                return {"id": "c1", "full_name": "Dana Reed",
                        "email": "dana@example.test", "phone": "+13025550134"}
            return None

        async def fetchval(self, query, *args):
            return None

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _tx(_ctx):
        yield _Conn()

    monkeypatch.setattr(ai_chat_store, "tenant_tx", _tx)
    monkeypatch.setattr(ai_chat_store, "tenant_key", lambda ctx: "k" * 32)

    client_id = "22222222-2222-2222-2222-222222222222"
    result = asyncio.run(ai_chat_store._execute_safe_tool(
        CTX, "user-1", "33333333-3333-3333-3333-333333333333", "draft_sms",
        {"client_id": client_id, "body": "Following up."}, "client", client_id,
    ))

    assert result["ok"] is False
    assert "per state" in result["error"]


def test_a_stored_number_that_cannot_be_normalised_is_refused_not_guessed():
    import ai_tools_gated as ai_chat_store  # the helper moved with the handlers

    assert ai_chat_store._to_e164("302-555-0134")[0] == "+13025550134"
    assert ai_chat_store._to_e164("1 (302) 555-0134")[0] == "+13025550134"
    assert ai_chat_store._to_e164("+44 20 7946 0958")[0] == "+442079460958"
    for ambiguous in ("555-0134", "", None, "12345678901234567890"):
        number, reason = ai_chat_store._to_e164(ambiguous)
        assert number is None and reason, ambiguous


def test_contract_generation_still_refuses_rather_than_drafting():
    """P12 replaces this with an approval-creating draft. Until then the refusal
    is the gate, and it must not quietly become a success."""
    for name in ("generate_contract", "generate_assignment_agreement"):
        assert ai_tool_policy.risk_for(name) is ActionRisk.LEGAL_DOCUMENT
        assert not ai_chat_store.is_agent_tool_available(name)


def test_no_gated_tool_is_offered_without_an_approval_creating_handler():
    """The allowlist controls what the model is offered. A gated name may only
    appear there if its handler creates an approval instead of acting."""
    import ai_tools_gated

    source = (BACKEND / "ai_tools_gated.py").read_text()
    for name in sorted(ai_tool_policy.GATED_TOOLS):
        if not ai_chat_store.is_agent_tool_available(name):
            continue
        assert name in ai_tools_gated.TOOLS_HANDLED, (
            f"{name} is offered and gated but is not handled in the module "
            f"where gated tools are audited"
        )
    # Each gated handler reaches an approval-creating seam: stage_command for
    # the command channels, and for contracts and marketplace the same route
    # functions a human posts to, which create the approval themselves.
    for seam in ("stage_command", "draft_document", "create_publication_from_contract"):
        assert seam in source, f"the gated module no longer reaches {seam}"


def test_the_gated_module_cannot_reach_a_provider():
    """Every tool that must not act lives in one file, so this is checkable.

    A gated tool that could send would make its own approval decorative — the
    action would already have happened by the time a human saw the request.
    """
    tree = ast.parse((BACKEND / "ai_tools_gated.py").read_text())
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name:
                referenced.add(name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                referenced.add(alias.asname or alias.name.split(".")[0])
            if isinstance(node, ast.ImportFrom) and node.module:
                referenced.add(node.module.split(".")[0])

    forbidden = {
        "send_twilio_sms", "send_acs_sms", "send_email", "smtplib",
        "SovereignVault", "publish_publication", "approve_command",
        "decide_approval", "enqueue_job", "write_contract_pdf",
    }
    # Read from the parse tree, not the file text: the module docstring names
    # publish_publication when explaining where the action actually happens,
    # and a substring scan cannot tell prose from a call.
    leaked = sorted(referenced & forbidden)
    assert not leaked, (
        f"the gated tool module reaches {leaked}; these tools request an "
        f"action, they do not perform one"
    )
    # draft_document and create_publication_from_contract are the approval-
    # creating seams, and are expected here.
    assert {"stage_command", "draft_document"} <= referenced


def test_a_gated_tool_never_returns_the_contract_body():
    """draft_document returns `editable_draft` — the rendered instrument. Putting
    that in the model's context invites it to quote or restate terms that only
    the vault copy is authoritative for."""
    source = (BACKEND / "ai_tools_gated.py").read_text()
    assert "editable_draft" in source, "the comment explaining the omission is gone"
    body = source.split("async def _draft_contract", 1)[1]
    returned = body.split("return {", 1)[-1]
    assert '"editable_draft"' not in returned
    assert '"contract_text_withheld": True' in returned


# ---------------------------------------------------------------------------
# 3. The agent cannot grant itself consent
# ---------------------------------------------------------------------------

def test_consent_writing_is_not_reachable_from_the_tool_surface():
    """The single most important assertion in this file.

    guard_outreach decides whether a homeowner may be contacted by reading
    outreach_consent and outreach_suppression. A model that can write either
    table can satisfy its own TCPA check, and the gate becomes decorative.
    Consent is a record of what a consumer said; a model writing it fabricates
    the consumer's answer.
    """
    surface = (
        set(ai_chat_agent.TOOLS)
        | set(ai_tool_policy.TOOL_RISK)
        | set(ai_tools_read.TOOLS_HANDLED)
        | {n for n in ai_chat_agent.TOOLS if ai_chat_store.is_agent_tool_available(n)}
    )
    forbidden = sorted(surface & ai_tool_policy.CONSENT_WRITE_NAMES)
    assert not forbidden, f"consent-writing tools are exposed: {forbidden}"


@pytest.mark.parametrize("module", ["ai_tools_read.py", "ai_chat_store.py"])
def test_no_tool_handler_writes_the_consent_or_suppression_tables(module):
    """Name-level absence is not enough — a handler could reach the same tables
    under any name at all."""
    source = (BACKEND / module).read_text()
    for table in ("outreach_consent", "outreach_suppression"):
        assert table not in source, (
            f"{module} references {table}; consent state must only be written "
            f"by the compliance surface a human drives"
        )
    assert "record_consent" not in source, f"{module} can call record_consent"


# ---------------------------------------------------------------------------
# 4. Ordering: approval, then the legal gate, then the provider
# ---------------------------------------------------------------------------

_PROVIDER_SENDERS = {
    "send_twilio_sms", "send_acs_sms", "sender",
    "initialize_twilio_call_state", "send_email",
}


def _calls_in(node) -> list[tuple[int, str]]:
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name:
                found.append((child.lineno, name))
    return found


def test_every_outreach_branch_gates_before_it_reaches_a_provider():
    """guard_outreach is the legal control and it runs last.

    Approval is the human control; TCPA/quiet-hours/suppression is the legal
    one. A branch that reached a provider before the gate, or that ignored
    ``decision.allowed``, would send on an approved-but-unlawful contact.
    """
    tree = ast.parse((BACKEND / "commands_api.py").read_text())
    executor = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_execute_command_job"
    )

    guard_lines = [line for line, name in _calls_in(executor) if name == "guard_outreach"]
    send_lines = [line for line, name in _calls_in(executor) if name in _PROVIDER_SENDERS]
    assert guard_lines, "the command executor no longer calls guard_outreach"
    assert send_lines, "no provider send found; this test has lost its subject"

    for send_line in send_lines:
        preceding = [line for line in guard_lines if line < send_line]
        assert preceding, (
            f"a provider send at line {send_line} of commands_api.py is not "
            f"preceded by guard_outreach in the same function"
        )

    # And the decision is actually consulted, not merely computed.
    source = inspect.getsource(
        __import__("commands_api")._execute_command_job
    )
    assert source.count("if not decision.allowed") >= len(guard_lines), (
        "a guard_outreach result is computed without being checked"
    )


# ---------------------------------------------------------------------------
# 5. Internal writes are actually undoable (P11)
# ---------------------------------------------------------------------------

def test_every_allowlisted_tool_is_offered_in_some_context():
    """The allowlist says a tool is permitted; the tool config decides whether
    the model ever sees it. A name in one and not the other is a capability
    that exists only on paper — create_client sat exactly there."""
    import ai_chat_agent as agent

    offered = {
        tool["toolSpec"]["name"]
        for context in (None, "client", "listing", "lead")
        for tool in (agent._tool_config(context) or {"tools": []})["tools"]
    }
    allowlisted = {n for n in agent.TOOLS if ai_chat_store.is_agent_tool_available(n)}
    assert not allowlisted - offered, (
        f"allowlisted but never offered: {sorted(allowlisted - offered)}"
    )


def test_the_two_providers_offer_exactly_the_same_surface():
    """There were three hand-maintained catalogs: TOOLS, the context-write
    mapping (duplicated), and _FOUNDRY_TOOLS — which had already fallen 13
    tools behind, so a Foundry deployment offered a smaller surface than a
    Bedrock one with no way to notice."""
    import ai_chat_agent as agent

    for context in (None, "client", "listing", "lead"):
        bedrock = {t["toolSpec"]["name"]
                   for t in (agent._tool_config(context) or {"tools": []})["tools"]}
        foundry = {t["name"] for t in agent._foundry_tools(context)}
        assert bedrock == foundry, f"{context}: {sorted(bedrock ^ foundry)}"
        if context:
            assert set(agent._CONTEXT_WRITE_TOOLS[context]) <= bedrock, context


def test_a_selected_record_unlocks_only_its_own_writes():
    """A client in context must not unlock a lead's stage move."""
    import ai_chat_agent as agent

    client = {t["toolSpec"]["name"] for t in agent._tool_config("client")["tools"]}
    lead = {t["toolSpec"]["name"] for t in agent._tool_config("lead")["tools"]}
    assert "move_deal_stage" in lead and "move_deal_stage" not in client
    assert "set_client_stage" in client and "set_client_stage" not in lead
    assert "update_listing" not in client and "update_listing" not in lead


def test_every_offered_internal_edit_records_an_undo_kind():
    """INTERNAL_EDIT's definition is "undoable through the ai_chat_actions
    ledger". Six tools returned early and wrote no ledger row at all, while
    _is_record_change still broadcast them as applied, undoable receipts.

    Read statically because the alternative is a live DB per branch; what
    matters is that no mutating branch can return ok=True without passing
    through _record_action, which is where undo_kind becomes mandatory.
    """
    import ai_chat_agent as agent

    source = (BACKEND / "ai_chat_store.py").read_text()
    body = source.split("async def _execute_safe_tool", 1)[1]
    internal = [
        name for name, risk in ai_tool_policy.TOOL_RISK.items()
        if risk is ActionRisk.INTERNAL_EDIT
        and ai_chat_store.is_agent_tool_available(name)
    ]
    assert internal, "no internal-edit tool is offered; this test lost its subject"

    for name in internal:
        marker = f'tool_name == "{name}"'
        if marker not in body:
            continue
        branch = body.split(marker, 1)[1].split("\n        elif tool_name", 1)[0]
        falls_through = "table, permitted" in branch or "table, id_field" in branch
        assert falls_through or "_record_action(" in branch, (
            f"{name} mutates and returns without writing an ai_chat_actions row"
        )


def test_a_receipt_never_offers_an_undo_it_cannot_perform():
    """create_client is recorded for audit and declares itself irreversible;
    the receipt has to carry that, because the UI reads `undoable`."""
    action = {"id": "aaaaaaaa-0000-0000-0000-000000000000",
              "undo_expires_at": None}
    receipt = ai_chat_store._applied(
        "create_client", action, record_type="client", record_id="c1",
        undo_kind="none", undo_unavailable_reason="cascades to ten tables",
    )
    assert receipt["undoable"] is False
    assert receipt["undo_expires_at"] is None
    assert receipt["undo_unavailable_reason"] == "cascades to ten tables"


def test_undo_only_restores_columns_on_its_allowlist():
    """`before` keys are interpolated into the UPDATE as column names. They come
    from our own ciphertext, but an allowlist makes that safe by construction."""
    for record_type, (table, columns) in ai_chat_store._UNDO_COLUMNS.items():
        assert columns, record_type
        assert all(column.replace("_", "").isalnum() for column in columns), record_type
    assert "clients" not in ai_chat_store._UNDO_DELETABLE_TABLES, (
        "a client row_delete would cascade to ten tables; that is not an undo"
    )


def test_a_staged_outreach_request_is_not_broadcast_as_an_applied_change():
    """A gated tool returns ok=True with `sent: False`. Putting that in the
    UI's `actions` list would render it as an applied "Record updated" receipt —
    claiming an email went out when it is sitting in an approval queue."""
    import ai_chat_agent as agent

    staged = {"ok": True, "approval_id": "a-1", "command_id": "c-1",
              "state": "awaiting_approval", "sent": False}
    for name in ("draft_email", "draft_sms", "call_contact"):
        assert agent._is_record_change(name, staged) is False, name

    applied = {"ok": True, "action_id": "act-1", "undoable": True}
    assert agent._is_record_change("set_client_stage", applied) is True
