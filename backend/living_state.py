"""living_state — a person's state is a fact, derived in one place.

A contact is not a row that looks the same forever. Dormant, engaged, on a
call, just off a call, under contract, closed: these are different situations
for the agent, and the object on screen should say which one before any words
do. This module is the ONE derivation. The frontend renders the result and
overlays only the fact it alone knows first — that its own softphone is
ringing (see ``oracle-app/src/neoh/livingModel.js``, which asserts its
vocabulary and thresholds equal to this file's).

Every state comes from something recorded:

* ``calling`` / ``after_call`` — ``agent_call_intents`` reached through the
  contact's ``legacy_client_id``;
* ``under_contract`` / ``closed`` — ``transactions``;
* ``engaged`` / ``quiet`` / ``dormant`` — recency and count of
  ``interaction_logs`` (which perception, sends and replies all write to).

Thresholds are stated priors, not fitted. They exist so the words on the card
are always a recorded time or count, never an adjective.

RLS scopes every query; there is no tenant predicate here on purpose.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from db.connection import tenant_tx
from tenancy import TenantContext

# Closed vocabulary, in ascending precedence. Keep in step with livingModel.js.
STATES = ("dormant", "quiet", "engaged", "closed", "under_contract", "after_call", "calling")

ENGAGED_DAYS = 7
DORMANT_DAYS = 45
AFTER_CALL_MINUTES = 30
CLOSED_RECENT_DAYS = 30
# A call intent that never completed stops counting as "calling" after this.
CALLING_STALE_MINUTES = 120

_ACTIVE_CALL_STATES = ("ringing", "in_progress")


@dataclass(frozen=True)
class LivingFacts:
    last_activity_at: Optional[datetime] = None
    signals_7d: int = 0
    transaction_id: Optional[str] = None
    transaction_status: Optional[str] = None
    closing_deadline: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    call_state: Optional[str] = None
    call_started_at: Optional[datetime] = None
    call_completed_at: Optional[datetime] = None


def derive(facts: LivingFacts, now: Optional[datetime] = None) -> dict[str, Any]:
    """Pure. Returns the living payload the API serves and the card renders."""
    now = now or datetime.now(timezone.utc)
    state = "dormant"
    since: Optional[datetime] = facts.last_activity_at

    if facts.last_activity_at is not None:
        age = now - facts.last_activity_at
        if age <= timedelta(days=ENGAGED_DAYS):
            state = "engaged"
        elif age <= timedelta(days=DORMANT_DAYS):
            state = "quiet"

    transaction = None
    if facts.transaction_status:
        transaction = {
            "id": facts.transaction_id,
            "status": facts.transaction_status,
            "closing_deadline": _iso(facts.closing_deadline),
            "closed_at": _iso(facts.closed_at),
        }
        if facts.transaction_status == "closed" and facts.closed_at is not None \
                and now - facts.closed_at <= timedelta(days=CLOSED_RECENT_DAYS):
            state, since = "closed", facts.closed_at
        if facts.transaction_status == "under_contract":
            state, since = "under_contract", None

    if facts.call_completed_at is not None \
            and now - facts.call_completed_at <= timedelta(minutes=AFTER_CALL_MINUTES):
        state, since = "after_call", facts.call_completed_at
    if facts.call_state in _ACTIVE_CALL_STATES and facts.call_started_at is not None \
            and now - facts.call_started_at <= timedelta(minutes=CALLING_STALE_MINUTES):
        state, since = "calling", facts.call_started_at

    return {
        "state": state,
        "since": _iso(since),
        "last_activity_at": _iso(facts.last_activity_at),
        "signals_7d": int(facts.signals_7d or 0),
        "transaction": transaction,
    }


async def living_for(ctx: TenantContext, client_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Living state for many clients in three queries, keyed by client id.

    Built for lists: a search page or the home screen asks once for every
    person it shows rather than once per card.
    """
    ids = [str(c) for c in client_ids if c]
    if not ids:
        return {}
    facts: dict[str, dict[str, Any]] = {cid: {} for cid in ids}
    async with tenant_tx(ctx) as conn:
        for row in await conn.fetch(
            """
            SELECT client_id::text AS cid,
                   max(created_at) AS last_activity_at,
                   count(*) FILTER (WHERE created_at > now() - interval '7 days') AS signals_7d
              FROM interaction_logs
             WHERE client_id = ANY($1::uuid[])
             GROUP BY client_id
            """,
            ids,
        ):
            facts[row["cid"]].update(
                last_activity_at=row["last_activity_at"], signals_7d=row["signals_7d"],
            )
        # The transaction that best describes the person now: a live contract
        # first, then whatever was touched most recently.
        for row in await conn.fetch(
            """
            SELECT DISTINCT ON (client_id)
                   client_id::text AS cid, id::text AS transaction_id, status,
                   closing_deadline, closed_at
              FROM transactions
             WHERE client_id = ANY($1::uuid[])
             ORDER BY client_id, (status = 'under_contract') DESC, updated_at DESC
            """,
            ids,
        ):
            facts[row["cid"]].update(
                transaction_id=row["transaction_id"], transaction_status=row["status"],
                closing_deadline=_as_dt(row["closing_deadline"]), closed_at=row["closed_at"],
            )
        for row in await conn.fetch(
            """
            SELECT DISTINCT ON (c.legacy_client_id)
                   c.legacy_client_id::text AS cid, i.state,
                   COALESCE(i.authorized_at, i.created_at) AS call_started_at,
                   i.completed_at AS call_completed_at
              FROM agent_call_intents i
              JOIN agent_contacts c ON c.id = i.contact_id
             WHERE c.legacy_client_id = ANY($1::uuid[])
               AND i.updated_at > now() - interval '3 hours'
             ORDER BY c.legacy_client_id, i.updated_at DESC
            """,
            ids,
        ):
            facts[row["cid"]].update(
                call_state=row["state"], call_started_at=row["call_started_at"],
                call_completed_at=row["call_completed_at"],
            )
    return {cid: derive(LivingFacts(**f)) for cid, f in facts.items()}


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def _as_dt(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    # DATE columns arrive as date; midnight UTC is honest enough for "closing Oct 12".
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
