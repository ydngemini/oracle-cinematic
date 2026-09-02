"""The Living Graph — beliefs that carry their source and know when they aged.

A CRM field holds a value. This holds a *claim*: who said it, when, how sure we
are, and whether anything since has contradicted it. The difference matters at
exactly one moment, but it is the moment the product is judged on — when the
system says something confident and wrong, and the agent asks where it got that.

Three behaviours are the whole point of the module:

**Provenance is mandatory.** `assert_belief` cannot be called without a
source_kind, and the read path returns it on every row. An assertion the agent
cannot trace is one they cannot correct, and a memory nobody can correct becomes
a memory nobody trusts — which is how AI memory systems turn into junk drawers.

**Contradiction is surfaced, not resolved.** When a new belief disagrees with a
standing one, both stay, both are marked `disputed`, and the pair is returned
together. Resolving it automatically would require knowing which signal wins,
and that depends on facts only the agent has: whether she mentioned the new area
in passing or moved her whole life there. "These two disagree, here is each with
its source" is both the honest answer and the more useful one.

**Decay is calculated, never stored.** A belief's confidence is written once and
never rewritten; what ages is the *reading* of it, computed at read time from
`learned_at` and the predicate's half-life. Storing decayed values would mean a
background job rewriting history, and would make "what did we believe in June"
unanswerable. See `effective_confidence`.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db.connection import tenant_tx
from tenancy import TenantContext

logger = logging.getLogger("oracle.belief_store")

# NOTE ON SCOPING. No query below carries `tenant_id = app_current_tenant()`.
# `beliefs` is FORCE ROW LEVEL SECURITY with
# `app_is_platform_admin() OR tenant_id = app_current_tenant()`, so the policy
# already scopes every read and write. Repeating only the second half of that
# predicate in application SQL narrows it — it hid a client's beliefs from a
# platform admin who could see the client itself — and it can never widen it.
# INSERTs still name app_current_tenant() explicitly, because there the function
# supplies the value rather than filtering on it.

#: How long a claim stays at full strength before the reading starts to fall.
#: These are half-lives in days, and they differ by an order of magnitude for a
#: reason: financing facts are documentary and change on a bank's schedule,
#: while an area preference is a mood that a single commute can rewrite.
#: A predicate absent here uses DEFAULT_HALF_LIFE_DAYS rather than never
#: decaying — an unknown claim type is not a durable one.
HALF_LIFE_DAYS: dict[str, float] = {
    "financing_type": 365.0,
    "pre_approval": 90.0,      # letters actually expire; see also valid_until
    "household_size": 730.0,
    "must_have": 180.0,
    "deal_breaker": 270.0,     # "no HOA" outlives "wants Ashburn"
    "decision_role": 365.0,
    "max_budget": 120.0,
    "prefers_area": 90.0,
    "timeline": 45.0,          # the fastest-rotting thing anyone tells an agent
    "objection": 60.0,
}
DEFAULT_HALF_LIFE_DAYS = 120.0

#: A decayed reading never drops below this. Something once said is never
#: evidence of nothing — it just stops outranking fresh evidence.
CONFIDENCE_FLOOR = 0.05

#: Statuses ordered by how much authority they carry in a contradiction. An
#: agent's pin beats a document, a document beats a claim, a claim beats a guess.
STATUS_AUTHORITY = {"hypothesis": 0, "inference": 1, "reported": 2, "confirmed": 3}


@dataclass(frozen=True)
class BeliefSource:
    kind: str
    ref: Optional[str] = None
    quote: Optional[str] = None


def effective_confidence(
    stored: float,
    predicate: str,
    learned_at: datetime,
    *,
    now: Optional[datetime] = None,
    pinned: bool = False,
    valid_until: Optional[datetime] = None,
) -> float:
    """What this claim is worth *today*, given when it was learned.

    Exponential decay on the predicate's half-life. Two overrides:

    A pinned belief does not decay. The agent asserted it, and re-asking them
    every ninety days whether their own correction still holds would make the
    correction feel like it did not take.

    A belief past `valid_until` is not decayed — it is dead, and returns the
    floor. Expiry is a fact about the claim (the letter ran out), not a guess
    about its freshness, so it is not something a half-life should smooth over.
    """
    if pinned:
        return round(min(stored, 0.99), 4)

    now = now or datetime.now(timezone.utc)
    if valid_until is not None and now >= valid_until:
        return CONFIDENCE_FLOOR

    age_days = max((now - learned_at).total_seconds() / 86400.0, 0.0)
    half_life = HALF_LIFE_DAYS.get(predicate, DEFAULT_HALF_LIFE_DAYS)
    decayed = stored * math.pow(0.5, age_days / half_life)
    return round(max(decayed, CONFIDENCE_FLOOR), 4)


def _row_to_belief(row: Any, now: datetime) -> dict[str, Any]:
    value = row["object_value"]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    pinned = row["pinned_at"] is not None
    stored = float(row["confidence"])
    return {
        "id": str(row["id"]),
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "predicate": row["predicate"],
        "value": value,
        "status": row["status"],
        # Both numbers are returned. The UI shows the effective one and can
        # explain the gap; hiding `stored_confidence` would make a decayed
        # belief look like a weak one, and they warrant different responses —
        # one needs re-asking, the other needs better evidence.
        "stored_confidence": round(stored, 4),
        "confidence": effective_confidence(
            stored, row["predicate"], row["learned_at"],
            now=now, pinned=pinned, valid_until=row["valid_until"],
        ),
        "source": {
            "kind": row["source_kind"],
            "ref": row["source_ref"],
            "quote": row["source_quote"],
        },
        "learned_at": row["learned_at"].isoformat(),
        "recorded_at": row["recorded_at"].isoformat(),
        "valid_until": row["valid_until"].isoformat() if row["valid_until"] else None,
        "revision_state": row["revision_state"],
        "pinned": pinned,
        "age_days": round((now - row["learned_at"]).total_seconds() / 86400.0, 1),
    }


async def assert_belief(
    ctx: TenantContext,
    *,
    subject_type: str,
    subject_id: str,
    predicate: str,
    value: Any,
    status: str,
    confidence: float,
    source: BeliefSource,
    learned_at: Optional[datetime] = None,
    valid_until: Optional[datetime] = None,
) -> dict[str, Any]:
    """Record a claim, and report what it disagrees with.

    Returns the new belief plus `contradicts`: the standing beliefs on the same
    predicate whose value differs. Both sides are moved to `disputed` — the new
    row included, because a claim that contradicts something is not yet settled
    just by virtue of being newer. Recency is evidence, not proof; the June
    statement may be the considered one and this week's searches idle curiosity.

    A repeat of something already believed is *not* a contradiction and does not
    dispute anything. It refreshes: the same value arriving again from a new
    source is corroboration, and gets its own row so the count of independent
    sources stays visible.
    """
    if not source.kind:
        raise ValueError("a belief without a source cannot be corrected, and will not be stored")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be strictly between 0 and 1, got {confidence}")

    learned_at = learned_at or datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    encoded = json.dumps(value)

    async with tenant_tx(ctx) as conn:
        standing = await conn.fetch(
            """
            SELECT * FROM beliefs
            WHERE subject_type = $1 AND subject_id = $2 AND predicate = $3
              AND revision_state IN ('active', 'disputed')
              AND retracted_at IS NULL
            ORDER BY learned_at DESC
            """,
            subject_type, subject_id, predicate,
        )
        conflicting = [r for r in standing if r["object_value"] != encoded]

        row = await conn.fetchrow(
            """
            INSERT INTO beliefs (
                tenant_id, subject_type, subject_id, predicate, object_value,
                status, confidence, source_kind, source_ref, source_quote,
                learned_at, valid_until, revision_state
            ) VALUES (
                app_current_tenant(), $1, $2, $3, $4::jsonb,
                $5, $6, $7, $8, $9, $10, $11, $12
            ) RETURNING *
            """,
            subject_type, subject_id, predicate, encoded,
            status, confidence, source.kind, source.ref, source.quote,
            learned_at, valid_until,
            "disputed" if conflicting else "active",
        )

        if conflicting:
            await conn.execute(
                "UPDATE beliefs SET revision_state = 'disputed' WHERE id = ANY($1::uuid[])",
                [r["id"] for r in conflicting],
            )

    result = _row_to_belief(row, now)
    result["contradicts"] = [
        dict(_row_to_belief(r, now), revision_state="disputed") for r in conflicting
    ]
    return result


async def beliefs_about(
    ctx: TenantContext, subject_type: str, subject_id: str,
    *, include_history: bool = False,
) -> dict[str, Any]:
    """Everything currently held about one entity, grouped by predicate.

    Disputes are lifted to the top level rather than left for the caller to
    detect by comparing values. A caller that has to notice the disagreement
    itself will eventually forget to, and then the UI shows one of two
    contradictory facts with no sign the other exists.
    """
    now = datetime.now(timezone.utc)
    states = ("active", "disputed", "superseded", "retracted") if include_history \
        else ("active", "disputed")

    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM beliefs
            WHERE subject_type = $1 AND subject_id = $2
              AND revision_state = ANY($3::text[])
            ORDER BY predicate, learned_at DESC
            """,
            subject_type, subject_id, list(states),
        )

    by_predicate: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_predicate.setdefault(row["predicate"], []).append(_row_to_belief(row, now))

    disputes = []
    for predicate, items in by_predicate.items():
        live = [b for b in items if b["revision_state"] == "disputed"]
        if len(live) < 2:
            continue
        # Rank by effective confidence so the freshest strong claim leads, then
        # by status authority — a document outranks a guess of equal confidence.
        ranked = sorted(
            live,
            key=lambda b: (b["confidence"], STATUS_AUTHORITY.get(b["status"], 0)),
            reverse=True,
        )
        disputes.append({
            "predicate": predicate,
            "leading": ranked[0],
            "challenged": ranked[1:],
            # Deliberately a question, not a verdict. The system does not know
            # which is true; it knows they cannot both be, which is the useful
            # thing to say and the thing an agent can resolve in one phone call.
            "question": _dispute_question(predicate, ranked),
        })

    return {
        "subject_type": subject_type,
        "subject_id": subject_id,
        "beliefs": by_predicate,
        "disputes": disputes,
        "as_of": now.isoformat(),
    }


