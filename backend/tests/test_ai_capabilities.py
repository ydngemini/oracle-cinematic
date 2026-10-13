"""Capability claims agree with the executable tool surface (audit A09, L07-01).

The audit's finding was that the assistant described more than it could do.
At 3bcab84 that was literal: the product knowledge in every long system prompt
claimed "MLS via real-time feed", CourtListener, Census and Regrid; the phone
prompt told the model to promise a transfer ("end with [CONNECT]") that no code
performed; and offered tools described fields their handlers never return.

These tests hold four surfaces to one registry (``ai_capabilities``):

1. offered tools — the registry is derived from the allowlist and the offer;
2. model-facing text — persona rules, product knowledge, phone prompt,
   codebase map and every offered tool's description;
3. API metadata — ``GET /api/ai/chat/capabilities``;
4. UI — the committed snapshot the frontend tests read, and the chat prompts
   the UI sends (``oracle-app/src/lib/assistantCapabilities.test.js``).

The guard is ``unbacked_claims``: a phrase that claims an area's capability is
allowed only while that area has an executable tool.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path

import pytest

import ai_capabilities as caps
import ai_chat_agent
import ai_chat_store
import ai_tool_policy
import ai_tools_read
import neoh_persona
from platform_policy import requires_approval

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent

CONTEXTS = (None, "client", "listing", "lead")


def _offered_anywhere() -> set[str]:
    names: set[str] = set()
    for context in CONTEXTS:
        names |= set(ai_chat_agent.offered_tool_names(context))
    return names


# ---------------------------------------------------------------------------
# 1. The registry covers the catalog and agrees with the offer
# ---------------------------------------------------------------------------

def test_every_catalogued_tool_is_in_exactly_one_area():
    seen: dict[str, str] = {}
    for area in caps.AREAS:
        for tool in area.tools:
            assert tool not in seen, f"{tool} is in {seen[tool]} and {area.id}"
            seen[tool] = area.id
    assert set(seen) == set(ai_chat_agent.TOOLS), (
        "not classified: " + str(sorted(set(ai_chat_agent.TOOLS) - set(seen)))
        + "; not in the catalog: " + str(sorted(set(seen) - set(ai_chat_agent.TOOLS)))
    )


def test_counts_at_this_sha():
    """126 catalogued, 57 executable — the numbers the ledger records. A change
    here is a deliberate surface change: update L07-01 with it."""
    assert len(ai_chat_agent.TOOLS) == 126
    assert len(caps.executable_tools()) == 57


def test_executable_means_offered_in_some_context():
    """The registry's "usable" set is exactly what the model can be offered."""
    assert caps.executable_tools() == _offered_anywhere()


def test_every_executable_tool_has_a_handler():
    handled = (
        set(ai_tools_read.TOOLS_HANDLED)
        | set(ai_chat_store._GATED_TOOL_NAMES)
        | set(ai_chat_store._AGENT_AVAILABLE_TOOLS)
    )
    for tool in caps.executable_tools():
        assert ai_chat_store.is_agent_tool_available(tool)
        assert tool in handled


@pytest.mark.parametrize("area", caps.AREAS, ids=lambda a: a.id)
def test_area_state_follows_its_tools(area):
    entry = next(e for e in caps.capabilities() if e["id"] == area.id)
    offered = [t for t in area.tools if ai_chat_store.is_agent_tool_available(t)]
    if not offered:
        assert entry["state"] == caps.UNAVAILABLE
        assert entry["state_label"] == "Not available yet"
        return
    assert entry["state"] != caps.UNAVAILABLE
    gated = [requires_approval(ai_tool_policy.TOOL_RISK[t]) for t in offered]
    # An area is either all approval-gated or none of it is, so its one state
    # label is true of every tool in it.
    assert all(gated) or not any(gated), f"{area.id} mixes gated and direct tools"
    if all(gated):
        assert entry["state"] == caps.APPROVAL_GATED


