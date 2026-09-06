"""Every environment variable the backend reads must be written down somewhere.

There are three surfaces that claim to describe configuration — `.env.example`,
`.env.prod.example`, and `config.ENV_VARS` — and until this test existed none of
them was checked against the code, so all three drifted. `config.py` carries a
written confession of the consequence: REGRID_API_TOKEN was missing from the
catalogue that claims to list every var the backend reads, "which is part of why
a 30-day token could lapse unnoticed."

The failure mode is quiet. Every secret in this codebase defaults to empty and
fails closed, which is the right behaviour — but it means an operator who
provisions from the examples gets a stack that boots clean and silently does
less than they think. A missing var is not a crash; it is a feature that never
runs.

So this is a ratchet, not an audit. Adding a new `os.getenv` to the backend now
fails here until it is either documented or explicitly declared internal, with a
reason. The point is that the next var cannot go missing the way REGRID's did.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent

_SKIP_DIRS = {"venv", "site-packages", "__pycache__", "node_modules", ".git"}
_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,}")


# ── Declared internal ────────────────────────────────────────────────────────
# Read by the backend, deliberately absent from the operator-facing templates.
# An entry here is a claim that no deployment ever needs to set it. Adding one
# is cheap; it should still be a decision, which is why each group says why.
_INTERNAL: frozenset[str] = frozenset(
    # Harvester tuning. Per-source scrape pacing, selectors and portal URLs.
    # Changing these is a code-level decision about a specific state's portal,
    # not deployment configuration.
    {
        "FIREHOSE_BASE_BACKOFF", "FIREHOSE_HTTP_TIMEOUT", "FIREHOSE_MAX_BACKOFF",
        "FIREHOSE_MAX_PER_STATE", "FIREHOSE_USER_AGENT",
        "HI_SOURCE_URL", "IL_COOK_PIN_CHUNK", "IL_SOURCE_URL", "NY_PLUTO_VERSION",
        "MD_SDAT_BASE_BACKOFF", "MD_SDAT_BATCH_SIZE", "MD_SDAT_EXPORT_SELECTOR",
        "MD_SDAT_EXPORT_URL", "MD_SDAT_HEADLESS", "MD_SDAT_JITTER",
        "MD_SDAT_MAX_BACKOFF", "MD_SDAT_MAX_RECORDS", "MD_SDAT_MAX_RETRIES",
        "MD_SDAT_MIN_INTERVAL", "MD_SDAT_NAV_TIMEOUT_MS", "MD_SDAT_PORTAL_URL",
        "ORACLE_DISTRESS_INTERVAL_MIN", "ORACLE_DISTRESS_MAX_PER_SOURCE",
        "ORACLE_LISTINGS_INTERVAL_HOURS", "ORACLE_RESO_LOOKBACK_HOURS",
        "ORACLE_LEAD_NORMALIZE_CATCHUP_ENABLED",
    }
    # 3D reconstruction. Parked 2026-08-26 — the pipeline is dormant behind
    # lazy boundaries. These stay undocumented deliberately: publishing knobs
    # for a path we are not running would advertise a capability we withdrew.
    | {
        "RECON_AWS_BATCH_JOBDEF", "RECON_AWS_BATCH_QUEUE", "RECON_AWS_TIMEOUT",
        "RECON_CLOUD_KEY", "RECON_CLOUD_URL", "RECON_HTTP_TIMEOUT",
        "RECON_POD_CLOUD_TYPE", "RECON_POD_GPU_IDS", "RECON_POD_IMAGE",
        "RECON_POD_MATCHER", "RECON_POD_TIMEOUT", "RECON_POD_TRANSPORT",
        "RECON_QUEUE_MAX", "RECON_REAP_INTERVAL", "RECON_REAP_MAX_AGE",
        "RECON_REMOTE_OUTPUT_HOSTS", "RECON_RUNPOD_TIMEOUT", "RECON_S3_BUCKET",
        "RECON_SERVERLESS_KEY", "RECON_SERVERLESS_URL", "RECON_STUB_FIXTURE",
        "RECON_TRAINER_CMD", "RECON_VIDEO_MAX_FRAMES", "RECON_VIDEO_SAMPLE_FPS",
        "RECON_WORKER_COUNT", "RUNPOD_API_KEY", "RUNPOD_ENDPOINT_ID",
        "RUNPOD_TRAINING_ENDPOINT_ID", "RUNPOD_TRAINING_TIMEOUT",
        "ONCOMPUTE_ENV_ID", "ONCOMPUTE_PRIVATE_KEY",
        "ORACLE_FEATURE_RECON_FLOORPLAN",
    }
    # Worker counts, poll intervals, timeouts and queue bounds. Defaults are
    # tuned in code against measured behaviour; overriding one is a debugging
    # act, not a deployment step.
    | {
        "ORACLE_CLIENT_AI_SWEEP_INTERVAL_HOURS", "ORACLE_CLIENT_AI_SWEEP_LIMIT",
        "ORACLE_CLIENT_AI_TIMEOUT_SECONDS", "ORACLE_DB_HEALTH_CHECK_INTERVAL",
        "ORACLE_ENGINE_LINGER_SECONDS", "ORACLE_JOB_POLL_SECONDS",
        "ORACLE_JOB_WORKERS", "ORACLE_LOCAL_LLM_TIMEOUT",
        "ORACLE_MARKET_DATA_TIMEOUT_SECONDS",
        "ORACLE_SCHED_TICK_SECONDS", "ORACLE_VIDEO_DOWNLOAD_TIMEOUT_SECONDS",
        "ORACLE_VIDEO_JOB_LEASE_SECONDS", "ORACLE_VIDEO_JOB_TIMEOUT_SECONDS",
        "ORACLE_VIDEO_POLL_SECONDS", "ORACLE_VIDEO_POLL_TIMEOUT_SECONDS",
        "ORACLE_VIDEO_SUBMIT_TIMEOUT_SECONDS", "ORACLE_VOICE_QUEUE_MAX",
        "ORACLE_VOICE_REPLY_BUDGET_SECONDS", "ORACLE_VOICE_WORKERS",
        "ORACLE_WS_MAX_CONNECTIONS", "ORACLE_FIREWORKS_TIMEOUT",
        "ORACLE_FIREWORKS_MAX_TOKENS", "ORACLE_FIREWORKS_MIN_TOKENS",
        "CONTRACT_GENERATION_RATE_LIMIT", "AWS_OBS_COPILOT_RATE_LIMIT",
        "AWS_OBS_MAX_ASG_DESIRED_CAPACITY", "ACS_STALE_CALL_TTL",
    }
    # Model and endpoint selection. Which model answers is a product decision
    # made in code; the credentials that reach it ARE documented.
    | {
        "ORACLE_AI_CHAT_MODEL", "ORACLE_AI_FIREWORKS_FALLBACK",
        "ORACLE_CLIENT_AI_MODEL", "ORACLE_CLIENT_AI_MODEL_ENABLED",
        "ORACLE_FAL_VIDEO_MODEL", "ORACLE_FIREWORKS_FAST_MODEL",
        "ORACLE_FIREWORKS_MODEL", "ORACLE_FIREWORKS_URL", "ORACLE_LLM_GATEWAY",
        "ORACLE_LOCAL_LLM_DISABLE_THINKING", "CRM_DRAFT_LLM", "LLAMA_SERVER_URL",
        "ANALYST_GPU_LAYERS", "ANALYST_MODEL_PATH", "ACS_VOICE_NAME",
        "ORACLE_FEATURE_CLIENT_AI_AUTOMATION",
    }
    # Dev-only escape hatches and one-shot job knobs. Setting any of these in
    # production is a mistake, so they are kept out of the prod template on
    # purpose. ORACLE_DB_SSL and ORACLE_MIGRATIONS_DIR are documented instead
    # in docs/, beside the runner that reads them.
    | {
        "ORACLE_ALLOW_LIVE_STRIPE", "ORACLE_DISABLE_RATE_LIMIT",
        "ORACLE_DEMO_PASS", "ORACLE_DEMO_ROLE", "ORACLE_DEMO_TENANT",
        "ORACLE_DEMO_USER", "ORACLE_DB_SSL", "ORACLE_MIGRATIONS_DIR",
        "ORACLE_CONTACT_SEARCH_BACKFILL_BATCH", "ORACLE_VIDEO_STUDIO_DISABLED",
        "K8S_CA_PATH", "K8S_TOKEN_PATH",
    }
    # Set by the platform, never by an operator: a provider SDK reads them, or
    # a startup probe writes them for later code to read back.
    | {
        "ORACLE_ACS_CREDENTIALS_VALIDATED", "ORACLE_SES_CREDENTIALS_VALIDATED",
        "ORACLE_TWILIO_CREDENTIALS_VALIDATED", "AWS_OBSERVABILITY_ENABLED",
        "ORACLE_ASSIGNOR_NAME", "ORACLE_AUTORENEW_DISCLOSURE",
    }
)


def _env_names(path: Path) -> set[str]:
    """Names passed to os.getenv / os.environ.get / os.environ[...] in one file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, UnicodeDecodeError):
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            target = ast.unparse(node.func)
            if node.func.attr == "getenv" and target.endswith("os.getenv"):
                found.add(first.value)
            elif node.func.attr in {"get", "pop"} and "environ" in target:
                found.add(first.value)
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if isinstance(node.slice.value, str) and ast.unparse(node.value).endswith("environ"):
                found.add(node.slice.value)
    return {name for name in found if _NAME.fullmatch(name)}