def _dispute_question(predicate: str, ranked: list[dict[str, Any]]) -> str:
    leading, challenger = ranked[0], ranked[1]
    subject = predicate.replace("_", " ")
    older, newer = sorted((leading, challenger), key=lambda b: b["learned_at"])
    return (
        f"Recorded {subject} as {json.dumps(older['value'])} "
        f"({older['age_days']:.0f} days ago, {older['source']['kind']}), "
        f"but {json.dumps(newer['value'])} "
        f"({newer['age_days']:.0f} days ago, {newer['source']['kind']}). "
        f"Worth confirming which still holds."
    )


async def correct_belief(
    ctx: TenantContext, belief_id: str, *, action: str, reason: Optional[str] = None,
) -> dict[str, Any]:
    """Agent control over what the system thinks it knows.

    `retract` stops a belief being read but keeps the row: the audit trail is
    the reason to have this table, and a delete would take the mistake out of
    the record along with the claim.

    `pin` freezes it against decay and gives it top authority. It also settles
    the dispute it belongs to — an agent who pins one side has answered the
    question, and leaving the pair marked disputed would keep asking it.
    """
    if action not in {"retract", "pin", "unpin"}:
        raise ValueError(f"unknown correction: {action}")

    async with tenant_tx(ctx) as conn:
        if action == "retract":
            row = await conn.fetchrow(
                """
                UPDATE beliefs
                   SET revision_state = 'retracted', retracted_at = now(),
                       retracted_by = $2, retraction_reason = $3
                 WHERE id = $1::uuid AND retracted_at IS NULL
             RETURNING *
                """,
                belief_id, ctx.agent_id, reason,
            )
        elif action == "pin":
            row = await conn.fetchrow(
                """
                UPDATE beliefs SET pinned_at = now(), pinned_by = $2,
                                   revision_state = 'active'
                 WHERE id = $1::uuid AND retracted_at IS NULL
             RETURNING *
                """,
                belief_id, ctx.agent_id,
            )
            if row is not None:
                # The other side of the dispute loses its claim on the agent's
                # attention, but stays readable as history.
                await conn.execute(
                    """
                    UPDATE beliefs SET revision_state = 'superseded', superseded_by = $1
                     WHERE subject_type = $2 AND subject_id = $3 AND predicate = $4
                       AND id <> $1 AND revision_state = 'disputed'
                    """,
                    row["id"], row["subject_type"], row["subject_id"], row["predicate"],
                )
        else:
            row = await conn.fetchrow(
                """
                UPDATE beliefs SET pinned_at = NULL, pinned_by = NULL
                 WHERE id = $1::uuid
             RETURNING *
                """,
                belief_id,
            )

    if row is None:
        raise LookupError(f"belief {belief_id} not found, or already retracted")
    return _row_to_belief(row, datetime.now(timezone.utc))
