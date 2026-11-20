"""Server-side sessions for the environment operator (external audit 28.3).

The deployment-configured operator (ORACLE_ADMIN_ID, and any environment
identity configured with the platform_admin role) has no users row, so the
0117 session epoch never covered it: logout only cleared the browser cookie,
and a copied operator token kept platform-wide authority until it expired,
through logout and through a passphrase rotation.

Every operator sign-in now inserts a row in operator_sessions (migration 0181),
keyed by the SHA-256 of a random `jti` the token carries. A token is a session
only while its row is unrevoked, unexpired, and carries the fingerprint of the
credential configured NOW, so rotating the passphrase or the second factor
ends every session issued before it. Where it is checked:

  * every tenant transaction of an operator session (tenancy.apply_rls_context
    runs app_begin_operator_session, as it runs app_begin_session for a users
    row), so every privileged gate and WebSocket admission that calls
    tenancy.verify_session_current checks it too;
  * every HTTP request carrying an operator token, by OperatorSessionMiddleware
    below, including routes that answer from memory and open no transaction;
  * /auth/session and /auth/verify (auth._live_session).

/auth/logout revokes the account's live rows. When the registry cannot be read
the request is refused (503 session_unverifiable for HTTP, 1011 for operator
streams): an unprovable operator session is never admitted.

Other environment (demo) identities keep their behaviour: they are trusted by
signature, dev/staging only (config.DEMO_LOGIN_ENVS).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING, Any, Optional

from log_redaction import actor_ref

if TYPE_CHECKING:
    from tenancy import TenantContext

log = logging.getLogger("oracle.auth.operator_sessions")

# secrets.token_urlsafe(32) is 43 characters; accept a little either side.
JTI_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_JTI_BYTES = 32
_PLATFORM_ADMIN = "platform_admin"

# The credential is stretched once per process (scrypt, 16 MiB), so a copy of
# the table together with the server secret is still not a fast passphrase
# oracle. The result is cached: the configured credential does not change
# while the process runs.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
_FINGERPRINTS: dict[str, str] = {}
_FINGERPRINT_CACHE_MAX = 16


def is_registry_identity(agent_id: Any, tenant_id: Any, role: Any) -> bool:
    """An environment identity (auth.DEMO_TENANCY, exact match on all three)
    whose role is platform_admin: its sessions live in operator_sessions."""
    from auth import DEMO_TENANCY  # lazy: auth imports nothing at module level from here

    role = getattr(role, "value", role)
    return (role == _PLATFORM_ADMIN and isinstance(agent_id, str)
            and DEMO_TENANCY.get(agent_id) == (tenant_id, role))


def any_registry_identity() -> bool:
    from auth import DEMO_TENANCY

    return any(role == _PLATFORM_ADMIN for _tenant, role in DEMO_TENANCY.values())


def jti_hash(jti: str) -> str:
    """What the database stores for a session id: never the id itself."""
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def _credential_material(agent_id: str) -> Optional[bytes]:
    import auth

    passphrase = auth.DEMO_CREDENTIALS.get(agent_id)
    if not passphrase:
        return None
    # The configured second factor belongs to the operator account only (as
    # at sign-in): rotating the TOTP secret or the code address ends sessions too.
    second = (f"{auth._ADMIN_TOTP_SECRET}\0{auth._ADMIN_OTP_EMAIL}"
              if auth._ADMIN_ID and agent_id == auth._ADMIN_ID else "")
    return "\0".join(("neoh-operator-credential:v1", agent_id.strip().lower(), passphrase, second)).encode()


def _derive(material: bytes, key: bytes) -> str:
    salt = hmac.new(key, b"neoh-operator-credential-salt:v1", hashlib.sha256).digest()
    stretched = hashlib.scrypt(material, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return hmac.new(key, b"neoh-operator-credential:v1:" + stretched, hashlib.sha256).hexdigest()


async def credential_fingerprint(agent_id: str) -> str:
    """Keyed, stretched, non-reversible fingerprint of the credential this
    environment identity signs in with today (passphrase, and for the operator
    its second-factor material). "" when none is configured, which matches no
    row: a session whose credential was removed is refused."""
    import auth

    material = _credential_material(agent_id)
    if material is None:
        return ""
    key = auth.SECRET_KEY.encode()
    cache_key = hashlib.sha256(key + b"\0" + material).hexdigest()
    fingerprint = _FINGERPRINTS.get(cache_key)
    if fingerprint is None:
        fingerprint = await asyncio.to_thread(_derive, material, key)
        if len(_FINGERPRINTS) >= _FINGERPRINT_CACHE_MAX:
            _FINGERPRINTS.clear()
        _FINGERPRINTS[cache_key] = fingerprint
    return fingerprint


async def register(agent_id: str) -> str:
    """Record a new operator session and return its jti (for the token).
    Raises when the registry cannot be written: no row, no session."""
    from auth import TOKEN_TTL_SECONDS, _admin_ctx
    from db.connection import tenant_tx

    jti = secrets.token_urlsafe(_JTI_BYTES)
    fingerprint = await credential_fingerprint(agent_id)
    async with tenant_tx(_admin_ctx()) as conn:
        await conn.execute(
            "INSERT INTO operator_sessions (jti_hash, account, credential_fingerprint, expires_at) "
            "VALUES ($1, $2, $3, now() + make_interval(secs => $4))",
            jti_hash(jti), agent_id.strip().lower(), fingerprint, TOKEN_TTL_SECONDS,
        )
    return jti


async def revoke(payload: dict) -> int:
    """Logout: end every live session of this operator account, as logout
    ends every device's session for a users-table account (epoch bump). Only
    when the presented session is itself current, so a stale or rotated-out
    token cannot keep signing the operator out. Returns the rows revoked."""
    agent_id, jti = payload.get("sub"), payload.get("jti")
    if not is_registry_identity(agent_id, payload.get("tenant_id"), payload.get("role")):
        return 0
    if not isinstance(jti, str) or not JTI_RE.match(jti):
        return 0
    from auth import _admin_ctx
    from db.connection import tenant_tx

    account = agent_id.strip().lower()
    fingerprint = await credential_fingerprint(agent_id)
    async with tenant_tx(_admin_ctx()) as conn:
        status = await conn.execute(
            "UPDATE operator_sessions SET revoked_at = now() "
            "WHERE account = $1 AND revoked_at IS NULL "
            "AND EXISTS (SELECT 1 FROM operator_sessions AS presented "
            "WHERE presented.jti_hash = $2 AND presented.account = $1 "
            "AND presented.revoked_at IS NULL AND presented.expires_at > now() "
            "AND presented.credential_fingerprint = $3)",
            account, jti_hash(jti), fingerprint,
        )
    revoked = int(str(status).rsplit(" ", 1)[-1]) if str(status).startswith("UPDATE") else 0
    log.info("Operator signed out: %d session(s) revoked (%s).", revoked, actor_ref(agent_id))
    return revoked


def renewal_claims(ctx: "TenantContext") -> dict:
    """Keyword arguments for auth._issue_jwt when an operator session's token
    is re-minted (policy renewal): same jti, and no later expiry than the
    registered session, which ends 24 h after sign-in."""
    if not ctx.operator_session:
        return {}
    claims: dict = {"extra": {"jti": ctx.operator_session}}
    if ctx.expires_at is not None:
        claims["ttl"] = max(1, int(ctx.expires_at - time.time()))
    return claims


# ── every HTTP request ────────────────────────────────────────────────────

_SELF_CHECKING_PREFIX = "/auth/"


def _authorization(scope) -> Optional[str]:
    """The credential the route would use: an explicit bearer header first,
    otherwise the session cookie (tenancy._request_authorization)."""
    from starlette.requests import HTTPConnection

    conn = HTTPConnection(scope)
    header = conn.headers.get("authorization")
    if header:
        return header
    token = conn.cookies.get("oracle_session", "")
    return f"Bearer {token}" if token else None


async def _verdict(authorization: str):
    """None to let the request through, or the response that refuses it."""
    from fastapi import HTTPException
    from starlette.responses import JSONResponse

    import auth
    import tenancy

    try:
        ctx = tenancy._context_from_authorization(
            authorization, allow_policy_pending=True, allow_stale_policy=True, allow_mfa_enrolment=True)
    except HTTPException:
        return None  # not a usable session at all; the route answers for itself
    if not ctx.operator_session:
        return None
    try:
        await auth._prove_session_live(ctx)
    except auth.SessionUnverifiable:
        return JSONResponse(status_code=503, content={"detail": auth._session_unverifiable().detail})
    except HTTPException:
        log.warning("Operator request refused: session ended (%s).", actor_ref(ctx.agent_id))
        response = JSONResponse(status_code=401, content={"detail": "Your session has ended. Sign in again."})
        auth._clear_session_cookie(response)
        return response
    return None


class OperatorSessionMiddleware:
    """Prove the registry row of every HTTP request that carries an operator
    token before the route runs. /auth/ routes are left to themselves: login
    and logout must work with a dead cookie, and /auth/session and
    /auth/verify run the same proof (auth._live_session) with their own
    answers. Requests without an operator token pass untouched (one HMAC
    check of the token's signature, no database)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope.get("type") == "http"
                and not str(scope.get("path", "")).startswith(_SELF_CHECKING_PREFIX)
                and any_registry_identity()):
            authorization = _authorization(scope)
            if authorization:
                refusal = await _verdict(authorization)
                if refusal is not None:
                    await refusal(scope, receive, send)
                    return
        await self.app(scope, receive, send)
