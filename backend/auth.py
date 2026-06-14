"""
Oracle — Authentication Router
JWT-based agent auth with in-memory session registry.

SECRET_KEY must be set via the environment variable ORACLE_SECRET_KEY.
The hardcoded placeholder is *only* active when running without that variable
(local dev). Startup emits a loud WARNING in that case.
"""

import hmac
import logging
import os
import time
from typing import Optional

import jwt
from fastapi import APIRouter, HTTPException, Header, Response, status
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging — never log secrets or token strings at any level.
# ---------------------------------------------------------------------------

log = logging.getLogger("oracle.auth")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_ENV_KEY = os.environ.get("ORACLE_SECRET_KEY", "")
if _ENV_KEY:
    SECRET_KEY: str = _ENV_KEY
else:
    SECRET_KEY = "ORACLE_DEV_PLACEHOLDER_KEY_replace_in_production_64bytes_abcdef1234"
    log.warning(
        "ORACLE_SECRET_KEY not set — using insecure placeholder key. "
        "This MUST be replaced before any non-local deployment."
    )

ALGORITHM = "HS256"
TOKEN_TTL_SECONDS = 86_400  # 24 hours

# Optional issuer / audience enforcement. Set these env vars to enable strict
# validation. Both must be present or both absent to avoid misconfiguration.
_JWT_ISSUER: Optional[str] = os.environ.get("ORACLE_JWT_ISSUER") or None
_JWT_AUDIENCE: Optional[str] = os.environ.get("ORACLE_JWT_AUDIENCE") or None

# Input length guards — reject obviously malformed inputs early.
_MAX_AGENT_ID_LEN = 128
_MAX_PASSPHRASE_LEN = 256
_MAX_TOKEN_LEN = 8192  # standard JWT headroom

# ---------------------------------------------------------------------------
# Demo credential store
# In production this would be a hashed-password database lookup.
# ---------------------------------------------------------------------------

DEMO_CREDENTIALS: dict[str, str] = {
    "oracle_agent": "nexus_access_2026",
    "analyst_01":   "scanner_ready",
    "ydn":          "sypher_core",
}

# ---------------------------------------------------------------------------
# Demo tenancy map — agent_id → (tenant_id, role). The Auth Gatekeeper stamps
# these into the JWT at login so every downstream request carries its domain
# + role (see tenancy.py / db/schema.sql). In production this is a `users`
# table lookup, not a literal dict.
# ---------------------------------------------------------------------------

PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APEX_TENANT_ID = "11111111-1111-1111-1111-111111111111"

DEMO_TENANCY: dict[str, tuple[str, str]] = {
    "ydn":          (PLATFORM_TENANT_ID, "platform_admin"),  # god-mode override
    "oracle_agent": (APEX_TENANT_ID,     "broker_owner"),
    "analyst_01":   (APEX_TENANT_ID,     "agent"),
}

# Optional operator account injected from the environment (gitignored .env) —
# real credentials must never appear in this file, which is in source control.
_ADMIN_ID = os.environ.get("ORACLE_ADMIN_ID", "")
_ADMIN_PASSPHRASE = os.environ.get("ORACLE_ADMIN_PASSPHRASE", "")
if _ADMIN_ID and _ADMIN_PASSPHRASE:
    DEMO_CREDENTIALS[_ADMIN_ID] = _ADMIN_PASSPHRASE
    DEMO_TENANCY[_ADMIN_ID] = (PLATFORM_TENANT_ID, "platform_admin")

# ---------------------------------------------------------------------------
# In-memory session registry
# Maps agent_id → {issued_at}  (token is NOT stored — no secret in memory)
# Capped at MAX_SESSIONS concurrent entries.
# ---------------------------------------------------------------------------

MAX_SESSIONS = 100

_session_registry: dict[str, dict] = {}


def _prune_expired_sessions() -> None:
    """Remove any sessions whose token has already expired."""
    now = time.time()
    expired = [
        agent_id
        for agent_id, entry in _session_registry.items()
        if entry["issued_at"] + TOKEN_TTL_SECONDS < now
    ]
    for agent_id in expired:
        del _session_registry[agent_id]
    if expired:
        log.debug("Pruned %d expired session(s) from registry.", len(expired))


