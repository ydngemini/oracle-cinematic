"""Two-step sign-in for tenant users: authenticator-app codes (L17-19).

Owner decision 2026-10-14: every tenant user may turn it on; broker owners
must (at their next sign-in); a broker owner can require it for the whole
brokerage (tenants.require_mfa). ISO 27001 A.8.5 gap #2.

This is the operator's second factor (totp.py, 0121), per account:
- the code check is totp.verify — RFC 6238, 30-second steps, ±1 step;
- a code is accepted once: the accepted step is recorded and a code at or
  before it is refused (0121 recorded used steps; one monotonic step per
  account is the same rule with one row);
- the secret is sealed with the user's brokerage key (pgcrypto under the
  per-tenant HKDF key, crypto.py) and never stored or logged in plaintext;
- five wrong codes lock the factor for 15 minutes (0121 retired an emailed
  code after five). The code is only ever checked after the password
  matched, so a lockout can be caused only by someone who has the password.
- recovery codes are 80-bit random strings, stored as SHA-256 and spent on
  first use.

Every query runs on a connection the caller opened in the platform context
(auth._admin_ctx): 0150 admits nothing else to these tables. Callers open
the transaction; `verify` returns its outcome rather than raising, so a
wrong code's counter is committed before the caller refuses the request.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import totp
from crypto import CryptoError, decrypt_pii, derive_tenant_key, encrypt_pii

ISSUER = "Neoh"
RECOVERY_CODE_COUNT = 10
MAX_FAILED_CODES = 5
LOCK_SECONDS = 15 * 60
# Starting setup without retyping the password is allowed this soon after it
# was typed at sign-in. Later, a stolen session could otherwise enrol the
# thief's phone and lock the owner out of their own account.
FRESH_SIGN_IN_SECONDS = 10 * 60

# Crockford base32: no I, L, O or U, so a code read aloud or retyped survives.
_RECOVERY_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RECOVERY_LENGTH = 16  # 16 × 5 bits = 80 bits
_CROCKFORD_FIXUPS = str.maketrans({"O": "0", "I": "1", "L": "1"})

# Roles that must use two-step sign-in whatever the brokerage chose. A
# platform_admin account in `users` holds cross-tenant power, so it is
# included with the broker owner.
REQUIRED_ROLES = frozenset({"broker_owner", "platform_admin"})

_SQL_STATE = (
    "SELECT f.confirmed_at, f.locked_until, "
    "       (f.locked_until IS NOT NULL AND f.locked_until > now()) AS locked, "
    "       (SELECT count(*) FROM user_mfa_recovery_codes c "
    "         WHERE c.user_id = f.user_id AND c.used_at IS NULL) AS recovery_remaining "
    "  FROM user_mfa_factors f WHERE f.user_id = $1"
)
_SQL_LOCK_FACTOR = (
    "SELECT tenant_id, totp_secret_ciphertext, confirmed_at, last_used_step, failed_attempts, "
    "       (locked_until IS NOT NULL AND locked_until > now()) AS locked "
    "  FROM user_mfa_factors WHERE user_id = $1 FOR UPDATE"
)
_SQL_UPSERT_PENDING = (
    "INSERT INTO user_mfa_factors (user_id, tenant_id, totp_secret_ciphertext) "
    "VALUES ($1, $2::uuid, $3) "
    "ON CONFLICT (user_id) DO UPDATE SET totp_secret_ciphertext = EXCLUDED.totp_secret_ciphertext, "
    "       tenant_id = EXCLUDED.tenant_id, last_used_step = NULL, updated_at = now() "
    " WHERE user_mfa_factors.confirmed_at IS NULL "
    "RETURNING user_id"
)
_SQL_CONFIRM = (
    "UPDATE user_mfa_factors SET confirmed_at = now(), last_used_step = $2::bigint, "
    "       failed_attempts = 0, locked_until = NULL, updated_at = now() "
    " WHERE user_id = $1 AND confirmed_at IS NULL"
)
_SQL_ACCEPT_STEP = (
    "UPDATE user_mfa_factors SET last_used_step = $2::bigint, failed_attempts = 0, locked_until = NULL, "
    "       updated_at = now() "
    " WHERE user_id = $1 AND (last_used_step IS NULL OR last_used_step < $2::bigint) "
    "RETURNING user_id"
)
_SQL_CLEAR_FAILURES = (
    "UPDATE user_mfa_factors SET failed_attempts = 0, locked_until = NULL, updated_at = now() "
    " WHERE user_id = $1"
)
_SQL_RECORD_FAILURE = (
    "UPDATE user_mfa_factors "
    "   SET failed_attempts = CASE WHEN $2::integer >= $3::integer THEN 0 ELSE $2::integer END, "
    "       locked_until = CASE WHEN $2::integer >= $3::integer "
    "                           THEN now() + make_interval(secs => $4::double precision) "
    "                           ELSE locked_until END, "
    "       updated_at = now() "
    " WHERE user_id = $1"
)
_SQL_SPEND_RECOVERY = (
    "UPDATE user_mfa_recovery_codes SET used_at = now() "
    " WHERE user_id = $1 AND code_hash = $2 AND used_at IS NULL RETURNING id"
)
_SQL_DELETE_RECOVERY = "DELETE FROM user_mfa_recovery_codes WHERE user_id = $1"
_SQL_INSERT_RECOVERY = (
    "INSERT INTO user_mfa_recovery_codes (user_id, tenant_id, code_hash) "
    "SELECT $1, $2::uuid, h FROM unnest($3::text[]) AS t(h)"
)
_SQL_DELETE_FACTOR = "DELETE FROM user_mfa_factors WHERE user_id = $1 RETURNING confirmed_at"
_SQL_ENABLED_AMONG = (
    "SELECT user_id FROM user_mfa_factors WHERE user_id = ANY($1::uuid[]) AND confirmed_at IS NOT NULL"
)


class MfaError(Exception):
    """A refusal the API turns into an HTTP answer. Messages name no secret."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    @property
    def detail(self) -> dict:
        return {"code": self.code, "message": self.message}