def _read_by_shipped_code() -> set[str]:
    """Every env var the backend reads, excluding test code."""
    names: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        parts = set(path.parts)
        if parts & _SKIP_DIRS or "tests" in parts:
            continue
        names |= _env_names(path)
    return names


def _documented_in(filename: str) -> set[str]:
    path = REPO / filename
    if not path.exists():
        return set()
    # Commented-out entries count: the file uses `# VAR=value` to document an
    # optional setting alongside the value it would take.
    return set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]{2,})=", path.read_text(), re.M))


def _catalogued_in_config() -> set[str]:
    import config

    return {name for group in config.ENV_VARS.values() for name in group}


def test_every_env_var_the_backend_reads_is_written_down():
    undocumented = sorted(
        _read_by_shipped_code()
        - _documented_in(".env.example")
        - _documented_in(".env.prod.example")
        - _catalogued_in_config()
        - _INTERNAL
    )
    assert not undocumented, (
        "These environment variables are read by backend code but appear in no "
        "operator-facing template and are not declared internal:\n  "
        + "\n  ".join(undocumented)
        + "\n\nDocument each in .env.example / .env.prod.example (or add it to "
        "config.ENV_VARS), or add it to _INTERNAL in this file with the group "
        "comment that says why no deployment needs to set it."
    )


