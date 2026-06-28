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
if os.environ.get("ORACLE_ENABLE_DEMO_LOGINS", "").lower() in ("1", "true", "yes"):
    DEMO_CREDENTIALS.update({
        "oracle_agent": "nexus_access_2026",
        "analyst_01":   "scanner_ready",
        "ydn":          "sypher_core",
    })
    DEMO_TENANCY.update({
        "ydn":          (PLATFORM_TENANT_ID, "platform_admin"),  # god-mode override
        "oracle_agent": (APEX_TENANT_ID,     "broker_owner"),
        "analyst_01":   (APEX_TENANT_ID,     "agent"),
    })

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


def _issue_jwt(sub: str, tenant_id: str, role: str, *, ttl: int = TOKEN_TTL_SECONDS, extra: Optional[dict] = None) -> str:
    now = time.time()
    payload: dict = {"sub": sub, "tenant_id": tenant_id, "role": role, "iat": now, "exp": now + ttl}
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
    signup inserts — login/register run before any caller context exists."""
    from tenancy import TenantContext  # lazy import avoids a startup cycle
    return TenantContext(agent_id="auth", tenant_id=PLATFORM_TENANT_ID, role="platform_admin")


async def _lookup_user(agent_id: str):
    from db.connection import tenant_tx
    async with tenant_tx(_admin_ctx()) as conn:
        return await conn.fetchrow(
            "SELECT tenant_id, role, password_hash FROM users "
            "WHERE lower(agent_id) = lower($1) AND is_active",
            agent_id,
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
    if credentials_ok:
        tenant_id, role = DEMO_TENANCY.get(body.agent_id, (body.agent_id, "agent"))
    else:
        row = await _lookup_user(body.agent_id)
        if row and _verify_pw(body.passphrase, row["password_hash"]):
            credentials_ok = True
            tenant_id, role = str(row["tenant_id"]), row["role"]

    if not credentials_ok:
        # Don't distinguish "unknown agent" from "wrong passphrase" — prevents
        # user enumeration via timing or error messages.
        log.debug("Failed login attempt for agent_id=%r.", body.agent_id)
        _apply_rl_headers(response, limit, remaining - 1 if remaining else 0, reset)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid agent credentials.",
        )

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
            "INSERT INTO users (tenant_id, agent_id, role, password_hash, email, full_name, company) "
            "VALUES ($1, $2, 'broker_owner', $3, $4, $5, $6)",
            trow["id"], email, pw_hash, email, body.full_name.strip(), company,
        )
    tenant_id = str(trow["id"])
    token = _issue_jwt(email, tenant_id, "broker_owner")
    _register_session(email)
    log.info("New signup: agent_id=%r tenant_id=%r", email, tenant_id)
    return LoginResponse(token=token, agent_id=email, expires_in=TOKEN_TTL_SECONDS, tenant_id=tenant_id, role="broker_owner")


@router.post("/forgot", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(body: ForgotRequest):
    """Email a time-limited reset link. Always 202 (never reveals whether the email
    has an account). The token is a short-TTL signed JWT with purpose=pwreset."""
    email = body.email.strip().lower()
    row = await _lookup_user(email)
    if row:
        token = _issue_jwt(email, str(row["tenant_id"]), row["role"], ttl=_RESET_TTL_SECONDS, extra={"purpose": "pwreset"})
        base = os.environ.get("ORACLE_BASE_URL", "https://neoh.app").rstrip("/")
        _send_reset_email(email, f"{base}/?reset={token}")
    return {"status": "ok", "detail": "If that email has an account, a reset link is on its way."}


@router.post("/reset", response_model=LoginResponse)
async def reset_password(body: ResetRequest, response: Response) -> LoginResponse:
    """Consume a reset token (purpose=pwreset), set the new password, log in."""
    if not (MIN_PASSWORD_LEN <= len(body.new_password) <= _MAX_PASSPHRASE_LEN):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    try:
        claims = decode_token(body.token)  # validates signature + expiry
    except HTTPException:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Reset link is invalid or has expired.")
    if claims.get("purpose") != "pwreset":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Reset link is invalid or has expired.")

    email = claims["sub"]
    from db.connection import tenant_tx
    pw_hash = _hash_pw(body.new_password)
    async with tenant_tx(_admin_ctx()) as conn:
        tid = await conn.fetchval(
            "UPDATE users SET password_hash = $2, updated_at = now() "
            "WHERE lower(agent_id) = lower($1) AND is_active RETURNING tenant_id",
            email, pw_hash,
        )
    if not tid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Account not found.")
    tenant_id, role = str(tid), claims.get("role", "agent")
    token = _issue_jwt(email, tenant_id, role)
    _register_session(email)
    log.info("Password reset for agent_id=%r", email)
    return LoginResponse(token=token, agent_id=email, expires_in=TOKEN_TTL_SECONDS, tenant_id=tenant_id, role=role)


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