def test_ops_role_tools_match_the_handlers_role_gate():
    source = (BACKEND / "ai_tools_read.py").read_text()
    gated = set(re.findall(r'_require_ops_role\(ctx, "([a-z_]+)"\)', source))
    assert gated == set(ai_tools_read.OPS_ROLE_TOOLS)


def test_an_agent_sees_owner_tools_as_role_limited_not_missing():
    agent = {e["id"]: e for e in caps.capabilities(ops_role=False)}
    owner = {e["id"]: e for e in caps.capabilities(ops_role=True)}
    assert owner["workspace_admin"]["state"] == caps.AVAILABLE
    assert agent["workspace_admin"]["state"] == caps.UNAVAILABLE
    assert agent["workspace_admin"]["role_limited"] is True
    assert owner["public_records"]["role_limited"] is False


def test_market_figures_are_limited_while_mls_is_off(monkeypatch):
    monkeypatch.delenv("ORACLE_MLS_ENABLED", raising=False)
    market = next(e for e in caps.capabilities() if e["id"] == "market")
    assert market["state"] == caps.DEGRADED
    assert market["limits"]


def test_mls_listing_tools_never_describe_mls():
    """D-2026-10-12-01: the two legacy-MLS-backed tools stay offered for the
    brokerage's own data, but no offered description may present MLS."""
    for tool in ("search_listings", "get_days_on_market"):
        spec = ai_chat_agent.TOOLS[tool]["toolSpec"]
        assert not re.search(r"\bMLS\b|\bIDX\b", spec["description"]), tool


# ---------------------------------------------------------------------------
# 2. Model-facing text names nothing without an executable tool
# ---------------------------------------------------------------------------

def _model_facing_surfaces() -> dict[str, str]:
    surfaces = {
        "persona.IDENTITY": neoh_persona.IDENTITY,
        "persona.AUTHORITY": neoh_persona.AUTHORITY,
        "persona.HONESTY": neoh_persona.HONESTY,
        "persona.STYLE": neoh_persona.STYLE,
        "persona.capability_block": neoh_persona.capability_block(),
        "NEOH_SYSTEM_KNOWLEDGE.md": neoh_persona.product_knowledge(),
        "ai_chat_agent._VOICE_PROMPT": ai_chat_agent._VOICE_PROMPT,
        "ai_chat_agent._CODEBASE_MAP": ai_chat_agent._CODEBASE_MAP,
    }
    for name in _offered_anywhere():
        spec = ai_chat_agent.TOOLS[name]["toolSpec"]
        props = spec["inputSchema"]["json"]["properties"]
        surfaces[f"tool:{name}"] = " ".join(
            [spec["description"]]
            + [str(p.get("description", "")) for p in props.values()]
        )
    for entry in caps.capabilities():
        if entry["state"] != caps.UNAVAILABLE:
            surfaces[f"area:{entry['id']}"] = entry["summary"]
    return surfaces


@pytest.mark.parametrize("surface", sorted(_model_facing_surfaces()))
def test_no_model_facing_text_claims_an_unexecutable_capability(surface):
    text = _model_facing_surfaces()[surface]
    assert not caps.unbacked_claims(text), (
        f"{surface} claims capabilities with no executable tool: "
        f"{caps.unbacked_claims(text)}"
    )


def test_the_guard_catches_the_claims_the_audit_found():
    """The guard is only worth having if it fails on the text it replaced."""
    for claim in (
        "Data Sources: MLS (Multiple Listing Service) via real-time feed.",
        "Court records via CourtListener.",
        "Census Bureau demographics.",
        'If the caller wants a human agent, say "Let me connect you" and end with [CONNECT].',
        "Identify homeowners who may benefit from an annual property and equity review.",
        "Firehose sync from BatchLeads/PropStream.",
    ):
        assert caps.unbacked_claims(claim), claim
    # …and lets a disclaimer through.
    assert not caps.unbacked_claims("MLS and IDX listing search is not part of this product.")