class MfaUnavailable(MfaError):
    """The encryption key is not configured: fail closed, never store plaintext."""

    def __init__(self) -> None:
        super().__init__(503, "MFA_UNAVAILABLE",
                         "Two-step sign-in is unavailable right now. Try again shortly.")


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    method: Optional[str] = None      # "totp" | "recovery"
    locked: bool = False              # refused because the factor is locked
    just_locked: bool = False         # this failure locked it

    def error(self) -> MfaError:
        if self.locked or self.just_locked:
            return MfaError(429, "MFA_LOCKED",
                            "Too many wrong codes. Wait 15 minutes, or ask your brokerage owner "
                            "to reset two-step sign-in.")
        return MfaError(401, "OTP_INVALID", "That code is not valid or has already been used. Try again.")


def required_reason(role: Any, tenant_requires: Any) -> Optional[str]:
    """Why this account must use two-step sign-in, or None when it is optional."""
    if str(role or "") in REQUIRED_ROLES:
        return "role"
    if bool(tenant_requires):
        return "brokerage"
    return None


def _key(tenant_id: Any) -> str:
    master = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not master:
        raise MfaUnavailable()
    try:
        return derive_tenant_key(str(tenant_id), master)
    except CryptoError:
        raise MfaUnavailable() from None


def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_LENGTH))
        codes.append("-".join(raw[i:i + 4] for i in range(0, _RECOVERY_LENGTH, 4)))
    return codes


def normalise_recovery_code(code: str) -> str:
    cleaned = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return cleaned.translate(_CROCKFORD_FIXUPS)


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(normalise_recovery_code(code).encode("ascii", "ignore")).hexdigest()


def looks_like_totp(code: str) -> bool:
    candidate = "".join((code or "").split())
    return len(candidate) == totp.DIGITS and candidate.isdigit()


def looks_like_recovery_code(code: str) -> bool:
    return len(normalise_recovery_code(code)) == _RECOVERY_LENGTH


