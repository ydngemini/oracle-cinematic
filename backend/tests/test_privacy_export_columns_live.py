"""Every binary column of every exported table has an export decision, on the
migrated schema (audit 29.5). A sealed column added without one would make
every export refuse (unexported_column); this names it in CI first.
Skipped without ORACLE_LIVE_DB_ADMIN_DSN."""

from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_every_binary_column_in_an_exported_table_is_decided():
    import asyncpg

    import privacy_export

    async def columns():
        conn = await asyncpg.connect(DSN)
        try:
            return await conn.fetch(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type = 'bytea' AND table_name = ANY($1::text[])",
                privacy_export.exported_tables())
        finally:
            await conn.close()

    undecided = sorted(
        f"{r['table_name']}.{r['column_name']}" for r in asyncio.run(columns())
        if r["column_name"] not in privacy_export.DECRYPT.get(r["table_name"], {})
        and r["column_name"] not in privacy_export.BINARY.get(r["table_name"], {})
        and not privacy_export._omit_by_name(r["column_name"]))
    assert undecided == [], f"add to DECRYPT, BINARY or the name policy: {undecided}"
