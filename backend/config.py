"""
Central configuration & startup validation for the Oracle backend.

Single source of truth for the cross-cutting, security- and portability-critical
settings. Narrowly-scoped per-module knobs may still read ``os.environ`` directly,
but three things live here so they cannot drift:

  * ``IS_DEV`` — the one definition of "are we in development", previously
    re-derived in auth.py / server.py / workflow_engine.py.
  * Portable model paths — the voice model / llama.cpp defaults used to be
    hardcoded to ``/media/ydn/...`` absolute paths that broke on any other host.
  * ``validate_or_die()`` — called from the app lifespan so a PRODUCTION boot
    fails fast (not lazily, mid-request) when a required secret is missing.

Remaining per-module ``os.environ`` reads (DB_*, STRIPE_*, SPATIAL_*, …) are
catalogued in ``ENV_VARS`` below and can be migrated onto this surface
incrementally; they are intentionally NOT force-rewired in one pass.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("oracle.config")

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
_DEV_VALUES = {"dev", "development", "local"}
ORACLE_ENV: str = os.environ.get("ORACLE_ENV", "").lower()
IS_DEV: bool = ORACLE_ENV in _DEV_VALUES


def flag(name: str, default: bool = False) -> bool:
    """Parse a boolean env var ('1'/'true'/'yes'/'on' → True)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Resolve stable JWT scope values before ``auth`` is imported. Managed