@pytest.mark.parametrize("compact", [False, True])
def test_both_persona_densities_carry_the_generated_block(compact):
    prompt = neoh_persona.build_system_prompt(compact=compact)
    assert caps.prompt_block() in prompt


def test_the_block_lists_every_unavailable_area_under_not_available():
    block = caps.prompt_block()
    line = next(l for l in block.splitlines() if caps.UNAVAILABLE_LEAD in l)
    for entry in caps.capabilities():
        if entry["state"] == caps.UNAVAILABLE:
            assert entry["label"] in line, entry["id"]
        else:
            assert entry["label"] not in line, entry["id"]


def test_the_phone_prompt_promises_no_transfer_or_lookup():
    """Nothing parses [CONNECT] or [END]: the markers were spoken aloud by TTS,
    and "let me connect you" was a promise no code kept."""
    prompt = ai_chat_agent._VOICE_PROMPT
    assert "[CONNECT]" not in prompt and "[END]" not in prompt
    assert "say you can look that up" not in prompt
    assert "cannot look anything up, transfer the call" in prompt


def test_offered_schemas_carry_no_parameter_the_handler_ignores():
    """Each of these was offered and silently dropped (or always refused)."""
    ignored = {
        "estimate_arv": ("beds", "baths"),
        "score_client_lead": ("reason",),
        "list_contract_templates": ("state", "document_type"),
        "get_audit_trail": ("limit",),
        "update_client": ("email", "phone"),
    }
    for tool, params in ignored.items():
        props = ai_chat_agent.TOOLS[tool]["toolSpec"]["inputSchema"]["json"]["properties"]
        for param in params:
            assert param not in props, f"{tool}.{param}"


def test_owner_only_tools_say_so():
    for tool in ai_tools_read.OPS_ROLE_TOOLS:
        desc = ai_chat_agent.TOOLS[tool]["toolSpec"]["description"]
        assert "Broker-owner only" in desc, tool


# ---------------------------------------------------------------------------
# 3. API metadata
# ---------------------------------------------------------------------------

def test_capabilities_route_is_registered_and_derived():
    import ai_chat_api

    paths = {route.path for route in ai_chat_api.router.routes}
    assert "/api/ai/chat/capabilities" in paths
    tree = ast.parse((BACKEND / "ai_chat_api.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "chat_capabilities")
    assert "ai_capabilities.payload" in ast.unparse(fn)


def test_capabilities_route_answers_per_role():
    import ai_chat_api
    from tenancy import Role, TenantContext

    def ctx(role):
        return TenantContext(
            tenant_id="00000000-0000-0000-0000-000000000001",
            agent_id="a@example.test", user_id="00000000-0000-0000-0000-0000000000aa",
            role=role,
        )

    owner = asyncio.run(ai_chat_api.chat_capabilities(ctx(Role.BROKER_OWNER)))
    agent = asyncio.run(ai_chat_api.chat_capabilities(ctx(Role.AGENT)))
    by_id = lambda body: {a["id"]: a for a in body["areas"]}  # noqa: E731
    assert by_id(owner)["workspace_admin"]["state"] == caps.AVAILABLE
    assert by_id(agent)["workspace_admin"]["role_limited"] is True
    assert owner["version"] == caps.REGISTRY_VERSION
    assert {s["state"] for s in owner["states"]} == set(caps.STATES)
    assert any("MLS" in item for item in owner["not_offered"])


# ---------------------------------------------------------------------------
# 4. UI snapshot
# ---------------------------------------------------------------------------

def test_ui_snapshot_is_current():
    path = (BACKEND / caps.SNAPSHOT_PATH).resolve()
    committed = json.loads(path.read_text())
    assert committed == caps.snapshot(), (
        "oracle-app/src/lib/assistantCapabilities.json is stale: run "
        "`python ai_capabilities.py --write-snapshot` from backend/"
    )
