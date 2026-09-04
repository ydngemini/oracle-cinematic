"""The one place a model is in the loop — and what stops it mattering.

A mission needs a sequence: who to approach, on which channel, in what order,
how far apart. That is a judgement call, and it is the kind of judgement a
model is genuinely good at. So this module asks one.

Everything else here exists to make that safe:

* **The model chooses; it never invents.** It is handed a numbered list of
  candidates the deterministic side already found (opportunity_engine, intent
  and beliefs) and may only refer to them BY INDEX. A step naming a subject
  that was not in the list cannot survive validation, because there is nowhere
  for a name to go — the schema has an integer.

* **Code drops what the schema cannot.** An index out of range, a channel the
  mission is not allowed to use, a nonsensical delay: dropped after
  validation, each with an `excluded_reason` that names what was wrong. The
  plan that comes out is a subset of what was asked for, never a superset.

* **A failure is a failure.** The gateway is asked for structured output, and
  a provider that cannot honour it refuses rather than returning prose. If the
  model returns something unparseable it is asked once more with the error
  appended; if that fails too this raises PlanUnavailable and the mission stays
  in `draft`. There is no fallback plan. A fabricated sequence of who to phone
  is worse than an empty screen, because the empty screen is obviously empty.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tenancy import TenantContext

logger = logging.getLogger(__name__)

#: Never ask a model to rank more than this. The list is already ranked by the
#: deterministic side; the model is sequencing, not searching.
MAX_CANDIDATES = 40

#: A plan longer than this is not a plan, it is a campaign nobody reviewed.
MAX_STEPS = 60

CHANNELS = ("email", "sms", "voice", "task")


class PlanUnavailable(RuntimeError):
    """No plan could be obtained. The mission stays in draft."""


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Index into the candidate list as given. Not a name: a name could be
    #: invented, an index either points at a real row or does not.
    candidate: int = Field(ge=0)
    channel: str
    #: Whole days from launch. Integer so "sometime soon" cannot be encoded.
    day_offset: int = Field(ge=0, le=365)
    #: One line, shown to the agent. Never sent to anyone.
    intent: str = Field(min_length=1, max_length=280)


class MissionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[PlanStep] = Field(default_factory=list, max_length=MAX_STEPS)
    reasoning: str = Field(default="", max_length=2000)


PLAN_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "mission_plan",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["steps", "reasoning"],
            "properties": {
                "steps": {
                    "type": "array",
                    "maxItems": MAX_STEPS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["candidate", "channel", "day_offset", "intent"],
                        "properties": {
                            "candidate": {"type": "integer", "minimum": 0},
                            "channel": {"type": "string", "enum": list(CHANNELS)},
                            "day_offset": {"type": "integer", "minimum": 0, "maximum": 365},
                            "intent": {"type": "string", "maxLength": 280},
                        },
                    },
                },
                "reasoning": {"type": "string", "maxLength": 2000},
            },
        },
    },
}

SYSTEM = (
    "You sequence outreach for a real-estate agent. You are given an objective "
    "and a numbered list of people already selected by another system.\n\n"
    "Choose which of them to contact, on which channel, and how many days from "
    "now. Refer to people ONLY by their number. You cannot add anyone: if "
    "someone who should be contacted is not in the list, say so in `reasoning` "
    "rather than inventing an entry.\n\n"
    "Prefer fewer, better-timed touches. Do not schedule two contacts with the "
    "same person on the same day. Every message is sent under the agent's own "
    "licence, so a plan that would embarrass them is a bad plan."
)


async def propose_plan(
    ctx: TenantContext,
    mission: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Return (steps, dropped, reasoning).

    `steps` reference candidates by their id, resolved here. `dropped` records
    every step the model proposed that code refused, with the reason — so a
    plan that came back half the size it should have can be explained rather
    than merely observed.
    """
    import llm_gateway

    usable = (candidates or [])[:MAX_CANDIDATES]
    if not usable:
        return [], [], "No candidates matched this objective."

    allowed = _allowed_channels(mission)
    if not allowed:
        return [], [], "This mission has no channels it is allowed to use."

    prompt = _prompt(mission, usable, allowed)
    plan = await _ask(llm_gateway, prompt)

    steps: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()

    for index, step in enumerate(plan.steps):
        reason = _refuse(step, usable, allowed, seen)
        if reason:
            dropped.append({"step_index": index, "reason": reason, "proposed": step.model_dump()})
            continue
        seen.add((step.candidate, step.day_offset))
        candidate = usable[step.candidate]
        steps.append({
            "step_index": len(steps),
            "candidate_id": candidate.get("id"),
            "subject_type": candidate.get("subject_type"),
            "subject_id": candidate.get("subject_id"),
            "channel": step.channel,
            "day_offset": step.day_offset,
            "intent": step.intent.strip(),
        })

    if dropped:
        logger.info(
            "mission planner: kept %d of %d proposed steps (%s)",
            len(steps), len(plan.steps),
            ", ".join(sorted({d["reason"] for d in dropped})),
        )
    return steps, dropped, plan.reasoning


