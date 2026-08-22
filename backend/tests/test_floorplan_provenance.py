"""Dimension provenance persistence for property_floorplans (migration 0075).

Before this, `auto-dimensions` already computed a manifest attributing every
number to measured/sourced/estimated/default, but the drawer threw it away
except for a toast count — a save could not tell a later reader which
dimensions were guesses. These tests pin the three things that make the
manifest actually reach the UI and stay honest once it's there:

  1. The manifest and scaffold_sha256 round-trip through save → read.
  2. A row/revision with neither is NULL, not {} or "" — "provenance unknown"
     must never render as "measured".
  3. scaffold_sha256 is computed from canonical (sorted-key) JSON, so the same
     geometry hashes the same regardless of dict key order — the exact
     property P10(b) needs to detect an accepted-unchanged scaffold reliably.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

import pytest

import floorplan_api as fp
from tenancy import Role, TenantContext


LEAD_ID = UUID("11111111-1111-1111-1111-111111111111")
FLOORPLAN_ID = UUID("22222222-2222-2222-2222-222222222222")
TENANT_ID = "33333333-3333-3333-3333-333333333333"

CTX = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT)

MINIMAL_DOCUMENT = {
    "schema_version": 1,
    "units": "metric",
    "levels": [],
    "walls": [],
    "rooms": [],
    "openings": [],
    "provenance": {"source": "manual", "ai_generated": False},
}


# ---------------------------------------------------------------------------
# _scaffold_sha256 — the hash P10(b) filters on
# ---------------------------------------------------------------------------

def test_scaffold_hash_is_stable_across_key_order():
    """The frontend and backend may serialise dict keys differently; the hash
    must not depend on which one produced the object."""
    a = {"rooms": [], "walls": [], "schema_version": 1}
    b = {"schema_version": 1, "walls": [], "rooms": []}
    assert fp._scaffold_sha256(a) == fp._scaffold_sha256(b)


def test_scaffold_hash_matches_manual_canonical_json():
    obj = {"a": 1, "b": [1, 2, 3]}
    expected = hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert fp._scaffold_sha256(obj) == expected


def test_scaffold_hash_changes_with_content():
    a = fp._scaffold_sha256({"rooms": []})
    b = fp._scaffold_sha256({"rooms": [{"id": "r1"}]})
    assert a != b


# ---------------------------------------------------------------------------
# SaveFloorplanRequest — the wire contract
# ---------------------------------------------------------------------------

def test_scaffold_sha256_must_look_like_a_hash():
    with pytest.raises(Exception):
        fp.SaveFloorplanRequest(document=MINIMAL_DOCUMENT, scaffold_sha256="not-a-hash")


def test_dimension_manifest_and_scaffold_sha256_are_optional():
    body = fp.SaveFloorplanRequest(document=MINIMAL_DOCUMENT)
    assert body.dimension_manifest is None
    assert body.scaffold_sha256 is None


# ---------------------------------------------------------------------------
# Save persists provenance; read returns it (or NULL, never a fabricated {})
# ---------------------------------------------------------------------------

class _FakeConn:
    """Enough of asyncpg's Connection to exercise save_floorplan/get_floorplan
    without a real database. Stores exactly one floorplan row plus its
    revisions, mirroring the two-table shape the route writes to."""

    def __init__(self):
        self.head: dict | None = None
        self.revisions: list[dict] = []
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if "FROM property_floorplans" in q and "FOR UPDATE" in q:
            return {"id": FLOORPLAN_ID} if self.head else None
        if "FROM property_floorplans" in q:
            return dict(self.head) if self.head else None
        if "FROM property_floorplan_revisions" in q and "revision = $2" in q:
            floorplan_id, revision = args
            for rev in self.revisions:
                if rev["floorplan_id"] == floorplan_id and rev["revision"] == revision:
                    return dict(rev)
            return None
        if "INSERT INTO property_floorplans" in q:
            (
                tenant_id, lead_id, listing_id, schema_version, document,
                total_sqft, wall_linear_ft, room_count, level_count,
                source, ai_generated, model_version, confidence,
                manifest, scaffold_sha256, created_by,
            ) = args
            self.head = {
                "id": FLOORPLAN_ID,
                "schema_version": schema_version,
                "document": document,
                "total_sqft": total_sqft,
                "wall_linear_ft": wall_linear_ft,
                "room_count": room_count,
                "level_count": level_count,
                "source": source,
                "ai_generated": ai_generated,
                "model_version": model_version,
                "confidence": confidence,
                "dimension_manifest": manifest,
                "scaffold_sha256": scaffold_sha256,
                "updated_at": datetime.now(timezone.utc),
            }
            return {"id": FLOORPLAN_ID}
        raise AssertionError(f"unexpected fetchrow: {q[:80]}")

    async def fetchval(self, query, *args):
        if "COALESCE(MAX(revision)" in query:
            existing = [r["revision"] for r in self.revisions if r["floorplan_id"] == args[0]]
            return (max(existing) + 1) if existing else 1
        if "SELECT 1 FROM leads" in query:
            return 1
        raise AssertionError(f"unexpected fetchval: {query[:80]}")

    async def execute(self, query, *args):
        self.executed.append((query, args))
        q = " ".join(query.split())
        if "UPDATE property_floorplans" in q:
            (
                _id, document, schema_version, total_sqft, wall_linear_ft,
                room_count, level_count, source, ai_generated, model_version,
                confidence, manifest, scaffold_sha256,
            ) = args
            self.head.update({
                "document": document, "schema_version": schema_version,
                "total_sqft": total_sqft, "wall_linear_ft": wall_linear_ft,
                "room_count": room_count, "level_count": level_count,
                "source": source, "ai_generated": ai_generated,
                "model_version": model_version, "confidence": confidence,
                "dimension_manifest": manifest, "scaffold_sha256": scaffold_sha256,
            })
        elif "INSERT INTO property_floorplan_revisions" in q:
            (
                tenant_id, floorplan_id, revision, document,
                total_sqft, wall_linear_ft, rehab_items,
                manifest, scaffold_sha256, created_by,
            ) = args
            self.revisions.append({
                "floorplan_id": floorplan_id, "revision": revision,
                "document": document, "total_sqft": total_sqft,
                "wall_linear_ft": wall_linear_ft, "rehab_items": rehab_items,
                "dimension_manifest": manifest, "scaffold_sha256": scaffold_sha256,
                "created_by": created_by, "created_at": datetime.now(timezone.utc),
            })


def _patch(monkeypatch, conn):
    @asynccontextmanager
    async def fake_tenant_tx(_ctx):
        yield conn

    monkeypatch.setattr(fp, "tenant_tx", fake_tenant_tx)
    return conn


def _save(monkeypatch, conn, *, manifest=None, scaffold_sha256=None):
    body = fp.SaveFloorplanRequest(
        document=MINIMAL_DOCUMENT,
        dimension_manifest=manifest,
        scaffold_sha256=scaffold_sha256,
    )
    return asyncio.run(fp.save_floorplan(body, lead_id=LEAD_ID, listing_id=None, ctx=CTX))


def test_save_persists_manifest_and_scaffold_hash(monkeypatch):
    conn = _patch(monkeypatch, _FakeConn())
    manifest = {"total_sqft": {"provenance": "estimated", "basis": "US frame default"}}
    scaffold_hash = "a" * 64

    _save(monkeypatch, conn, manifest=manifest, scaffold_sha256=scaffold_hash)

    assert conn.head["dimension_manifest"] == json.dumps(manifest)
    assert conn.head["scaffold_sha256"] == scaffold_hash
    assert conn.revisions[0]["dimension_manifest"] == json.dumps(manifest)
    assert conn.revisions[0]["scaffold_sha256"] == scaffold_hash


def test_save_without_provenance_stores_null_not_empty_object(monkeypatch):
    """A manual draw has no machine origin. NULL must mean that — an empty
    {} would read as 'a manifest exists and every field is unaccounted for',
    which is a worse claim than admitting nothing is known."""
    conn = _patch(monkeypatch, _FakeConn())

    _save(monkeypatch, conn)

    assert conn.head["dimension_manifest"] is None
    assert conn.head["scaffold_sha256"] is None


def test_get_floorplan_returns_null_provenance_honestly(monkeypatch):
    conn = _patch(monkeypatch, _FakeConn())
    _save(monkeypatch, conn)

    result = asyncio.run(fp.get_floorplan(lead_id=LEAD_ID, listing_id=None, revision=None, ctx=CTX))

    assert result["dimension_manifest"] is None
    assert result["scaffold_sha256"] is None
    assert result["revision"] is None  # the live head is not pinned to a revision number


def test_get_floorplan_round_trips_provenance(monkeypatch):
    conn = _patch(monkeypatch, _FakeConn())
    manifest = {"wall_height_m": {"provenance": "default", "basis": "US frame construction default"}}
    scaffold_hash = "b" * 64

    _save(monkeypatch, conn, manifest=manifest, scaffold_sha256=scaffold_hash)
    result = asyncio.run(fp.get_floorplan(lead_id=LEAD_ID, listing_id=None, revision=None, ctx=CTX))

    assert result["dimension_manifest"] == manifest
    assert result["scaffold_sha256"] == scaffold_hash


def test_get_floorplan_by_revision_reads_history_not_head(monkeypatch):
    """Save twice; ?revision=1 must return the FIRST document, not the
    current one — this is the read path the revisions panel depends on."""
    conn = _patch(monkeypatch, _FakeConn())
    first_hash = "c" * 64
    _save(monkeypatch, conn, scaffold_sha256=first_hash)

    edited_document = dict(MINIMAL_DOCUMENT)
    edited_document["rooms"] = [{
        "id": "r1", "name": "Room", "type": "other", "polygon": [(0, 0), (1, 0), (1, 1)],
    }]
    body = fp.SaveFloorplanRequest(document=edited_document)
    asyncio.run(fp.save_floorplan(body, lead_id=LEAD_ID, listing_id=None, ctx=CTX))

    assert len(conn.revisions) == 2

    revision_1 = asyncio.run(
        fp.get_floorplan(lead_id=LEAD_ID, listing_id=None, revision=1, ctx=CTX)
    )
    assert revision_1["revision"] == 1
    assert revision_1["scaffold_sha256"] == first_hash
    assert revision_1["metrics"]["room_count"] == 0  # the ORIGINAL save had no rooms

    revision_2 = asyncio.run(
        fp.get_floorplan(lead_id=LEAD_ID, listing_id=None, revision=2, ctx=CTX)
    )
    assert revision_2["metrics"]["room_count"] == 1


def test_get_floorplan_unknown_revision_is_404(monkeypatch):
    conn = _patch(monkeypatch, _FakeConn())
    _save(monkeypatch, conn)

    with pytest.raises(fp.HTTPException) as excinfo:
        asyncio.run(fp.get_floorplan(lead_id=LEAD_ID, listing_id=None, revision=99, ctx=CTX))
    assert excinfo.value.status_code == 404


def test_get_floorplan_revision_with_no_plan_at_all_is_404_not_empty(monkeypatch):
    """Distinguishes 'no plan yet' (empty-state, 200) from 'this revision link
    is stale' (404) — the drawer renders those very differently."""
    conn = _patch(monkeypatch, _FakeConn())

    with pytest.raises(fp.HTTPException) as excinfo:
        asyncio.run(fp.get_floorplan(lead_id=LEAD_ID, listing_id=None, revision=1, ctx=CTX))
    assert excinfo.value.status_code == 404


def test_revision_response_derives_ai_disclosure_from_embedded_provenance(monkeypatch):
    """The revisions table has no ai_generated/model_version columns of its
    own — the disclosure must come from the document's own embedded
    provenance, or a historical AI-generated plan would read as human-made."""
    conn = _patch(monkeypatch, _FakeConn())
    ai_document = dict(MINIMAL_DOCUMENT)
    ai_document["provenance"] = {
        "source": "ai_vision", "ai_generated": True, "model_version": "cv-1.0",
    }
    body = fp.SaveFloorplanRequest(document=ai_document)
    asyncio.run(fp.save_floorplan(body, lead_id=LEAD_ID, listing_id=None, ctx=CTX))

    result = asyncio.run(
        fp.get_floorplan(lead_id=LEAD_ID, listing_id=None, revision=1, ctx=CTX)
    )
    assert result["ai_generated"] is True
    assert result["model_version"] == "cv-1.0"
    assert result["disclosure"] == fp.FLOORPLAN_AI_DISCLOSURE