def _register_session(agent_id: str) -> None:
    _prune_expired_sessions()
    if len(_session_registry) >= MAX_SESSIONS:
        log.warning(
            "Session registry at capacity (%d entries) — rejecting login for agent_id=%r.",
            MAX_SESSIONS,
            agent_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session registry at capacity. Try again shortly.",
        )
    _session_registry[agent_id] = {
        "issued_at": time.time(),
        "agent_id": agent_id,
    }
    log.debug("Session registered for agent_id=%r.", agent_id)


def active_sessions() -> list[dict]:
    """Sanitized snapshot of the live session registry for the platform-admin
    ops surface (admin_ops.py). Tokens are never stored in the registry, so
    there is nothing secret to leak here — just who is logged in and when."""
    _prune_expired_sessions()
    snapshot = []
    for agent_id, entry in _session_registry.items():
        tenant_id, role = DEMO_TENANCY.get(agent_id, (agent_id, "agent"))
        snapshot.append(
            {
                "agent_id": agent_id,
                "tenant_id": tenant_id,
                "role": role,
                "issued_at": entry["issued_at"],
                "expires_at": entry["issued_at"] + TOKEN_TTL_SECONDS,
            }
        )
    return snapshot


# ---------------------------------------------------------------------------
# Rate-limit state — per-agent_id sliding window (login endpoint only).
# Intentionally lightweight: a proper Redis-backed limiter belongs in the
# reverse proxy layer; this is a last-resort backend guard.
# ---------------------------------------------------------------------------

_RL_WINDOW_SECONDS = 60
_RL_MAX_ATTEMPTS = 10  # per window per agent_id (login attempts)

_rl_attempts: dict[str, list[float]] = {}  # agent_id → list of attempt timestamps


def _check_rate_limit(agent_id: str) -> tuple[int, int, int]:
    """Enforce and return (limit, remaining, reset_epoch) for the given agent.

    Raises HTTP 429 if the window is exhausted.
    """
    now = time.time()
    window_start = now - _RL_WINDOW_SECONDS
    attempts = _rl_attempts.get(agent_id, [])
    # Slide the window — drop timestamps older than window_start
    attempts = [t for t in attempts if t > window_start]
    attempts.append(now)
    _rl_attempts[agent_id] = attempts

    remaining = max(0, _RL_MAX_ATTEMPTS - len(attempts))
    reset_epoch = int(window_start + _RL_WINDOW_SECONDS)

    if len(attempts) > _RL_MAX_ATTEMPTS:
        log.warning(
            "Rate limit exceeded for agent_id=%r (%d attempts in %ds window).",
            agent_id,
            len(attempts),
            _RL_WINDOW_SECONDS,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again later.",
            headers={
                "X-RateLimit-Limit": str(_RL_MAX_ATTEMPTS),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset_epoch),
                "Retry-After": str(_RL_WINDOW_SECONDS),
            },
        )
    return _RL_MAX_ATTEMPTS, remaining, reset_epoch


def _apply_rl_headers(response: Response, limit: int, remaining: int, reset: int) -> None:
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(reset)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    agent_id: str
    passphrase: str


class LoginResponse(BaseModel):
    token: str
    agent_id: str
    expires_in: int  # seconds
    # Stamped from the tenancy map so the frontend can gate role-specific
    # surfaces (e.g. the platform-admin OPS tab) without decoding the JWT.
    tenant_id: str
    role: str


class VerifyResponse(BaseModel):
    agent_id: str
    tenant_id: Optional[str] = None
    role: Optional[str] = None
    issued_at: float
    expires_at: float


# ---------------------------------------------------------------------------
# Token decode — single validation path shared by /verify and the tenancy
# gatekeeper (tenancy.require_context). Raises 401 on invalid/expired tokens.
# SECURITY: never propagate raw jwt exception messages to the caller — they
# can leak algorithm or structural information. Log detail at DEBUG only.
# ---------------------------------------------------------------------------


