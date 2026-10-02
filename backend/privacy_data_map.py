"""Where customer data lives, and what erasure does to each place.

Every table a migration creates is classified here: its retention category
(retention_policy.RetentionCategory) and its disposition when a brokerage is
erased. tests/test_privacy_data_map.py fails when a migration adds a table this
map does not name, so a new table cannot quietly escape erasure — or be
quietly destroyed when it holds evidence that must be kept.

docs/privacy-data-map.md is the narrative version (plaintext vs encrypted
columns, object keys, caches, browser storage). This module is the one the
erasure engine (privacy_lifecycle.py) executes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from retention_policy import RetentionCategory as C


class Disposition(str, Enum):
    ERASE = "erase"            # rows WHERE tenant_id = T are deleted
    VIA_PARENT = "via_parent"  # no tenant_id; removed by FK cascade from an erased parent
    RETAIN = "retain"          # kept after erasure, under its category's retention period
    TOMBSTONE = "tombstone"    # reduced to a keyed hash (suppression_tombstones), then deleted
    TENANT_ROW = "tenant_row"  # the tenants row itself: scrubbed, never deleted
    GLOBAL = "global"          # not brokerage data (reference, public, platform, licensed feed)


@dataclass(frozen=True)
class TableEntry:
    category: C
    disposition: Disposition
    note: str = ""


E, VP, R, T, G = (Disposition.ERASE, Disposition.VIA_PARENT, Disposition.RETAIN,
                  Disposition.TOMBSTONE, Disposition.GLOBAL)

TABLES: dict[str, TableEntry] = {
    # ── account ──────────────────────────────────────────────────────────
    "tenants": TableEntry(C.ACCOUNT, Disposition.TENANT_ROW,
                          "name/slug/website scrubbed; the row anchors receipts"),
    "users": TableEntry(C.ACCOUNT, E, "agent_id is the user's email"),
    "user_profiles": TableEntry(C.ACCOUNT, E, "tenant_id is text; no FK"),
    "team_memberships": TableEntry(C.ACCOUNT, E),
    "agent_licenses": TableEntry(C.ACCOUNT, E),
    "agent_ce_log": TableEntry(C.ACCOUNT, E, "no FK to tenants"),
    "agent_ai_settings": TableEntry(C.ACCOUNT, E),
    "autonomy_preferences": TableEntry(C.ACCOUNT, E),
    "agent_routing_state": TableEntry(C.ACCOUNT, E),
    "brokerage_invitations": TableEntry(C.CAPABILITY_TOKEN, E),
    "brokerage_setup_progress": TableEntry(C.ACCOUNT, E),
    "user_policy_acceptances": TableEntry(C.ACCOUNT, E),
    "account_security_acceptances": TableEntry(C.ACCOUNT, E),
    # ── contact PII / CRM ────────────────────────────────────────────────
    "clients": TableEntry(C.CONTACT_PII, E, "name/email/phone plaintext"),
    "agent_contacts": TableEntry(C.CONTACT_PII, E, "PII encrypted; HMAC lookups"),
    "leads": TableEntry(C.BUSINESS_CRM, E, "payload holds owner names"),
    "transaction_parties": TableEntry(C.CONTACT_PII, E),
    "contact_property_relationships": TableEntry(C.CONTACT_PII, E),
    "buyer_profiles": TableEntry(C.CONTACT_PII, E),
    "lead_intake_events": TableEntry(C.CONTACT_PII, E),
    "client_notes": TableEntry(C.BUSINESS_CRM, E),
    "client_tasks": TableEntry(C.BUSINESS_CRM, E),
    "client_activities": TableEntry(C.BUSINESS_CRM, E),
    "client_tags": TableEntry(C.BUSINESS_CRM, E),
    "client_segments": TableEntry(C.BUSINESS_CRM, E),
    "showings": TableEntry(C.BUSINESS_CRM, E),
    "listings": TableEntry(C.BUSINESS_CRM, E),
    "listing_grants": TableEntry(C.BUSINESS_CRM, VP, "grantor/grantee tenant columns; cascades from listings"),
    "transactions": TableEntry(C.BUSINESS_CRM, E),
    "transaction_offers": TableEntry(C.BUSINESS_CRM, E),
    "transaction_milestones": TableEntry(C.BUSINESS_CRM, E),
    "compliance_checklist_items": TableEntry(C.BUSINESS_CRM, E),
    "buyer_requests": TableEntry(C.BUSINESS_CRM, E),
    "marketplace_publications": TableEntry(C.BUSINESS_CRM, E),
    "marketplace_matches": TableEntry(C.BUSINESS_CRM, E),
    "intake_handoff_tasks": TableEntry(C.BUSINESS_CRM, E),
    "lead_routing_rules": TableEntry(C.BUSINESS_CRM, E),
    "smart_plans": TableEntry(C.BUSINESS_CRM, E),
    "smart_plan_revisions": TableEntry(C.BUSINESS_CRM, E, "immutable except under erasure"),
    "smart_plan_enrollments": TableEntry(C.BUSINESS_CRM, E),
    "smart_plan_step_runs": TableEntry(C.BUSINESS_CRM, E),
    "missions": TableEntry(C.BUSINESS_CRM, E),
    "mission_candidates": TableEntry(C.BUSINESS_CRM, E),
    "mission_actions": TableEntry(C.BUSINESS_CRM, E),
    "mission_events": TableEntry(C.BUSINESS_CRM, E),
    "hyperlocal_sites": TableEntry(C.BUSINESS_CRM, E, "public site: unpublished at freeze"),
    "hyperlocal_site_revisions": TableEntry(C.BUSINESS_CRM, E),
    "hyperlocal_site_domains": TableEntry(C.BUSINESS_CRM, E),
    "hyperlocal_site_collaborators": TableEntry(C.BUSINESS_CRM, E),
    "hyperlocal_site_attribution_events": TableEntry(C.COMMUNICATION_METADATA, E),
    "studio_campaigns": TableEntry(C.BUSINESS_CRM, E),
    "tenant_action_budgets": TableEntry(C.OPERATIONAL, E),
    "tenant_contract_template_registrations": TableEntry(C.BUSINESS_CRM, E),
    "lead_pipeline_counts": TableEntry(C.DERIVED, E),
    # ── communication ────────────────────────────────────────────────────
    "sms_messages": TableEntry(C.COMMUNICATION_CONTENT, E, "bodies plaintext"),
    "email_outbox": TableEntry(C.COMMUNICATION_CONTENT, E),
    "interaction_logs": TableEntry(C.COMMUNICATION_CONTENT, E),
    "inbound_voice_calls": TableEntry(C.CALL_TRANSCRIPT, E, "transcript encrypted"),
    "contact_intake_sessions": TableEntry(C.CALL_TRANSCRIPT, E),
    "negotiation_events": TableEntry(C.CALL_TRANSCRIPT, E),
    "command_executions": TableEntry(C.COMMUNICATION_CONTENT, E, "outbound drafts"),
    "telephony_routes": TableEntry(C.COMMUNICATION_METADATA, E, "numbers released first"),
    "provider_purchases": TableEntry(C.FINANCIAL_BILLING, E, "number purchase intents; numbers released first"),
    "messaging_routes": TableEntry(C.COMMUNICATION_METADATA, E, "hosted SMS disconnected first"),
    "messaging_hosted_documents": TableEntry(C.DOCUMENT, E, "LOA/invoice objects deleted"),
    "tenant_messaging_brands": TableEntry(C.CONTACT_PII, E, "EIN, address"),
    "tenant_messaging_campaigns": TableEntry(C.COMMUNICATION_METADATA, E),
    "live_call_sessions": TableEntry(C.COMMUNICATION_METADATA, E),
    "agent_call_intents": TableEntry(C.COMMUNICATION_METADATA, E),
    "contact_nurture_jobs": TableEntry(C.COMMUNICATION_METADATA, E),
    "lead_response_events": TableEntry(C.COMMUNICATION_METADATA, E),
    # ── consent ──────────────────────────────────────────────────────────
    "outreach_consent": TableEntry(C.CONSENT_SUPPRESSION, E,
                                   "plaintext proof; opt-outs survive as tombstones"),
    "outreach_suppression": TableEntry(C.CONSENT_SUPPRESSION, T),
    "outreach_attempt_log": TableEntry(C.CONSENT_SUPPRESSION, E),
    "suppression_tombstones": TableEntry(C.CONSENT_SUPPRESSION, R, "keyed hash only, 5 years"),
    "action_approvals": TableEntry(C.AUDIT_SECURITY, E),
    "protected_override_events": TableEntry(C.AUDIT_SECURITY, E),
    "source_licenses": TableEntry(C.BUSINESS_CRM, E),
    # ── billing (Neoh is the controller of its own billing records) ──────
    "subscriptions": TableEntry(C.FINANCIAL_BILLING, R, "7 years; Stripe ids, no customer content"),
    "billing_usage_events": TableEntry(C.FINANCIAL_BILLING, R, "7 years"),
    "stripe_webhook_events": TableEntry(C.FINANCIAL_BILLING, G, "event ids only"),
    # ── AI ───────────────────────────────────────────────────────────────
    "ai_chat_messages": TableEntry(C.AI_CHAT, E),
    "ai_chat_actions": TableEntry(C.AI_CHAT, E),
    "ai_chat_message_attachments": TableEntry(C.AI_CHAT, E),
    "ai_tool_operations": TableEntry(C.AI_CHAT, E),
    "user_interactions": TableEntry(C.AI_CHAT, E, "raw chat turns plaintext; tenant_id text"),
    "ai_decision_traces": TableEntry(C.DERIVED, E),
    "agent_decisions": TableEntry(C.DERIVED, E),
    "beliefs": TableEntry(C.AI_MEMORY, E),
    "outcome_events": TableEntry(C.DERIVED, E),
    "client_ai_state": TableEntry(C.AI_MEMORY, E),
    "style_training_examples": TableEntry(C.AI_MEMORY, E),
    "model_registry": TableEntry(C.DERIVED, E),
    "model_training_runs": TableEntry(C.DERIVED, E),
    "model_evaluations": TableEntry(C.DERIVED, E),
    "intelligence_scores": TableEntry(C.DERIVED, E),
    "property_signals": TableEntry(C.DERIVED, E),
    "property_characteristic_inferences": TableEntry(C.DERIVED, E),
    "zoning_analyses": TableEntry(C.DERIVED, E),
    "title_findings": TableEntry(C.DERIVED, E),
    "entity_nodes": TableEntry(C.DERIVED, E),
    "entity_links": TableEntry(C.DERIVED, E),
    # ── documents & media ────────────────────────────────────────────────
    "contract_documents": TableEntry(C.DOCUMENT, E, "objects deleted from the vault bucket"),
    "contract_draft_workspaces": TableEntry(C.DOCUMENT, E),
    "contract_synthesis_artifacts": TableEntry(C.DOCUMENT, E),
    "contract_templates": TableEntry(C.DOCUMENT, E),
    "ai_record_attachments": TableEntry(C.DOCUMENT, E),
    "property_media": TableEntry(C.MEDIA, E, "objects deleted by key"),
    "media_blobs": TableEntry(C.MEDIA, VP, "cascades from property_media"),
    "property_pano_scenes": TableEntry(C.MEDIA, E),
    "property_floorplans": TableEntry(C.SPATIAL_DERIVED, E),
    "property_floorplan_revisions": TableEntry(C.SPATIAL_DERIVED, E),
    "property_view_upload_links": TableEntry(C.CAPABILITY_TOKEN, E),
    "capture_sessions": TableEntry(C.SPATIAL_SOURCE, E, "no FK to tenants"),
    "reconstruction_jobs": TableEntry(C.SPATIAL_DERIVED, E),
    "spatial_tour_variants": TableEntry(C.SPATIAL_DERIVED, E),
    "video_studio_jobs": TableEntry(C.MEDIA, E),
    "voice_walkthrough_jobs": TableEntry(C.CALL_AUDIO, E),
    # ── audit / security ─────────────────────────────────────────────────
    "audit_ledger": TableEntry(C.AUDIT_SECURITY, R, "hash chain; expires by age, not by closure"),
    "audit_anomaly_alerts": TableEntry(C.AUDIT_SECURITY, R),
    "audit_chain_checkpoints": TableEntry(C.AUDIT_SECURITY, G, "hash anchors of expired audit rows; no customer data"),
    "api_rate_limit_windows": TableEntry(C.OPERATIONAL, G, "minutes-long windows"),
    "process_heartbeats": TableEntry(C.OPERATIONAL, G),
    "ops_alerts": TableEntry(C.OPERATIONAL, G, "component incidents; no customer data"),
    "operator_otp_challenges": TableEntry(C.SECRET, G, "operator, not customer"),
    "operator_totp_uses": TableEntry(C.SECRET, G, "operator, not customer"),
    # ── secrets & capability tokens ──────────────────────────────────────
    "provider_credentials": TableEntry(C.SECRET, E, "revoked at the provider first"),
    "lead_source_connectors": TableEntry(C.SECRET, E),
    "oauth_authorization_states": TableEntry(C.CAPABILITY_TOKEN, E),
    "password_reset_tokens": TableEntry(C.CAPABILITY_TOKEN, E),
    "client_portals": TableEntry(C.CAPABILITY_TOKEN, E),
    # ── operational ──────────────────────────────────────────────────────
    "automation_jobs": TableEntry(C.OPERATIONAL, E),
    "automation_job_attempts": TableEntry(C.OPERATIONAL, E),
    "harvest_sources": TableEntry(C.PUBLIC_DATA, E),
    "harvest_runs": TableEntry(C.PUBLIC_DATA, E),
    "source_records": TableEntry(C.PUBLIC_DATA, E),
    "di_cache": TableEntry(C.CACHE, G, "shared vendor cache with TTLs, not tenant-keyed"),
    "schema_migrations": TableEntry(C.OPERATIONAL, G),
    # ── privacy records (survive erasure by design) ──────────────────────
    "privacy_operations": TableEntry(C.PRIVACY_RECORD, R),
    "erasure_ledger": TableEntry(C.PRIVACY_RECORD, R),
    "legal_holds": TableEntry(C.PRIVACY_RECORD, R),
    # ── global reference / public / licensed ─────────────────────────────
    "public_property_records": TableEntry(C.PUBLIC_DATA, G),
    "public_market_metrics": TableEntry(C.PUBLIC_DATA, G),
    "county_market_stats": TableEntry(C.PUBLIC_DATA, G),
    "state_market_stats": TableEntry(C.PUBLIC_DATA, G),
    "fema_communities": TableEntry(C.PUBLIC_DATA, G),
    "fema_flood_zones": TableEntry(C.PUBLIC_DATA, G),
    "school_districts": TableEntry(C.PUBLIC_DATA, G),
    "parcel_zoning": TableEntry(C.PUBLIC_DATA, G),
    "state_regulatory_profiles": TableEntry(C.PUBLIC_DATA, G),
    "state_licensing_requirements": TableEntry(C.PUBLIC_DATA, G),
    "state_advertising_rules": TableEntry(C.PUBLIC_DATA, G),
    "state_contract_templates": TableEntry(C.PUBLIC_DATA, G),
    "state_disclosure_forms": TableEntry(C.PUBLIC_DATA, G),
    "state_reciprocity_matrix": TableEntry(C.PUBLIC_DATA, G),
    "authorized_document_sources": TableEntry(C.PUBLIC_DATA, G),
    "authorized_form_source_links": TableEntry(C.PUBLIC_DATA, G),
    "contract_template_sources": TableEntry(C.PUBLIC_DATA, G),
    "oracle_mls_listings": TableEntry(C.LICENSED_MLS, G, "purged per feed on licence termination"),
    "mls_boards": TableEntry(C.LICENSED_MLS, G),
    "mls_sync_status": TableEntry(C.LICENSED_MLS, G),
    "mls_feed_entitlements": TableEntry(C.LICENSED_MLS, E),
}

# Tables erasure must never delete from, whatever the catalog says. The DB
# function privacy_erase_tenant_batch() carries the same list — keep in step
# (tests/test_privacy_data_map.py compares them).
NEVER_ERASED = frozenset(
    name for name, entry in TABLES.items()
    if entry.disposition in (Disposition.RETAIN, Disposition.TENANT_ROW, Disposition.GLOBAL)
)

# Text columns that hold user identities (emails) in retained tables. Erasure
# replaces them with a stable pseudonym so the retained evidence stays
# internally consistent but no longer names a person.
PSEUDONYMIZE_ON_ERASURE: dict[str, tuple[str, ...]] = {
    "audit_anomaly_alerts": ("actor_id",),
}


def entry(table: str) -> TableEntry:
    return TABLES[table]


def erasable() -> list[str]:
    return sorted(n for n, e in TABLES.items() if e.disposition in (Disposition.ERASE, Disposition.TOMBSTONE))
