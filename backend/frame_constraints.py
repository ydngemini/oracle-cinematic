"""frame_constraints — what a person said must NOT happen, enforced by the server.

Prompt 05 (T05.04, T05.07; AC05.04). A `SemanticFrame` records "don't message
her yet", "just draft it", "after the manager signs off", "not before Monday"
and "don't contact the Hendersons". This module turns those into
`ActiveConstraint` rows and checks them at the two places an action can start:

* the agent tool chokepoint, `ai_chat_store.execute_safe_tool` (07), and
* a mission step, `missions.policy.evaluate_action` (08).

The model never enforces anything here. It is told the constraints so it can
explain them, but a tool call or a mission step that would break one is
refused by this code, with a reason, whatever the model wrote.

What each constraint means, everywhere (one table, so chat and missions cannot
disagree):

    no_send            nothing that contacts a person is prepared or sent —
                       no draft email/text, call, calendar invite or tour
                       share. Missions withhold every email/sms/voice step.
    do_not_contact     the same, for the named person (or everyone, when the
                       person could not be pinned down — see `unresolved`).
    draft_only         drafts for approval are fine (email, text, contract);
                       anything else that reaches outside is refused. A
                       mission may stage drafts but never release them, and
                       never stages a call.
    approval_required  nothing applies without a person's approval: direct
                       CRM edits are refused (they apply at once), approval-
                       gated requests are fine. A mission never self-releases.
    not_before         before the time, nothing effectful happens. A mission
                       step due earlier is moved to that time.

Fail closed. A scoped constraint whose person could not be resolved applies
to everyone; a tool whose target cannot be determined counts as hitting the
scoped person; a stored constraint list that cannot be read becomes "no send,
approval required" for everyone.

Nothing here reads or writes the database, and nothing here holds a name, an
address or the person's words: constraints carry record ids, kinds and times.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ai_tool_policy import READ_ONLY_TOOLS, TOOL_RISK
from platform_policy import ActionRisk, requires_approval

logger = logging.getLogger(__name__)

ENV_FLAG = "ORACLE_SEMANTIC_FRAME_ENABLED"
_OFF = frozenset({"0", "false", "off", "no"})


def enabled() -> bool:
    """ON unless explicitly switched off (owner, 2026-10-14).

    Unset means on. Only a recognisable "off" value turns it off: a typo keeps
    the safety layer running rather than silently dropping it.
    """
    raw = (os.getenv(ENV_FLAG) or "").strip().lower()
    return raw not in _OFF


#: A chat constraint stops applying this long after the turn that stated it.
#: Long enough to cover "don't message her yet ... ok, draft the email" in one
#: sitting; short enough that yesterday's "no sends" does not silently block
#: today's work. Missions keep theirs for the life of the mission.
DISCOURSE_TTL = timedelta(minutes=30)
MAX_ACTIVE = 12

Kind = Literal["no_send", "draft_only", "approval_required", "not_before", "do_not_contact"]
KINDS: tuple[str, ...] = Kind.__args__

#: Tools that contact or reach a person (07's risk classes, not a second list).
CONTACT_RISKS = frozenset({
    ActionRisk.OUTREACH, ActionRisk.LIVE_CALL, ActionRisk.CALENDAR_WRITE,
    ActionRisk.BIDDING_MESSAGE,
})
#: Gated tools whose result is a draft a person reviews before anything
#: happens. Allowed under draft_only; everything else gated is refused.
DRAFT_TOOLS = frozenset({
    "draft_email", "draft_sms", "draft_contract", "generate_contract",
    "generate_assignment_agreement",
})
#: Mission channels that reach a person.
OUTBOUND_CHANNELS = frozenset({"email", "sms", "voice"})
#: Mission channels that produce a draft (a call is not a draft).
DRAFT_CHANNELS = frozenset({"email", "sms"})

#: Goals whose negation means "do not reach out", even when the parser forgot
#: the matching constraint ("Don't call her" with no do_not_contact).
_EXTERNAL_GOALS = frozenset({"send_message", "call", "schedule"})
_BLOCKING = frozenset({"no_send", "draft_only", "do_not_contact"})


class ActiveConstraint(BaseModel):
    """One standing restriction. Machine-readable; no free text."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Kind
    #: Record ids it is about. None = everyone.
    records: Optional[tuple[str, ...]] = None
    #: True when the person meant someone specific but the frame could not
    #: say who; the constraint then applies to everyone.
    unresolved: bool = False
    not_before: Optional[datetime] = None
    #: Where it came from: an assistant turn id, or "mission".
    origin: str = Field(min_length=1, max_length=64)
    stated_at: datetime

    @field_validator("not_before", "stated_at")
    @classmethod
    def _aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and v.utcoffset() is None:
            raise ValueError("a constraint time needs an offset")
        return v

    def applies_to(self, targets: frozenset[str]) -> bool:
        if self.records is None:
            return True
        # A call whose target cannot be determined might be the named person.
        if not targets:
            return True
        return bool(targets & set(self.records))


