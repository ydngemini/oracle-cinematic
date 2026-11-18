"""What the webhook signing-key registry finds in the real credential table
(external audit 25.4). Today the schema refuses a stored Telnyx credential
(provider_credentials_provider_check), so no brokerage can run its own
Telnyx account and only the platform keys are trusted: this pins that fact,
so widening the CHECK is a deliberate change that brings brokerage keys (and
the per-brokerage binding) into play.

Skipped unless ORACLE_LIVE_DB_ADMIN_DSN is set (see test_privacy_lifecycle_live).
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_stored_brokerage_keys_enter_the_registry(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import telnyx_signing
    from crypto import derive_tenant_key, encrypt_pii
    from db import connection

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    key_material = f"live-public-key-{s}"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"tk-{s}", f"TK {s}")
        ciphertext = await encrypt_pii(
            admin, json.dumps({"api_key": f"api-{s}", "public_key": key_material}),
            derive_tenant_key(T, os.environ["ORACLE_ENCRYPTION_MASTER_KEY"]))
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute(
                "INSERT INTO provider_credentials (tenant_id, provider, account_label, token_ciphertext, scopes, "
                "created_by) VALUES ($1,'telnyx','default',$2,'{}'::text[],'live@test')", T, ciphertext)
        # The registry reads the table without error and finds no brokerage key.
        assert await telnyx_signing._tenant_credentials() == []
        monkeypatch.setenv("TELNYX_PUBLIC_KEY", "platform-key")
        registry = await telnyx_signing._registry(force=True)
        assert list(registry.keys) == ["platform-key"] and not registry.own_account_tenants
    finally:
        await admin.execute("DELETE FROM provider_credentials WHERE tenant_id=$1", T)
        await admin.execute("DELETE FROM tenants WHERE id=$1", T)
        await admin.close()
        await connection.close_pool()
        telnyx_signing.reset_cache()
