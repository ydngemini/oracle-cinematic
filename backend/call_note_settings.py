"""Per-agent choice of how AI call notes land (owner decision D-2026-10-12-03).

``review`` (the default) saves the note as *proposed* for the agent to approve,
edit or discard; ``direct`` saves it approved. The choice lives in the agent's
own ``agent_ai_settings.preferences`` under ``call_notes_mode``.
"""

from __future__ import annotations

from typing import Any, Optional

CALL_NOTE_MODES = ("review", "direct")
DEFAULT_CALL_NOTE_MODE = "review"


def normalize_mode(value: Any) -> str:
    return value if value in CALL_NOTE_MODES else DEFAULT_CALL_NOTE_MODE


async def read_mode(conn: Any, tenant_id: str, agent_id: Optional[str]) -> str:
    """The agent's chosen mode, or the default when unset or unknown."""
    if not agent_id:
        return DEFAULT_CALL_NOTE_MODE
    value = await conn.fetchval(
        """SELECT s.preferences->>'call_notes_mode'
             FROM agent_ai_settings s
             JOIN users u ON u.id = s.user_id
            WHERE s.tenant_id = $1::uuid AND lower(u.agent_id) = lower($2)""",
        str(tenant_id), agent_id,
    )
    return normalize_mode(value)


async def write_mode(conn: Any, tenant_id: str, agent_id: str, mode: str) -> str:
    """Set only ``call_notes_mode``; every other preference is preserved."""
    mode = normalize_mode(mode)
    user_id = await conn.fetchval(
        "SELECT id FROM users WHERE lower(agent_id) = lower($1)", agent_id)
    if user_id is None:
        raise LookupError("agent not found")
    await conn.execute(
        """INSERT INTO agent_ai_settings (tenant_id, user_id, preferences)
           VALUES ($1::uuid, $2, jsonb_build_object('call_notes_mode', $3::text))
           ON CONFLICT (tenant_id, user_id) DO UPDATE
              SET preferences = agent_ai_settings.preferences
                                || jsonb_build_object('call_notes_mode', $3::text),
                  updated_at = now()""",
        str(tenant_id), user_id, mode,
    )
    return mode
