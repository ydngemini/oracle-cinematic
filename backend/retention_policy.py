"""The one place Neoh decides how long each kind of data lives.

Every retention period, trigger and deletion behaviour is defined here and
nowhere else — modules ask `policy_for(category)` instead of hard-coding
"30 days" or "forever" (privacy mission, 2026-10-01). The human-readable
version, with the legal reasoning and what is still awaiting counsel, is
docs/data-retention-policy.md; the field-by-field map is docs/privacy-data-map.md.

Durations are configurable because most are business or legal policy, not
engineering facts. A default marked `counsel_review=True` is an engineering
placeholder chosen to be conservative and must be confirmed before it is
represented to customers as policy.

A retention clock starts at a TRIGGER (when the data stops being needed for
its purpose), not at creation: a client record a brokerage still works is not
"old". `None` duration means "kept while the trigger has not fired" — for
tenant data that is the life of the brokerage's account.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Optional

POLICY_VERSION = "2026-10-01.1"


class RetentionCategory(str, Enum):
    ACCOUNT = "account"                          # users, profiles, memberships
    BUSINESS_CRM = "business_crm"                # clients, leads, deals, tasks, notes
    CONTACT_PII = "contact_pii"                  # names, emails, phones, addresses of people in the CRM
    COMMUNICATION_CONTENT = "communication_content"   # SMS/email/chat bodies
    COMMUNICATION_METADATA = "communication_metadata"  # who/when/status of a message or call
    CALL_AUDIO = "call_audio"                    # recorded audio, if any
    CALL_TRANSCRIPT = "call_transcript"          # transcripts and intake answers of calls
    CONSENT_SUPPRESSION = "consent_suppression"  # opt-in evidence, STOP / do-not-contact
    FINANCIAL_BILLING = "financial_billing"      # subscription and usage records, Stripe ids
    AI_CHAT = "ai_chat"                          # user <-> Neoh conversation turns
    AI_MEMORY = "ai_memory"                      # durable facts/preferences Neoh learned
    DERIVED = "derived"                          # scores, matches, beliefs, decision traces
    DOCUMENT = "document"                        # contracts, PDFs, vault documents
    MEDIA = "media"                              # property photos/videos, uploads
    SPATIAL_SOURCE = "spatial_source"            # original 3D capture media (irreplaceable)
    SPATIAL_DERIVED = "spatial_derived"          # reconstructions, SOG, floorplans (rebuildable)
    AUDIT_SECURITY = "audit_security"            # audit ledger, security events
    SECRET = "secret"                            # provider credentials, OAuth tokens
    CAPABILITY_TOKEN = "capability_token"        # invites, reset links, portal/upload links, OAuth state
    PUBLIC_DATA = "public_data"                  # harvested public records
    LICENSED_MLS = "licensed_mls"                # platform-wide licensed listing feed
    CACHE = "cache"                              # integration and session caches
    OPERATIONAL = "operational"                  # finished jobs, rate-limit windows, heartbeats
    EXPORT_ARTIFACT = "export_artifact"          # a generated data export
    PRIVACY_RECORD = "privacy_record"            # privacy requests, erasure receipts, holds


class Trigger(str, Enum):
    TENANT_ERASED = "tenant_erased"              # brokerage closure completed its grace period
    USER_OFFBOARDED = "user_offboarded"
    RECORD_DELETED = "record_deleted"            # a person/record was deleted in the CRM
    MESSAGE_SENT = "message_sent"
    CALL_COMPLETED = "call_completed"
    CONVERSATION_INACTIVE = "conversation_inactive"
    SOURCE_DELETED = "source_deleted"            # the thing it was derived from went away
    EXPIRED = "expired"                          # the token/link expired or was used
    JOB_FINISHED = "job_finished"
    CREATED = "created"
    LAST_EVENT = "last_event"                    # the newest event for that subject
    ACCOUNT_CLOSED = "account_closed"            # billing relationship ended
    LICENSE_ENDED = "license_ended"


class DeletionMode(str, Enum):
    ERASE = "erase"                  # rows and objects removed
    ANONYMIZE = "anonymize"          # shape/totals kept, identity removed
    HASH_TOMBSTONE = "hash_tombstone"  # only a keyed hash survives (suppression)
    RETAIN = "retain"                # kept for a stated basis (audit, accounting)
    REVOKE_AND_ERASE = "revoke_and_erase"  # external revoke first, then erase


@dataclass(frozen=True)
class Policy:
    category: RetentionCategory
    trigger: Trigger
    retention_days: Optional[int]    # after the trigger; None = until the trigger fires
    deletion_mode: DeletionMode
    legal_hold_applies: bool         # a hold blocks physical deletion
    backup_behavior: str             # what happens to copies in backups
    basis: str                       # why it is kept that long
    counsel_review: bool             # duration is a placeholder pending counsel
    env: Optional[str] = None        # override variable

    def as_dict(self) -> dict:
        d = asdict(self)
        return {k: (v.value if isinstance(v, Enum) else v) for k, v in d.items()}


# DigitalOcean Managed PostgreSQL keeps daily backups / PITR for 7 days and
# they cannot be edited, downloaded or selectively purged (docs/disaster-
# recovery.md). Every erasure is therefore complete in the live system at once
# and in backups when they age out.
BACKUP_DAYS = 7
_BACKUP = (f"Remains in managed database backups for up to {BACKUP_DAYS} days after "
           "erasure, then expires; a restore re-applies completed erasures before "
           "service resumes (scripts/reapply-erasures.py).")
_BACKUP_OBJECTS = ("Object storage deletion is immediate for the live object; Spaces "
                   "versioning is off, so no prior versions remain.")


def _days(env: str, default: Optional[int]) -> Optional[int]:
    raw = os.getenv(env)
    if raw is None or not raw.strip():
        return default
    if raw.strip().lower() in ("none", "unlimited", "keep"):
        return None
    value = int(raw)
    if not 0 <= value <= 3650:
        raise ValueError(f"{env} must be between 0 and 3650 days")
    return value


def _p(category, trigger, default, mode, *, hold=True, backup=_BACKUP, basis, counsel, env=None):
    days = _days(env, default) if env else default
    return Policy(category, trigger, days, mode, hold, backup, basis, counsel, env)


def _build() -> dict[RetentionCategory, Policy]:
    C, T, M = RetentionCategory, Trigger, DeletionMode
    policies = [
        _p(C.ACCOUNT, T.TENANT_ERASED, 0, M.ERASE,
           basis="Login identity is needed while the brokerage account exists; an "
                 "offboarded agent's account is suspended (not erased) so the "
                 "brokerage keeps attribution of their historical work.",
           counsel=False),
        _p(C.BUSINESS_CRM, T.TENANT_ERASED, 0, M.ERASE,
           basis="The brokerage's own records (Neoh acts as its processor). Brokerages "
                 "carry their own record-keeping duties; they keep the data by exporting "
                 "before closure.",
           counsel=False),
        _p(C.CONTACT_PII, T.RECORD_DELETED, 0, M.ERASE,
           basis="Deleted with the CRM record or the brokerage.", counsel=False),
        _p(C.COMMUNICATION_CONTENT, T.TENANT_ERASED, None, M.ERASE,
           env="ORACLE_MESSAGE_BODY_RETENTION_DAYS",
           basis="Message bodies are part of the brokerage's client file. Set a duration "
                 "to expire bodies earlier while keeping metadata.",
           counsel=True),
        _p(C.COMMUNICATION_METADATA, T.TENANT_ERASED, 0, M.ERASE,
           basis="Chronology, delivery and consent evidence for the brokerage.",
           counsel=False),
        _p(C.CALL_AUDIO, T.CALL_COMPLETED, 30, M.ERASE,
           env="ORACLE_CALL_AUDIO_RETENTION_DAYS",
           basis="Audio is the most sensitive form of a call; the transcript and summary "
                 "carry the business value.",
           counsel=True),
        _p(C.CALL_TRANSCRIPT, T.CALL_COMPLETED, None, M.ERASE,
           env="ORACLE_CALL_TRANSCRIPT_RETENTION_DAYS",
           basis="Transcripts hold far more personal detail than the call record, but a "
                 "brokerage's 'substantive communications with parties' are ITS records "
                 "(e.g. TREC 22 TAC 535.2: 4 years; Maryland 5) — Neoh, as processor, does "
                 "not expire them on its own schedule. Set a duration per deployment once "
                 "the brokerage's policy allows it.",
           counsel=True),
        _p(C.CONSENT_SUPPRESSION, T.LAST_EVENT, 1826, M.HASH_TOMBSTONE,
           env="ORACLE_CONSENT_EVIDENCE_RETENTION_DAYS",
           basis="Proof of consent and of opt-out must outlive the contact record so a "
                 "deleted or re-imported person is never contacted again: an internal "
                 "do-not-call request lasts 5 years (47 CFR 64.1200(d)(6)); TCPA claims "
                 "have a 4-year limitations period (28 USC 1658). Only keyed hashes of "
                 "the normalised phone/email survive contact deletion.",
           counsel=True),
        _p(C.FINANCIAL_BILLING, T.ACCOUNT_CLOSED, 2557, M.ANONYMIZE,
           env="ORACLE_BILLING_RECORD_RETENTION_DAYS",
           basis="Neoh's own accounting/tax records (IRS: 3 years generally, up to 7 for "
                 "some claims — the conservative case is used). "
                 "Kept without CRM content; Stripe keeps its own payment records.",
           counsel=True),
        _p(C.AI_CHAT, T.TENANT_ERASED, None, M.ERASE,
           env="ORACLE_AI_CHAT_RETENTION_DAYS",
           basis="Conversation history with Neoh belongs to the agent's working context. "
                 "Set a duration to expire inactive conversations.",
           counsel=True),
        _p(C.AI_MEMORY, T.SOURCE_DELETED, 0, M.ERASE,
           basis="A learned fact must not outlive the record it came from.",
           counsel=False),
        _p(C.DERIVED, T.SOURCE_DELETED, 0, M.ERASE,
           basis="Scores, matches and traces are regenerable copies of their source.",
           counsel=False),
        _p(C.DOCUMENT, T.TENANT_ERASED, 0, M.ERASE, backup=_BACKUP + " " + _BACKUP_OBJECTS,
           basis="Contracts are the brokerage's records; it exports them before closure.",
           counsel=False),
        _p(C.MEDIA, T.TENANT_ERASED, 0, M.ERASE, backup=_BACKUP_OBJECTS,
           basis="Property media is the brokerage's.", counsel=False),
        _p(C.SPATIAL_SOURCE, T.TENANT_ERASED, 0, M.ERASE, backup=_BACKUP_OBJECTS,
           basis="Original captures are irreplaceable customer input — kept for the life "
                 "of the property record, never discarded because processing failed.",
           counsel=False),
        _p(C.SPATIAL_DERIVED, T.SOURCE_DELETED, 0, M.ERASE, backup=_BACKUP_OBJECTS,
           basis="Rebuildable from the source capture.", counsel=False),
        _p(C.AUDIT_SECURITY, T.CREATED, 730, M.RETAIN, hold=True,
           env="ORACLE_AUDIT_RETENTION_DAYS",
           basis="Tamper-evident record of who did what. Append-only: it is NOT edited "
                 "when a brokerage closes; rows hold actor ids and action names, never "
                 "message, transcript or document content.",
           counsel=True),
        _p(C.SECRET, T.USER_OFFBOARDED, 0, M.REVOKE_AND_ERASE, hold=False,
           basis="Credentials are revoked at the provider and deleted the moment their "
                 "owner leaves; nothing justifies keeping them.",
           counsel=False),
        _p(C.CAPABILITY_TOKEN, T.EXPIRED, 30, M.ERASE, hold=False,
           env="ORACLE_EXPIRED_TOKEN_RETENTION_DAYS",
           basis="Kept briefly after expiry or use for abuse investigation; only hashes "
                 "are ever stored.", counsel=False),
        _p(C.PUBLIC_DATA, T.CREATED, 730, M.ERASE,
           env="ORACLE_RAW_SOURCE_RETENTION_DAYS",
           basis="Raw harvested public-record payloads (existing purge).", counsel=False),
        _p(C.LICENSED_MLS, T.LICENSE_ENDED, 0, M.ERASE,
           basis="Platform-wide licensed feed governed by each MLS licence; a closing "
                 "brokerage loses its entitlement and matches, not the shared feed.",
           counsel=True),
        _p(C.CACHE, T.TENANT_ERASED, 0, M.ERASE, hold=False, backup="Not backed up.",
           basis="Regenerable.", counsel=False),
        _p(C.OPERATIONAL, T.JOB_FINISHED, 90, M.ERASE, hold=False,
           env="ORACLE_FINISHED_JOB_RETENTION_DAYS",
           basis="Debugging window for finished background work.", counsel=False),
        _p(C.EXPORT_ARTIFACT, T.CREATED, 7, M.ERASE, hold=False, backup=_BACKUP_OBJECTS,
           env="ORACLE_EXPORT_RETENTION_DAYS",
           basis="A complete copy of a brokerage's data — removed quickly once it has "
                 "been downloaded.", counsel=False),
        _p(C.PRIVACY_RECORD, T.CREATED, 2557, M.RETAIN,
           env="ORACLE_PRIVACY_RECORD_RETENTION_DAYS",
           basis="Proof that a request was handled or data was erased; holds no erased "
                 "content.", counsel=True),
    ]
    return {p.category: p for p in policies}


def policy_for(category: RetentionCategory) -> Policy:
    """Policy for a category, reading overrides from the environment at call
    time so an operator change needs no code release."""
    return _build()[RetentionCategory(category)]


def all_policies() -> list[dict]:
    return [p.as_dict() for p in _build().values()]


# Brokerage lifecycle windows (business policy, not law).
def closure_grace_days() -> int:
    """Days between an owner requesting closure and irreversible erasure,
    during which the owner can export or cancel the closure."""
    days = _days("ORACLE_CLOSURE_GRACE_DAYS", 30)
    return 30 if days is None else days


def canceled_account_erasure_days() -> Optional[int]:
    """Days after a subscription ends (without a closure request) before the
    account is scheduled for erasure. None (default) = never automatically —
    an operator schedules it; set a value once counsel has agreed the
    customer-facing terms."""
    return _days("ORACLE_CANCELED_ACCOUNT_ERASURE_DAYS", None)
