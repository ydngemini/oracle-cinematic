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
]


def validate_or_die() -> None:
    """Fail the boot fast when a production deployment is missing a critical
    secret. In development, log the relaxed posture and continue."""
    if IS_DEV:
        log.warning(
            "ORACLE_ENV=%r — DEV mode; production secret validation relaxed.",
            ORACLE_ENV or "(unset)",
        )
        return
    missing = [f"{name} ({why})" for name, why in _REQUIRED_IN_PROD if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Refusing to start: missing required production secret(s): "
            + "; ".join(missing)
        )
    log.info("Config validated for production — all required secrets present.")


# ---------------------------------------------------------------------------
# Catalogue of every env var the backend reads (documentation + migration map).
# Grouped by subsystem; defaults shown where the reader supplies one.
# ---------------------------------------------------------------------------
ENV_VARS: dict[str, tuple[str, ...]] = {
    "core": ("ORACLE_ENV", "ORACLE_SECRET_KEY", "ORACLE_ENCRYPTION_MASTER_KEY",
             "ORACLE_CORS_ORIGINS", "ORACLE_BASE_URL", "ORACLE_DEMO_TENANT_ID",
             "ORACLE_ENABLE_DEMO_LOGINS", "ORACLE_JWT_ISSUER", "ORACLE_JWT_AUDIENCE",
             "ORACLE_ADMIN_ID", "ORACLE_ADMIN_PASSPHRASE"),
    "db": ("ORACLE_DB_HOST", "ORACLE_DB_PORT", "ORACLE_DB_NAME", "ORACLE_DB_USER",
           "ORACLE_DB_PASSWORD", "ORACLE_DB_SSLMODE", "ORACLE_DB_POOL_MIN",
           "ORACLE_DB_POOL_MAX", "ORACLE_RDS_CA_BUNDLE"),
    "stripe": ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_ID",
               "STRIPE_AUTOMATIC_TAX", "STRIPE_REQUIRE_TOS"),
    "spatial": ("SPATIAL_DEVICE", "SPATIAL_RESOLUTION", "SPATIAL_ALLOW_WEB_SCRAPE",
                "GS_PATH", "DUST3R_PATH", "ORACLE_SPLAT_DIR"),
    "voice": ("QWEN_VOICE_MODEL", "LLAMA_CPP_PATH", "VOICE_GPU_LAYERS",
              "ORACLE_WHISPER_MODEL", "ORACLE_AUDIO_STAGING", "ORACLE_AUDIO_MAX_BYTES"),
    "ml": ("AWS_REGION", "BEDROCK_REGION"),
    "ops": ("ORACLE_WS_IDLE_TIMEOUT", "ORACLE_TOUR_RATE_LIMIT", "ORACLE_AUDIT_SQLITE",
            "ORACLE_DISPOSITION_SWEEP_INTERVAL", "ORACLE_DANGER_ZONE_DAYS",
            "SCOUT_REGIONAL_ENABLED", "ZILLOW_COOKIE",
            "QWEN_VOICE_DEPLOYMENT", "QWEN_VOICE_NAMESPACE", "QWEN_VOICE_HPA"),
    "platform_features": (
        "ORACLE_FEATURE_AUTOMATION", "ORACLE_FEATURE_MUNICIPAL_HARVESTS",
        "ORACLE_FEATURE_PREDICTIVE_INTELLIGENCE", "ORACLE_FEATURE_MARKETPLACE",
        "ORACLE_FEATURE_LOCAL_MODELS", "ORACLE_FEATURE_SPATIAL_TOURS",
        "ORACLE_FEATURE_CONTRACTS", "ORACLE_RAW_SOURCE_RETENTION_DAYS",
        "ORACLE_CALL_AUDIO_RETENTION_DAYS", "ORACLE_CALL_TRANSCRIPT_RETENTION_DAYS",
    ),
}
