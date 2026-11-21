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

/auth/logout revokes the account's live rows. A sign-in and every process
start (boot) revoke the rows another credential issued, so rolling a rotated
credential back does not revive them. When the registry cannot be read the
request is refused (503 session_unverifiable for HTTP, 1011 for operator
streams): an unprovable operator session is never admitted.

The account is lower()-cased in SQL on every path (insert, revoke, the check
in app_begin_operator_session), so both sides normalise it the same way.

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

from log_redaction import actor_ref, redact_path

if TYPE_CHECKING:
    from tenancy import TenantContext

log = logging.getLogger("oracle.auth.operator_sessions")

# secrets.token_urlsafe(32) is 43 characters; accept a little either side.
# Always matched with fullmatch(): `$` would also accept a trailing newline.
JTI_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
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


def valid_jti(jti: Any) -> bool:
    return isinstance(jti, str) and JTI_RE.fullmatch(jti) is not None


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


_REVOKE_OTHER_CREDENTIALS = (
    "UPDATE operator_sessions SET revoked_at = now() "
    "WHERE account = lower($1) AND revoked_at IS NULL AND credential_fingerprint <> $2"
)


def _revoked_count(status: Any) -> int:
    text = str(status)
    return int(text.rsplit(" ", 1)[-1]) if text.startswith("UPDATE") else 0


async def register(agent_id: str) -> str:
    """Record a new operator session and return its jti (for the token).
    Rows issued under another credential are revoked first, so a rotated-out
    credential that is later configured again finds them ended. Raises when
    the registry cannot be written: no row, no session."""
    from auth import TOKEN_TTL_SECONDS, _admin_ctx
    from db.connection import tenant_tx

    jti = secrets.token_urlsafe(_JTI_BYTES)
    fingerprint = await credential_fingerprint(agent_id)
    async with tenant_tx(_admin_ctx()) as conn:
        stale = _revoked_count(await conn.execute(_REVOKE_OTHER_CREDENTIALS, agent_id, fingerprint))
        await conn.execute(
            "INSERT INTO operator_sessions (jti_hash, account, credential_fingerprint, expires_at) "
            "VALUES ($1, lower($2), $3, now() + make_interval(secs => $4))",
            jti_hash(jti), agent_id, fingerprint, TOKEN_TTL_SECONDS,
        )
    if stale:
        log.warning("Operator sign-in revoked %d session(s) issued under a previous credential (%s).",
                    stale, actor_ref(agent_id))
    return jti


def registry_identities() -> list[str]:
    from auth import DEMO_TENANCY

    return [agent for agent, (_tenant, role) in DEMO_TENANCY.items() if role == _PLATFORM_ADMIN]


async def boot() -> None:
    """Once per process, off the request path: stretch each operator
    credential (so the first check does not run scrypt while holding a
    platform-pool connection) and revoke the sessions another credential
    issued (a restart is how a rotated credential takes effect). Never raises."""
    import db.connection as dbc
    from auth import _admin_ctx

    for agent_id in registry_identities():
        try:
            fingerprint = await credential_fingerprint(agent_id)
            if dbc.get_pool() is None:
                continue
            async with dbc.tenant_tx(_admin_ctx()) as conn:
                stale = _revoked_count(await conn.execute(_REVOKE_OTHER_CREDENTIALS, agent_id, fingerprint))
            if stale:
                log.warning("Operator credential changed: %d session(s) issued under the previous one "
                            "revoked at boot (%s).", stale, actor_ref(agent_id))
        except Exception as exc:  # noqa: BLE001 - a boot step must not stop the boot
            log.warning("Operator session boot step skipped (%s): %s", actor_ref(agent_id),
                        type(exc).__name__)


_boot_tasks: set = set()


def start_boot() -> None:
    task = asyncio.get_running_loop().create_task(boot(), name="operator-sessions-boot")
    _boot_tasks.add(task)
    task.add_done_callback(_boot_tasks.discard)


