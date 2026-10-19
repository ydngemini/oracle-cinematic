"""A correction withdraws what the previous turn staged (prompt 05, F05.04,
T05.04 remainder; ledger L05-07).

"Text Sarah about the open house" stages a text to the Sarah the model
picked; "actually, the other Sarah" used to leave that first text waiting in
the approval queue, one tap from going to the wrong person. Now, when a chat
turn opens with a correction, every command the PREVIOUS turn staged that is
still awaiting approval and was requested by this same agent is withdrawn
through the same path as the Reject button (commands_api.withdraw_command),
and the model is told to say so. Nothing that already left (approved,
accepted, sent) is touched, and nothing from earlier turns or other people.

The cue is deterministic and deliberately narrow (English and Spanish, the
launch languages): the correction words have to open the message. A missed
cue leaves the staged request for a human to reject, as before; a false cue
withdraws a request the agent can stage again — the cheaper error.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping

from tenancy import TenantContext

logger = logging.getLogger("oracle.correction")

REASON = "Withdrawn: the agent corrected this request in the next message."

_CUE = re.compile(
    r"""^\s*(?:
        (?:no[,.!]?\s+)?(?:actually|i\s+meant|i\s+mean|sorry,?\s+i\s+meant|wait,?\s+(?:no|not))\b
      | no[,.!]?\s+(?:not\s+(?:her|him|them|that\s+one)|the\s+other)\b
      | (?:not\s+(?:her|him|them|that\s+one))[,.!]?\s
      | the\s+other\s+(?:one|\w+)\b
      | (?:no[,.!]?\s+)?(?:en\s+realidad|mejor\s+(?:no|a|la|el)|quise\s+decir|perd[oó]n,?\s+quise\s+decir)\b
      | no[,.!]?\s+(?:a\s+)?(?:la\s+otra|el\s+otro)\b
      | (?:la\s+otra|el\s+otro)\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def is_correction(text: str) -> bool:
    """True when a message opens by correcting the previous request."""
    return bool(text) and bool(_CUE.match(text))


def _previous_assistant_id(bundle: Mapping[str, Any]) -> str | None:
    messages = list(bundle.get("messages") or [])
    if messages and messages[-1].get("role") == "user":
        messages = messages[:-1]
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("id"):
            return str(message["id"])
    return None


def _last_user_text(bundle: Mapping[str, Any]) -> str:
    messages = bundle.get("messages") or []
    if messages and messages[-1].get("role") == "user":
        return str(messages[-1].get("content") or "")
    return ""


_STAGED_SQL = """
    SELECT tool_name, result, result_ciphertext, tenant_id AS result_ciphertext_tenant_id
      FROM ai_tool_operations
     WHERE tenant_id = $1::uuid AND assistant_id = $2::uuid AND status = 'completed'
       AND tool_name = ANY($3::text[])
"""


async def withdraw_superseded(ctx: TenantContext, bundle: Mapping[str, Any]) -> list[str]:
    """Withdraw what the previous turn staged when this turn corrects it.
    Returns the command ids withdrawn (empty when there was nothing to do).
    Never raises: a failure here leaves the requests for a human to reject."""
    if not is_correction(_last_user_text(bundle)):
        return []
    previous = _previous_assistant_id(bundle)
    if not previous:
        return []
    try:
        return await _withdraw(ctx, previous)
    except Exception:  # noqa: BLE001 — the turn goes on; the queue still holds them
        logger.warning("correction: staged requests not withdrawn", exc_info=True)
        return []


async def _withdraw(ctx: TenantContext, assistant_id: str) -> list[str]:
    import commands_api
    import sealed_text
    from ai_tool_policy import GATED_TOOLS
    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(_STAGED_SQL, ctx.tenant_id, assistant_id, sorted(GATED_TOOLS))
        rows = await sealed_text.open_rows(conn, rows, sealed_text.TOOL_RESULT)
    command_ids: list[str] = []
    for row in rows:
        receipt = row["result"]
        if isinstance(receipt, str):
            try:
                receipt = json.loads(receipt)
            except json.JSONDecodeError:
                continue
        if isinstance(receipt, dict) and receipt.get("command_id") and not receipt.get("reused_existing_request"):
            command_ids.append(str(receipt["command_id"]))
    withdrawn: list[str] = []
    for command_id in dict.fromkeys(command_ids):
        command = await commands_api._get_command(ctx, command_id)
        if not command or command["state"] != "awaiting_approval":
            continue
        if str(command.get("created_by") or "").lower() != ctx.agent_id.lower():
            continue
        await commands_api.withdraw_command(ctx, command, reason=REASON)
        withdrawn.append(command_id)
    if withdrawn:
        logger.info("correction withdrew %d staged request(s)", len(withdrawn))
    return withdrawn


def prompt_note(withdrawn: list[str]) -> str:
    """What the model is told, so the reply says it plainly."""
    if not withdrawn:
        return ""
    n = len(withdrawn)
    return (
        f"\n\n## WITHDRAWN BECAUSE OF THE AGENT'S CORRECTION\n"
        f"The agent corrected their previous request, so {n} request{'s' if n != 1 else ''} "
        "staged for approval in the previous turn "
        f"{'were' if n != 1 else 'was'} withdrawn and will not be sent. Say so in one short "
        "sentence before acting on the corrected request."
    )