@dataclass(frozen=True)
class Refusal:
    """Why an action was refused. `code` is stable and goes to the ledger;
    `message` is product language and names nobody."""
    kind: str
    code: str
    message: str

    def receipt(self) -> dict[str, Any]:
        return {
            "ok": False,
            "refused": True,
            "constraint": self.kind,
            "error": self.message,
        }


def _who(c: ActiveConstraint) -> str:
    if c.unresolved:
        return "the person you meant (I could not tell who, so all contact is held)"
    if c.records is None:
        return "anyone"
    return "this person"


def _when(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# From a frame
# ---------------------------------------------------------------------------

def from_frame(frame: Any, *, origin: str, stated_at: datetime) -> list[ActiveConstraint]:
    """The constraints a SemanticFrame states.

    A scoped constraint is scoped only to candidates `authorize_frame` kept.
    On an unauthorised frame no candidate counts, so every constraint becomes
    unscoped: a restriction is never narrowed by an identity nobody checked.
    """
    authorized = bool(getattr(frame, "authorized", False))
    out: list[ActiveConstraint] = []

    def scoped(mention_index: Optional[int]) -> tuple[Optional[tuple[str, ...]], bool]:
        if mention_index is None:
            return None, False
        if not authorized or mention_index >= len(frame.mentions):
            return None, True
        ids = tuple(sorted({
            str(c.id) for c in frame.mentions[mention_index].candidates
            if c.access is not None
        }))
        return (ids, False) if ids else (None, True)

    for c in frame.constraints:
        if c.kind not in KINDS:
            continue  # "other" carries no machine meaning
        records, unresolved = scoped(c.mention)
        not_before = None
        if c.kind == "not_before":
            if c.time is None or c.time >= len(frame.times):
                continue  # the frame validator already refuses this
            not_before = frame.times[c.time].start
        out.append(ActiveConstraint(
            kind=c.kind, records=records, unresolved=unresolved,
            not_before=not_before, origin=origin, stated_at=stated_at,
        ))

    if not any(c.kind in _BLOCKING for c in out):
        # "Don't call her" with the constraint forgotten still means don't.
        if any(g.negated and g.label in _EXTERNAL_GOALS for g in frame.goals):
            out.append(ActiveConstraint(
                kind="no_send", origin=origin, stated_at=stated_at,
            ))
    return _dedupe(out)


def lifts(frame: Any) -> Optional[frozenset[str]]:
    """What an explicit new request to reach out lifts, or None.

    "Don't message her yet" ... later "OK, text her now": the newest
    instruction wins for the blocking kinds (no_send, draft_only,
    do_not_contact). Returns the record ids the new request is about
    (empty = it named nobody, which lifts only unscoped constraints).
    approval_required and not_before are never lifted by a later sentence:
    they end by expiry or by the time passing.
    """
    if frame is None or not getattr(frame, "authorized", False):
        return None
    if any(c.kind in _BLOCKING for c in frame.constraints):
        return None
    if not any((not g.negated) and g.label in _EXTERNAL_GOALS for g in frame.goals):
        return None
    return frozenset(
        str(c.id) for m in frame.mentions for c in m.candidates if c.access is not None
    )


def merge(
    standing: Iterable[ActiveConstraint],
    new: Iterable[ActiveConstraint],
    *,
    lifted_for: Optional[frozenset[str]],
    now: datetime,
    ttl: Optional[timedelta] = DISCOURSE_TTL,
) -> list[ActiveConstraint]:
    """Standing constraints, aged and amended by this turn, plus its own.

    A clarification amends; it never stacks a contradictory action: a lift
    removes the blocking constraint it contradicts rather than leaving both.
    """
    kept: list[ActiveConstraint] = []
    for c in standing:
        if ttl is not None and c.stated_at + ttl < now:
            continue
        if c.kind == "not_before" and c.not_before is not None and c.not_before <= now:
            continue
        if lifted_for is not None and c.kind in _BLOCKING:
            if c.records is None or (set(c.records) & lifted_for):
                continue
        kept.append(c)
    merged = _dedupe(kept + list(new))
    if len(merged) > MAX_ACTIVE:
        # Keep the newest. Dropping a restriction is a loss of safety, so it
        # is logged — by count and kind, never by person.
        logger.warning("frame constraints: %d active, keeping the newest %d",
                       len(merged), MAX_ACTIVE)
        merged = sorted(merged, key=lambda c: c.stated_at)[-MAX_ACTIVE:]
    return merged


def _dedupe(items: Sequence[ActiveConstraint]) -> list[ActiveConstraint]:
    seen: dict[tuple, ActiveConstraint] = {}
    for c in items:
        key = (c.kind, c.records, c.not_before)
        if key not in seen or seen[key].stated_at < c.stated_at:
            seen[key] = c
    return list(seen.values())


# ---------------------------------------------------------------------------
# Storage shape
# ---------------------------------------------------------------------------

def dump(items: Iterable[ActiveConstraint]) -> list[dict[str, Any]]:
    return [c.model_dump(mode="json") for c in items]


def load(raw: Any, *, origin: str = "unreadable") -> list[ActiveConstraint]:
    """Parse a stored list. Unreadable input fails CLOSED: a restriction that
    was recorded and cannot be read is treated as the strictest one."""
    if raw is None:
        return []
    try:
        if not isinstance(raw, list):
            raise ValueError("not a list")
        return [ActiveConstraint.model_validate(item) for item in raw]
    except (ValidationError, ValueError, TypeError):
        logger.warning("frame constraints: a stored list could not be read; failing closed")
        now = datetime.now(timezone.utc)
        return [
            ActiveConstraint(kind="no_send", origin=origin[:64], stated_at=now),
            ActiveConstraint(kind="approval_required", origin=origin[:64], stated_at=now),
        ]


# ---------------------------------------------------------------------------
# The tool boundary (07)
# ---------------------------------------------------------------------------

_TARGET_KEYS = ("client_id", "lead_id", "contact_id", "deal_id", "listing_id",
                "transaction_id", "property_id")


def tool_targets(tool_input: Any, context_type: Optional[str],
                 context_id: Optional[str]) -> frozenset[str]:
    """Every record id a tool call could act on: the selected record plus any
    id the call names. Over-inclusive on purpose — a scoped constraint
    matches if ANY of them is the person."""
    ids: set[str] = set()
    if context_type and context_id:
        ids.add(str(context_id))
    if isinstance(tool_input, dict):
        for key in _TARGET_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                ids.add(value.strip())
    return frozenset(ids)


def check_tool(
    constraints: Sequence[ActiveConstraint],
    tool_name: str,
    *,
    targets: frozenset[str],
    now: datetime,
) -> Optional[Refusal]:
    """None if the call may run; otherwise the first constraint it breaks."""
    if not constraints or tool_name in READ_ONLY_TOOLS:
        return None
    risk = TOOL_RISK.get(tool_name)
    if risk is None or risk is ActionRisk.READ_ONLY:
        # Unclassified names never reach here (execute_safe_tool's offered
        # list); codebase_summary/web_search are reads.
        return None
    gated = requires_approval(risk)
    contacts = risk in CONTACT_RISKS

    for c in constraints:
        if not c.applies_to(targets):
            continue
        if c.kind == "do_not_contact" and contacts:
            return Refusal(c.kind, "FRAME_DO_NOT_CONTACT",
                           f"You asked me not to contact {_who(c)}, so I did not "
                           f"prepare this. Nothing was drafted or sent.")
        if c.kind == "no_send" and contacts:
            return Refusal(c.kind, "FRAME_NO_SEND",
                           "You asked me not to send anything yet, so I did not "
                           "prepare this message or request. Tell me when it "
                           "should go and I will draft it for your approval.")
        if c.kind == "draft_only" and gated and tool_name not in DRAFT_TOOLS:
            return Refusal(c.kind, "FRAME_DRAFT_ONLY",
                           "You asked for drafts only. This would request an "
                           "action rather than a draft, so I did not stage it.")
        if c.kind == "approval_required" and not gated:
            return Refusal(c.kind, "FRAME_APPROVAL_REQUIRED",
                           "You asked for this to wait for approval. This change "
                           "would apply immediately, so I did not make it.")
        if (c.kind == "not_before" and c.not_before is not None
                and now < c.not_before):
            return Refusal(c.kind, "FRAME_NOT_BEFORE",
                           f"You asked me not to do this before "
                           f"{_when(c.not_before)}, so nothing was changed or "
                           f"staged. Ask me again then.")
    return None


# ---------------------------------------------------------------------------
# The mission step boundary (08)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StepGate:
    """What the stated constraints say about one mission step."""
    withhold: Optional[str] = None      # blocked_reason; the step is not taken
    defer_until: Optional[datetime] = None
    no_release: Optional[str] = None    # may stage, never self-release
    kind: Optional[str] = None


def check_mission_step(
    constraints: Sequence[ActiveConstraint],
    channel: str,
    *,
    subject_ids: frozenset[str],
    now: datetime,
) -> StepGate:
    outbound = channel in OUTBOUND_CHANNELS
    no_release: Optional[str] = None
    no_release_kind: Optional[str] = None
    defer: Optional[datetime] = None
    for c in constraints:
        if not c.applies_to(subject_ids):
            continue
        if c.kind == "do_not_contact" and outbound:
            return StepGate(withhold=f"stated constraint: do not contact {_who(c)}",
                            kind=c.kind)
        if c.kind == "no_send" and outbound:
            return StepGate(withhold="stated constraint: no sends", kind=c.kind)
        if c.kind == "draft_only" and outbound:
            if channel not in DRAFT_CHANNELS:
                return StepGate(withhold="stated constraint: drafts only (a call is not a draft)",
                                kind=c.kind)
            no_release, no_release_kind = "stated constraint: drafts only", c.kind
        if c.kind == "approval_required" and no_release is None:
            no_release, no_release_kind = "stated constraint: approval required", c.kind
        if (c.kind == "not_before" and c.not_before is not None
                and now < c.not_before):
            defer = max(defer, c.not_before) if defer else c.not_before
    if defer is not None:
        return StepGate(defer_until=defer, kind="not_before")
    return StepGate(no_release=no_release, kind=no_release_kind)


# ---------------------------------------------------------------------------
# What the model is told (it explains; the server enforces)
# ---------------------------------------------------------------------------

_PLAIN = {
    "no_send": "do not send or prepare any message, call, invite or share",
    "do_not_contact": "do not contact the person concerned in any way",
    "draft_only": "drafts for approval only; request no other action",
    "approval_required": "nothing may be applied without approval; make no direct record changes",
    "not_before": "do nothing effectful before {t}",
}


def prompt_block(constraints: Sequence[ActiveConstraint]) -> str:
    """A short, PII-free note for the system prompt."""
    if not constraints:
        return ""
    lines = []
    for c in constraints:
        text = _PLAIN[c.kind].format(t=_when(c.not_before) if c.not_before else "")
        scope = " (applies to everyone)" if c.records is None else " (a specific record)"
        lines.append(f"- {text}{scope}")
    return (
        "## STATED CONSTRAINTS — ENFORCED BY THE SERVER\n"
        "The person set these limits in this conversation. Tool calls that break "
        "one are refused by the server whatever you request, so do not attempt "
        "them; say plainly what you did not do and why.\n" + "\n".join(lines)
    )