def display_secret(secret: str) -> str:
    """The manual-entry key in groups of four, the way authenticator apps show it."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


async def state(conn, user_id: Any) -> dict:
    row = await conn.fetchrow(_SQL_STATE, user_id)
    if row is None:
        return {"enabled": False, "pending": False, "enrolled_at": None,
                "recovery_codes_remaining": 0, "locked_until": None}
    enabled = row["confirmed_at"] is not None
    return {
        "enabled": enabled,
        "pending": not enabled,
        "enrolled_at": row["confirmed_at"],
        "recovery_codes_remaining": int(row["recovery_remaining"] or 0) if enabled else 0,
        "locked_until": row["locked_until"] if row["locked"] else None,
    }


async def begin_enrolment(conn, *, user_id: Any, tenant_id: Any, account: str) -> dict:
    """A fresh secret for an authenticator app. Replaces an unfinished setup;
    refuses while a confirmed factor exists (turn it off or reset it first)."""
    import qr_code

    key = _key(tenant_id)
    existing = await conn.fetchrow(_SQL_LOCK_FACTOR, user_id)
    if existing is not None and existing["confirmed_at"] is not None:
        raise MfaError(409, "MFA_ALREADY_ENABLED", "Two-step sign-in is already on for this account.")
    secret = totp.new_secret()
    try:
        sealed = await encrypt_pii(conn, secret, key)
    except CryptoError:
        raise MfaUnavailable() from None
    stored = await conn.fetchval(_SQL_UPSERT_PENDING, user_id, str(tenant_id), sealed)
    if stored is None:  # confirmed concurrently
        raise MfaError(409, "MFA_ALREADY_ENABLED", "Two-step sign-in is already on for this account.")
    uri = totp.provisioning_uri(secret, account, ISSUER)
    return {
        "secret": display_secret(secret),
        "otpauth_uri": uri,
        "qr": qr_code.rows(uri),
        "issuer": ISSUER,
        "account": account,
        "digits": totp.DIGITS,
        "period": totp.STEP_SECONDS,
    }


async def _open_secret(conn, row) -> str:
    try:
        return await decrypt_pii(conn, bytes(row["totp_secret_ciphertext"]), _key(row["tenant_id"]))
    except CryptoError:
        raise MfaUnavailable() from None


async def _replace_recovery_codes(conn, user_id: Any, tenant_id: Any) -> list[str]:
    codes = new_recovery_codes()
    await conn.execute(_SQL_DELETE_RECOVERY, user_id)
    await conn.execute(_SQL_INSERT_RECOVERY, user_id, str(tenant_id), [hash_recovery_code(c) for c in codes])
    return codes


async def confirm_enrolment(conn, *, user_id: Any, code: str) -> list[str]:
    """Turn the factor on with a first code from the app; returns the one-time
    recovery codes (shown once, stored only as hashes)."""
    row = await conn.fetchrow(_SQL_LOCK_FACTOR, user_id)
    if row is None:
        raise MfaError(409, "MFA_NOT_STARTED", "Start the setup again: no authenticator key is waiting.")
    if row["confirmed_at"] is not None:
        raise MfaError(409, "MFA_ALREADY_ENABLED", "Two-step sign-in is already on for this account.")
    step = totp.verify(await _open_secret(conn, row), code)
    if step is None:
        raise MfaError(400, "OTP_INVALID",
                       "That code does not match. Check the time on your phone and enter the newest code.")
    await conn.execute(_SQL_CONFIRM, user_id, step)
    return await _replace_recovery_codes(conn, user_id, row["tenant_id"])


async def verify(conn, *, user_id: Any, code: Optional[str]) -> VerifyResult:
    """Check a sign-in code or a recovery code for an enabled factor. Never
    raises for a wrong code: the counter it writes must commit."""
    row = await conn.fetchrow(_SQL_LOCK_FACTOR, user_id)
    if row is None or row["confirmed_at"] is None:
        return VerifyResult(ok=False)
    if row["locked"]:
        return VerifyResult(ok=False, locked=True)
    candidate = (code or "").strip()
    if looks_like_totp(candidate):
        step = totp.verify(await _open_secret(conn, row), candidate)
        if step is not None and await conn.fetchval(_SQL_ACCEPT_STEP, user_id, step) is not None:
            return VerifyResult(ok=True, method="totp")
    elif looks_like_recovery_code(candidate):
        if await conn.fetchval(_SQL_SPEND_RECOVERY, user_id, hash_recovery_code(candidate)) is not None:
            await conn.execute(_SQL_CLEAR_FAILURES, user_id)
            return VerifyResult(ok=True, method="recovery")
    attempts = int(row["failed_attempts"] or 0) + 1
    await conn.execute(_SQL_RECORD_FAILURE, user_id, attempts, MAX_FAILED_CODES, LOCK_SECONDS)
    return VerifyResult(ok=False, just_locked=attempts >= MAX_FAILED_CODES)


async def regenerate_recovery_codes(conn, *, user_id: Any) -> list[str]:
    row = await conn.fetchrow(_SQL_LOCK_FACTOR, user_id)
    if row is None or row["confirmed_at"] is None:
        raise MfaError(409, "MFA_NOT_ENABLED", "Two-step sign-in is not on for this account.")
    return await _replace_recovery_codes(conn, user_id, row["tenant_id"])


async def remove(conn, *, user_id: Any) -> bool:
    """Delete the factor and its recovery codes. True when a confirmed factor existed."""
    await conn.execute(_SQL_DELETE_RECOVERY, user_id)
    confirmed = await conn.fetch(_SQL_DELETE_FACTOR, user_id)
    return any(r["confirmed_at"] is not None for r in confirmed)


async def enabled_among(conn, user_ids: Iterable[Any]) -> set[str]:
    ids = [str(u) for u in user_ids]
    if not ids:
        return set()
    return {str(r["user_id"]) for r in await conn.fetch(_SQL_ENABLED_AMONG, ids)}