async def revoke(payload: dict) -> int:
    """Logout: end every live session of this operator account, as logout
    ends every device's session for a users-table account (epoch bump). Only
    when the presented session is itself current, so a stale or rotated-out
    token cannot keep signing the operator out. Returns the rows revoked."""
    agent_id, jti = payload.get("sub"), payload.get("jti")
    if not is_registry_identity(agent_id, payload.get("tenant_id"), payload.get("role")):
        return 0
    if not valid_jti(jti):
        return 0
    from auth import _admin_ctx
    from db.connection import tenant_tx

    fingerprint = await credential_fingerprint(agent_id)
    async with tenant_tx(_admin_ctx()) as conn:
        status = await conn.execute(
            "UPDATE operator_sessions SET revoked_at = now() "
            "WHERE account = lower($1) AND revoked_at IS NULL "
            "AND EXISTS (SELECT 1 FROM operator_sessions AS presented "
            "WHERE presented.jti_hash = $2 AND presented.account = lower($1) "
            "AND presented.revoked_at IS NULL AND presented.expires_at > now() "
            "AND presented.credential_fingerprint = $3)",
            agent_id, jti_hash(jti), fingerprint,
        )
    revoked = _revoked_count(status)
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

# The only paths the middleware leaves alone, matched exactly: everything else,
# including any /auth/ route added later, is checked. login, logout and csrf
# must work with a dead cookie; session and verify run the same proof
# themselves (auth._live_session) with their own answers; register, forgot,
# reset, invitation and accept-invite are pre-sign-in forms that never read the
# session, so a stale operator cookie must not block them. The policy and
# two-step routes do read it and are checked (they also prove it in their own
# transaction).
SELF_CHECKING_PATHS = frozenset({
    "/auth/login", "/auth/logout", "/auth/csrf", "/auth/session", "/auth/verify",
    "/auth/register", "/auth/forgot", "/auth/reset", "/auth/invitation", "/auth/accept-invite",
})


def _ip_ref(scope) -> str:
    """A stable, non-reversible reference for the caller's address (the
    rate limiter's view of it), for the refusal log line. Keyed with the
    server secret auth resolved at import (never a public constant: with no
    ORACLE_SECRET_KEY set, a dev process uses its ephemeral key, so the
    reference cannot be recomputed from an address list)."""
    from starlette.requests import Request

    import auth
    from rate_limit_middleware import _get_client_ip

    try:
        ip = _get_client_ip(Request(scope))
    except Exception:  # noqa: BLE001 - a log reference must never fail the request
        ip = "unknown"
    key = auth.SECRET_KEY.encode()
    return "ip:" + hmac.new(key, b"log-ip:v1:" + ip.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:12]


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


async def _verdict(authorization: str, scope=None):
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
        log.warning("Operator request refused: its session could not be checked (%s, %s).",
                    actor_ref(ctx.agent_id), redact_path(str((scope or {}).get("path", "-"))))
        return JSONResponse(status_code=503, content={"detail": auth._session_unverifiable().detail})
    except HTTPException:
        # Someone presented an ended operator session: a copied token after
        # logout or a rotation, or the operator's own stale cookie.
        log.warning("Operator request refused: session ended (%s, %s %s, %s).", actor_ref(ctx.agent_id),
                    (scope or {}).get("method", "-"), redact_path(str((scope or {}).get("path", "-"))),
                    _ip_ref(scope) if scope else "ip:-")
        response = JSONResponse(status_code=401, content={"detail": "Your session has ended. Sign in again."})
        auth._clear_session_cookie(response)
        return response
    return None


class OperatorSessionMiddleware:
    """Prove the registry row of every HTTP request that carries an operator
    token before the route runs, except on SELF_CHECKING_PATHS. Requests
    without an operator token pass untouched (one HMAC check of the token's
    signature, no database)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope.get("type") == "http"
                and str(scope.get("path", "")) not in SELF_CHECKING_PATHS
                and any_registry_identity()):
            authorization = _authorization(scope)
            if authorization:
                refusal = await _verdict(authorization, scope)
                if refusal is not None:
                    await refusal(scope, receive, send)
                    return
        await self.app(scope, receive, send)
