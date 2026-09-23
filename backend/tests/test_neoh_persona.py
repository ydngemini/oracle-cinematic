"""Neoh's persona — one source, two densities, no stale facts.

These exist because the persona forked in production and nothing noticed. The
only assertion that guarded it was a single `"Never delete or archive" in
BASE_SYSTEM_PROMPT`, so the long prompt and the compact Foundry prompt drifted
in opposite directions for months: one gained the safety rules, the other
gained the capability-honesty rules, and which set a user got depended on which
provider rung answered their question.

The anti-drift test is `test_every_rule_layer_survives_both_densities`. If
someone adds a rule to one projection and not the other, that fails.
"""

from __future__ import annotations

import pytest

import neoh_persona as persona


# Identifiers from the disabled Azure subscription the prompt used to recite.
# Any of these reappearing means someone put deployment trivia back into a
# third-party model's context.
STALE_INFRA = (
    "Container Apps", "120ea104", "neoh-kv", "azurecr.io",
    "North Central", "neoh-kimi-k2-6", "Flexible Server",
)


def test_both_densities_are_produced():
    assert persona.build_system_prompt()
    assert persona.build_system_prompt(compact=True)


def test_compact_is_substantially_smaller():
    full = persona.build_system_prompt()
    compact = persona.build_system_prompt(compact=True)
    assert len(compact) < len(full) / 3


@pytest.mark.parametrize("compact", [False, True])
def test_every_rule_layer_survives_both_densities(compact):
    """The anti-drift ratchet.

    A rule that only applies on some provider rungs is not a rule. The compact
    projection may drop KNOWLEDGE; it may never drop IDENTITY, AUTHORITY or
    HONESTY.
    """
    prompt = persona.build_system_prompt(compact=compact)
    for layer in (persona.IDENTITY, persona.AUTHORITY, persona.HONESTY):
        for line in layer.splitlines():
            line = line.strip().lstrip("- ")
            if len(line) > 24:           # skip headers and short fragments
                assert line in prompt, f"missing from compact={compact}: {line[:60]}"


@pytest.mark.parametrize("compact", [False, True])
def test_authority_rules_are_present(compact):
    prompt = persona.build_system_prompt(compact=compact)
    assert "Never delete or archive data." in prompt
    assert "Undo is available" in prompt


@pytest.mark.parametrize("compact", [False, True])
def test_capability_honesty_rules_are_present(compact):
    """These lived only on the Foundry prompt, so the live Fireworks path had
    no rule against inventing MLS or billing data."""
    prompt = persona.build_system_prompt(compact=compact)
    assert "Only claim access to a capability" in prompt
    assert "Never invent MLS" in prompt


@pytest.mark.parametrize("compact", [False, True])
def test_neoh_does_not_claim_to_know_where_it_runs(compact):
    prompt = persona.build_system_prompt(compact=compact)
    assert "do not reliably know where you are deployed" in prompt


@pytest.mark.parametrize("compact", [False, True])
def test_no_stale_infrastructure_identifiers(compact):
    prompt = persona.build_system_prompt(compact=compact)
    for token in STALE_INFRA:
        assert token not in prompt, f"stale deployment trivia back in the prompt: {token}"


def test_the_retired_azure_knowledge_file_is_not_loaded():
    from pathlib import Path
    here = Path(persona.__file__).parent
    assert not (here / "NEOH_AZURE_DEPLOYMENT.md").exists()


def test_product_knowledge_carries_no_deployment_claims():
    text = persona.product_knowledge()
    for token in STALE_INFRA:
        assert token not in text


# ---------------------------------------------------------------------------
# Brokerage grounding
# ---------------------------------------------------------------------------

PROFILE = {
    "name": "Lockwood Realty",
    "org_type": "brokerage",
    "primary_state": "DE",
    "team": {"active_members": 4, "pending_invitations": 2},
    "capabilities": {
        "phone": "READY", "agent_invites": "READY",
        "mls": "NOT_STARTED", "billing": "NEEDS_ACTION", "readiness": "BLOCKED",
    },
}


def test_brokerage_block_names_the_business_and_market():
    block = persona.brokerage_block(PROFILE)
    assert "Lockwood Realty" in block
    assert "DE" in block
    assert "4 active member" in block


