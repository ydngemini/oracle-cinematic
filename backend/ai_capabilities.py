"""What the assistant can do — derived from the executable tool surface (A09).

The audit found the assistant described more than it could do: the long-form
product knowledge told the model it had a real-time MLS feed, court records,
Census data and a Google calendar; several offered tools described fields their
handlers never return; the phone prompt promised to "connect you" to a person
and to "look that up", with no transfer and no tools on that path. Each surface
had its own idea of the capability list, and none of them read the allowlist.

This module is the one answer to "what can Neoh do right now", and it is
derived, never restated:

- whether a tool can run at all comes from ``ai_chat_store.is_agent_tool_available``
  (the executable allowlist — a name is offered exactly when a handler exists);
- whether running it needs a human comes from ``ai_tool_policy.TOOL_RISK``
  through ``platform_policy.requires_approval``;
- whether its source is limited comes from the switches that limit it
  (``mls_switch.mls_enabled`` for ZIP-level days on market);
- whether this actor may use it comes from the same role gate the handler
  applies (``ai_tools_read.OPS_ROLE_TOOLS``).

What is written by hand here is only the grouping of tools into areas a person
would recognise, and one plain sentence per area. ``tests/test_ai_capabilities.py``
fails when an area's sentence names something none of its tools does, when a
catalogued tool is in no area, or when any model- or user-facing surface (the
persona, the product knowledge, the phone prompt, offered tool descriptions,
the UI snapshot) names a capability with no executable tool behind it.

Every capability is in exactly one state (prompt 07 T07.13):

- ``available``       — runs now, read-only or an undoable internal edit;
- ``approval_gated``  — runs now, but only stages: a human approves before any
                        message, call, calendar write, contract or listing;
- ``degraded``        — runs now with a named limit on its source;
- ``setup_required``  — needs a credential or registration first;
- ``unavailable``     — no executable tool on this deployment (or not for this
                        role). The UI says "Not available yet".

No module-level import of ``ai_chat_agent``: that module builds the persona at
import, and the persona reads this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

AVAILABLE = "available"
APPROVAL_GATED = "approval_gated"
DEGRADED = "degraded"
SETUP_REQUIRED = "setup_required"
UNAVAILABLE = "unavailable"

STATES: tuple[str, ...] = (AVAILABLE, APPROVAL_GATED, DEGRADED, SETUP_REQUIRED, UNAVAILABLE)

# The words the UI shows. Kept here so the API, the persona and the page use
# one vocabulary (CONTRACT C6: the same words in the UI and the timeline).
STATE_LABELS: dict[str, str] = {
    AVAILABLE: "Available",
    APPROVAL_GATED: "Needs your approval",
    DEGRADED: "Limited",
    SETUP_REQUIRED: "Needs setup",
    UNAVAILABLE: "Not available yet",
}

# Bumped whenever an area, its tools or its wording changes, so a cached UI
# snapshot can tell it is stale.
REGISTRY_VERSION = "2026-10-13.1"


@dataclass(frozen=True)
class Area:
    id: str
    label: str
    # One plain sentence of what this area does when it is usable. It may only
    # name what its tools do; the guard test reads it against CLAIM_TERMS.
    summary: str
    tools: tuple[str, ...]
    # Said instead of ``summary`` limits when the area is not fully usable.
    limit_note: str = ""


# Every tool in ai_chat_agent.TOOLS is in exactly one area (enforced by test).
# Areas whose tools are all off the allowlist are reported as "Not available
# yet"; listing them is the point — the model and the page can then say so
# instead of improvising.
AREAS: tuple[Area, ...] = (
    Area(
        "clients", "Client lookups",
        "Find the clients you can see and read their profile, tasks, activity and "
        "contact history, and see which of your buyers fit a property you have open.",
        ("search_clients", "get_client_detail", "list_client_tasks",
         "list_client_activity", "get_client_contact_history", "suggest_client_matches"),
    ),
    Area(
        "client_edits", "Edit the open client",
        "Change fields, add a note, a tag or a lead score, move the stage, reassign or "
        "archive the client you have open, or create a new client. Each change except "
        "creating a client can be undone from its receipt.",
        ("update_client", "add_client_note", "set_client_stage", "add_client_tag",
         "score_client_lead", "assign_client", "archive_client", "create_client"),
    ),
    Area(
        "deals", "Deals and deadlines",
        "List your deals and open one: its stage, recorded financials, transaction "
        "steps, closing checklist and upcoming deadlines, plus deal counts per stage.",
        ("list_deals", "get_deal_detail", "track_deadlines", "get_team_pipeline",
         "list_closing_checklist", "get_transaction_workflow",
         "get_deal_financial_summary", "check_contract_deadlines"),
    ),
    Area(
        "deal_edits", "Edit the open deal or listing",
        "Move the deal you have open to a new stage, add a deal note, or change the "
        "address or status of the listing you have open. Each change can be undone.",
        ("move_deal_stage", "create_deal_note", "update_listing"),
    ),
    Area(
        "outreach", "Email, text, call and calendar drafts",
        "Draft an email or a text, request a call, or draft a calendar event for the "
        "client you have open. Nothing is sent, dialed or written to a calendar until "
        "you approve it, and delivery needs a connected channel.",
        ("draft_email", "draft_sms", "call_contact", "schedule_event"),
    ),
    Area(
        "contract_drafts", "Contract drafts",
        "Draft a contract for the deal you have open from an approved template and "
        "queue it for review. Nothing is signed or binding.",
        ("draft_contract",),
    ),
    Area(
        "marketplace", "Marketplace listing drafts",
        "Create a draft marketplace listing from a signed contract and queue it for "
        "approval. Buyers cannot see it until a person approves it.",
        ("publish_to_marketplace",),
    ),
    Area(
        "properties", "Your properties",
        "Search your brokerage's own listings, open one, list its photos and 3D tour "
        "assets, and find recently sold comparable public records near an address.",
        ("search_listings", "get_listing_detail", "list_comparable_sales",
         "list_property_photos", "get_property_tour"),
    ),
    Area(
        "estimates", "Rough deal math",
        "Estimate after-repair value from nearby sold records, estimate rehab from a "
        "rule-of-thumb band, and calculate a maximum allowable offer. These are "
        "estimates, not appraisals.",
        ("estimate_arv", "estimate_rehab", "calculate_mao"),
    ),
    Area(
        "market", "Market figures",
        "County and state market figures and days on market for the area a ZIP code "
        "is in, each with its as-of date.",
        ("get_market_trends", "get_days_on_market"),
        limit_note="county and state level only; no ZIP-level figures or forecasts",
    ),
    Area(
        "reference", "Templates and disclosures",
        "List the contract templates and the state disclosure forms on file.",
        ("list_contract_templates", "list_required_disclosures"),
    ),
    Area(
        "workspace", "Workspace setup",
        "Say which features are switched on, which accounts have been saved and their "
        "last check, and agent performance from recorded deals.",
        ("get_feature_flags", "list_providers", "get_agent_performance"),
    ),
    Area(
        "workspace_admin", "Workspace health and billing (owner only)",
        "For the broker-owner: workspace counts, the job queue, recent failures, the "
        "audit trail, saved integrations, a local health check, database counts and "
        "billing status.",
        ("get_tenant_health", "get_job_queue", "list_recent_errors", "get_audit_trail",
         "list_integration_status", "run_health_check", "get_database_stats",
         "get_billing_status", "list_billing_invoices"),
    ),
    Area(
        "research", "Web search",
        "Search the public web and say which sources it used.",
        ("web_search", "codebase_summary"),
    ),
    # ── Not executable on this release ────────────────────────────────
    Area(
        "public_records", "Public records",
        "Flood zone, zoning, tax, deed and ownership history, permits, liens, distress "
        "signals, court and government records, and property background reports.",
        ("get_flood_zone", "get_zoning_info", "list_tax_records", "get_owner_history",
         "check_permits", "get_property_distress", "list_property_liens",
         "search_public_records", "run_property_background", "get_govinfo_record"),
    ),
    Area(
        "neighborhood", "Neighborhood and investor data",
        "Schools, demographics, neighborhood profiles and investor activity.",
        ("get_nearest_schools", "get_demographics", "analyze_neighborhood",
         "get_investor_activity_profile", "get_investment_activity"),
    ),
    Area(
        "market_analytics", "Market analytics",
        "ZIP market snapshots, market comparisons, rent estimates, absorption, hot "
        "markets, distress maps, price reductions, heatmaps, lead-feed summaries and "
        "appreciation forecasts.",
        ("list_market_snapshots", "compare_markets", "get_rent_estimates",
         "get_absorption_rate", "list_hot_markets", "get_distress_map",
         "forecast_appreciation", "get_price_reductions", "get_market_heatmap",
         "get_firehose_summary"),
    ),
    Area(
        "underwriting", "Full underwriting",
        "Underwriting models, ROI, cap rate, cash-on-cash, holding, closing and "
        "interest costs, break-even, financing comparisons, portfolio returns, deal "
        "risk and exit-strategy suggestions.",
        ("run_underwriting_model", "calculate_deal_roi", "calculate_cap_rate",
         "calculate_cash_on_cash", "estimate_holding_costs", "estimate_closing_costs",
         "calculate_break_even", "calculate_interest_costs",
         "estimate_after_repair_value", "compare_financing_options",
         "get_portfolio_performance", "analyze_deal_risk", "suggest_disposition"),
    ),
    Area(
        "negotiations", "Negotiations",
        "Deals under negotiation and their counter-offers.",
        ("list_active_negotiations", "list_counter_offers"),
    ),
    Area(
        "contract_analysis", "Contract review and signatures",
        "Generating contracts and assignment agreements outside the approved draft "
        "flow, reviewing, extracting and comparing contract terms, parties, revision "
        "history, signatures, attorney-review status and form libraries.",
        ("generate_contract", "generate_assignment_agreement", "review_contract_terms",
         "extract_contract_data", "compare_contract_versions", "list_contract_parties",
         "list_document_history", "verify_contract_signatures", "check_attorney_review",
         "get_form_library"),
    ),
    Area(
        "legal_reference", "State law and fair-housing checks",
        "State real-estate law summaries, disclosure requirements, legal form lists, "
        "fair-housing text checks and retention policies.",
        ("get_state_laws", "get_disclosure_requirements", "list_legal_forms",
         "check_fair_housing", "get_retention_policy"),
    ),
    Area(
        "documents_media", "Documents, videos and tour links",
        "Client documents, document uploads and download links, property videos, 3D "
        "build status and shareable tour links.",
        ("list_client_documents", "upload_property_document", "get_document_download_url",
         "list_property_videos", "get_splat_status", "generate_tour_link",
         "share_property_tour"),
    ),
    Area(
        "spatial_spend", "3D builds and listing videos",
        "Starting a photoreal 3D build or an AI listing video from chat. Both cost "
        "money and are started from the property's own panel instead.",
        ("request_property_reconstruction", "request_listing_video"),
    ),
    Area(
        "team", "Team roster and commissions",
        "The brokerage roster, agent profiles, commission tracking and onboarding "
        "status.",
        ("list_team_members", "get_agent_profile", "list_agent_commissions",
         "get_onboarding_status"),
    ),
    Area(
        "client_merge", "Merging clients",
        "Merging two client records into one.",
        ("merge_duplicate_clients",),
    ),
)

AREA_BY_ID: dict[str, Area] = {area.id: area for area in AREAS}
AREA_OF_TOOL: dict[str, str] = {
    tool: area.id for area in AREAS for tool in area.tools
}

# Never offered, never described as a feature (owner decision D-2026-10-12-01).
NOT_OFFERED_BY_DECISION: tuple[str, ...] = ("MLS, IDX or RESO listing search",)


ROLE_REASON = "limited to the broker-owner"


# ---------------------------------------------------------------------------
# Per-tool state
# ---------------------------------------------------------------------------

def _degraded_reason(tool: str) -> Optional[str]:
    """A source limit the handler itself reports, stated once here."""
    if tool == "get_market_trends":
        return "county and state figures only; no ZIP-level aggregate or forecast exists here"
    if tool == "get_days_on_market":
        from mls_switch import mls_enabled

        if not mls_enabled():
            return "county and state medians only; ZIP-level days on market is not offered"
    return None


def tool_state(tool: str, *, ops_role: bool = True) -> tuple[str, str]:
    """``(state, reason)`` for one tool, for an actor with or without the
    broker-owner role. Unknown names are unavailable."""
    from ai_chat_store import is_agent_tool_available
    from ai_tool_policy import TOOL_RISK
    from ai_tools_read import OPS_ROLE_TOOLS
    from platform_policy import requires_approval

    if not is_agent_tool_available(tool):
        return UNAVAILABLE, "no executable tool on this release"
    if not ops_role and tool in OPS_ROLE_TOOLS:
        return UNAVAILABLE, ROLE_REASON
    risk = TOOL_RISK.get(tool)
    if risk is None:
        # A tool with no risk class is never offered (test_ai_tool_policy), so
        # reaching this means the allowlist and the policy disagree.
        return UNAVAILABLE, "no risk class"
    if requires_approval(risk):
        return APPROVAL_GATED, "stages a request; a person approves before anything happens"
    reason = _degraded_reason(tool)
    if reason:
        return DEGRADED, reason
    return AVAILABLE, ""


def _area_state(area: Area, *, ops_role: bool) -> tuple[str, list[str], list[dict]]:
    tools = []
    for name in area.tools:
        state, reason = tool_state(name, ops_role=ops_role)
        tools.append({"name": name, "state": state, "reason": reason})
    usable = [t for t in tools if t["state"] != UNAVAILABLE]
    if not usable:
        return UNAVAILABLE, [], tools
    states = {t["state"] for t in usable}
    if states == {APPROVAL_GATED}:
        state = APPROVAL_GATED
    elif DEGRADED in states:
        state = DEGRADED
    elif SETUP_REQUIRED in states:
        state = SETUP_REQUIRED
    else:
        state = AVAILABLE
    if state == DEGRADED and area.limit_note:
        limits = [area.limit_note]
    else:
        limits = sorted({t["reason"] for t in usable if t["state"] == DEGRADED and t["reason"]})
    return state, limits, tools


def capabilities(*, ops_role: bool = True) -> list[dict]:
    """The registry as data: one entry per area, with its derived state.

    ``ops_role=False`` is the view of an agent or other non-owner role, for
    whom the broker-owner tools are refused by their handlers."""
    out = []
    for area in AREAS:
        state, limits, tools = _area_state(area, ops_role=ops_role)
        # Unavailable because of who is asking, not because nothing exists:
        # "Not available yet" would be false for the broker-owner's tools.
        role_limited = state == UNAVAILABLE and all(
            t["reason"] == ROLE_REASON for t in tools
        )
        out.append({
            "id": area.id,
            "label": area.label,
            "summary": area.summary,
            "state": state,
            "state_label": STATE_LABELS[state],
            "limits": limits,
            "role_limited": role_limited,
            "tools": tools,
        })
    return out


def executable_tools(*, ops_role: bool = True) -> frozenset[str]:
    return frozenset(
        tool for area in AREAS for tool in area.tools
        if tool_state(tool, ops_role=ops_role)[0] != UNAVAILABLE
    )


# ---------------------------------------------------------------------------
# Claim terms — how a description is checked against the registry
# ---------------------------------------------------------------------------

# A phrase that, used affirmatively, claims the capability of one area. A
# surface (persona, product knowledge, phone prompt, offered tool descriptions,
# area summaries, the UI's chat prompts) may only use it while that area has an
# executable tool. ``None`` means no area can ever back it (MLS is out by owner
# decision D-2026-10-12-01). A sentence that negates ("not", "never", "no"…) is
# a disclaimer, not a claim, and is skipped.
#
# The patterns are plain regular expressions that mean the same in Python and
# JavaScript (no lookbehind, no inline flags): the frontend guard
# (oracle-app/src/lib/assistantCapabilities.test.js) runs the same list from
# the generated snapshot.
CLAIM_TERMS: tuple[tuple[str, Optional[str]], ...] = (
    (r"\bMLS\b", None),
    (r"\bIDX\b", None),
    (r"\bRESO\b", None),
    (r"flood zones?", "public_records"),
    (r"zoning (info|classification|lookup)", "public_records"),
    (r"building permits?|permit history", "public_records"),
    (r"\bliens?\b", "public_records"),
    (r"owner(ship)? history|deed history|chain of title", "public_records"),
    (r"tax (records|assessments?)", "public_records"),
    (r"court records?", "public_records"),
    (r"public[- ]records? (search|lookup|research)|search public records|public-property evidence",
     "public_records"),
    (r"courtlistener|govinfo|regrid|county assessor", "public_records"),
    (r"\bschools?\b", "neighborhood"),
    (r"demographics?|census", "neighborhood"),
    (r"neighbou?rhood (profile|analysis)", "neighborhood"),
    (r"investor activity", "neighborhood"),
    (r"rent estimates?", "market_analytics"),
    (r"absorption", "market_analytics"),
    (r"hot markets?", "market_analytics"),
    (r"price (drops|reductions)", "market_analytics"),
    (r"heat ?maps?", "market_analytics"),
    (r"\bforecasts?\b", "market_analytics"),
    (r"batchleads|propstream|firehose", "market_analytics"),
    (r"underwriting models?", "underwriting"),
    (r"cap rates?", "underwriting"),
    (r"cash[- ]on[- ]cash", "underwriting"),
    (r"break[- ]even", "underwriting"),
    (r"\bROI\b", "underwriting"),
    (r"equity reviews?|estimate (the )?equity", "underwriting"),
    (r"counter[- ]offers?", "negotiations"),
    (r"signature status|verify (the )?signatures?", "contract_analysis"),
    (r"attorney[- ]review status", "contract_analysis"),
    (r"extract (contract|key) terms|compare contract versions", "contract_analysis"),
    (r"form library", "contract_analysis"),
    (r"state (real[- ]estate )?laws?", "legal_reference"),
    (r"fair[- ]housing (check|review)", "legal_reference"),
    (r"retention polic(y|ies)", "legal_reference"),
    (r"upload (a )?documents?", "documents_media"),
    (r"download links?", "documents_media"),
    (r"drone|walkthrough video|property videos?", "documents_media"),
    (r"tour links?|share (a |the )?(3d )?tour", "documents_media"),
    (r"3d builds?|reconstruct", "spatial_spend"),
    (r"listing videos?|marketing videos?", "spatial_spend"),
    (r"commissions?", "team"),
    (r"(team|brokerage) roster", "team"),
    (r"merge (duplicate )?(clients?|records)", "client_merge"),
    # Things no tool on any path does: a phone transfer, a callback promise.
    (r"connect you|transfer (you|the call)", None),
)

# Sentences end at . ! or ?, at a blank line, or where a list item or heading
# starts; a wrapped line is the same sentence. Lists separated by ";" stay one
# sentence, so "Not available yet: a; b; c" is one disclaimer.
SENTENCE_SPLIT = r"(?<=[.!?])\s+|\n\s*\n|\n(?=\s*[-#*])"

# A sentence with one of these words disclaims rather than claims.
NEGATION = r"\b(not|never|no|cannot|can't|can’t|isn't|aren't|without|unavailable|don't|do not|nor)\b"


def unbacked_claims(text: str, *, ops_role: bool = True) -> list[tuple[str, str]]:
    """``(pattern, sentence)`` for each affirmative claim in ``text`` whose
    area has no executable tool. Empty means the text agrees with the
    registry."""
    import re

    usable = {e["id"] for e in capabilities(ops_role=ops_role) if e["state"] != UNAVAILABLE}
    negation = re.compile(NEGATION, re.IGNORECASE)
    found = []
    for sentence in re.split(SENTENCE_SPLIT, text):
        if not sentence.strip() or negation.search(sentence):
            continue
        for pattern, area in CLAIM_TERMS:
            if area in usable:
                continue
            if re.search(pattern, sentence, re.IGNORECASE):
                found.append((pattern, sentence.strip()))
    return found


def snapshot() -> dict:
    """The committed UI snapshot (oracle-app/src/lib/assistantCapabilities.json).

    The page reads the live API; this file lets the frontend's own tests check
    its hand-written chat prompts against the same registry without a server.
    ``tests/test_ai_capabilities.py`` fails when it is stale — regenerate with
    ``python ai_capabilities.py --write-snapshot`` from backend/."""
    return {
        "version": REGISTRY_VERSION,
        "generated_by": "backend/ai_capabilities.py --write-snapshot",
        "areas": [
            {"id": e["id"], "label": e["label"], "state": e["state"]}
            for e in capabilities(ops_role=True)
        ],
        "claim_terms": [{"pattern": p, "area": a} for p, a in CLAIM_TERMS],
        "negation": NEGATION,
        "sentence_split": SENTENCE_SPLIT,
    }


SNAPSHOT_PATH = "../oracle-app/src/lib/assistantCapabilities.json"


def payload(*, ops_role: bool = True) -> dict:
    """The API body for ``GET /api/ai/chat/capabilities``."""
    return {
        "version": REGISTRY_VERSION,
        "states": [{"state": s, "label": STATE_LABELS[s]} for s in STATES],
        "areas": capabilities(ops_role=ops_role),
        "not_offered": list(NOT_OFFERED_BY_DECISION),
        "note": (
            "Derived from the tools the assistant can actually run. Edits and "
            "drafts need the matching record open; anything that reaches a person "
            "outside the brokerage waits for approval."
        ),
    }


def _labels(entries: Iterable[dict]) -> str:
    return "; ".join(
        entry["label"] + (f" ({', '.join(entry['limits'])})" if entry.get("limits") else "")
        for entry in entries
    )


def prompt_block() -> str:
    """The model-facing projection, appended to the persona at both densities.

    Written for the broker-owner view (the widest); the role gate in each
    handler refuses the owner-only tools for everyone else, and says why."""
    areas = capabilities(ops_role=True)
    by_state: dict[str, list[dict]] = {s: [] for s in STATES}
    for entry in areas:
        by_state[entry["state"]].append(entry)
    lines = [PROMPT_HEADER]
    if by_state[AVAILABLE]:
        lines.append("- Available (edits need the record open): "
                     + _labels(by_state[AVAILABLE]) + ".")
    if by_state[DEGRADED]:
        lines.append("- Limited: " + _labels(by_state[DEGRADED]) + ".")
    if by_state[APPROVAL_GATED]:
        lines.append("- Stages for approval only, never sends: "
                     + _labels(by_state[APPROVAL_GATED]) + ".")
    if by_state[SETUP_REQUIRED]:
        lines.append("- Needs setup first: " + _labels(by_state[SETUP_REQUIRED]) + ".")
    if by_state[UNAVAILABLE]:
        lines.append(
            "- " + UNAVAILABLE_LEAD + _labels(by_state[UNAVAILABLE]) + "."
        )
    lines.append("- Not offered: " + "; ".join(NOT_OFFERED_BY_DECISION) + ".")
    return "\n".join(lines)


PROMPT_HEADER = "## WHAT YOU CAN DO (from the tools you can run)"
UNAVAILABLE_LEAD = "Not available yet (say so; never imitate them): "


def _write_snapshot() -> str:
    import json
    import os

    path = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), SNAPSHOT_PATH)
    )
    body = json.dumps(snapshot(), indent=2, ensure_ascii=False) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.replace(tmp, path)  # atomic: this partition has truncated files before
    return path


if __name__ == "__main__":
    import sys

    if "--write-snapshot" in sys.argv:
        print(_write_snapshot())
    else:
        print(prompt_block())
