"""One risk class per agent tool — the single source of truth for the tool surface.

Two lists of "read-only tool names" used to exist, in ``ai_chat_store`` and in
``ai_chat_agent``, and they had drifted: ``get_transaction_workflow`` and
``get_deal_financial_summary`` were read-only in one and not the other. The
agent-side list decides ``_is_record_change``, so a read that the store thought
harmless would have been broadcast to the UI as an applied "Record updated"
receipt whose Undo button POSTs to ``.../actions/undefined/undo`` — a read has no
action_id. Both modules now derive their sets from ``TOOL_RISK`` here, so the
question "is this tool a mutation?" has exactly one answer.

``call_contact`` was the sharper problem: it sat in the store's *read-only* set
while its handler creates a LIVE_CALL approval. Nothing consumed that set as a
risk oracle yet, which is precisely why it was worth fixing before something did.

**Classification rule** (applied once, here, not re-decided per caller):
read-only; a tenant-internal undoable edit; or — if it reaches a third party,
contacts a consumer, moves money, produces a legal instrument, or changes
permissions — the matching ``ActionRisk``. ``platform_policy.requires_approval``
turns a class into a routing decision; do not reimplement that judgement.

Membership of ``READ_ONLY`` also decides whether a tool needs a selected record
(``ai_chat_store._execute_safe_tool``). Moving a name between classes therefore
changes runtime behaviour and is a deliberate change, never a tidy-up.
"""

from __future__ import annotations

from platform_policy import ActionRisk, requires_approval


# Names that must never appear in the tool catalog at all. An agent that can
# grant itself consent has no TCPA gate: consent is a record of what a consumer
# said, and a model writing that record fabricates the consumer's answer.
# Enforced by test, not by comment.
CONSENT_WRITE_NAMES: frozenset[str] = frozenset({
    "record_consent",
    "grant_consent",
    "set_consent",
    "add_consent",
    "opt_in_contact",
    "confirm_opt_in",
    "clear_suppression",
    "remove_suppression",
    "delete_suppression",
    "unsubscribe_override",
})