def test_brokerage_block_says_what_is_NOT_set_up():
    """The load-bearing half: it stops Neoh offering to text a lead from a
    tenant whose SMS rail was never configured."""
    block = persona.brokerage_block(PROFILE)
    assert "do not offer these" in block
    assert "mls" in block and "billing" in block


def test_brokerage_block_does_not_list_readiness_as_a_capability():
    block = persona.brokerage_block(PROFILE)
    assert "readiness" not in block


def test_brokerage_block_is_empty_without_a_profile():
    assert persona.brokerage_block(None) == ""
    assert persona.brokerage_block({}) == ""


def test_brokerage_block_survives_a_partial_profile():
    assert "Lockwood" in persona.brokerage_block({"name": "Lockwood"})


def test_brokerage_block_is_marked_as_data_not_instruction():
    block = persona.brokerage_block(PROFILE)
    assert "not instruction" in block


def test_grounding_reaches_the_composed_prompt():
    prompt = persona.build_system_prompt(brokerage=PROFILE)
    assert "Lockwood Realty" in prompt


# ---------------------------------------------------------------------------
# Grounding reaches every provider, not just the last one
# ---------------------------------------------------------------------------

import asyncio                                    # noqa: E402
from contextlib import asynccontextmanager        # noqa: E402

import ai_chat_agent                              # noqa: E402
from tenancy import Role, TenantContext           # noqa: E402

CTX = TenantContext(
    agent_id="owner@a.test",
    tenant_id="aaaaaaaa-0000-0000-0000-00000000000a",
    role=Role.BROKER_OWNER,
)


def _bundle(record=None):
    return {
        "messages": [],
        "record": record,
        "attachments": [],
        "assistant": {"context_type": None, "context_id": None},
    }


def _capture_prompt(monkeypatch, *, record=None, brokerage=PROFILE):
    """Run _generate far enough to see the system prompt it built."""
    seen = {}

    async def fake_brokerage(_ctx):
        return brokerage

    class FakeMemory:
        def __init__(self, _ctx): pass
        async def inject_jit_prompt(self, _agent, base): return base

    def fake_providers(*_a, **_k):
        # Synchronous in the real module: it returns a list of provider rungs.
        return []

    async def fake_local(_ctx, _bundle_, system_prompt, *a, **k):
        seen["prompt"] = system_prompt
        return ("ok", [])

    monkeypatch.setattr(ai_chat_agent, "_resolve_brokerage_context", fake_brokerage)
    monkeypatch.setattr(ai_chat_agent, "SessionManager", FakeMemory)
    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", fake_providers)
    monkeypatch.setattr(ai_chat_agent, "FIREWORKS_ENABLED", False)
    monkeypatch.setattr(ai_chat_agent, "AI_PROVIDER", "local")
    monkeypatch.setattr(ai_chat_agent, "_local_fallback", fake_local)
    asyncio.run(ai_chat_agent._generate(CTX, _bundle(record), "assistant-1"))
    return seen.get("prompt", "")


def test_the_selected_record_reaches_the_first_provider_tried(monkeypatch):
    """It used to be appended after the gateway and Fireworks branches had
    already returned, so on the live provider the model never saw the record
    the user had selected."""
    prompt = _capture_prompt(monkeypatch, record={"full_name": "Sarah Johnson"})
    assert "SELECTED RECORD" in prompt
    assert "Sarah Johnson" in prompt


def test_no_record_means_no_record_block(monkeypatch):
    prompt = _capture_prompt(monkeypatch, record=None)
    assert "SELECTED RECORD" not in prompt


def test_the_brokerage_reaches_the_first_provider_tried(monkeypatch):
    prompt = _capture_prompt(monkeypatch)
    assert "Lockwood Realty" in prompt


def test_a_turn_still_works_when_grounding_is_unavailable(monkeypatch):
    """Grounding that fails is grounding the turn does without — it must never
    be the reason a question goes unanswered."""
    prompt = _capture_prompt(monkeypatch, brokerage=None)
    assert "You are NEOH" in prompt
    assert "Lockwood Realty" not in prompt


def test_brokerage_resolution_swallows_a_broken_database(monkeypatch):
    @asynccontextmanager
    async def exploding_tx(_ctx):
        raise RuntimeError("database is down")
        yield  # pragma: no cover

    monkeypatch.setattr("db.connection.tenant_tx", exploding_tx)
    assert asyncio.run(ai_chat_agent._resolve_brokerage_context(CTX)) is None