def decode_token(raw_token: str) -> dict:
    if not raw_token or len(raw_token) > _MAX_TOKEN_LEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token.",
        )

    decode_kwargs: dict = {"algorithms": [ALGORITHM]}
    if _JWT_ISSUER:
        decode_kwargs["issuer"] = _JWT_ISSUER
    if _JWT_AUDIENCE:
        decode_kwargs["audience"] = _JWT_AUDIENCE

    try:
        payload = jwt.decode(raw_token, SECRET_KEY, **decode_kwargs)
    except jwt.ExpiredSignatureError:
        log.debug("Token validation failed: expired signature.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired.",
        )
    except jwt.InvalidTokenError as exc:
        # Log at DEBUG only — never at WARNING/ERROR to avoid log-spamming from
        # probes, and never surfacing internal jwt error messages to callers.
        log.debug("Token validation failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token.",
        )

    # Explicit expiry check — belt-and-suspenders against library edge cases.
    exp = payload.get("exp")
    if exp is None or time.time() > exp:
        log.debug("Token validation failed: missing or past exp claim.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired.",
        )

    return payload


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, response: Response) -> LoginResponse:
    """
    Validate agent credentials and return a signed JWT.

    Accepts:  { "agent_id": str, "passphrase": str }
    Returns:  { "token": str, "agent_id": str, "expires_in": int }
    """
    # --- Input length guards --------------------------------------------------
    if len(body.agent_id) > _MAX_AGENT_ID_LEN or len(body.passphrase) > _MAX_PASSPHRASE_LEN:
        log.debug("Login rejected: oversized input fields.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid agent credentials.",
        )

    # --- Rate limit check (per agent_id) -------------------------------------
    limit, remaining, reset = _check_rate_limit(body.agent_id)

    # --- Credential validation (constant-time comparison) --------------------
    expected = DEMO_CREDENTIALS.get(body.agent_id, "")
    # hmac.compare_digest prevents timing-based enumeration of valid agent IDs.
    credentials_ok = bool(expected) and hmac.compare_digest(
        expected.encode(), body.passphrase.encode()
    )
    if not credentials_ok:
        # Don't distinguish "unknown agent" from "wrong passphrase" — prevents
        # user enumeration via timing or error messages.
        log.debug("Failed login attempt for agent_id=%r.", body.agent_id)
        _apply_rl_headers(response, limit, remaining - 1 if remaining else 0, reset)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid agent credentials.",
        )

    # Resolve the agent's domain + role. Demo creds without a tenancy entry
    # fall back to an isolated agent in their own single-user tenant.
    tenant_id, role = DEMO_TENANCY.get(body.agent_id, (body.agent_id, "agent"))

    now = time.time()
    payload: dict = {
        "sub": body.agent_id,
        "tenant_id": tenant_id,
        "role": role,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
    }
    if _JWT_ISSUER:
        payload["iss"] = _JWT_ISSUER
    if _JWT_AUDIENCE:
        payload["aud"] = _JWT_AUDIENCE

    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    # jwt.encode returns str in PyJWT >= 2.x (always the case here).
    if isinstance(token, bytes):  # pragma: no cover — defensive for older installs
        token = token.decode("utf-8")

    _register_session(body.agent_id)
    log.info("Successful login for agent_id=%r, tenant_id=%r.", body.agent_id, tenant_id)

    _apply_rl_headers(response, limit, remaining, reset)
    return LoginResponse(
        token=token,
        agent_id=body.agent_id,
        expires_in=TOKEN_TTL_SECONDS,
        tenant_id=tenant_id,
        role=role,
    )


@router.post("/verify", response_model=VerifyResponse)
def verify(authorization: Optional[str] = Header(default=None)) -> VerifyResponse:
    """
    Decode and validate a Bearer JWT.

    Expects:  Authorization: Bearer <token>
    Returns:  { "agent_id": str, "issued_at": float, "expires_at": float }
              or 401 on invalid / expired token.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
        )

    raw_token = authorization.removeprefix("Bearer ").strip()
    payload = decode_token(raw_token)

    return VerifyResponse(
        agent_id=payload["sub"],
        tenant_id=payload.get("tenant_id"),
        role=payload.get("role"),
        issued_at=payload["iat"],
        expires_at=payload["exp"],
    )
