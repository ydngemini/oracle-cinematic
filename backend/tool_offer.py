"""What this turn's model is offered, and why the rest is not (prompt 07,
T07.04, AC07.01; ledger L07-03).

The builders in ai_chat_agent (offered_tool_names / _tool_config /
_foundry_tools / _local_tools) decide what the RELEASE offers for a selected
record: tools that exist, have a handler and a risk class (L07-01). This module
subtracts what THIS actor cannot use right now and says why — once, in the
system prompt, and again in the executor's refusal — so the model tells the
person "texting needs setup" or "that report is the broker-owner's" instead of
staging a request that can go nowhere or calling a tool that refuses it.

Two kinds of reason, both read from where they are already decided:

  permission    the broker-owner reads (tool_capabilities ``owner_only``, from
                ai_tools_read.OPS_ROLE_TOOLS) are not offered to an agent. The
                handler's own role check stays as the second line.
  integration   draft_sms / draft_email / call_contact / schedule_event are not
                offered while the brokerage has definitively not set up that
                channel — channel_setup.gaps, which mirrors the preconditions
                the approved command itself fails closed on, never re-decided
                here.

Unknown is not "missing" (C6): when the channel state cannot be read (no pool,
a timeout, an error) nothing is withheld for it, and a staged request fails
honestly later exactly as it did before this module existed.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

logger = logging.getLogger("oracle.tool_offer")

#: Which offered tool each delivery channel stands behind. Keys are the
#: channels channel_setup.gaps reports.
CHANNEL_TOOLS: dict[str, tuple[str, ...]] = {
    "sms": ("draft_sms",),
    "email": ("draft_email",),
    "voice": ("call_contact",),
    "calendar": ("schedule_event",),
}

PROMPT_LEAD = "TOOLS NOT OFFERED IN THIS CONVERSATION"
_GAPS_TIMEOUT_S = 3.0
_REMEMBERED_TURNS = 2048


@dataclass(frozen=True)
class ToolOffer:
    context_type: Optional[str]
    offered: frozenset[str]
    #: tool name -> one sentence naming the missing permission or integration.
    withheld: Mapping[str, str] = field(default_factory=dict)

    def prompt_note(self) -> str:
        """The block appended to the system prompt. Empty when nothing is withheld."""
        if not self.withheld:
            return ""
        lines = [PROMPT_LEAD, "If asked for one of these, say what it needs; never claim to have done it "
                              "or pretend the capability does not exist."]
        for tool in sorted(self.withheld):
            lines.append(f"- {tool}: {self.withheld[tool]}")
        return "\n".join(lines)

    def refusal(self, tool: str) -> Optional[str]:
        """The executor's message for a withheld tool, or None when it is offered."""
        if tool in self.offered:
            return None
        reason = self.withheld.get(tool)
        if reason:
            return f"{tool} is not offered in this conversation: {reason}"
        return f"{tool} is not available for this conversation."


def ops_role(ctx: Any) -> bool:
    """Whether this actor may use the broker-owner reads (ai_tools_read._require_ops_role)."""
    from tenancy import Role

    role = getattr(ctx, "role", None)
    return role is Role.BROKER_OWNER or role is Role.PLATFORM_ADMIN


def _owner_only_tools() -> frozenset[str]:
    from tool_capabilities import registry

    return frozenset(name for name, cap in registry().items() if cap.owner_only)


def for_actor(ctx: Any, context_type: Optional[str],
              channel_gaps: Optional[Mapping[str, str]] = None) -> ToolOffer:
    """The release's offer for this record, minus what this actor cannot use."""
    import ai_chat_agent

    base = frozenset(ai_chat_agent.offered_tool_names(context_type))
    withheld: dict[str, str] = {}
    if not ops_role(ctx):
        # Says what this session lacks, never who the person "is": a job queued
        # without a recorded role runs as an agent by default, not by proof.
        for tool in base & _owner_only_tools():
            withheld[tool] = ("limited to the broker-owner (it reports on the whole workspace); "
                              "this session does not carry the broker-owner role.")
    for channel, reason in (channel_gaps or {}).items():
        for tool in CHANNEL_TOOLS.get(channel, ()):
            if tool in base and reason:
                withheld[tool] = str(reason)
    return ToolOffer(context_type, base - frozenset(withheld), withheld)


async def channel_gaps(ctx: Any) -> dict[str, str]:
    """The channels this actor definitively cannot use yet, with the setup each
    needs. {} when unknown — never raises, never blocks a turn for long."""
    from db.connection import get_pool

    if get_pool() is None:
        return {}
    try:
        import channel_setup

        gaps = await asyncio.wait_for(channel_setup.gaps(ctx), timeout=_GAPS_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — unknown setup state withholds nothing
        logger.debug("channel setup state unavailable for this turn", exc_info=True)
        return {}
    return {str(k): str(v) for k, v in (gaps or {}).items() if v}


async def for_turn(ctx: Any, context_type: Optional[str]) -> ToolOffer:
    return for_actor(ctx, context_type, await channel_gaps(ctx))


# One entry per assistant turn, keyed by (tenant, turn id), so the executor —
# whose signature is fixed by its many callers — answers with the same offer the
# model was shown. A turn id is never reused; the oldest entries simply fall off.
_TURNS: "OrderedDict[tuple[str, str], ToolOffer]" = OrderedDict()


def remember(ctx: Any, message_id: str, offer: ToolOffer) -> None:
    key = (str(getattr(ctx, "tenant_id", "")), str(message_id))
    _TURNS[key] = offer
    _TURNS.move_to_end(key)
    while len(_TURNS) > _REMEMBERED_TURNS:
        _TURNS.popitem(last=False)


def current(ctx: Any, message_id: Optional[str], context_type: Optional[str]) -> ToolOffer:
    """The offer the model was shown for this turn, or — for a call made outside
    a remembered turn — the role-only offer (channel state unknown)."""
    offer = _TURNS.get((str(getattr(ctx, "tenant_id", "")), str(message_id)))
    if offer is None or offer.context_type != context_type:
        offer = for_actor(ctx, context_type)
    return offer
