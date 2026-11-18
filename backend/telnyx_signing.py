"""Which Telnyx signing keys may authenticate an inbound webhook, and for
which brokerages (external audit 25.4).

The webhook used to verify every event with one process-wide key,
TELNYX_PUBLIC_KEY. An event from a brokerage's own Telnyx account (its own
signing key) failed, and nothing tied the key that signed an event to the
brokerage the event was about. Now:

- the registry holds the platform key (TELNYX_PUBLIC_KEY, plus
  TELNYX_PUBLIC_KEY_PREVIOUS while rotating) and each brokerage's own key from
  its stored Telnyx credential (public_key, plus public_key_previous);
- an event is authentic if one of those keys verifies the Ed25519 signature
  over "timestamp|raw body" and the timestamp is within 5 minutes;
- it may act for a brokerage only if the verifying key is that brokerage's
  own, or the platform's and the brokerage runs on the platform account. A
  number's destination never authenticates anything.
- A STOP is never dropped: any authenticated key may record an opt-out for a
  brokerage it is tied to, and the platform key (Telnyx-held, unforgeable)
  for any holder of the number — over-suppression is the safe direction.

"Own account" mirrors how the app resolves a brokerage's Telnyx credential
(messaging_api._telnyx_credentials): a brokerage runs its own account only if
its stored api_key is not the platform's and it stored its own public_key;
otherwise the platform key speaks for it.

Today no brokerage can store a Telnyx credential (the provider endpoints
accept google/smtp/twilio, and provider_credentials_provider_check refuses
'telnyx'), so only the platform keys exist. Before that changes, a brokerage
public_key must be controlled — fetched from Telnyx with the brokerage's
api_key or approved by an operator — not trusted as typed (L10-18).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

logger = logging.getLogger("oracle.messaging.signing")

CACHE_SECONDS = 60.0
# A miss (no key verifies) reloads the brokerage keys at most this often.
MISS_RELOAD_SECONDS = 30.0
# After a failed load, the last good set is kept and a reload is retried soon.
FAILED_LOAD_RETRY_SECONDS = 5.0
TIMESTAMP_TOLERANCE_SECONDS = 300


@dataclass(frozen=True)
class Verified:
    """What the verifying key proves: the platform account, and/or the
    brokerages whose own account holds the key."""
    platform: bool
    tenants: frozenset[str]
    label: str
    own_account_tenants: frozenset[str] = field(default_factory=frozenset)
    # brokerages whose stored credential could not be read: refusals for them
    # are temporary (retry), not a verdict
    unreadable_tenants: frozenset[str] = field(default_factory=frozenset)

    def refusal_is_temporary(self, tenant_id: Any) -> bool:
        return str(tenant_id or "") in self.unreadable_tenants

    def may_act_for(self, tenant_id: Any) -> bool:
        tenant = str(tenant_id or "")
        if not tenant:
            return False
        if tenant in self.tenants:
            return True
        return self.platform and tenant not in self.own_account_tenants

    def may_suppress_for(self, tenant_id: Any) -> bool:
        """An authenticated STOP: recorded for a brokerage this key may act
        for, or for any brokerage when the platform key signed it."""
        return self.may_act_for(tenant_id) or (self.platform and bool(str(tenant_id or "")))


@dataclass
class _Key:
    verify_key: Any            # nacl VerifyKey, built once
    platform: bool
    tenants: frozenset[str]
    label: str


@dataclass
class _Registry:
    keys: dict[str, _Key]
    own_account_tenants: frozenset[str]
    unreadable_tenants: frozenset[str] = frozenset()


@dataclass
class _TenantKeys:
    credentials: list[tuple[str, Optional[dict[str, Any]]]]   # None: the row could not be read
    loaded_at: float
    built: Optional[tuple[tuple[str, str, str], "_Registry"]] = None   # (platform env, registry)


_cache: Optional[_TenantKeys] = None
_last_miss_reload = 0.0
_load_lock: Optional[asyncio.Lock] = None
_warned_rotation = False


class CredentialsUnavailable(Exception):
    """The credential table could not be read."""


async def _tenant_credentials() -> list[tuple[str, Optional[dict[str, Any]]]]:
    """Every active stored Telnyx credential: (tenant_id, fields), or
    (tenant_id, None) for a row that could not be decrypted. Raises
    CredentialsUnavailable if the table cannot be read at all."""
    from crypto import decrypt_pii, derive_tenant_key
    from db.connection import tenant_tx
    from tenancy import Role, TenantContext

    master = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    ctx = TenantContext(agent_id="telnyx-webhook-keys", role=Role.PLATFORM_ADMIN,
                        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"))
    try:
        return await _decrypt_all(tenant_tx, ctx, master, decrypt_pii, derive_tenant_key)
    except Exception as exc:  # noqa: BLE001
        raise CredentialsUnavailable(str(exc)) from exc


async def _decrypt_all(tenant_tx, ctx, master, decrypt_pii, derive_tenant_key):
    out: list[tuple[str, Optional[dict[str, Any]]]] = []
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            "SELECT tenant_id::text AS tenant_id, token_ciphertext FROM provider_credentials "
            "WHERE provider='telnyx' AND disabled_at IS NULL AND (expires_at IS NULL OR expires_at > now())")
        for row in rows:
            fields: Optional[dict[str, Any]] = None
            try:
                if not master:
                    raise ValueError("ORACLE_ENCRYPTION_MASTER_KEY is not set")
                raw = await decrypt_pii(conn, row["token_ciphertext"], derive_tenant_key(row["tenant_id"], master))
                decoded = json.loads(raw) if raw else {}
                fields = decoded if isinstance(decoded, dict) else None
            except Exception:  # noqa: BLE001 — one unreadable credential must not stop the others
                logger.warning("Telnyx credential for tenant %s unreadable; its key is not trusted", row["tenant_id"])
            out.append((row["tenant_id"], fields))
    return out


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def _build(credentials: list[tuple[str, Optional[dict[str, Any]]]]) -> _Registry:
    from nacl.signing import VerifyKey

    keys: dict[str, _Key] = {}

    def add(key: Any, *, platform: bool = False, tenant: Optional[str] = None, label: str) -> None:
        key = str(key or "").strip()
        if not key:
            return
        try:
            raw = base64.b64decode(key, validate=True)
            verify_key = VerifyKey(raw) if len(raw) == 32 else None
        except Exception:  # noqa: BLE001
            verify_key = None
        if verify_key is None:
            logger.warning("Telnyx signing key (%s) is not a valid Ed25519 public key; ignored", label)
            return
        current = keys.get(key)
        if current is None:
            keys[key] = _Key(verify_key, platform, frozenset({tenant} if tenant else ()), label)
        else:
            keys[key] = _Key(current.verify_key, current.platform or platform,
                             current.tenants | ({tenant} if tenant else set()), current.label)

    add(os.getenv("TELNYX_PUBLIC_KEY"), platform=True, label="platform")
    add(os.getenv("TELNYX_PUBLIC_KEY_PREVIOUS"), platform=True, label="platform-previous")
    platform_api = str(os.getenv("TELNYX_API_KEY", "")).strip()
    platform_fp = _fingerprint(platform_api) if platform_api else None
    own: set[str] = set()
    unreadable: set[str] = set()
    for tenant_id, fields in credentials:
        if fields is None:
            # Unreadable: fail closed for everything but STOPs (the platform
            # key no longer speaks for it; may_suppress_for still records them),
            # and its other events are retried rather than refused for good.
            own.add(tenant_id)
            unreadable.add(tenant_id)
            continue
        api_key = str(fields.get("api_key") or "").strip()
        has_own_key = bool(str(fields.get("public_key") or "").strip())
        if api_key and _fingerprint(api_key) != platform_fp and has_own_key:
            own.add(tenant_id)
        if has_own_key:
            add(fields.get("public_key"), tenant=tenant_id, label="tenant")
            add(fields.get("public_key_previous"), tenant=tenant_id, label="tenant-previous")
    return _Registry(keys=keys, own_account_tenants=frozenset(own), unreadable_tenants=frozenset(unreadable))


async def _registry(*, force: bool = False) -> _Registry:
    """The platform keys are read from the environment every time (a rotation
    takes effect at once); the brokerage credentials are cached. A failed
    load keeps the last good set and retries soon; it is never cached as
    'no brokerage keys'."""
    global _cache, _load_lock, _warned_rotation
    if os.getenv("TELNYX_PUBLIC_KEY_PREVIOUS") and not _warned_rotation:
        _warned_rotation = True
        logger.warning("ORACLE_ALERT telnyx_signing_key_rotation TELNYX_PUBLIC_KEY_PREVIOUS is set: the old "
                       "platform signing key is still trusted; clear it once the rotation is complete")
    stale = _cache is None or time.monotonic() - _cache.loaded_at > CACHE_SECONDS
    if force or stale:
        if _load_lock is None:
            _load_lock = asyncio.Lock()
        async with _load_lock:
            stale = _cache is None or time.monotonic() - _cache.loaded_at > CACHE_SECONDS
            if force or stale:
                try:
                    _cache = _TenantKeys(credentials=await _tenant_credentials(), loaded_at=time.monotonic())
                except CredentialsUnavailable:
                    logger.warning("brokerage Telnyx signing keys unavailable; keeping the last known set",
                                   exc_info=True)
                    previous = _cache.credentials if _cache else []
                    _cache = _TenantKeys(credentials=previous,
                                         loaded_at=time.monotonic() - CACHE_SECONDS + FAILED_LOAD_RETRY_SECONDS)
    if _cache is None:
        return _build([])
    # Built once per load and platform-key setting, not on every request.
    env = (os.getenv("TELNYX_PUBLIC_KEY", ""), os.getenv("TELNYX_PUBLIC_KEY_PREVIOUS", ""),
           os.getenv("TELNYX_API_KEY", ""))
    if _cache.built is None or _cache.built[0] != env:
        _cache.built = (env, _build(_cache.credentials))
    return _cache.built[1]


def reset_cache() -> None:
    global _cache, _last_miss_reload, _warned_rotation
    _cache, _last_miss_reload, _warned_rotation = None, 0.0, False


def _match(registry: _Registry, raw_body: str, headers: Mapping[str, str]) -> Optional[str]:
    """The registry key whose Ed25519 signature checks out, or None. The
    timestamp is checked once; each key is a prebuilt VerifyKey (no client is
    built per key), and the first match wins — two distinct keys cannot both
    verify one signature."""
    from nacl.exceptions import BadSignatureError

    normalized = {name.lower(): value for name, value in headers.items()}
    signature_header, timestamp_header = (normalized.get("telnyx-signature-ed25519"),
                                          normalized.get("telnyx-timestamp"))
    if not signature_header or not timestamp_header:
        return None
    try:
        timestamp = int(timestamp_header)
        signature = base64.b64decode(signature_header, validate=True)
    except Exception:  # noqa: BLE001
        return None
    if abs(time.time() - timestamp) > TIMESTAMP_TOLERANCE_SECONDS or len(signature) != 64:
        return None
    message = f"{timestamp_header}|{raw_body}".encode("utf-8")
    for key, entry in registry.keys.items():
        try:
            entry.verify_key.verify(message, signature)
        except BadSignatureError:
            continue
        return key
    return None


class NoSigningKeys(Exception):
    """No key is configured at all: webhooks cannot be authenticated."""


async def verify(raw_body: str, headers: Mapping[str, str]) -> tuple[Any, Verified]:
    """The event and what authenticated it. Raises ValueError if no
    authorized key verifies it (tampered, unknown or retired key, stale
    timestamp), NoSigningKeys if none is configured."""
    global _last_miss_reload
    from messaging_provider import TelnyxMessagingProvider

    registry = await _registry()
    if not registry.keys:
        raise NoSigningKeys("Telnyx webhook validation is not configured.")
    key = _match(registry, raw_body, headers)
    if key is None and time.monotonic() - _last_miss_reload > MISS_RELOAD_SECONDS:
        _last_miss_reload = time.monotonic()
        registry = await _registry(force=True)
        key = _match(registry, raw_body, headers)
    if key is None:
        raise ValueError("no authorized Telnyx signing key verifies this webhook")
    entry = registry.keys[key]
    # Parsed once, through the SDK with the one key that matched (it re-checks).
    event = TelnyxMessagingProvider().validate_webhook(raw_body, dict(headers), credentials={"public_key": key})
    return event, Verified(platform=entry.platform, tenants=entry.tenants, label=entry.label,
                           own_account_tenants=registry.own_account_tenants,
                           unreadable_tenants=registry.unreadable_tenants)
