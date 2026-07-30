"""Shared policy, provenance, and approval primitives for Oracle intelligence.

Every new platform surface imports this module.  Keeping these rules in one
place prevents an individual router or worker from quietly weakening the
public-record, fair-housing, provenance, or human-approval posture.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Feature(str, Enum):
    AUTOMATION = "automation"
    MUNICIPAL_HARVESTS = "municipal_harvests"
    PREDICTIVE_INTELLIGENCE = "predictive_intelligence"
    MARKETPLACE = "marketplace"
    LOCAL_MODELS = "local_models"
    SPATIAL_TOURS = "spatial_tours"
    CONTRACTS = "contracts"
    AI_CHAT = "ai_chat"


_FEATURE_ENV = {
    Feature.AUTOMATION: "ORACLE_FEATURE_AUTOMATION",
    Feature.MUNICIPAL_HARVESTS: "ORACLE_FEATURE_MUNICIPAL_HARVESTS",
    Feature.PREDICTIVE_INTELLIGENCE: "ORACLE_FEATURE_PREDICTIVE_INTELLIGENCE",
    Feature.MARKETPLACE: "ORACLE_FEATURE_MARKETPLACE",
    Feature.LOCAL_MODELS: "ORACLE_FEATURE_LOCAL_MODELS",
    Feature.SPATIAL_TOURS: "ORACLE_FEATURE_SPATIAL_TOURS",
    Feature.CONTRACTS: "ORACLE_FEATURE_CONTRACTS",
    Feature.AI_CHAT: "ORACLE_FEATURE_AI_CHAT",
}


def feature_enabled(feature: Feature, *, default: bool = True) -> bool:
    """Return a staged feature-flag value.

    The code ships enabled in development and can be independently disabled in
    a staged ECS rollout.  A false value produces 404 instead of advertising a
    disabled high-risk capability.
    """
    raw = os.getenv(_FEATURE_ENV[feature])
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def require_feature(feature: Feature) -> None:
    if feature_enabled(feature):
        return
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Feature is not enabled for this deployment.",
    )


class EvidenceStatus(str, Enum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    MIXED = "mixed"


class SourceCitation(BaseModel):
    """One source supporting an intelligence result."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=2, max_length=160)
    record_id: Optional[str] = Field(default=None, max_length=240)
    source_url: Optional[str] = Field(default=None, max_length=2_048)
    observed_at: date
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    license: str = Field(default="public-record", min_length=2, max_length=120)
    evidence_status: EvidenceStatus = EvidenceStatus.OBSERVED

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not value.startswith(("https://", "http://")):
            raise ValueError("source_url must use http or https")
        return value


class UnderwritingTrace(BaseModel):
    """Reproducible calculation record; never hidden chain-of-thought."""

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any]
    formulas: list[str]
    comparable_evidence: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    outputs: dict[str, Any]


class IntelligenceEnvelope(BaseModel):
    """Mandatory response wrapper for every intelligence calculation."""

    model_config = ConfigDict(extra="forbid")

    analysis_type: str = Field(min_length=2, max_length=80)
    subject_id: str = Field(min_length=1, max_length=240)
    evidence_status: EvidenceStatus
    observation_date: date
    confidence: float = Field(ge=0.0, le=1.0)
    model_version: str = Field(min_length=1, max_length=160)
    sources: list[SourceCitation] = Field(min_length=1)
    result: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)
    trace: Optional[UnderwritingTrace] = None

    @model_validator(mode="after")
    def validate_provenance(self) -> "IntelligenceEnvelope":
        observed = any(s.evidence_status is EvidenceStatus.OBSERVED for s in self.sources)
        if not observed:
            raise ValueError("at least one observed source is required")
        if self.evidence_status is EvidenceStatus.OBSERVED:
            if any(s.evidence_status is not EvidenceStatus.OBSERVED for s in self.sources):
                raise ValueError("observed results cannot cite inferred sources")
        return self


class ActionRisk(str, Enum):
    READ_ONLY = "read_only"
    INTERNAL_EDIT = "internal_edit"
    OUTREACH = "outreach"
    LIVE_CALL = "live_call"
    CALENDAR_WRITE = "calendar_write"
    FINANCIAL = "financial"
    BIDDING_MESSAGE = "bidding_message"
    LEGAL_DOCUMENT = "legal_document"
    ROLE_OVERRIDE = "role_override"


APPROVAL_REQUIRED: frozenset[ActionRisk] = frozenset(
    {
        ActionRisk.OUTREACH,
        ActionRisk.LIVE_CALL,
        ActionRisk.CALENDAR_WRITE,
        ActionRisk.FINANCIAL,
        ActionRisk.BIDDING_MESSAGE,
        ActionRisk.LEGAL_DOCUMENT,
        ActionRisk.ROLE_OVERRIDE,
    }
)


