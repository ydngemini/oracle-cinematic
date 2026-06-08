"""
Oracle — PII encryption / decryption helpers (decision 008).

Key hierarchy
─────────────
                        ORACLE_ENCRYPTION_MASTER_KEY   (env var, never persisted)
                                       │
                            HKDF-SHA-256 (info = tenant_id UTF-8)
                                       │
                        per-tenant derived key  (hex string, 32 bytes)
                                       │
                       pgp_sym_encrypt / pgp_sym_decrypt  (pgcrypto)
                                       │
                        ciphertext column  (stored in Postgres)

Why three levels?
- Master key rotates rarely; tenant keys can be re-derived on demand — no
  per-tenant secret store required.
- Tenant keys are derived independently, so a leaked tenant key exposes only
  that tenant's data, not the whole platform.
- pgcrypto's pgp_sym_encrypt adds a per-message salt (S2K), so even two
  identical plaintexts produce different ciphertexts.

SQL wrapper functions (migration 0006):
  oracle_encrypt(plaintext text)   → bytea   — reads app.encryption_key GUC
  oracle_decrypt(ciphertext bytea) → text    — reads app.encryption_key GUC

Those functions let application SQL use encryption transparently without
passing the key in every query parameter.  The GUC is set per-transaction
via pii_context() below so it is always scoped correctly and never leaks
across connection-pool hops.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import struct
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from tenancy import TenantContext
from audit_ledger import ledger, AuditCategory

log = logging.getLogger("oracle.crypto")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HKDF_HASH = "sha256"
_DERIVED_KEY_LEN = 32     # bytes → 64-char hex
_HKDF_INFO_PREFIX = b"oracle-pii-v1:"


class CryptoError(RuntimeError):
    """Raised for any crypto operation failure (key missing, pgcrypto absent,
    decryption failure).  Callers should translate to HTTP 500 — never expose
    the message body to the client."""


# ---------------------------------------------------------------------------
# 1. Key derivation
# ---------------------------------------------------------------------------

def _get_master_key() -> bytes:
    """Read and validate ORACLE_ENCRYPTION_MASTER_KEY from the environment.

    Raises CryptoError (not KeyError) so callers get a consistent exception
    type regardless of why the key is unavailable.
    """
    raw = os.environ.get("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not raw:
        raise CryptoError(
            "ORACLE_ENCRYPTION_MASTER_KEY is not set. "
            "Export the master key before starting the Oracle backend."
        )
    return raw.encode("utf-8")


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """HKDF-Extract (RFC 5869 §2.2): PRK = HMAC-Hash(salt, IKM)."""
    return hmac.new(salt, ikm, _HKDF_HASH).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """HKDF-Expand (RFC 5869 §2.3): produce `length` bytes from PRK + info."""
    hash_len = hashlib.new(_HKDF_HASH).digest_size
    n = -(-length // hash_len)          # ceil division
    t, okm = b"", b""
    for i in range(1, n + 1):
        t = hmac.new(prk, t + info + struct.pack("B", i), _HKDF_HASH).digest()
        okm += t
    return okm[:length]


def derive_tenant_key(tenant_id: str, master_key: str) -> str:
    """Derive a deterministic, tenant-specific encryption key from the master
    secret using HKDF-SHA-256 (RFC 5869).

    Args:
        tenant_id:  UUID string identifying the tenant (used as HKDF info).
        master_key: The platform master secret (hex or raw string).
                    In production, pass ``os.environ["ORACLE_ENCRYPTION_MASTER_KEY"]``.

    Returns:
        A 64-character hex string (32-byte derived key) safe to use as a
        pgp_sym_encrypt passphrase.

    The derivation is deterministic and stateless — you can re-derive the same
    key at any time from (tenant_id, master_key) without storing anything.
    """
    if not tenant_id:
        raise CryptoError("derive_tenant_key: tenant_id must not be empty.")
    if not master_key:
        raise CryptoError("derive_tenant_key: master_key must not be empty.")

    ikm = master_key.encode("utf-8") if isinstance(master_key, str) else master_key
    # Use a zero salt (common practice when IKM is already a strong secret).
    salt = bytes(_DERIVED_KEY_LEN)
    prk = _hkdf_extract(salt, ikm)
    info = _HKDF_INFO_PREFIX + tenant_id.encode("utf-8")
    derived = _hkdf_expand(prk, info, _DERIVED_KEY_LEN)
    return derived.hex()


# ---------------------------------------------------------------------------
# 2. Low-level pgcrypto helpers
# ---------------------------------------------------------------------------

async def encrypt_pii(conn, plaintext: str, tenant_key: str) -> bytes:
    """Encrypt *plaintext* with pgcrypto's OpenPGP symmetric cipher.

    Args:
        conn:       An asyncpg connection (or transaction connection).
        plaintext:  The sensitive string to encrypt.
        tenant_key: The 64-char hex key returned by :func:`derive_tenant_key`.

    Returns:
        Raw bytea ciphertext as a Python ``bytes`` object.

    Raises:
        CryptoError: pgcrypto extension is not installed, or any Postgres error.
    """
    if not plaintext:
        raise CryptoError("encrypt_pii: plaintext must not be empty.")
    if not tenant_key:
        raise CryptoError("encrypt_pii: tenant_key must not be empty.")

    try:
        row = await conn.fetchrow(
            "SELECT pgp_sym_encrypt($1::text, $2::text) AS ct",
            plaintext,
            tenant_key,
        )
    except Exception as exc:
        _raise_pgcrypto_error("encrypt_pii", exc)

    ct: bytes = row["ct"]
    log.debug("encrypt_pii: produced %d-byte ciphertext.", len(ct))
    return ct


async def decrypt_pii(conn, ciphertext: bytes, tenant_key: str) -> str:
    """Decrypt pgcrypto-encrypted *ciphertext*.

    Args:
        conn:       An asyncpg connection (or transaction connection).
        ciphertext: The ``bytes`` value stored in the PII column.
        tenant_key: The 64-char hex key returned by :func:`derive_tenant_key`.

    Returns:
        The original plaintext string.

    Raises:
        CryptoError: Wrong key, corrupted ciphertext, pgcrypto absent, or any
                     Postgres error.
    """
    if not ciphertext:
        raise CryptoError("decrypt_pii: ciphertext must not be empty.")
    if not tenant_key:
        raise CryptoError("decrypt_pii: tenant_key must not be empty.")

    try:
        row = await conn.fetchrow(
            "SELECT pgp_sym_decrypt($1::bytea, $2::text) AS pt",
            ciphertext,
            tenant_key,
        )
    except Exception as exc:
        _raise_pgcrypto_error("decrypt_pii", exc)

    pt: str = row["pt"]
    return pt


# ---------------------------------------------------------------------------
# 3. Per-connection GUC context manager
# ---------------------------------------------------------------------------

@asynccontextmanager
async def pii_context(conn, ctx: TenantContext) -> AsyncGenerator[None, None]:
    """Set the ``app.encryption_key`` GUC on *conn* for the duration of the
    ``async with`` block, then clear it.

    Usage::

        async with pii_context(conn, ctx):
            # SQL inside here can call oracle_encrypt() / oracle_decrypt()
            # without an explicit key parameter — the GUC carries it.
            row = await conn.fetchrow("SELECT oracle_decrypt(ssn) FROM leads WHERE id=$1", lid)

    The key is set with ``is_local = true`` when called inside an existing
    transaction (the common case via ``tenant_tx``), so it resets automatically
    on transaction end.  The explicit RESET in the ``finally`` block is a
    belt-and-suspenders guard for cases where the caller holds a raw connection
    outside a transaction.

    Raises:
        CryptoError: ORACLE_ENCRYPTION_MASTER_KEY not set.
    """
    master_key = _get_master_key().decode("utf-8")
    tenant_key = derive_tenant_key(ctx.tenant_id, master_key)

    try:
        await conn.execute(
            "SELECT set_config('app.encryption_key', $1, true)",
            tenant_key,
        )
        log.debug(
            "pii_context: app.encryption_key GUC set for tenant=%r agent=%r.",
            ctx.tenant_id,
            ctx.agent_id,
        )
        yield
    finally:
        # Clear the key so it does not linger if the connection escapes the pool
        # without a full transaction reset.
        try:
            await conn.execute(
                "SELECT set_config('app.encryption_key', '', false)"
            )
        except Exception:  # noqa: BLE001 — best-effort cleanup, never swallow the original exc
            log.warning(
                "pii_context: failed to clear app.encryption_key GUC for tenant=%r.",
                ctx.tenant_id,
            )


# ---------------------------------------------------------------------------
# 4. High-level audited accessor
# ---------------------------------------------------------------------------

async def access_pii_field(
    conn,
    ctx: TenantContext,
    target_id: str,
    field_name: str,
    ciphertext: bytes,
) -> str:
    """Decrypt *ciphertext* and record an ACCESS_PII audit entry in the ledger.

    Every PII field read via this function leaves a tamper-proof chain-of-custody
    entry so compliance teams can answer "who read whose phone number, and when".

    Args:
        conn:        An asyncpg connection (already inside a tenant transaction).
        ctx:         The requesting agent's TenantContext.
        target_id:   The UUID of the record whose PII is being accessed
                     (e.g. a lead_id or client_id).
        field_name:  Human-readable column name, e.g. ``"phone"`` or ``"ssn"``.
        ciphertext:  The raw bytea value to decrypt.

    Returns:
        The plaintext string.

    Raises:
        CryptoError: Key not set, decryption failure, or pgcrypto absent.
    """
    master_key = _get_master_key().decode("utf-8")
    tenant_key = derive_tenant_key(ctx.tenant_id, master_key)

    # Decrypt first — if this fails we must NOT write an audit entry, because
    # the access never actually succeeded.
    plaintext = await decrypt_pii(conn, ciphertext, tenant_key)

    # Record access in the tamper-proof audit chain.
    try:
        ledger.record(
            category=AuditCategory.USER_STATE_CHANGE,
            actor=ctx.agent_id,
            action="ACCESS_PII",
            payload={
                "tenant_id": ctx.tenant_id,
                "target_id": target_id,
                "field": field_name,
                "role": ctx.role.value,
            },
        )
    except Exception as exc:  # noqa: BLE001
        # Audit failure is serious but must not silently swallow the decrypted
        # value — log loudly and let the caller handle.
        log.error(
            "access_pii_field: audit write failed for actor=%r target=%r field=%r: %s",
            ctx.agent_id, target_id, field_name, exc,
        )
        raise CryptoError(
            f"PII access succeeded but audit ledger write failed for "
            f"target={target_id!r} field={field_name!r}. "
            "Treating as error to maintain compliance guarantees."
        ) from exc

    log.info(
        "ACCESS_PII: actor=%r tenant=%r target=%r field=%r role=%r",
        ctx.agent_id, ctx.tenant_id, target_id, field_name, ctx.role.value,
    )
    return plaintext


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _raise_pgcrypto_error(caller: str, exc: Exception) -> None:
    """Normalise asyncpg / Postgres errors into CryptoError with clear messages.

    Never re-raises raw Postgres errors to callers — they often contain key
    material or plaintext fragments in their message strings.
    """
    msg = str(exc).lower()
    if "pgp_sym_encrypt" in msg or "pgp_sym_decrypt" in msg or "function" in msg:
        raise CryptoError(
            f"{caller}: pgcrypto is not installed or pgp_sym_* functions are "
            "unavailable. Ensure migration 0006 has been applied and pgcrypto "
            "is enabled on the database."
        ) from exc
    if "wrong key" in msg or "bad passphrase" in msg or "decrypt" in msg:
        raise CryptoError(
            f"{caller}: decryption failed — ciphertext is corrupted or the "
            "wrong tenant key was used. This may indicate tenant_id mismatch "
            "or master key rotation without re-encryption."
        ) from exc
    # Generic fallback — log the real error internally, surface a safe message.
    log.error("%s: unexpected Postgres error during crypto operation: %s", caller, exc)
    raise CryptoError(
        f"{caller}: unexpected database error during crypto operation. "
        "See server logs for details."
    ) from exc
