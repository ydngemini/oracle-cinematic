"""Published vs draft/archived sites through the public route, against real
PostgreSQL with FORCE RLS (skipped without ORACLE_LIVE_DB_ADMIN_DSN)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_only_published_sites_are_public():
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg
    from fastapi import HTTPException
    from starlette.requests import Request
    from starlette.responses import Response

    import public_sites_api as ps
    from db import connection

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant = str(uuid.uuid4())
    tslug = f"pub-{tenant[:8]}"
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Public site test')", tenant, tslug)
        sites = {}
        for slug, status in (("live-site", "published"), ("draft-site", "draft"), ("old-site", "archived")):
            site_id, rev_id = uuid.uuid4(), uuid.uuid4()
            sites[slug] = site_id
            # The published-revision FK is deferred, so link the revision after both rows exist.
            await admin.execute(
                """INSERT INTO hyperlocal_sites (id, tenant_id, owner_agent_id, name, slug, template_key, status)
                   VALUES ($1,$2,'agent-a',$3,$4,'editorial',$5)""",
                site_id, tenant, f"{slug} name", slug, status)
            await admin.execute(
                """INSERT INTO hyperlocal_site_revisions (id, tenant_id, site_id, revision, brand_theme, content,
                                                          content_sha256, created_by)
                   VALUES ($1,$2,$3,1,'{}'::jsonb,'{"headline":"Hello"}'::jsonb,$4,'agent-a')""",
                rev_id, tenant, site_id, hashlib.sha256(slug.encode()).hexdigest())
            if status != "draft":
                await admin.execute("UPDATE hyperlocal_sites SET published_revision_id=$2 WHERE id=$1", site_id, rev_id)
        body = await ps.public_site(tslug, "live-site", req, Response())
        assert body["content"] == {"headline": "Hello"} and body["name"] == "live-site name"
        for slug in ("draft-site", "old-site", "missing-site"):
            with pytest.raises(HTTPException) as exc:
                await ps.public_site(tslug, slug, req, Response())
            assert exc.value.status_code == 404, slug
        # Archiving takes effect on the very next request.
        await admin.execute("UPDATE hyperlocal_sites SET status='archived' WHERE id=$1", sites["live-site"])
        with pytest.raises(HTTPException):
            await ps.public_site(tslug, "live-site", req, Response())
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("hyperlocal_site_revisions", "hyperlocal_sites"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id=$1", uuid.UUID(tenant))
            await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(tenant))
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