TOOL_RISK: dict[str, ActionRisk] = {
    # ── Read-only (101) ────────────────────────────────────────────────
    "analyze_deal_risk": ActionRisk.READ_ONLY,
    # Reads what a property can show and why anything is missing. No spend.
    "get_property_tour": ActionRisk.READ_ONLY,
    "analyze_neighborhood": ActionRisk.READ_ONLY,
    "calculate_break_even": ActionRisk.READ_ONLY,
    "calculate_cap_rate": ActionRisk.READ_ONLY,
    "calculate_cash_on_cash": ActionRisk.READ_ONLY,
    "calculate_deal_roi": ActionRisk.READ_ONLY,
    "calculate_interest_costs": ActionRisk.READ_ONLY,
    "calculate_mao": ActionRisk.READ_ONLY,
    "check_attorney_review": ActionRisk.READ_ONLY,
    "check_contract_deadlines": ActionRisk.READ_ONLY,
    "check_fair_housing": ActionRisk.READ_ONLY,
    "check_permits": ActionRisk.READ_ONLY,
    "codebase_summary": ActionRisk.READ_ONLY,
    "compare_contract_versions": ActionRisk.READ_ONLY,
    "compare_financing_options": ActionRisk.READ_ONLY,
    "compare_markets": ActionRisk.READ_ONLY,
    "estimate_after_repair_value": ActionRisk.READ_ONLY,
    "estimate_arv": ActionRisk.READ_ONLY,
    "estimate_closing_costs": ActionRisk.READ_ONLY,
    "estimate_holding_costs": ActionRisk.READ_ONLY,
    "estimate_rehab": ActionRisk.READ_ONLY,
    "extract_contract_data": ActionRisk.READ_ONLY,
    "forecast_appreciation": ActionRisk.READ_ONLY,
    "generate_tour_link": ActionRisk.READ_ONLY,
    "get_absorption_rate": ActionRisk.READ_ONLY,
    "get_agent_performance": ActionRisk.READ_ONLY,
    "get_agent_profile": ActionRisk.READ_ONLY,
    "get_audit_trail": ActionRisk.READ_ONLY,
    "get_billing_status": ActionRisk.READ_ONLY,
    "get_client_contact_history": ActionRisk.READ_ONLY,
    "get_client_detail": ActionRisk.READ_ONLY,
    "get_database_stats": ActionRisk.READ_ONLY,
    "get_days_on_market": ActionRisk.READ_ONLY,
    "get_deal_detail": ActionRisk.READ_ONLY,
    "get_deal_financial_summary": ActionRisk.READ_ONLY,
    "get_demographics": ActionRisk.READ_ONLY,
    "get_disclosure_requirements": ActionRisk.READ_ONLY,
    "get_distress_map": ActionRisk.READ_ONLY,
    "get_document_download_url": ActionRisk.READ_ONLY,
    "get_feature_flags": ActionRisk.READ_ONLY,
    "get_firehose_summary": ActionRisk.READ_ONLY,
    "get_flood_zone": ActionRisk.READ_ONLY,
    "get_form_library": ActionRisk.READ_ONLY,
    "get_govinfo_record": ActionRisk.READ_ONLY,
    "get_investment_activity": ActionRisk.READ_ONLY,
    "get_investor_activity_profile": ActionRisk.READ_ONLY,
    "get_job_queue": ActionRisk.READ_ONLY,
    "get_listing_detail": ActionRisk.READ_ONLY,
    "get_market_heatmap": ActionRisk.READ_ONLY,
    "get_market_trends": ActionRisk.READ_ONLY,
    "get_nearest_schools": ActionRisk.READ_ONLY,
    "get_onboarding_status": ActionRisk.READ_ONLY,
    "get_owner_history": ActionRisk.READ_ONLY,
    "get_portfolio_performance": ActionRisk.READ_ONLY,
    "get_price_reductions": ActionRisk.READ_ONLY,
    "get_property_distress": ActionRisk.READ_ONLY,
    "get_rent_estimates": ActionRisk.READ_ONLY,
    "get_retention_policy": ActionRisk.READ_ONLY,
    "get_splat_status": ActionRisk.READ_ONLY,
    "get_state_laws": ActionRisk.READ_ONLY,
    "get_team_pipeline": ActionRisk.READ_ONLY,
    "get_tenant_health": ActionRisk.READ_ONLY,
    "get_transaction_workflow": ActionRisk.READ_ONLY,
    "get_zoning_info": ActionRisk.READ_ONLY,
    "list_active_negotiations": ActionRisk.READ_ONLY,
    "list_agent_commissions": ActionRisk.READ_ONLY,
    "list_billing_invoices": ActionRisk.READ_ONLY,
    "list_client_activity": ActionRisk.READ_ONLY,
    "list_client_documents": ActionRisk.READ_ONLY,
    "list_client_tasks": ActionRisk.READ_ONLY,
    "list_closing_checklist": ActionRisk.READ_ONLY,
    "list_comparable_sales": ActionRisk.READ_ONLY,
    "list_contract_parties": ActionRisk.READ_ONLY,
    "list_contract_templates": ActionRisk.READ_ONLY,
    "list_counter_offers": ActionRisk.READ_ONLY,
    "list_deals": ActionRisk.READ_ONLY,
    "list_document_history": ActionRisk.READ_ONLY,
    "list_hot_markets": ActionRisk.READ_ONLY,
    "list_integration_status": ActionRisk.READ_ONLY,
    "list_legal_forms": ActionRisk.READ_ONLY,
    "list_market_snapshots": ActionRisk.READ_ONLY,
    "list_property_liens": ActionRisk.READ_ONLY,
    "list_property_photos": ActionRisk.READ_ONLY,
    "list_property_videos": ActionRisk.READ_ONLY,
    "list_providers": ActionRisk.READ_ONLY,
    "list_recent_errors": ActionRisk.READ_ONLY,
    "list_required_disclosures": ActionRisk.READ_ONLY,
    "list_tax_records": ActionRisk.READ_ONLY,
    "list_team_members": ActionRisk.READ_ONLY,
    "review_contract_terms": ActionRisk.READ_ONLY,
    "run_health_check": ActionRisk.READ_ONLY,
    "run_property_background": ActionRisk.READ_ONLY,
    "run_underwriting_model": ActionRisk.READ_ONLY,
    "search_clients": ActionRisk.READ_ONLY,
    "search_listings": ActionRisk.READ_ONLY,
    "search_public_records": ActionRisk.READ_ONLY,
    "suggest_client_matches": ActionRisk.READ_ONLY,
    "suggest_disposition": ActionRisk.READ_ONLY,
    "track_deadlines": ActionRisk.READ_ONLY,
    "verify_contract_signatures": ActionRisk.READ_ONLY,
    "web_search": ActionRisk.READ_ONLY,

    # ── Tenant-internal, undoable through the ai_chat_actions ledger (13) ──
    # ``merge_duplicate_clients`` is the outlier: it is tenant-internal but a
    # merge is not field-level undoable, so it stays off the allowlist even
    # though its class permits it. Class decides routing; the allowlist decides
    # what is offered. They are different questions.
    "add_client_note": ActionRisk.INTERNAL_EDIT,
    "add_client_tag": ActionRisk.INTERNAL_EDIT,
    "archive_client": ActionRisk.INTERNAL_EDIT,
    "assign_client": ActionRisk.INTERNAL_EDIT,
    "create_client": ActionRisk.INTERNAL_EDIT,
    "create_deal_note": ActionRisk.INTERNAL_EDIT,
    "merge_duplicate_clients": ActionRisk.INTERNAL_EDIT,
    "move_deal_stage": ActionRisk.INTERNAL_EDIT,
    "score_client_lead": ActionRisk.INTERNAL_EDIT,
    "set_client_stage": ActionRisk.INTERNAL_EDIT,
    "update_client": ActionRisk.INTERNAL_EDIT,
    "update_listing": ActionRisk.INTERNAL_EDIT,
    "upload_property_document": ActionRisk.INTERNAL_EDIT,

    # ── Produces a legal instrument (2) ──────────────────────────────
    "draft_contract": ActionRisk.LEGAL_DOCUMENT,
    "generate_assignment_agreement": ActionRisk.LEGAL_DOCUMENT,
    "generate_contract": ActionRisk.LEGAL_DOCUMENT,

    # ── Reaches a consumer on an asynchronous channel (2) ────────
    # These stage a command_executions row in `awaiting_approval` and return
    # its approval id. Nothing here sends: execution runs only through
    # commands_api.approve_command, which enqueues command:execute, where
    # guard_outreach applies TCPA, quiet hours and suppression last.
    "draft_email": ActionRisk.OUTREACH,
    "draft_sms": ActionRisk.OUTREACH,

    # ── Writes to a third-party calendar (1) ─────────────────────
    "schedule_event": ActionRisk.CALENDAR_WRITE,

    # ── Offers a property for sale (1) ───────────────────────────
    # Publication is a disposition decision with money behind it, which is why
    # marketplace_api already classifies it FINANCIAL.
    "publish_to_marketplace": ActionRisk.FINANCIAL,

    # Both of these spend real money per call — a pod reconstruction rents a GPU
    # (~$0.25-0.35), a video bills a generation provider. FINANCIAL is what puts
    # them behind an approval, so the model requests the spend and a human makes
    # it. An agent that could loop on either would bill without a ceiling.
    "request_property_reconstruction": ActionRisk.FINANCIAL,
    "request_listing_video": ActionRisk.FINANCIAL,

    # ── Contacts a consumer on a live channel (1) ────────────────
    "call_contact": ActionRisk.LIVE_CALL,
}


READ_ONLY_TOOLS: frozenset[str] = frozenset(
    name for name, risk in TOOL_RISK.items() if risk is ActionRisk.READ_ONLY
)

GATED_TOOLS: frozenset[str] = frozenset(
    name for name, risk in TOOL_RISK.items() if requires_approval(risk)
)


def risk_for(tool_name: str) -> ActionRisk:
    """The risk class of a tool, refusing rather than guessing.

    An unclassified name reaching this function means a tool was added to the
    catalog without a decision about what it may do. Defaulting to READ_ONLY
    would let that omission ship as a permission.
    """
    try:
        return TOOL_RISK[tool_name]
    except KeyError:
        raise KeyError(
            f"{tool_name!r} has no risk class. Add it to TOOL_RISK before "
            f"exposing it to the model."
        ) from None


def is_gated(tool_name: str) -> bool:
    """Whether the tool must create an approval instead of acting."""
    return requires_approval(risk_for(tool_name))