def test_config_env_vars_catalogue_has_no_duplicates():
    """A name in two groups means the catalogue was edited without reading it."""
    import config

    seen: dict[str, str] = {}
    duplicated: list[str] = []
    for group, names in config.ENV_VARS.items():
        for name in names:
            if name in seen:
                duplicated.append(f"{name} (in {seen[name]!r} and {group!r})")
            seen[name] = group
    assert not duplicated, "Duplicate entries in config.ENV_VARS:\n  " + "\n  ".join(duplicated)


def test_declared_internal_vars_are_actually_read():
    """Stop _INTERNAL becoming its own graveyard.

    An allowlist that is never pruned drifts exactly the way the templates did.
    If a var stops being read, it should leave this list rather than sit here
    implying the backend still honours it.
    """
    stale = sorted(_INTERNAL - _read_by_shipped_code())
    assert not stale, (
        "Declared internal but no longer read by any backend code — remove "
        "from _INTERNAL:\n  " + "\n  ".join(stale)
    )


@pytest.mark.parametrize("template", [".env.example", ".env.prod.example"])
def test_templates_declare_each_variable_once(template):
    """A var assigned twice means the second value silently wins."""
    path = REPO / template
    active = re.findall(r"^([A-Z][A-Z0-9_]{2,})=", path.read_text(), re.M)
    duplicated = sorted({name for name in active if active.count(name) > 1})
    assert not duplicated, f"{template} assigns these more than once: {duplicated}"
