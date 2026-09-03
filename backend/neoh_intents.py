"""neoh_intents — what the ⌘K box understood, decided in code.

The universal input has to render an interface, not answer with a paragraph.
That only works if the question is classified by something deterministic:
these are ordered regular expressions, each with a resolver that calls
services this codebase already has and hands the result to `neoh_render`,
which is pure.

**The model is not in this path at all.** It is not asked to classify, and it
is certainly not asked to emit UI — a model that can emit markup can emit a
fabricated number inside a heading, and once rendered that is
indistinguishable from a real one. What the model still does is answer
everything these patterns do NOT cover: a miss returns `fallthrough`, and the
frontend then sends the same text down the existing chat channel. So the
fixed vocabulary never becomes a cage; it is a fast, honest path in front of
a general one.

Adding an intent means adding a pattern and a resolver here, and a primitive
the registry already draws. If a question needs a primitive that does not
exist, the answer is to add it to the vocabulary on purpose — not to let the
model improvise one.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Optional

import neoh_render as render
from tenancy import TenantContext

logger = logging.getLogger(__name__)

MAX_TEXT = 400

#: How many people a name may match before Neoh asks instead of guessing.
NAME_MATCH_LIMIT = 5


class Intent:
    """One pattern and the resolver it hands its captures to."""

    __slots__ = ("name", "pattern", "resolve")

    def __init__(self, name: str, pattern: str, resolve: Callable[..., Awaitable[dict]]):
        self.name = name
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.resolve = resolve


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

async def _who_to_call(ctx: TenantContext, _match: re.Match) -> dict[str, Any]:
    """Reuses the Command Center's briefing verbatim.

    Ranking it again here would give the same card two different positions in
    one product depending on which surface asked — the exact double-count the
    single-source rule exists to prevent.
    """
    import command_center

    briefing = await command_center.briefing(ctx)
    return render.who_to_call(briefing)


async def _show_person(ctx: TenantContext, match: re.Match) -> dict[str, Any]:
    import search_api

    query = (match.group("name") or "").strip()
    if len(query) < 2:
        return render.fallthrough("name too short to resolve")

    found = await search_api.search(ctx, query, ["people"], NAME_MATCH_LIMIT)
    matches = [hit for hit in found.get("results", []) if hit.get("kind") == "people"]
    if not matches:
        # "Show 412 Delaware" is the same grammar pointing at a house. People
        # come first because a person named like a street is still a person;
        # this only runs when nobody matched AND the text reads as an address.
        if search_api.looks_like_address(query):
            hits = await search_api.search(ctx, query, ["properties"], NAME_MATCH_LIMIT)
            properties = [h for h in hits.get("results", []) if h.get("kind") == "properties"]
            if properties:
                return render.properties_found(properties, query)
        return render.answer(
            "show_person", f"Nothing matches “{query}”.", [],
        )
    if len(matches) > 1:
        return render.person_choices(matches, query)

    client_id = matches[0].get("id")
    record, intent, timeline = await _person_detail(ctx, client_id)
    if record is None:
        # A search hit that will not load is a contact, not a client record —
        # say so rather than rendering an empty person card.
        return render.answer(
            "show_person",
            f"{matches[0].get('label')} is in the contact book but has no client "
            f"record yet, so there is nothing to read from.",
            [],
        )
    return render.person(record, intent, timeline)


async def _properties_for(ctx: TenantContext, match: re.Match) -> dict[str, Any]:
    import search_api

    query = (match.group("name") or "").strip()
    if len(query) < 2:
        return render.fallthrough("name too short to resolve")

    found = await search_api.search(ctx, query, ["people"], NAME_MATCH_LIMIT)
    matches = [hit for hit in found.get("results", []) if hit.get("kind") == "people"]
    if not matches:
        return render.answer("properties_for", f"No one matches “{query}”.", [])
    if len(matches) > 1:
        return render.person_choices(matches, query)

    name = matches[0].get("label") or query
    candidates = await _candidates_for(ctx, matches[0].get("id"))
    return render.properties_for(name, candidates)


async def _deal_blocker(ctx: TenantContext, match: re.Match) -> dict[str, Any]:
    import search_api

    query = (match.group("deal") or "").strip()
    if len(query) < 2:
        return render.fallthrough("no deal named")

    found = await search_api.search(ctx, query, ["deals"], NAME_MATCH_LIMIT)
    matches = [hit for hit in found.get("results", []) if hit.get("kind") == "deals"]
    if not matches:
        return render.answer("deal_blocker", f"No deal matches “{query}”.", [])
    if len(matches) > 1:
        return render.answer(
            "deal_blocker",
            f"{len(matches)} deals match “{query}”.",
            [render.block("comparison", title="Which deal?", options=[
                {
                    "id": str(m.get("id")),
                    "label": m.get("label"),
                    "detail": m.get("sublabel"),
                    "href": f"/deal/{m.get('id')}",
                }
                for m in matches
            ])],
        )

    transaction, milestones = await _deal_detail(ctx, matches[0].get("id"))
    if transaction is None:
        return render.answer("deal_blocker", f"That deal could not be loaded.", [])
    return render.deal_blocker(transaction, milestones)


# ---------------------------------------------------------------------------
# The table. Order matters: the first pattern that matches wins, so the more
# specific questions are listed before the ones that would also swallow them.
# ---------------------------------------------------------------------------

INTENTS: tuple[Intent, ...] = (
    # "find properties for Sarah" must beat "show Sarah", which would match too.
    Intent(
        "properties_for",
        r"^\s*(?:find|show|get|pull|what)\s+(?:me\s+)?(?:some\s+)?"
        r"(?:propert(?:y|ies)|houses?|homes?|listings?)\s+(?:for|to)\s+(?P<name>.+?)\s*\??$",
        _properties_for,
    ),
    Intent(
        "deal_blocker",
        r"^\s*(?:what(?:'s| is| are)?\s+)?(?:holding|blocking|stuck|stopping|"
        r"waiting\s+on|hold(?:ing)?\s+up)\s*(?:on\s+|up\s+)?(?P<deal>.+?)\s*(?:up)?\s*\??$",
        _deal_blocker,
    ),
    Intent(
        "who_to_call",
        r"^\s*(?:who|whom)\s+(?:should|do|can|must)\s+i\s+"
        r"(?:call|ring|phone|contact|reach|talk\s+to|follow\s+up\s+with)"
        r"(?:\s+.*)?\??\s*$",
        _who_to_call,
    ),
    # The bare imperative form of the same question.
    Intent(
        "who_to_call",
        r"^\s*(?:my\s+)?(?:call\s+list|call\s+queue|who\s+to\s+call|"
        r"next\s+best\s+(?:action|call)s?)\s*\??\s*$",
        _who_to_call,
    ),
    Intent(
        "show_person",
        r"^\s*(?:show|open|pull\s+up|bring\s+up|tell\s+me\s+about|who\s+is)\s+"
        r"(?:me\s+)?(?P<name>.+?)\s*\??$",
        _show_person,
    ),
)


async def ask(ctx: TenantContext, text: str) -> dict[str, Any]:
    """Classify, resolve, render. Never raises; a failure falls through.

    A resolver that breaks must not cost the person their question: the text
    still reaches the model, which is what would have happened without this
    module at all.
    """
    cleaned = (text or "").strip()[:MAX_TEXT]
    if not cleaned:
        return render.fallthrough("empty")

    for intent in INTENTS:
        match = intent.pattern.match(cleaned)
        if not match:
            continue
        try:
            return await intent.resolve(ctx, match)
        except Exception:  # noqa: BLE001 — a broken resolver degrades to chat
            logger.exception("neoh intent %s failed for %r", intent.name, cleaned[:80])
            return render.fallthrough(f"resolver {intent.name} failed")
    return render.fallthrough("no pattern matched")


# ---------------------------------------------------------------------------
# Data access. Kept here rather than in the renderer so the renderer stays
# pure and testable without a database.
# ---------------------------------------------------------------------------

async def _person_detail(
    ctx: TenantContext, client_id: Optional[str],
) -> tuple[Optional[dict], Optional[dict], list[dict]]:
    import intent_states
    from db.connection import tenant_tx

    if not client_id:
        return None, None, []

    async with tenant_tx(ctx) as conn:
        # No tenant predicate: RLS scopes this, and repeating half the policy
        # would hide the row from a platform admin.
        record = await conn.fetchrow(
            """SELECT id, full_name, client_type, stage, email, phone, company
                 FROM clients WHERE id=$1::uuid""",
            client_id,
        )
        if record is None:
            return None, None, []
        rows = await conn.fetch(
            """SELECT interaction_type, direction, created_at, subject
                 FROM interaction_logs
                WHERE client_id=$1::uuid
                ORDER BY created_at DESC
                LIMIT 5""",
            client_id,
        )

    timeline = [
        {
            # `subject` is the human line where one exists (an email subject,
            # a call note); the type is the honest fallback. There is no
            # `summary` column and no `occurred_at` — this table records when
            # it was written, in `created_at`.
            "label": (r["subject"] or (r["interaction_type"] or "").replace("_", " ")),
            "at": r["created_at"].isoformat() if r["created_at"] else None,
            "direction": r["direction"],
            "done": True,
        }
        for r in rows
    ]

    intent: Optional[dict[str, Any]] = None
    try:
        intent = await intent_states.read_intent(ctx, str(client_id))
    except Exception:  # noqa: BLE001 — the card is still worth showing
        logger.exception("read_intent failed for %s", client_id)

    return dict(record), intent, timeline


async def _candidates_for(ctx: TenantContext, client_id: Optional[str]) -> list[dict]:
    """The shortlist the automation already maintains, not a new matcher."""
    import client_ai_automation
    from db.connection import tenant_tx

    if not client_id:
        return []
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM client_ai_state WHERE client_id=$1::uuid",
            client_id,
        )
    if row is None:
        return []
    state = client_ai_automation.automation_state_json(row)
    candidates = state.get("property_candidates") or []
    return [c for c in candidates if isinstance(c, dict)]


async def _deal_detail(
    ctx: TenantContext, transaction_id: Optional[str],
) -> tuple[Optional[dict], list[dict]]:
    from db.connection import tenant_tx

    if not transaction_id:
        return None, []
    async with tenant_tx(ctx) as conn:
        transaction = await conn.fetchrow(
            "SELECT * FROM transactions WHERE id=$1::uuid", transaction_id,
        )
        if transaction is None:
            return None, []
        milestones = await conn.fetch(
            """SELECT milestone_type, title, status, due_at, completed_at
                 FROM transaction_milestones
                WHERE transaction_id=$1::uuid
                ORDER BY due_at NULLS LAST, created_at""",
            transaction_id,
        )
    return dict(transaction), [dict(m) for m in milestones]
