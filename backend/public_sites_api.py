"""Published hyperlocal sites, served to the public (prompt 14 T14.05).

Publishing returned ``platform_path: /sites/{slug}``, but nothing served that
path, so a published site was unreachable. Site slugs are unique only within a
brokerage (``UNIQUE (tenant_id, slug)``), so the public address carries the
brokerage's globally unique slug too: ``/sites/{brokerage}/{site}``.

Only a site whose status is ``published`` and that has a published revision is
served, and only its public content (name, template, theme, content). Draft,
preview and archived sites, and anything internal to a revision (IDX feed list,
source manifest, authorship), never leave. Responses are ``no-cache``: a client
may keep a copy but revalidates every view, and every view reads the database,
so a rollback, unpublish or archive takes effect on the next request (F14.07).
The ETag names the live revision, its content hash and when it went live, so
re-pointing between two revisions with identical content (a rollback, T14.04)
still changes it. A 404 is ``no-store``: a missing page is never kept.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Request, Response, status

from db.connection import tenant_tx
from tenancy import Role, TenantContext

router = APIRouter(prefix="/api/public/sites", tags=["Public sites"])

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,126}[a-z0-9]$")
_PLATFORM_TENANT = "00000000-0000-0000-0000-000000000000"


def public_site_path(tenant_slug: str, site_slug: str) -> str:
    return f"/sites/{tenant_slug}/{site_slug}"


def _reader() -> TenantContext:
    # Read-only and narrowed by the query below to published rows.
    return TenantContext(
        agent_id="public-site-reader",
        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", _PLATFORM_TENANT),
        role=Role.PLATFORM_ADMIN,
    )


def _object(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def page_etag(row: Mapping[str, Any]) -> str:
    published_at = row["published_at"].isoformat() if row["published_at"] else ""
    digest = hashlib.sha256(
        f'{row["revision_id"]}\n{row["content_sha256"]}\n{published_at}'.encode("utf-8")
    ).hexdigest()
    return f'"{digest}"'


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "site not found", headers={"Cache-Control": "no-store"})


@router.get("/{tenant_slug}/{site_slug}")
async def public_site(tenant_slug: str, site_slug: str, request: Request, response: Response) -> Any:
    if not _SLUG_RE.fullmatch(tenant_slug or "") or not _SLUG_RE.fullmatch(site_slug or ""):
        raise _not_found()
    async with tenant_tx(_reader()) as conn:
        row = await conn.fetchrow(
            """SELECT s.name, s.template_key, s.published_at,
                      r.id AS revision_id, r.revision, r.brand_theme, r.content, r.content_sha256
                 FROM hyperlocal_sites s
                 JOIN tenants t ON t.id = s.tenant_id
                 JOIN hyperlocal_site_revisions r
                   ON r.tenant_id = s.tenant_id AND r.site_id = s.id AND r.id = s.published_revision_id
                WHERE t.slug = $1 AND s.slug = $2 AND s.status = 'published'""",
            tenant_slug, site_slug,
        )
    if row is None:
        raise _not_found()
    etag = page_etag(row)
    headers = {"Cache-Control": "no-cache", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    response.headers.update(headers)
    return {
        "name": row["name"],
        "template_key": row["template_key"],
        "revision": row["revision"],
        "published_at": row["published_at"].isoformat() if row["published_at"] else None,
        "brand_theme": _object(row["brand_theme"]),
        "content": _object(row["content"]),
    }