def requires_approval(risk: ActionRisk) -> bool:
    return risk in APPROVAL_REQUIRED


def validate_approval_reason(reason: str) -> str:
    cleaned = " ".join((reason or "").split())
    if len(cleaned) < 8 or len(cleaned) > 500:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Approval reason must be between 8 and 500 characters.",
        )
    return cleaned


# Protected traits, private consumer data, and expressly excluded covert
# inference techniques.  Matching operates on normalized *field names*, not
# values, so a street named "Church" is not incorrectly treated as religion.
_PROHIBITED_FIELDS = frozenset(
    {
        "race",
        "ethnicity",
        "religion",
        "national_origin",
        "citizenship",
        "sex",
        "sexual_orientation",
        "gender_identity",
        "familial_status",
        "pregnancy",
        "disability",
        "medical_condition",
        "veteran_status",
        "age_of_occupant",
        "children",
        "credit_score",
        "consumer_credit",
        "consumer_credit_utilization",
        "utility_shutoff",
        "utility_payment_history",
        "bank_balance",
        "biometric",
        "micro_tremor",
        "voice_stress",
        "emotion_detection",
        "personality_profile",
        "psychographic_profile",
        "covert_profile",
        "private_contact",
    }
)

_FIELD_NORMALIZER = re.compile(r"[^a-z0-9]+")


def _normalized_field(value: Any) -> str:
    return _FIELD_NORMALIZER.sub("_", str(value).strip().lower()).strip("_")


def prohibited_fields(payload: Any, *, _prefix: str = "") -> list[str]:
    """Return prohibited input paths found anywhere in a nested payload."""
    found: list[str] = []
    if isinstance(payload, Mapping):
        for raw_key, value in payload.items():
            key = _normalized_field(raw_key)
            path = f"{_prefix}.{key}" if _prefix else key
            if key in _PROHIBITED_FIELDS or any(
                key.startswith(f"{blocked}_") for blocked in _PROHIBITED_FIELDS
            ):
                found.append(path)
            found.extend(prohibited_fields(value, _prefix=path))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            found.extend(prohibited_fields(value, _prefix=f"{_prefix}[{index}]"))
    return found


def enforce_public_property_data(payload: Any) -> None:
    """Reject sensitive/private inputs before scoring, matching, or outreach."""
    blocked = sorted(set(prohibited_fields(payload)))
    if not blocked:
        return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "PROHIBITED_DATA",
            "message": "Only public, licensed property and market data may be used.",
            "fields": blocked,
        },
    )


_ALLOWED_TONES = frozenset({"concise", "warm", "formal", "neutral", "direct"})


def communication_preferences(
    explicit_preferences: Optional[Mapping[str, Any]],
    *,
    approved_tone: Optional[str] = None,
) -> dict[str, Any]:
    """Return communication preferences from explicit, non-inferred settings."""
    raw = dict(explicit_preferences or {})
    result: dict[str, Any] = {}
    if "channel" in raw and raw["channel"] in {"email", "sms", "call"}:
        result["channel"] = raw["channel"]
    if "contact_window" in raw and isinstance(raw["contact_window"], str):
        result["contact_window"] = raw["contact_window"][:80]
    tone = approved_tone or raw.get("tone")
    if tone in _ALLOWED_TONES:
        result["tone"] = tone
    result["basis"] = "explicit_preferences_and_conversation_content"
    return result


PUBLIC_PROPERTY_DATA_POLICY = {
    "allowed": [
        "licensed MLS and assessor records",
        "recorder, tax, court, permit, zoning, and municipal records",
        "aggregate census, crime, flood, inventory, and commercial activity",
        "consented conversation content and explicit communication preferences",
    ],
    "prohibited": sorted(_PROHIBITED_FIELDS),
    "professional_review_required": [
        "legal documents and redlines",
        "title and lien conclusions",
        "zoning conclusions",
        "tax estimates",
        "offers, outreach, calls, calendar writes, and bidding messages",
    ],
    "retention": {
        "raw_public_source_records_days": int(
            os.getenv("ORACLE_RAW_SOURCE_RETENTION_DAYS", "730")
        ),
        "call_audio_days": int(os.getenv("ORACLE_CALL_AUDIO_RETENTION_DAYS", "30")),
        "call_transcripts_days": int(
            os.getenv("ORACLE_CALL_TRANSCRIPT_RETENTION_DAYS", "365")
        ),
        "audit_events": "immutable",
    },
}


def latest_observation(sources: Iterable[SourceCitation]) -> date:
    dates = [source.observed_at for source in sources]
    if not dates:
        raise ValueError("at least one source is required")
    return max(dates)
