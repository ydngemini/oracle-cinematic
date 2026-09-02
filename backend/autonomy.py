"""The autonomy dial — per capability, with a ceiling nobody can turn past.

One global "AI on/off" asks the wrong question. An agent may be glad for Neoh to
file notes and enrich records unattended, and never want it within reach of a
counter-offer. Collapsing both into one switch means the cautious answer
disables the useful half, so the switch ends up off and the product ends up
unused.

Three levels, and the boundary between them is *consequence*, not confidence:

    observe    analyses and recommends; touches nothing
    assist     prepares the work; a person releases it
    autopilot  acts within the agent's stated rules

CEILINGS. Some categories cannot reach autopilot regardless of preference, and
the ceiling lives in the CHECK constraints of 0095 rather than here. That
placement is the whole point: a limit enforced in application code is a limit
until someone adds a second write path, and this codebase already has more than
one way to reach most tables. The constants below are a *mirror* of those
constraints so the UI can grey out an option instead of offering it and failing
the save — `assert_ceilings_match_database` exists to catch the two drifting.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from db.connection import tenant_tx
from tenancy import TenantContext

logger = logging.getLogger("oracle.autonomy")

LEVELS = ("observe", "assist", "autopilot")

#: Legal, financial or fiduciary consequence. Advice here is regulated, and a
#: wrong move is not correctable by an apology.
OBSERVE_ONLY = ("offers", "pricing", "contract_changes", "legal_financial")

#: Outbound contact reaches a real person under the agent's licence and cannot
#: be recalled. Drafting is fine; sending is the agent's signature.
ASSIST_MAX = ("calls", "texts", "emails")

CATEGORIES: dict[str, dict[str, str]] = {
    "crm_hygiene": {
        "label": "CRM hygiene",
        "detail": "Filing notes, deduplicating records, enriching fields.",
    },
    "research": {
        "label": "Research",
        "detail": "Pulling comps, public records and market context before you ask.",
    },
    "drafting": {
        "label": "Drafting",
        "detail": "Preparing messages and summaries for you to review.",
    },
    "texts": {"label": "Texts", "detail": "SMS to clients."},
    "emails": {"label": "Emails", "detail": "Email to clients."},
    "calls": {"label": "Calls", "detail": "Outbound voice contact."},
    "scheduling": {
        "label": "Scheduling",
        "detail": "Booking and moving showings and appointments.",
    },
    "offers": {"label": "Offers", "detail": "Anything that constitutes or alters an offer."},
    "pricing": {"label": "Pricing advice", "detail": "Recommending list or offer prices."},
    "contract_changes": {"label": "Contract changes", "detail": "Amendments, addenda, terms."},
    "legal_financial": {
        "label": "Legal & financial",
        "detail": "Conclusions a licensed professional should be giving.",
    },
}

#: Where a category starts before anyone touches the dial. Conservative on
#: purpose: a default that acts is a default nobody consented to.
DEFAULTS: dict[str, str] = {
    "crm_hygiene": "assist",
    "research": "autopilot",   # reversible, reads only, no one is contacted
    "drafting": "assist",
    "texts": "observe",
    "emails": "observe",
    "calls": "observe",
    "scheduling": "observe",
    **{c: "observe" for c in OBSERVE_ONLY},
}


def ceiling_for(category: str) -> str:
    """The highest level this category is permitted to reach."""
    if category in OBSERVE_ONLY:
        return "observe"
    if category in ASSIST_MAX:
        return "assist"
    return "autopilot"


def permitted_levels(category: str) -> list[str]:
    return list(LEVELS[: LEVELS.index(ceiling_for(category)) + 1])


async def get_settings(ctx: TenantContext) -> dict[str, Any]:
    """Every category with its current level, its ceiling and why it has one."""
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            "SELECT category, level FROM autonomy_preferences "
            "WHERE tenant_id = app_current_tenant() AND user_id = app_current_agent()"
        )
    chosen = {r["category"]: r["level"] for r in rows}

    out = []
    for category, meta in CATEGORIES.items():
        ceiling = ceiling_for(category)
        out.append({
            "category": category,
            "label": meta["label"],
            "detail": meta["detail"],
            "level": chosen.get(category, DEFAULTS[category]),
            "is_default": category not in chosen,
            "ceiling": ceiling,
            "permitted": permitted_levels(category),
            "ceiling_reason": (
                "Legal, financial or fiduciary consequence — Neoh can analyse "
                "and recommend here, never act."
                if category in OBSERVE_ONLY else
                "Reaches a person under your licence and cannot be recalled — "
                "Neoh can draft, you send."
                if category in ASSIST_MAX else None
            ),
        })
    return {"categories": out, "levels": list(LEVELS)}


async def set_level(ctx: TenantContext, category: str, level: str) -> dict[str, Any]:
    """Set one dial. Refuses above the ceiling with a reason, not a 500.

    The database would reject this anyway. Checking first turns a constraint
    violation into an explanation, which is the difference between a UI that
    teaches the rule and one that looks broken.
    """
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category}")
    if level not in LEVELS:
        raise ValueError(f"unknown level: {level}")

    ceiling = ceiling_for(category)
    if LEVELS.index(level) > LEVELS.index(ceiling):
        raise PermissionError(
            f"{CATEGORIES[category]['label']} cannot go above '{ceiling}'. "
            f"This limit is enforced by the database and is not configurable."
        )

    async with tenant_tx(ctx) as conn:
        await conn.execute(
            """
            INSERT INTO autonomy_preferences (tenant_id, user_id, category, level, updated_by)
            VALUES (app_current_tenant(), app_current_agent(), $1, $2, app_current_agent())
            ON CONFLICT (tenant_id, user_id, category)
            DO UPDATE SET level = EXCLUDED.level, updated_at = now(),
                          updated_by = EXCLUDED.updated_by
            """,
            category, level,
        )
    return {"category": category, "level": level, "ceiling": ceiling}


async def may_act(ctx: TenantContext, category: str, *, reversible: bool) -> tuple[bool, str]:
    """Whether Neoh may act unattended in this category right now.

    `reversible` is required rather than inferred, so the caller has to have
    thought about it. An irreversible action never runs on autopilot even where
    the dial permits it — the dial expresses appetite, not physics.
    """
    settings = await get_settings(ctx)
    entry = next((c for c in settings["categories"] if c["category"] == category), None)
    if entry is None:
        return False, f"unknown category: {category}"

    level = entry["level"]
    if level != "autopilot":
        return False, f"{entry['label']} is set to '{level}' — prepared, not sent."
    if not reversible:
        return False, (
            f"{entry['label']} is on autopilot, but this specific action cannot "
            f"be undone, so it still needs you."
        )
    return True, f"{entry['label']} is on autopilot and this action is reversible."
