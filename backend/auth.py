"""
Oracle — Authentication Router
JWT-based agent auth with in-memory session registry.

SECRET_KEY must be set via the environment variable ORACLE_SECRET_KEY.
If it is unset the app FAILS TO START — except in development (ORACLE_ENV in
dev/development/local), where an ephemeral per-process key is generated so local
runs work without a static secret in source.
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import jwt
from fastapi import APIRouter, HTTPException, Header, Request, Response, status
from pydantic import BaseModel

from policy_contract import PLATFORM_POLICY_VERSION

# ---------------------------------------------------------------------------
# Logging — never log secrets or token strings at any level.
# ---------------------------------------------------------------------------

log = logging.getLogger("oracle.auth")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_ENV_KEY = os.environ.get("ORACLE_SECRET_KEY", "")
_IS_DEV = os.environ.get("ORACLE_ENV", "").lower() in {"dev", "development", "local"}

if _ENV_KEY:
    SECRET_KEY: str = _ENV_KEY
elif _IS_DEV:
    # Dev only: no static secret in source. A hardcoded placeholder is forgeable
    # by anyone with repo access and lives forever in git history, so we mint an
    # ephemeral per-process key instead — tokens are valid only for this run.
    SECRET_KEY = secrets.token_hex(32)
    log.warning(
        "ORACLE_SECRET_KEY not set — generated an ephemeral dev key. Tokens will "
        "not survive a restart; set ORACLE_SECRET_KEY for stable local sessions."
    )
else:
    raise RuntimeError(
        "ORACLE_SECRET_KEY is not set. Refusing to start outside development. "
        "Set ORACLE_SECRET_KEY in the environment (or ORACLE_ENV=dev for an "
        "ephemeral local key)."
    )

ALGORITHM = "HS256"
TOKEN_TTL_SECONDS = 86_400  # 24 hours

# Issuer / audience are mandatory outside development. A partial pair is always
# rejected: validating only one side creates an easy configuration-dependent
# downgrade where tokens minted for another service can be accepted here.
_JWT_ISSUER: Optional[str] = (os.environ.get("ORACLE_JWT_ISSUER") or "").strip() or None
_JWT_AUDIENCE: Optional[str] = (os.environ.get("ORACLE_JWT_AUDIENCE") or "").strip() or None


def _validate_jwt_scope_config(
    *, is_dev: bool, issuer: Optional[str], audience: Optional[str]
) -> None:
    if bool(issuer) != bool(audience):
        raise RuntimeError(
            "ORACLE_JWT_ISSUER and ORACLE_JWT_AUDIENCE must be configured together."
        )
    if not is_dev and not issuer:
        raise RuntimeError(
            "ORACLE_JWT_ISSUER and ORACLE_JWT_AUDIENCE are required outside development."
        )


_validate_jwt_scope_config(
    is_dev=_IS_DEV,
    issuer=_JWT_ISSUER,
    audience=_JWT_AUDIENCE,
)

# Input length guards — reject obviously malformed inputs early.
_MAX_AGENT_ID_LEN = 128
_MAX_PASSPHRASE_LEN = 256
_MAX_TOKEN_LEN = 8192  # standard JWT headroom

# ---------------------------------------------------------------------------
# Demo credential store
# In production this would be a hashed-password database lookup.
# ---------------------------------------------------------------------------

# Hardcoded demo logins are populated below ONLY when ORACLE_ENABLE_DEMO_LOGINS=1,
# so they never ship to a public deployment by default. Production auth comes from
# the env-injected ORACLE_ADMIN_ID / ORACLE_ADMIN_PASSPHRASE operator account.
DEMO_CREDENTIALS: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Demo tenancy map — agent_id → (tenant_id, role). The Auth Gatekeeper stamps
# these into the JWT at login so every downstream request carries its domain
# + role (see tenancy.py / db/schema.sql). In production this is a `users`
# table lookup, not a literal dict.
# ---------------------------------------------------------------------------

PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APEX_TENANT_ID = "11111111-1111-1111-1111-111111111111"

DEMO_TENANCY: dict[str, tuple[str, str]] = {}

# Local-dev convenience logins — OFF unless explicitly enabled. Never enable on an
# internet-facing box; use ORACLE_ADMIN_ID / ORACLE_ADMIN_PASSPHRASE instead.
# SECURITY: Demo credentials must only be set via environment variables.
# Hardcoded credentials have been removed to prevent accidental exposure.
_DEMO_USER = os.environ.get("ORACLE_DEMO_USER", "")
_DEMO_PASS = os.environ.get("ORACLE_DEMO_PASS", "")
_DEMO_ROLE = os.environ.get("ORACLE_DEMO_ROLE", "agent")
_DEMO_TENANT = os.environ.get("ORACLE_DEMO_TENANT", APEX_TENANT_ID)
if os.environ.get("ORACLE_ENABLE_DEMO_LOGINS", "").lower() in ("1", "true", "yes"):
    if _DEMO_USER and _DEMO_PASS:
        DEMO_CREDENTIALS[_DEMO_USER] = _DEMO_PASS
        DEMO_TENANCY[_DEMO_USER] = (_DEMO_TENANT, _DEMO_ROLE)

# Optional operator account injected from the environment (gitignored .env) —
# real credentials must never appear in this file, which is in source control.
_ADMIN_ID = os.environ.get("ORACLE_ADMIN_ID", "")
_ADMIN_PASSPHRASE = os.environ.get("ORACLE_ADMIN_PASSPHRASE", "")
if _ADMIN_ID and _ADMIN_PASSPHRASE:
    DEMO_CREDENTIALS[_ADMIN_ID] = _ADMIN_PASSPHRASE
    DEMO_TENANCY[_ADMIN_ID] = (PLATFORM_TENANT_ID, "platform_admin")

# ---------------------------------------------------------------------------
# DB-backed credentials + self-serve signup. The operator/demo dict above stays
# as the platform-admin fallback; everyone else is a users-table lookup. Password
# hashing uses stdlib scrypt (no extra dependency).
# ---------------------------------------------------------------------------
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
_RESET_TTL_SECONDS = 1800  # password-reset link valid 30 min
_RESET_JTI_BYTES = 32
_RESET_PURPOSE = "pwreset"
_RESET_ROLE = "password_reset"
_RESET_ERROR_DETAIL = "Reset link is invalid or has expired."
_FORGOT_RESPONSE = {
    "status": "ok",
    "detail": "If that email has an account, a reset link is on its way.",
}
MIN_PASSWORD_LEN = 10


def _hash_pw(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt$%s$%s" % (base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def _verify_pw(password: str, stored: Optional[str]) -> bool:
    if not stored:
        return False
    try:
        scheme, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:  # noqa: BLE001 — any parse/format error == invalid
        return False


def _slugify(text: str) -> str:
    base = "".join(c if c.isalnum() else "-" for c in (text or "").lower()).strip("-")[:40] or "tenant"
    return "%s-%s" % (base, secrets.token_hex(3))


def _hash_reset_jti(jti: str) -> str:
    """Return the irreversible database representation of a reset-token ID."""
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def _issue_jwt(sub: str, tenant_id: str, role: str, *, ttl: int = TOKEN_TTL_SECONDS, extra: Optional[dict] = None) -> str:
    now = time.time()
    payload: dict = {
        "sub": sub,
        "tenant_id": tenant_id,
        "role": role,
        "policy_version": PLATFORM_POLICY_VERSION,
        "iat": now,
        "exp": now + ttl,
    }
    if extra:
        payload.update(extra)
    if _JWT_ISSUER:
        payload["iss"] = _JWT_ISSUER
    if _JWT_AUDIENCE:
        payload["aud"] = _JWT_AUDIENCE
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return token.decode("utf-8") if isinstance(token, bytes) else token


def _admin_ctx():
    """Platform-admin TenantContext (RLS bypass) for pre-auth user lookups +
    signup inserts — login/register run before any caller context exists.
    role MUST be the Role enum: apply_rls_context() reads ctx.role.value."""
    from tenancy import TenantContext, Role  # lazy import avoids a startup cycle
    return TenantContext(agent_id="auth", tenant_id=PLATFORM_TENANT_ID, role=Role.PLATFORM_ADMIN)


async def _lookup_user(agent_id: str):
    from db.connection import tenant_tx
    async with tenant_tx(_admin_ctx()) as conn:
        return await conn.fetchrow(
            "SELECT users.id, users.agent_id, users.tenant_id, users.role, users.password_hash, "
            "users.policy_acceptance_required, "
            "EXISTS (SELECT 1 FROM user_policy_acceptances AS acceptance "
            "WHERE acceptance.user_id = users.id AND acceptance.policy_version = $2) "
            "AS has_current_policy_acceptance "
            "FROM users WHERE lower(users.agent_id) = lower($1) AND users.is_active",
            agent_id,
            PLATFORM_POLICY_VERSION,
        )


def _send_reset_email(to_email: str, link: str) -> None:
    """Best-effort password-reset email via SES. If SES isn't set up yet, log and
    move on — the /forgot endpoint still returns 202 (no account enumeration).
    Sender configurable via ORACLE_SES_SENDER (must be an SES-verified identity)."""
    sender = os.environ.get("ORACLE_SES_SENDER", "no-reply@neoh.app")
    region = os.environ.get("AWS_REGION", "us-east-1")
    try:
        import boto3
        boto3.client("sesv2", region_name=region).send_email(
            FromEmailAddress=sender,
            Destination={"ToAddresses": [to_email]},
            Content={"Simple": {
                "Subject": {"Data": "Reset your Neoh password"},
                "Body": {
                    "Html": {"Data": f'<p>Reset your Neoh password (link valid 30 minutes):</p><p><a href="{link}">{link}</a></p>'},
                    "Text": {"Data": f"Reset your Neoh password (valid 30 minutes): {link}"},
                },
            }},
        )
        log.info("Reset email sent to %r", to_email)
    except Exception as e:  # noqa: BLE001 — email is best-effort; never leak to the caller
        log.warning("Reset email not sent (SES not configured?): %s", e)

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


class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: str = ""
    company: str = ""


class ForgotRequest(BaseModel):
    email: str


class ResetRequest(BaseModel):
    token: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class LoginResponse(BaseModel):
    token: Optional[str] = None
    agent_id: str
    expires_in: int  # seconds
    # Stamped from the tenancy map so the frontend can gate role-specific
    # surfaces (e.g. the platform-admin OPS tab) without decoding the JWT.
    tenant_id: str
    role: str
    policy_acceptance_required: bool = False


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


def _set_session_cookie(response: Response, token: str) -> None:
    """Install the browser session without exposing the JWT to JavaScript."""
    import config

    response.set_cookie(
        key="oracle_session",
        value=token,
        httponly=True,
        secure=not config.IS_DEV,
        samesite="lax" if config.IS_DEV else "none",
        max_age=TOKEN_TTL_SECONDS,
        path="/",
    )


def _browser_token(token: str) -> Optional[str]:
    """Keep bearer compatibility for local/API clients, never production browsers."""
    import config

    return token if config.IS_DEV else None


@router.get("/csrf")
def csrf_bootstrap(request: Request, response: Response) -> dict[str, str]:
    """Return the double-submit value while installing its matching API cookie."""
    from csrf_middleware import get_or_issue_csrf_cookie

    return {"csrf_token": get_or_issue_csrf_cookie(request, response)}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> Response:
    # FastAPI's injected Response has no concrete status until the framework
    # builds a response. Returning it directly requires setting the status
    # explicitly or Uvicorn receives ``status_code=None``.
    response.status_code = status.HTTP_204_NO_CONTENT
    response.delete_cookie("oracle_session", path="/", samesite="none", secure=True)
    response.delete_cookie("csrf_token", path="/", samesite="none", secure=True)
    return response


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, response: Response) -> LoginResponse:
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

    # --- Credential validation -----------------------------------------------
    # 1) operator/demo dict (constant-time). 2) DB-backed users (self-serve signups).
    expected = DEMO_CREDENTIALS.get(body.agent_id, "")
    # hmac.compare_digest prevents timing-based enumeration of valid agent IDs.
    credentials_ok = bool(expected) and hmac.compare_digest(
        expected.encode(), body.passphrase.encode()
    )
    policy_acceptance_required = False
    if credentials_ok:
        tenant_id, role = DEMO_TENANCY.get(body.agent_id, (body.agent_id, "agent"))
    else:
        row = await _lookup_user(body.agent_id)
        if row and _verify_pw(body.passphrase, row["password_hash"]):
            credentials_ok = True
            tenant_id, role = str(row["tenant_id"]), row["role"]
            policy_acceptance_required = (
                bool(row["policy_acceptance_required"])
                or not bool(row["has_current_policy_acceptance"])
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

    token = _issue_jwt(
        body.agent_id,
        tenant_id,
        role,
        extra={"policy_pending": True} if policy_acceptance_required else None,
    )

    _register_session(body.agent_id)
    log.info("Successful login for agent_id=%r, tenant_id=%r.", body.agent_id, tenant_id)

    _set_session_cookie(response, token)

    _apply_rl_headers(response, limit, remaining, reset)
    return LoginResponse(
        token=_browser_token(token),
        agent_id=body.agent_id,
        expires_in=TOKEN_TTL_SECONDS,
        tenant_id=tenant_id,
        role=role,
        policy_acceptance_required=policy_acceptance_required,
    )


@router.post("/register", response_model=LoginResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, response: Response) -> LoginResponse:
    """Self-serve broker signup: create a fresh tenant + a broker_owner user, then
    log them in. agent_id (the JWT sub) is the normalized email."""
    email = body.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1] or len(email) > _MAX_AGENT_ID_LEN:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Enter a valid email address.")
    if not (MIN_PASSWORD_LEN <= len(body.password) <= _MAX_PASSPHRASE_LEN):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Password must be at least {MIN_PASSWORD_LEN} characters.")

    from db.connection import tenant_tx
    pw_hash = _hash_pw(body.password)
    company = (body.company or "").strip()
    async with tenant_tx(_admin_ctx()) as conn:
        if await conn.fetchval("SELECT 1 FROM users WHERE lower(agent_id) = lower($1)", email):
            raise HTTPException(status.HTTP_409_CONFLICT, "An account with that email already exists.")
        trow = await conn.fetchrow(
            "INSERT INTO tenants (slug, name) VALUES ($1, $2) RETURNING id",
            _slugify(company or email.split("@")[0]), company or email,
        )
        await conn.execute(
            "INSERT INTO users (tenant_id, agent_id, role, password_hash, email, full_name, company, policy_acceptance_required) "
            "VALUES ($1, $2, 'broker_owner', $3, $4, $5, $6, true)",
            trow["id"], email, pw_hash, email, body.full_name.strip(), company,
        )
    tenant_id = str(trow["id"])
    token = _issue_jwt(email, tenant_id, "broker_owner", extra={"policy_pending": True})
    _set_session_cookie(response, token)
    _register_session(email)
    log.info("New signup: agent_id=%r tenant_id=%r", email, tenant_id)
    return LoginResponse(
        token=_browser_token(token),
        agent_id=email,
        expires_in=TOKEN_TTL_SECONDS,
        tenant_id=tenant_id,
        role="broker_owner",
        policy_acceptance_required=True,
    )


@router.post("/forgot", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(body: ForgotRequest):
    """Email a time-limited reset link. Always 202 (never reveals whether the email
    has an account). Only a SHA-256 digest of the random JWT ID is persisted."""
    email = body.email.strip().lower()
    try:
        row = await _lookup_user(email) if 0 < len(email) <= _MAX_AGENT_ID_LEN else None
        if row:
            jti = secrets.token_urlsafe(_RESET_JTI_BYTES)
            token = _issue_jwt(
                str(row["agent_id"]),
                str(row["tenant_id"]),
                _RESET_ROLE,
                ttl=_RESET_TTL_SECONDS,
                extra={"purpose": _RESET_PURPOSE, "jti": jti},
            )
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=_RESET_TTL_SECONDS)

            from db.connection import tenant_tx
            async with tenant_tx(_admin_ctx()) as conn:
                await conn.execute(
                    "INSERT INTO password_reset_tokens "
                    "(jti_hash, user_id, tenant_id, expires_at) VALUES ($1, $2, $3, $4)",
                    _hash_reset_jti(jti),
                    row["id"],
                    row["tenant_id"],
                    expires_at,
                )

            base = os.environ.get("ORACLE_BASE_URL", "https://neoh.app").rstrip("/")
            _send_reset_email(email, f"{base}/?reset={token}")
    except Exception:  # noqa: BLE001 - forgot must never disclose account or infrastructure state
        log.exception("Password reset request could not be completed.")

    return dict(_FORGOT_RESPONSE)


@router.post("/reset", response_model=LoginResponse)
async def reset_password(body: ResetRequest, response: Response) -> LoginResponse:
    """Atomically consume a reset token, set the new password, and log in."""
    if not (MIN_PASSWORD_LEN <= len(body.new_password) <= _MAX_PASSPHRASE_LEN):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    try:
        claims = decode_token(body.token)  # validates signature + expiry
    except HTTPException:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _RESET_ERROR_DETAIL)

    jti = claims.get("jti")
    agent_id = claims.get("sub")
    tenant_claim = claims.get("tenant_id")
    if (
        claims.get("purpose") != _RESET_PURPOSE
        or claims.get("role") != _RESET_ROLE
        or not isinstance(jti, str)
        or not (_RESET_JTI_BYTES <= len(jti) <= 128)
        or not isinstance(agent_id, str)
        or not (0 < len(agent_id) <= _MAX_AGENT_ID_LEN)
        or not isinstance(tenant_claim, str)
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _RESET_ERROR_DETAIL)
    try:
        tenant_id = str(UUID(tenant_claim))
    except (ValueError, AttributeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _RESET_ERROR_DETAIL)

    from db.connection import tenant_tx
    pw_hash = _hash_pw(body.new_password)
    async with tenant_tx(_admin_ctx()) as conn:
        row = await conn.fetchrow(
            "WITH consumed_reset AS ("
            "UPDATE password_reset_tokens AS reset_token "
            "SET consumed_at = now() "
            "FROM users AS reset_user "
            "WHERE reset_token.jti_hash = $1 "
            "AND reset_token.consumed_at IS NULL "
            "AND reset_token.expires_at > now() "
            "AND reset_token.tenant_id = $2::uuid "
            "AND reset_user.id = reset_token.user_id "
            "AND reset_user.tenant_id = reset_token.tenant_id "
            "AND lower(reset_user.agent_id) = lower($3) "
            "AND reset_user.is_active "
            "RETURNING reset_token.user_id, reset_token.tenant_id"
            ") "
            "UPDATE users AS account "
            "SET password_hash = $4, updated_at = now() "
            "FROM consumed_reset "
            "WHERE account.id = consumed_reset.user_id "
            "AND account.tenant_id = consumed_reset.tenant_id "
            "AND account.is_active "
            "RETURNING account.id, account.agent_id, account.tenant_id, account.role, "
            "account.policy_acceptance_required, "
            "EXISTS (SELECT 1 FROM user_policy_acceptances AS acceptance "
            "WHERE acceptance.user_id = account.id AND acceptance.policy_version = $5) "
            "AS has_current_policy_acceptance",
            _hash_reset_jti(jti),
            tenant_id,
            agent_id,
            pw_hash,
            PLATFORM_POLICY_VERSION,
        )
        if not row:
            # Raise before tenant_tx exits so a pathological CTE partial result
            # is rolled back together with the token-consumption update.
            raise HTTPException(status.HTTP_400_BAD_REQUEST, _RESET_ERROR_DETAIL)

    current_agent_id = str(row["agent_id"])
    tenant_id = str(row["tenant_id"])
    role = str(row["role"])
    policy_acceptance_required = (
        bool(row["policy_acceptance_required"])
        or not bool(row["has_current_policy_acceptance"])
    )
    token = _issue_jwt(
        current_agent_id,
        tenant_id,
        role,
        extra={"policy_pending": True} if policy_acceptance_required else None,
    )
    _register_session(current_agent_id)
    _set_session_cookie(response, token)
    log.info("Password reset for agent_id=%r", current_agent_id)
    return LoginResponse(
        token=_browser_token(token),
        agent_id=current_agent_id,
        expires_in=TOKEN_TTL_SECONDS,
        tenant_id=tenant_id,
        role=role,
        policy_acceptance_required=policy_acceptance_required,
    )


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    authorization: Optional[str] = Header(default=None),
):
    """Logged-in self-service password change (current → new). DB users only — the
    env operator account is managed via ORACLE_ADMIN_PASSPHRASE, not here.
    Uses auth's own decode_token (no tenancy import) to stay self-contained."""
    authorization = authorization or (
        f"Bearer {request.cookies.get('oracle_session')}"
        if request.cookies.get("oracle_session")
        else None
    )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required.")
    claims = decode_token(authorization.split(" ", 1)[1])  # validates sig+exp → 401
    agent_id = claims.get("sub", "")
    if not (MIN_PASSWORD_LEN <= len(body.new_password) <= _MAX_PASSPHRASE_LEN):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"New password must be at least {MIN_PASSWORD_LEN} characters.")
    row = await _lookup_user(agent_id)
    if not row or not row["password_hash"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This account's password is managed by your administrator, not self-service.")
    if not _verify_pw(body.current_password, row["password_hash"]):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect.")

    from db.connection import tenant_tx
    new_hash = _hash_pw(body.new_password)
    async with tenant_tx(_admin_ctx()) as conn:
        await conn.execute(
            "UPDATE users SET password_hash = $2, updated_at = now() WHERE lower(agent_id) = lower($1)",
            agent_id, new_hash,
        )
    log.info("Password changed (self-service) for agent_id=%r", agent_id)
    return {"status": "ok", "detail": "Password updated."}


@router.post("/verify", response_model=VerifyResponse)
def verify(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> VerifyResponse:
    """
    Decode and validate a Bearer JWT.

    Expects:  Authorization: Bearer <token>
    Returns:  { "agent_id": str, "issued_at": float, "expires_at": float }
              or 401 on invalid / expired token.
    """
    authorization = authorization or (
        f"Bearer {request.cookies.get('oracle_session')}"
        if request.cookies.get("oracle_session")
        else None
    )
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
