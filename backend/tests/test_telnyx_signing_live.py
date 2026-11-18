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


def test_the_schema_refuses_brokerage_telnyx_keys_and_the_registry_reads_the_table(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import telnyx_signing
    from crypto import derive_tenant_key, encrypt_pii
    from db import connection

    # CI's database job sets no master key; any key serves here (nothing is
    # stored, and the registry query finds no rows to decrypt).
    if not os.environ.get("ORACLE_ENCRYPTION_MASTER_KEY"):
        monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "live-test-master-key-" + secrets.token_hex(16))
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
        # The registry's own query runs against the real table (no error is
        # swallowed here: _decrypt_all raises) and finds no brokerage key.
        from db.connection import tenant_tx
        from crypto import decrypt_pii
        from tenancy import Role, TenantContext

        assert os.environ.get("ORACLE_ENCRYPTION_MASTER_KEY")
        platform = TenantContext(agent_id="live", role=Role.PLATFORM_ADMIN,
                                 tenant_id="00000000-0000-0000-0000-000000000000")
        assert await telnyx_signing._decrypt_all(
            tenant_tx, platform, os.environ["ORACLE_ENCRYPTION_MASTER_KEY"], decrypt_pii, derive_tenant_key) == []
        # (What the registry builds from that is unit-tested in test_telnyx_signing.)
    finally:
        await admin.execute("DELETE FROM provider_credentials WHERE tenant_id=$1", T)
        await admin.execute("DELETE FROM tenants WHERE id=$1", T)
        await admin.close()
        await connection.close_pool()
        telnyx_signing.reset_cache()