def _refuse(
    step: PlanStep,
    candidates: list[dict[str, Any]],
    allowed: set[str],
    seen: set[tuple[int, int]],
) -> Optional[str]:
    """Why code will not carry this step. None means it stands."""
    if step.candidate >= len(candidates):
        # The one that matters: a number pointing at nobody. The schema stops a
        # name being invented; this stops an index being invented.
        return f"names candidate {step.candidate}, which is not in the list"
    if step.channel not in allowed:
        return f"uses {step.channel}, which this mission is not allowed to use"
    if (step.candidate, step.day_offset) in seen:
        return "is a second contact with the same person on the same day"
    return None


def _allowed_channels(mission: dict[str, Any]) -> set[str]:
    allowed = mission.get("allowed_channels") or []
    return {c for c in allowed if c in CHANNELS}


def _prompt(
    mission: dict[str, Any], candidates: list[dict[str, Any]], allowed: set[str],
) -> str:
    lines = [
        f"Objective ({mission.get('objective_kind')}): {mission.get('objective_text')}",
    ]
    if mission.get("target_count"):
        lines.append(f"Target: {mission['target_count']}")
    if mission.get("deadline"):
        lines.append(f"Deadline: {mission['deadline']}")
    lines.append(f"Channels you may use: {', '.join(sorted(allowed))}")
    lines.append("")
    lines.append("People (refer to these by number only):")
    for i, candidate in enumerate(candidates):
        lines.append(f"  {i}. {_describe(candidate)}")
    return "\n".join(lines)


def _describe(candidate: dict[str, Any]) -> str:
    """What the model is told about one person. Deliberately thin: everything
    here is already known to the deterministic side, and anything the model is
    shown it may repeat."""
    bits = [str(candidate.get("label") or candidate.get("subject_id") or "unnamed")]
    if candidate.get("stage"):
        bits.append(str(candidate["stage"]))
    if candidate.get("why"):
        bits.append(str(candidate["why"]))
    score = candidate.get("score")
    if isinstance(score, (int, float)):
        bits.append(f"score {score:.2f}")
    return " — ".join(bits)


async def _ask(gateway, prompt: str) -> MissionPlan:
    """Ask once; on an unusable answer ask again with the error; then give up.

    The second attempt exists because a schema violation is often a near miss
    the model can correct when shown it. Two failures is a signal, not noise,
    and inventing a plan at that point would be the worst possible response.
    """
    attempt_prompt = prompt
    last_error: Optional[str] = None

    for attempt in (1, 2):
        try:
            raw = await gateway.complete(
                attempt_prompt,
                task="analysis",
                system=SYSTEM,
                response_format=PLAN_SCHEMA,
                max_tokens=2048,
                temperature=0.2,
            )
        except Exception as exc:  # noqa: BLE001 — including LLMUnavailable
            # A model that cannot be reached, or that refuses because no
            # provider honours structured output, IS "no plan could be
            # obtained". Found by running a tick on a stack with no litellm
            # installed: the exception escaped propose_plan's contract, past
            # the executor's `except PlanUnavailable`, and killed the tick.
            raise PlanUnavailable(f"the planner could not be reached: {exc}") from exc
        try:
            return MissionPlan.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:500]
            logger.warning("mission planner: attempt %d unusable: %s", attempt, last_error)
            attempt_prompt = (
                f"{prompt}\n\nYour previous answer could not be used:\n"
                f"{last_error}\n\nAnswer again, matching the schema exactly."
            )

    raise PlanUnavailable(
        f"The planner returned an unusable plan twice: {last_error}"
    )


def plan_summary(steps: list[dict[str, Any]], dropped: list[dict[str, Any]]) -> str:
    """One sentence for the journal and the UI."""
    if not steps and not dropped:
        return "No plan was produced."
    if not steps:
        return f"Every proposed step was refused ({len(dropped)} of {len(dropped)})."
    if dropped:
        return (
            f"{len(steps)} step{'s' if len(steps) != 1 else ''} planned; "
            f"{len(dropped)} refused."
        )
    return f"{len(steps)} step{'s' if len(steps) != 1 else ''} planned."