# deployments already provide one of the public application origins, so they do
# not need duplicate secrets merely to mint and validate this service's tokens.
# Explicit JWT settings always win.
def _first_setting(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value.rstrip("/")
    return ""


ORACLE_DOMAIN: str = _first_setting(
    "ORACLE_DOMAIN",
    "ORACLE_PUBLIC_BASE_URL",
    "ORACLE_BASE_URL",
)
JWT_ISSUER: str = _first_setting(
    "ORACLE_JWT_ISSUER",
    "ORACLE_JWT_TENANT_ISSUER",
) or ORACLE_DOMAIN
JWT_AUDIENCE: str = _first_setting(
    "ORACLE_JWT_AUDIENCE",
    "ORACLE_JWT_TENANT_AUDIENCE",
) or ORACLE_DOMAIN or JWT_ISSUER

# ``auth.py`` intentionally reads these at import time. Publishing the resolved
# non-secret values here keeps its issuer/audience pair complete when server.py
# imports this module first, while preserving explicit operator configuration.
if JWT_ISSUER:
    os.environ.setdefault("ORACLE_JWT_ISSUER", JWT_ISSUER)
if JWT_AUDIENCE:
    os.environ.setdefault("ORACLE_JWT_AUDIENCE", JWT_AUDIENCE)

# The one public origin every outbound link is built from — password resets,
# Stripe success/cancel URLs, client-portal links, command callbacks. Four
# modules each carried their own `os.getenv("ORACLE_BASE_URL",
# "http://localhost:5173")`, so a deployment that set ORACLE_DOMAIN or
# ORACLE_PUBLIC_BASE_URL but not ORACLE_BASE_URL — none of which is required in
# prod — booted cleanly and mailed real users a localhost reset link.
#
# Resolving through ORACLE_DOMAIN closes that: it already falls back
# ORACLE_DOMAIN -> ORACLE_PUBLIC_BASE_URL -> ORACLE_BASE_URL, and it is
# effectively guaranteed non-empty in production because JWT_ISSUER derives from
# it and IS required. The localhost default therefore only survives in dev.
# A function, not a constant: commands_api and auth resolved this per request,
# and freezing it at import would silently change that (and break any caller
# that sets the variable after import, which the OAuth tests legitimately do).
# Module-level consumers simply call it once at import, as they always did.
def public_base_url() -> str:
    """The public origin every outbound link is built from."""
    resolved = _first_setting(
        "ORACLE_DOMAIN",
        "ORACLE_PUBLIC_BASE_URL",
        "ORACLE_BASE_URL",
    )
    return (resolved or "http://localhost:5173").rstrip("/")

ENABLE_WEBHOOKS: bool = flag("ORACLE_ENABLE_WEBHOOKS", default=False)
ORACLE_FEATURE_VIDEO_STUDIO: bool = flag("ORACLE_FEATURE_VIDEO_STUDIO", default=False)


# ---------------------------------------------------------------------------
# Portable paths — env-overridable, defaulting under ORACLE_MODELS_DIR instead
# of a machine-specific absolute path.
# ---------------------------------------------------------------------------
MODELS_DIR: Path = Path(
    os.environ.get("ORACLE_MODELS_DIR", str(Path.home() / "oracle-models"))
)
QWEN_VOICE_MODEL: Path = Path(
    os.environ.get("QWEN_VOICE_MODEL", str(MODELS_DIR / "qwen2-audio-closer.gguf"))
)
LLAMA_CPP_PATH: Path = Path(
    os.environ.get("LLAMA_CPP_PATH", str(MODELS_DIR / "llama.cpp"))
)

# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------
# Secrets that MUST be present outside development. (env name, feature guarded)
_REQUIRED_IN_PROD: list[tuple[str, str]] = [
    ("ORACLE_SECRET_KEY", "JWT signing"),
    ("ORACLE_ENCRYPTION_MASTER_KEY", "PII encryption (pgcrypto)"),
    ("ORACLE_JWT_ISSUER", "JWT issuer binding"),
    ("ORACLE_JWT_AUDIENCE", "JWT audience binding"),
]

_WEBHOOK_SECRETS: list[tuple[str, str]] = [
    ("ORACLE_ACS_WEBHOOK_SECRET", "ACS webhook authentication"),
    ("ORACLE_CUSTOM_CALL_WEBHOOK_SECRET", "custom-call webhook authentication"),
]

_QWEN_REALTIME_SETTINGS: list[tuple[str, str]] = [
    ("DASHSCOPE_API_KEY", "Qwen Omni realtime authentication"),
]

_TWILIO_REALTIME_SETTINGS: list[tuple[str, str]] = [
    ("TWILIO_ACCOUNT_SID", "Twilio Media Streams account binding"),
    ("TWILIO_AUTH_TOKEN", "Twilio WebSocket signature validation"),
    ("TWILIO_FROM_NUMBER", "Twilio outbound caller ID"),
    ("ORACLE_PUBLIC_BASE_URL", "Twilio WSS endpoint routing"),
]


# ---------------------------------------------------------------------------
# Placeholder / weak-secret rejection.
#
# `validate_or_die` used to ask only `if not os.environ.get(name)`, and
# infra/terraform/secrets.tf seeds the app secret with the literal string
# "REPLACE_ME" so the ECS task definition can reference each JSON key before an
# operator fills it. "REPLACE_ME" is not empty, so it sailed through — a fresh
# production deploy would boot signing JWTs with it, encrypting PII with it,
# and, because auth.py registers ORACLE_ADMIN_ID/ORACLE_ADMIN_PASSPHRASE as a
# platform_admin login whenever both are non-empty (and terraform hardcodes a
# real ORACLE_ADMIN_ID), serving a public admin account whose password is
# "REPLACE_ME". validate_or_die then logged "Config validated for production".
#
# A secret that is present but worthless is more dangerous than one that is
# absent, because the absent one already failed the boot.
# ---------------------------------------------------------------------------
_PLACEHOLDER_SECRETS: frozenset[str] = frozenset(
    {
        "replace_me", "replaceme", "change_me", "changeme", "placeholder",
        "todo", "tbd", "xxx", "secret", "password", "passphrase", "test",
        "testing", "example", "dummy", "sample", "default", "none", "null",
        "unset", "foo", "bar", "admin", "letmein", "123456",
    }
)

# Floors, not targets. 32 chars is the usual minimum for an HS256 signing key
# and for key-derivation input; the operator passphrase reuses auth.py's own
# MIN_PASSWORD_LEN rather than inventing a second number (kept as a literal
# here because auth.py imports config, not the other way round).
_MIN_OPERATOR_PASSPHRASE_LEN = 10
_SECRETS_NEEDING_STRENGTH: list[tuple[str, str, int]] = [
    ("ORACLE_SECRET_KEY", "JWT signing", 32),
    ("ORACLE_ENCRYPTION_MASTER_KEY", "PII encryption (pgcrypto)", 32),
    ("ORACLE_ADMIN_PASSPHRASE", "platform_admin login", _MIN_OPERATOR_PASSPHRASE_LEN),
    ("ORACLE_ACS_WEBHOOK_SECRET", "ACS webhook authentication", 16),
    ("ORACLE_CUSTOM_CALL_WEBHOOK_SECRET", "custom-call webhook authentication", 16),
]


def _weak_secret_reason(value: str, min_len: int) -> str | None:
    """Why this value is unusable as a secret, or None if it is fine.

    Never returns the value itself — the reason is logged and raised, and a
    secret that leaks through its own rejection message is no better off.
    """
    stripped = value.strip()
    normalized = stripped.lower().replace("-", "_")
    if normalized in _PLACEHOLDER_SECRETS:
        return "it is a placeholder value"
    if normalized.startswith("replace_me") or normalized.startswith("change_me"):
        return "it is a placeholder value"
    if len(stripped) < min_len:
        return f"it is shorter than the {min_len}-character minimum"
    if len(set(stripped)) < 4:
        return "it repeats too few distinct characters to be a real secret"
    return None


def validate_or_die() -> None:
    """Fail the boot fast when a production deployment is missing a critical
    secret. In development, log the relaxed posture and continue."""
    # ACS readiness check — log warnings for missing telephony config (non-fatal)
    _acs_missing = []
    if not os.environ.get("ACS_CONNECTION_STRING"):
        _acs_missing.append("ACS_CONNECTION_STRING")
    if not os.environ.get("ACS_FROM_NUMBER"):
        _acs_missing.append("ACS_FROM_NUMBER")
    if not os.environ.get("ORACLE_PUBLIC_BASE_URL"):
        _acs_missing.append("ORACLE_PUBLIC_BASE_URL")
    if _acs_missing:
        log.warning(
            "ACS phone calls are NOT operational — missing: %s",
            ", ".join(_acs_missing),
        )
    else:
        log.info("ACS telephony config is present — phone calls are operational.")

    if IS_DEV:
        log.warning(
            "ORACLE_ENV=%r — DEV mode; production secret validation relaxed.",
            ORACLE_ENV or "(unset)",
        )
        return
    enable_webhooks = flag("ORACLE_ENABLE_WEBHOOKS", default=False)
    required = list(_REQUIRED_IN_PROD)
    if enable_webhooks:
        required.extend(_WEBHOOK_SECRETS)
    acs_qwen_enabled = flag("ORACLE_QWEN_REALTIME_ENABLED", default=False)
    twilio_qwen_enabled = flag(
        "ORACLE_TWILIO_QWEN_REALTIME_ENABLED",
        default=False,
    )
    twilio_account_tier = os.environ.get(
        "ORACLE_TWILIO_ACCOUNT_TIER",
        "",
    ).strip().lower()
    if twilio_qwen_enabled and twilio_account_tier in {"trial", "free-trial"}:
        log.warning(
            "Twilio realtime is disabled because Voice Trial blocks the "
            "<Stream> verb; outbound CRM AI calls remain unavailable until "
            "the Twilio project is upgraded."
        )
        twilio_qwen_enabled = False
    if acs_qwen_enabled or twilio_qwen_enabled:
        required.extend(_QWEN_REALTIME_SETTINGS)
    if acs_qwen_enabled:
        required.append(
            ("ORACLE_ACS_RESOURCE_ID", "ACS signed WebSocket JWT audience")
        )
    if twilio_qwen_enabled:
        required.extend(_TWILIO_REALTIME_SETTINGS)
    missing = [f"{name} ({why})" for name, why in required if not os.environ.get(name)]
    if (
        (acs_qwen_enabled or twilio_qwen_enabled)
        and not os.environ.get("DASHSCOPE_WORKSPACE_ID")
        and not os.environ.get("DASHSCOPE_REALTIME_URL")
    ):
        missing.append(
            "DASHSCOPE_WORKSPACE_ID or DASHSCOPE_REALTIME_URL "
            "(Qwen Omni realtime workspace routing)"
        )
    if missing:
        raise RuntimeError(
            "Refusing to start: missing required production secret(s): "
            + "; ".join(missing)
        )

    # Present-but-worthless is checked separately from absent, because the two
    # failures need different words: one says "you forgot to set this", the
    # other says "what you set will not protect anything".
    weak: list[str] = []
    for name, why, min_len in _SECRETS_NEEDING_STRENGTH:
        value = os.environ.get(name, "")
        if not value:
            # Absence is the `missing` check's business. These entries are
            # optional (the operator account, the webhook secrets when webhooks
            # are off), so an unset one is not this function's complaint.
            continue
        reason = _weak_secret_reason(value, min_len)
        if reason:
            weak.append(f"{name} ({why}) — {reason}")
    if weak:
        raise RuntimeError(
            "Refusing to start: placeholder or weak production secret(s): "
            + "; ".join(weak)
        )
    log.info(
        "Config validated for production — all required settings present; webhooks=%s.",
        "enabled" if enable_webhooks else "disabled",
    )


# ---------------------------------------------------------------------------
# Catalogue of every env var the backend reads (documentation + migration map).
# Grouped by subsystem; defaults shown where the reader supplies one.
# ---------------------------------------------------------------------------
ENV_VARS: dict[str, tuple[str, ...]] = {
    "core": ("ORACLE_ENV", "ORACLE_DOMAIN", "ORACLE_SECRET_KEY", "ORACLE_ENCRYPTION_MASTER_KEY",
             "ORACLE_CORS_ORIGINS", "ORACLE_BASE_URL", "ORACLE_DEMO_TENANT_ID",
             "ORACLE_ENABLE_DEMO_LOGINS", "ORACLE_JWT_ISSUER", "ORACLE_JWT_AUDIENCE",
             "ORACLE_JWT_TENANT_ISSUER", "ORACLE_JWT_TENANT_AUDIENCE",
             "ORACLE_ADMIN_ID", "ORACLE_ADMIN_PASSPHRASE"),
    "db": ("ORACLE_DB_HOST", "ORACLE_DB_PORT", "ORACLE_DB_NAME", "ORACLE_DB_USER",
           "ORACLE_DB_PASSWORD", "ORACLE_DB_SSLMODE", "ORACLE_DB_POOL_MIN",
           "ORACLE_DB_POOL_MAX", "ORACLE_RDS_CA_BUNDLE", "ORACLE_DB_CA_BUNDLE",
           "ORACLE_DB_ADMIN_USER", "ORACLE_DB_ADMIN_PASSWORD",
           "ORACLE_DB_APP_PASSWORD", "ORACLE_DB_MAINTENANCE_NAME"),
    "stripe": ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_ID",
               "STRIPE_AUTOMATIC_TAX", "STRIPE_REQUIRE_TOS"),
    "spatial": ("SPATIAL_DEVICE", "SPATIAL_RESOLUTION", "SPATIAL_ALLOW_WEB_SCRAPE",
                "GS_PATH", "DUST3R_PATH", "ORACLE_SPLAT_DIR", "ORACLE_SPLAT_STORAGE"),
    "voice": (
        "QWEN_VOICE_MODEL", "LLAMA_CPP_PATH", "VOICE_GPU_LAYERS",
        "ORACLE_WHISPER_MODEL", "ORACLE_AUDIO_STAGING", "ORACLE_AUDIO_MAX_BYTES",
        "ORACLE_QWEN_REALTIME_ENABLED", "DASHSCOPE_API_KEY",
        "DASHSCOPE_WORKSPACE_ID", "DASHSCOPE_REGION", "DASHSCOPE_REALTIME_URL",
        "QWEN_REALTIME_MODEL", "QWEN_REALTIME_VOICE", "QWEN_REALTIME_MAX_TURNS",
        "ORACLE_ACS_RESOURCE_ID", "ORACLE_TWILIO_QWEN_REALTIME_ENABLED",
        "ORACLE_TWILIO_ACCOUNT_TIER",
    ),
    "ml": (
        "AWS_REGION", "BEDROCK_REGION", "ORACLE_AI_CHAT_PROVIDER",
        "ORACLE_FOUNDRY_PROJECT_ENDPOINT", "ORACLE_FOUNDRY_AGENT_NAME",
        "ORACLE_FOUNDRY_MODEL", "ORACLE_MARKET_AI_SUPERVISOR_ENABLED",
        "ORACLE_MARKET_AI_SUPERVISOR_MODEL",
        "ORACLE_MARKET_AI_SUPERVISOR_TIMEOUT_SECONDS", "AZURE_CLIENT_ID",
    ),
    "federal_sources": ("GOVINFO_API_KEY", "DATA_GOV_API_KEY"),
    "commands": (
        "ORACLE_PUBLIC_BASE_URL", "ORACLE_SES_FROM_EMAIL",
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI",
        "ACS_CONNECTION_STRING", "ACS_FROM_NUMBER",
        "TWILIO_ACCOUNT_SID", "TWILIO_API_KEY", "TWILIO_API_SECRET",
        "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "ORACLE_TWILIO_TWIML_URL",
        # ORACLE_TWILIO_ACCOUNT_TIER is catalogued under "voice", which is where
        # it is read. Listing it twice made the catalogue self-contradicting
        # about which subsystem owns it.
        "ORACLE_ENABLE_WEBHOOKS", "ORACLE_ACS_WEBHOOK_SECRET",
        "ORACLE_CUSTOM_CALL_WEBHOOK_SECRET",
        "ORACLE_CUSTOM_CALL_API_URL", "ORACLE_CUSTOM_CALL_AUTH_TOKEN",
        "ORACLE_CUSTOM_CALL_FROM_NUMBER",
    ),
    "ops": ("ORACLE_WS_IDLE_TIMEOUT", "ORACLE_TOUR_RATE_LIMIT",
            "ORACLE_AUTHENTICATED_API_RATE_LIMIT", "ORACLE_AUDIT_SQLITE",
            "ORACLE_DISPOSITION_SWEEP_INTERVAL", "ORACLE_DANGER_ZONE_DAYS",
            "SCOUT_REGIONAL_ENABLED", "ZILLOW_COOKIE",
            "QWEN_VOICE_DEPLOYMENT", "QWEN_VOICE_NAMESPACE", "QWEN_VOICE_HPA",
            "ORACLE_CLAMD_SOCKET", "ORACLE_CLAMD_HOST", "ORACLE_CLAMD_PORT"),
    "platform_features": (
        "ORACLE_FEATURE_AUTOMATION", "ORACLE_FEATURE_MUNICIPAL_HARVESTS",
        "ORACLE_FEATURE_PREDICTIVE_INTELLIGENCE", "ORACLE_FEATURE_MARKETPLACE",
        "ORACLE_FEATURE_LOCAL_MODELS", "ORACLE_FEATURE_SPATIAL_TOURS",
        "ORACLE_FEATURE_CONTRACTS", "ORACLE_FEATURE_AI_CHAT", "ORACLE_RAW_SOURCE_RETENTION_DAYS",
        "ORACLE_CALL_AUDIO_RETENTION_DAYS", "ORACLE_CALL_TRANSCRIPT_RETENTION_DAYS",
        "ORACLE_FEATURE_VIDEO_STUDIO",  # Enables the Video Marketing Studio tab
        # feature_enabled() defaults these to TRUE when unset, so they must stay
        # visible to the config audit. POWER_DIALER gates outbound autodialing.
        "ORACLE_FEATURE_POWER_DIALER", "ORACLE_FEATURE_SALES_AI",
        "ORACLE_FEATURE_SMART_PLANS",
    ),
    "video_studio": (
        "ORACLE_VIDEO_PROVIDER",         # sora | veo (default: sora)
        "ORACLE_AZURE_OPENAI_ENDPOINT",  # Sora: base endpoint (retires 2026-09-15)
        "ORACLE_SORA_DEPLOYMENT",        # Sora: deployment name (default: sora-2-estate)
        "ORACLE_VEO_PROJECT",            # Veo: billing-enabled GCP project
        "ORACLE_VEO_LOCATION",           # Veo: region (default: us-central1)
        "ORACLE_VEO_MODEL",              # Veo: model id (default: veo-3.1-generate-001)
        "ORACLE_VIDEO_MAX_IMAGES",       # Max photos per job (default: 4)
        "ORACLE_VIDEO_CLIP_SECONDS",     # Duration per clip (default: 8)
        "ORACLE_VIDEO_DAILY_QUOTA",      # Daily seconds cap per tenant (default: 120)
    ),
    # Licensed data providers. REGRID_API_TOKEN was absent from this catalogue
    # despite the module docstring claiming it lists every var the backend reads —
    # which is part of why a 30-day token could lapse unnoticed.
    "data_providers": (
        "REGRID_API_TOKEN",              # 30-day JWT; expiry is surfaced at /api/data/health
    ),
    "shared_storage": ("ORACLE_SHARED_STORAGE_DIR", "ORACLE_CONTRACT_OUTPUT_DIR"),
}
