"""scripts/access_review.py lists privileged access, read-only, against real
PostgreSQL (L17-13; skipped without ORACLE_LIVE_DB_ADMIN_DSN, and when the
scripts/ folder is not next to backend/)."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from pathlib import Path

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "access_review.py"
pytestmark = [
    pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)"),
    pytest.mark.skipif(not SCRIPT.exists(), reason="scripts/access_review.py not mounted"),
]


def _module():
    spec = importlib.util.spec_from_file_location("access_review", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Recording:
    """Wraps a connection and records every statement the script sends."""

    def __init__(self, conn):
        self._conn, self.sql = conn, []

    def transaction(self, **kw):
        self.readonly = kw.get("readonly")
        return self._conn.transaction(**kw)

    async def execute(self, q, *a):
        self.sql.append(q)
        return await self._conn.execute(q, *a)

    async def fetch(self, q, *a):
        self.sql.append(q)
        return await self._conn.fetch(q, *a)

    async def fetchrow(self, q, *a):
        self.sql.append(q)
        return await self._conn.fetchrow(q, *a)


def test_access_review_lists_privileged_access_read_only():
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    from db import connection

    review = _module()
    admin = await asyncpg.connect(DSN)
    tenant = str(uuid.uuid4())
    owner = f"review-owner-{tenant[:8]}@example.test"
    await connection.init_pool(min_size=1, max_size=2)
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1, $2, 'Access review test')",
                            tenant, f"review-{tenant[:8]}")
        await admin.execute(
            "INSERT INTO users (tenant_id, agent_id, role, email, password_hash, is_active) VALUES "
            "($1, $2, 'broker_owner', $2, 'scrypt$x$y', true), ($1, $3, 'broker_owner', $3, 'scrypt$x$y', false), "
            "($1, $4, 'agent', $4, 'scrypt$x$y', true)",
            tenant, owner, "suspended-" + owner, "agent-" + owner)
        async with connection.get_platform_pool().acquire() as conn:
            rec = _Recording(conn)
            report = await review.collect(rec, env={"ORACLE_ADMIN_ID": "ops@example.test",
                                                    "ORACLE_ADMIN_TOTP_SECRET": "JBSWY3DPEHPK3PXP"})
        assert rec.readonly is True
        assert not [q for q in rec.sql if any(w in q.upper().split() for w in ("INSERT", "UPDATE", "DELETE"))]

        roles = {r["rolname"]: r for r in report["db_roles"]}
        assert roles["postgres"]["rolsuper"] is True
        assert roles["oracle_app"]["rolsuper"] is False and roles["oracle_app"]["rolbypassrls"] is False
        assert "platform_admin_role" in roles["oracle_platform_login"]["member_of"]
        assert not any(name.startswith("pg_") for name in roles)

        mine = next(o for o in report["broker_owners"] if o["tenant_id"] == tenant)
        assert (mine["active_owners"], mine["suspended_owners"]) == (1, 1)
        assert mine["active_ids"] == [owner] and mine["suspended_ids"] == ["suspended-" + owner]
        expected = await admin.fetchval("SELECT count(*) FROM users WHERE role = 'broker_owner' AND is_active")
        assert sum(o["active_owners"] for o in report["broker_owners"]) == expected

        admins = report["platform_admins"]
        assert admins[0]["source"].startswith("environment")
        assert len(admins) - 1 == await admin.fetchval("SELECT count(*) FROM users WHERE role = 'platform_admin'")
        assert report["operator_mfa"]["factor_in_this_environment"] == "totp"

        markdown, csv_text = review.render_markdown(report), review.render_csv(report)
        assert "## 1. Database roles" in markdown and owner in markdown
        assert "JBSWY3DPEHPK3PXP" not in markdown + csv_text
        assert csv_text.splitlines()[0] == "section,name,attributes"
    finally:
        await admin.execute("DELETE FROM users WHERE tenant_id = $1", tenant)
        await admin.execute("DELETE FROM tenants WHERE id = $1", tenant)
        await connection.close_pool()
        await admin.close()
