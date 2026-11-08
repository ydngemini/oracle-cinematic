"""The ToolCapability registry (T07.02, AC07.01, L07-02): one entry per
catalogued tool, every field derived from the source that decides it, and
nothing unmeasured presented as a number."""

from __future__ import annotations

import pytest

import tool_capabilities as tc
from ai_chat_agent import TOOLS
from ai_chat_store import CONTEXT_FREE_TOOLS, is_agent_tool_available
from ai_tool_policy import TOOL_RISK
from ai_tools_read import OPS_ROLE_TOOLS
from platform_policy import ActionRisk, requires_approval

REG = tc.registry()


def test_every_catalog_tool_has_exactly_one_entry_and_a_risk_class():
    assert set(REG) == set(TOOLS) == set(TOOL_RISK)
    assert all(c.risk == TOOL_RISK[n].value for n, c in REG.items())


def test_purpose_and_schema_come_from_the_catalog_the_model_sees():
    for name, cap in REG.items():
        assert cap.purpose == TOOLS[name]["toolSpec"]["description"] and cap.purpose
    by_schema: dict[str, set[str]] = {}
    for name, cap in REG.items():
        schema = TOOLS[name]["toolSpec"]["inputSchema"]["json"]
        assert cap.schema_version == tc._schema_version(schema)
        by_schema.setdefault(cap.schema_version, set()).add(repr(sorted(schema["properties"])))
    # one version per distinct schema, never one for all
    assert all(len(props) == 1 for props in by_schema.values()) and len(by_schema) > 10


def test_effect_and_confirmation_follow_the_risk_class():
    for name, cap in REG.items():
        risk = TOOL_RISK[name]
        if risk is ActionRisk.READ_ONLY:
            assert (cap.effect, cap.confirmation, cap.idempotency) == ("read", "none", "not_needed")
        elif requires_approval(risk):
            assert (cap.effect, cap.confirmation) == ("external_effect", "approval")
        else:
            assert (cap.effect, cap.confirmation) == ("internal_edit", "undoable_receipt")
        if cap.effect != "read":
            assert cap.idempotency == "action_ledger" and cap.retry == "replay_recorded_result"


def test_offer_role_and_record_rules_match_the_runtime():
    for name, cap in REG.items():
        assert cap.offered is is_agent_tool_available(name)
        assert cap.owner_only is (name in OPS_ROLE_TOOLS)
        if cap.owner_only and cap.offered:
            assert cap.state_agent == "unavailable" != cap.state_owner
        if cap.effect == "read" or name in CONTEXT_FREE_TOOLS:
            assert not cap.needs_selected_record
        else:
            assert cap.needs_selected_record
    assert REG["call_contact"].needs_selected_record and REG["call_contact"].confirmation == "approval"


def test_every_tool_belongs_to_one_capability_area():
    assert all(c.area for c in REG.values())


def test_nothing_unmeasured_is_presented_as_a_number():
    for cap in REG.values():
        for field in tc.UNMEASURED:
            assert getattr(cap, field) is None and field in cap.unmeasured


def test_the_registry_version_moves_with_any_entry(monkeypatch):
    before = tc.registry_version()
    assert before.startswith("tools/")
    changed = dict(REG)
    changed["search_clients"] = tc.ToolCapability(**{**tc.asdict(REG["search_clients"]), "schema_version": "x"})
    monkeypatch.setattr(tc, "registry", lambda: changed)
    assert tc.registry_version() != before


@pytest.mark.parametrize("name", ["record_consent", "clear_suppression"])
def test_consent_writers_have_no_capability(name):
    assert name not in REG
