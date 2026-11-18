"""Which Telnyx signing keys may authenticate an inbound webhook, and for
which brokerages (external audit 25.4).

The webhook used to verify every event with one process-wide key,
TELNYX_PUBLIC_KEY. An event from a brokerage's own Telnyx account (its own
signing key) failed, and nothing tied the key that signed an event to the
brokerage the event was about. Now:

- the registry holds the platform key (TELNYX_PUBLIC_KEY, plus
  TELNYX_PUBLIC_KEY_PREVIOUS while rotating) and each brokerage's own key from
  its stored Telnyx credential (public_key, plus public_key_previous);
- an event is authentic if one of those keys verifies its raw body and
  timestamp (the SDK rejects a timestamp more than 5 minutes off);
- it may act for a brokerage only if the verifying key is that brokerage's
  own, or the platform's and the brokerage has no Telnyx account of its own.
  A number's destination never authenticates anything.

Today no brokerage can store a Telnyx credential through the app (the
provider endpoints accept google/smtp/twilio), so every brokerage is on the
platform account; the binding is in place for when one can.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

logger = logging.getLogger("oracle.messaging.signing")

CACHE_SECONDS = 60.0
# A miss (no key verifies) reloads the registry at most this often: a
# brokerage that just saved its key is picked up, a flood of forgeries is not
# a flood of credential decryptions.
MISS_RELOAD_SECONDS = 30.0


@dataclass(frozen=True)
class Verified:
    """What the verifying key(s) prove: the platform account, and/or the
    brokerages whose own account holds the key."""
    platform: bool
    tenants: frozenset[str]
    label: str
    own_account_tenants: frozenset[str] = field(default_factory=frozenset)

    def may_act_for(self, tenant_id: Any) -> bool:
        tenant = str(tenant_id or "")
        if not tenant:
            return False
        if tenant in self.tenants:
            return True
        return self.platform and tenant not in self.own_account_tenants


@dataclass
class _Registry:
    keys: dict[str, tuple[bool, frozenset[str], str]]   # key -> (platform, tenants, label)
    own_account_tenants: frozenset[str]


@dataclass
class _TenantKeys:
    credentials: list[tuple[str, dict[str, Any]]]
    loaded_at: float


_cache: Optional[_TenantKeys] = None
_last_miss_reload = 0.0


async def _tenant_credentials() -> list[tuple[str, dict[str, Any]]]:
    """Every active stored Telnyx credential, decrypted (tenant_id, fields)."""
    from crypto import decrypt_pii, derive_tenant_key
    from db.connection import tenant_tx
    from tenancy import Role, TenantContext

    master = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not master:
        return []
    ctx = TenantContext(agent_id="telnyx-webhook-keys", role=Role.PLATFORM_ADMIN,
                        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"))
    out: list[tuple[str, dict[str, Any]]] = []
    try:
        return await _decrypt_all(tenant_tx, ctx, master, decrypt_pii, derive_tenant_key, out)
    except Exception:  # noqa: BLE001 — the platform key still works without the database
        logger.warning("brokerage Telnyx signing keys unavailable; only the platform key is trusted", exc_info=True)
        return out


async def _decrypt_all(tenant_tx, ctx, master, decrypt_pii, derive_tenant_key, out):
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            "SELECT tenant_id::text AS tenant_id, token_ciphertext FROM provider_credentials "
            "WHERE provider='telnyx' AND disabled_at IS NULL AND (expires_at IS NULL OR expires_at > now())")
        for row in rows:
            try:
                raw = await decrypt_pii(conn, row["token_ciphertext"], derive_tenant_key(row["tenant_id"], master))
                fields = json.loads(raw) if raw else {}
            except Exception:  # noqa: BLE001 — one unreadable credential must not stop the others
                logger.warning("Telnyx credential for tenant %s unreadable; its key is not trusted", row["tenant_id"])
                continue
            if isinstance(fields, dict):
                out.append((row["tenant_id"], fields))
    return out


def _build(credentials: list[tuple[str, dict[str, Any]]]) -> _Registry:
    keys: dict[str, tuple[bool, frozenset[str], str]] = {}

    def add(key: Any, *, platform: bool = False, tenant: Optional[str] = None, label: str) -> None:
        key = str(key or "").strip()
        if not key:
            return
        was_platform, tenants, old_label = keys.get(key, (False, frozenset(), label))
        keys[key] = (was_platform or platform, tenants | ({tenant} if tenant else set()),
                     old_label if key in keys else label)

    add(os.getenv("TELNYX_PUBLIC_KEY"), platform=True, label="platform")
    add(os.getenv("TELNYX_PUBLIC_KEY_PREVIOUS"), platform=True, label="platform-previous")
    own: set[str] = set()
    for tenant_id, fields in credentials:
        if str(fields.get("api_key") or "").strip():
            own.add(tenant_id)      # sends through its own account: the platform key does not speak for it
        add(fields.get("public_key"), tenant=tenant_id, label="tenant")
        add(fields.get("public_key_previous"), tenant=tenant_id, label="tenant-previous")
    return _Registry(keys=keys, own_account_tenants=frozenset(own))


async def _registry(*, force: bool = False) -> _Registry:
    """The platform keys are read from the environment every time (a rotation
    takes effect at once); only the decrypted brokerage keys are cached."""
    global _cache
    if force or _cache is None or time.monotonic() - _cache.loaded_at > CACHE_SECONDS:
        _cache = _TenantKeys(credentials=await _tenant_credentials(), loaded_at=time.monotonic())
    return _build(_cache.credentials)


def reset_cache() -> None:
    global _cache, _last_miss_reload
    _cache, _last_miss_reload = None, 0.0


def _try(registry: _Registry, raw_body: str, headers: Mapping[str, str]) -> tuple[Any, Optional[Verified]]:
    from messaging_provider import TelnyxMessagingProvider

    adapter = TelnyxMessagingProvider()
    event = None
    platform, tenants, labels = False, set(), []
    for key, (is_platform, key_tenants, label) in registry.keys.items():
        try:
            event = adapter.validate_webhook(raw_body, dict(headers), credentials={"public_key": key})
        except Exception:  # noqa: BLE001 — not this key
            continue
        platform = platform or is_platform
        tenants |= set(key_tenants)
        labels.append(label)
    if event is None:
        return None, None
    return event, Verified(platform=platform, tenants=frozenset(tenants), label="+".join(labels),
                           own_account_tenants=registry.own_account_tenants)


class NoSigningKeys(Exception):
    """No key is configured at all: webhooks cannot be authenticated."""


async def verify(raw_body: str, headers: Mapping[str, str]) -> tuple[Any, Verified]:
    """The event and what authenticated it. Raises ValueError if no
    authorized key verifies it (tampered, unknown or retired key, stale
    timestamp), NoSigningKeys if none is configured."""
    global _last_miss_reload
    registry = await _registry()
    if not registry.keys:
        registry = await _registry(force=True)
        if not registry.keys:
            raise NoSigningKeys("Telnyx webhook validation is not configured.")
    event, verified = _try(registry, raw_body, headers)
    if verified is None and time.monotonic() - _last_miss_reload > MISS_RELOAD_SECONDS:
        _last_miss_reload = time.monotonic()
        event, verified = _try(await _registry(force=True), raw_body, headers)
    if verified is None:
        raise ValueError("no authorized Telnyx signing key verifies this webhook")
    return event, verified
