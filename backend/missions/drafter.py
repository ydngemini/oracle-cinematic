"""The message itself — the last thing a mission was missing.

The planner decides WHO, on WHICH channel, and WHEN, and writes a one-line
intent for the agent. It deliberately does not write the message: sequencing
and composing are different jobs, and a planner that also drafts tends to
produce forty variations of one sentence.

This writes the message, under the same containment the planner has:

* **Structured output or nothing.** The gateway refuses providers that cannot
  honour a schema, so a provider that would return prose is skipped rather than
  parsed. Two unusable answers raise `DraftUnavailable` and the action is
  recorded as blocked — never sent with a placeholder body.

* **It is given only what is already known.** A name, a channel, the agent's
  own objective and the one-line intent. It is not given the property record,
  because anything it is shown it may repeat, and a message that states a fact
  about someone's home is a message that can state a WRONG fact about someone's
  home under the agent's licence.

* **The prompt forbids inventing specifics** — no prices, no dates, no square
  footage, no claims about the market — and a check after the fact refuses a
  draft containing a currency amount, because that is the single most damaging
  thing to get wrong and the cheapest to detect.

The result still goes to `stage_command`, still creates an approval row, and
still passes `guard_outreach` in the worker. This module composes text; it
sends nothing.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tenancy import TenantContext

logger = logging.getLogger(__name__)

#: SMS is billed and read in one glance. Above this a carrier splits it into
#: multiple segments, which costs more and reads worse.
SMS_MAX = 320
EMAIL_SUBJECT_MAX = 120
EMAIL_BODY_MAX = 1500

#: Generous on purpose. The message itself is a few hundred characters, but a
#: reasoning model spends its budget thinking BEFORE it emits content, and a
#: budget sized to the answer produces an empty completion with
#: finish_reason='length'. The gateway correctly treats that as a failure, so a
#: tight budget here reads as "no model could write the message" — which is how
#: this was found, on a live run against Fireworks and Azure.
DRAFT_TOKEN_BUDGET = 2048

#: A number with a currency marker. Prices are the most damaging thing for a
#: model to invent here and the easiest to catch, so they are refused outright
#: rather than trusted to the prompt.
MONEY = re.compile(r"[$£€]\s?\d|(?<![\w.])\d[\d,]{2,}(?:\.\d+)?\s?(?:k|K|dollars|USD)\b")


class DraftUnavailable(RuntimeError):
    """No usable message could be composed. The action is blocked, not sent."""


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(default="", max_length=EMAIL_SUBJECT_MAX)
    body: str = Field(min_length=1, max_length=EMAIL_BODY_MAX)


DRAFT_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "outreach_draft",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["subject", "body"],
            "properties": {
                "subject": {"type": "string", "maxLength": EMAIL_SUBJECT_MAX},
                "body": {"type": "string", "maxLength": EMAIL_BODY_MAX},
            },
        },
    },
}

SYSTEM = (
    "You write one short outreach message for a real-estate agent, in their "
    "voice, to one person.\n\n"
    "Rules, all of them hard:\n"
    "- State no specifics you were not given. No prices, no valuations, no "
    "dates, no square footage, no claims about the market or their home.\n"
    "- Do not promise anything on the agent's behalf.\n"
    "- Write to open a conversation, not to close one.\n"
    "- No subject line for a text message; leave `subject` empty.\n"
    "- Plain sentences. No emoji, no exclamation marks, no merge-field "
    "placeholders like {name} — you are given the name, use it.\n\n"
    "This message is sent under the agent's own licence and cannot be "
    "un-sent, so a message you are unsure about is worse than no message."
)


async def draft_message(
    ctx: TenantContext,
    *,
    channel: str,
    recipient_name: str,
    objective: str,
    intent: str,
    agent_name: str = "",
) -> Draft:
    """Compose one message, or raise. Never returns a placeholder."""
    import llm_gateway

    limit = SMS_MAX if channel == "sms" else EMAIL_BODY_MAX
    prompt = (
        f"Channel: {channel}\n"
        f"To: {recipient_name or 'the client'}\n"
        f"From: {agent_name or 'their agent'}\n"
        f"What the agent is trying to achieve: {objective}\n"
        f"Why this person, now: {intent}\n"
        f"Hard limit: {limit} characters in the body."
    )

    last_error: Optional[str] = None
    attempt_prompt = prompt
    for attempt in (1, 2):
        try:
            raw = await llm_gateway.complete(
                attempt_prompt, task="analysis", system=SYSTEM,
                response_format=DRAFT_SCHEMA, max_tokens=DRAFT_TOKEN_BUDGET,
                temperature=0.4,
            )
        except Exception as exc:  # noqa: BLE001 — includes LLMUnavailable
            raise DraftUnavailable(f"no model could write the message: {exc}") from exc

        try:
            draft = Draft.model_validate_json(raw)
            problem = _refuse(draft, channel, limit)
            if problem:
                raise ValueError(problem)
            if channel == "sms":
                # A text has no subject; a model that supplies one is not wrong
                # so much as confused about the channel.
                draft = Draft(subject="", body=draft.body)
            return draft
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:400]
            logger.info("mission drafter: attempt %d unusable: %s", attempt, last_error)
            attempt_prompt = (
                f"{prompt}\n\nYour previous draft could not be used:\n"
                f"{last_error}\n\nWrite it again, obeying every rule."
            )

    raise DraftUnavailable(f"the drafter returned an unusable message twice: {last_error}")


def _refuse(draft: Draft, channel: str, limit: int) -> Optional[str]:
    """Why this draft cannot be sent. None means it stands."""
    body = draft.body.strip()
    if not body:
        return "the body is empty"
    if len(body) > limit:
        return f"the body is {len(body)} characters; the limit for {channel} is {limit}"
    if MONEY.search(body) or MONEY.search(draft.subject or ""):
        # The prompt forbids this; the check is here because the prompt is not
        # a guarantee and a wrong price under the agent's licence is the worst
        # single failure this feature can produce.
        return "it states a monetary figure, which this system does not have"
    if "{" in body or "}" in body:
        return "it contains an unfilled placeholder"
    if channel == "email" and not (draft.subject or "").strip():
        return "an email needs a subject"
    return None
