"""The approved reason for an outbound AI call, the same for every voice
engine (audit 34.3).

An outbound call is placed only after a person approved a command; that
command's reason is the briefing (plivo_call_handler stores it in the call
state, capped at 800 characters). ElevenLabs put it in its prompt; the Qwen
engine did not, so a Qwen outbound call opened without knowing why it was
calling. Both now take the briefing from here and nothing beyond it: no
property fact, price or offer the approved reason does not contain.
"""

from __future__ import annotations

from typing import Any, Optional

BRIEFING_MAX_CHARS = 800

GOAL = ("Goal: find out whether they are interested in the home and, if so, whether they "
        "would like a showing.")
LIMIT = ("Say nothing about the home, its price, terms or any offer beyond what the reason "
         "above states; if they ask for more, their agent will follow up. You cannot book or "
         "hold times: their agent will follow up to confirm. You are the agent's automated "
         "assistant: never say you are the agent or speak as them; say you are calling on "
         "their behalf.")

# The approved command's fields that say why and about what the call is
# (34.3 review): the chat tool writes `reason`; a typed command writes
# `objective` and `property_address`. Openings and scripts are NOT passed:
# they are written in the agent's own voice ("Hi Ana, this is Sam Lee"), and
# the AI must never speak as the agent after disclosing it is automated.
_DRAFT_FIELDS = (
    ("reason", "{}"),
    ("objective", "Request: {}"),
    ("property_address", "Property: {}"),
)


def briefing_from_draft(draft: Optional[dict[str, Any]], *, on_behalf_of: str = "") -> str:
    """The briefing for a call, built only from the approved command's own
    fields (plus whose behalf the call is on), capped at BRIEFING_MAX_CHARS."""
    draft = draft or {}
    lines: list[str] = []
    seen: set[str] = set()
    for key, template in _DRAFT_FIELDS:
        value = " ".join(str(draft.get(key) or "").split())
        if value and value not in seen:
            seen.add(value)
            lines.append(template.format(value))
    who = " ".join(str(on_behalf_of or "").split())[:120]
    if who and lines:
        lines.insert(0, f"Calling on behalf of: {who}")  # first, so a long reason cannot cut it off
    return "\n".join(lines)[:BRIEFING_MAX_CHARS]


def approved_briefing(state: Optional[dict[str, Any]]) -> str:
    """The approved command's reason, trimmed and capped, or ''."""
    return str((state or {}).get("briefing") or "").strip()[:BRIEFING_MAX_CHARS]


def briefing_instructions(state: Optional[dict[str, Any]]) -> str:
    """The prompt addendum for an outbound call with an approved reason, or ''."""
    briefing = approved_briefing(state)
    if not briefing:
        return ""
    return (f"\n\nWhy you are calling (approved by the agent; do not add to it): {briefing}\n"
            f"{GOAL} {LIMIT}")


def on_behalf_of(identity: Optional[dict[str, Any]], login: str) -> str:
    """'<name> of <brokerage>' from an agent_profile.load_agent_identity()
    result. Its name falls back to the login (an email address), which must
    never be spoken on a call: a name equal to the login or containing '@' is
    dropped."""
    identity = identity or {}
    name = " ".join(str(identity.get("name") or "").split())
    if name.lower() == str(login or "").lower() or "@" in name:
        name = ""
    brokerage = " ".join(str(identity.get("brokerage") or "").split())
    return " of ".join(v for v in (name, brokerage) if v)
