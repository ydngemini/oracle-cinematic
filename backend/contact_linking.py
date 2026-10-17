"""Texts and calls that arrived before their contact existed are linked to it
once it does (prompt 04, T04.13, AC04.01; ledger L04-04).

Inbound texts (messaging_data._match_contact) and calls (inbound_voice.
_match_contact) link only to a contact that already exists; neither creates
one. So a person who texted or called first and then came in through a lead
webhook got a contact and a client, while the text and the call stayed
attached to nobody for good — no timeline, no owner, no follow-up.

When lead intake settles a contact for a phone, this links that brokerage's
earlier unlinked inbound texts (same E.164 sender) and inbound calls (same
caller phone lookup hash) to it. Only rows with no contact yet are touched —
never one already linked to someone — and only in the intake's own tenant
transaction, so RLS applies. Counts are returned; numbers are never logged.
"""

from __future__ import annotations

from typing import Any, Optional

#: How far back an unlinked text or call is still the same conversation.
LINK_WINDOW_DAYS = 90

_SMS = """
    UPDATE sms_messages
       SET contact_id = $2::uuid, client_id = COALESCE(client_id, $3::uuid)
     WHERE tenant_id = $1::uuid AND contact_id IS NULL AND direction = 'inbound'
       AND from_e164 = $4 AND created_at > now() - make_interval(days => $5)
"""
_CALLS = """
    UPDATE inbound_voice_calls
       SET contact_id = $2::uuid, client_id = COALESCE(client_id, $3::uuid), updated_at = now()
     WHERE tenant_id = $1::uuid AND contact_id IS NULL
       AND caller_phone_lookup_hash = $4 AND created_at > now() - make_interval(days => $5)
"""


def _count(status: str) -> int:
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


async def link_earlier(conn: Any, *, tenant_id: str, contact_id: str, client_id: Optional[str],
                       phone_e164: Optional[str], phone_hash: Optional[str]) -> dict[str, int]:
    """Attach earlier unlinked inbound texts and calls from this phone."""
    linked = {"texts": 0, "calls": 0}
    if phone_e164:
        linked["texts"] = _count(await conn.execute(
            _SMS, tenant_id, contact_id, client_id, phone_e164, LINK_WINDOW_DAYS))
    if phone_hash:
        linked["calls"] = _count(await conn.execute(
            _CALLS, tenant_id, contact_id, client_id, phone_hash, LINK_WINDOW_DAYS))
    return linked
