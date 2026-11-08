"""The versioned ToolCapability registry (prompt 07, T07.02, AC07.01; ledger
L07-02).

One entry per catalogued agent tool, DERIVED from the sources that already
decide each property — never restated:

    purpose, input schema   ai_chat_agent.TOOLS (the description and parameter
                            schema the model is shown); schema_version is a
                            hash of that schema, so a changed contract changes it
    effect, risk            ai_tool_policy.TOOL_RISK (read / internal edit /
                            external effect, and the ActionRisk class)
    confirmation            platform_policy.requires_approval: an approval
                            before anything happens; an internal edit applies
                            with an undoable receipt; a read needs none
    selected record         ai_chat_store.needs_selected_record
    idempotency / retry     ai_chat_store._execute_safe_tool: every effectful
                            call is claimed in the action ledger by (message,
                            call index, arguments hash) and a repeat replays
                            the recorded result; reads have no effect to protect
    offered, state          ai_chat_store.is_agent_tool_available and
                            ai_capabilities.tool_state (with and without the
                            broker-owner role); owner-only from OPS_ROLE_TOOLS
    area, audit category    ai_capabilities.AREAS (each tool is in one area)

Not derivable today and therefore None, listed in `unmeasured`: latency and
cost estimates (nothing measures them per tool), a per-tool timeout (the
pool's command timeout applies to all) and a verifier (no tool has one).
Writing a number there would be a design target presented as a measurement
(CONTRACT C6).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Optional

READ = "read"
INTERNAL_EDIT = "internal_edit"
EXTERNAL_EFFECT = "external_effect"

UNMEASURED = ("latency_ms_p95", "cost_estimate", "timeout_s", "verifier")


@dataclass(frozen=True)
class ToolCapability:
    name: str
    purpose: str
    schema_version: str
    area: str
    risk: str
    effect: str
    confirmation: str            # none | undoable_receipt | approval
    needs_selected_record: bool
    idempotency: str             # not_needed | action_ledger
    retry: str                   # safe | replay_recorded_result
    offered: bool
    owner_only: bool
    state_agent: str
    state_owner: str
    latency_ms_p95: Optional[float] = None
    cost_estimate: Optional[float] = None
    timeout_s: Optional[float] = None
    verifier: Optional[str] = None
    unmeasured: tuple[str, ...] = UNMEASURED


def _schema_version(spec: dict) -> str:
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _purpose(spec: dict) -> str:
    """ai_chat_agent._tool builds {"toolSpec": {name, description, inputSchema: {json}}}."""
    return str(spec["toolSpec"]["description"])


def _parameters(spec: dict) -> dict:
    return spec["toolSpec"]["inputSchema"]["json"]


@lru_cache(maxsize=1)
def registry() -> dict[str, ToolCapability]:
    from ai_capabilities import AREAS, tool_state
    from ai_chat_agent import TOOLS
    from ai_chat_store import is_agent_tool_available, needs_selected_record
    from ai_tool_policy import risk_for
    from ai_tools_read import OPS_ROLE_TOOLS
    from platform_policy import ActionRisk, requires_approval

    area_of = {tool: area.id for area in AREAS for tool in area.tools}
    out: dict[str, ToolCapability] = {}
    for name, spec in TOOLS.items():
        risk = risk_for(name)  # raises for an unclassified tool: never guessed
        if risk is ActionRisk.READ_ONLY:
            effect, confirmation = READ, "none"
        elif requires_approval(risk):
            effect, confirmation = EXTERNAL_EFFECT, "approval"
        else:
            effect, confirmation = INTERNAL_EDIT, "undoable_receipt"
        out[name] = ToolCapability(
            name=name,
            purpose=_purpose(spec),
            schema_version=_schema_version(_parameters(spec)),
            area=area_of.get(name, ""),
            risk=risk.value,
            effect=effect,
            confirmation=confirmation,
            needs_selected_record=needs_selected_record(name),
            idempotency="not_needed" if effect == READ else "action_ledger",
            retry="safe" if effect == READ else "replay_recorded_result",
            offered=is_agent_tool_available(name),
            owner_only=name in OPS_ROLE_TOOLS,
            state_agent=tool_state(name, ops_role=False)[0],
            state_owner=tool_state(name, ops_role=True)[0],
        )
    return out


def registry_version() -> str:
    """Changes whenever any entry changes (a schema, a risk class, an offer)."""
    entries = [asdict(c) for c in sorted(registry().values(), key=lambda c: c.name)]
    return "tools/" + _schema_version({"entries": entries})


def as_dict() -> dict:
    return {"version": registry_version(),
            "tools": {n: asdict(c) | {"unmeasured": list(c.unmeasured)} for n, c in sorted(registry().items())}}
