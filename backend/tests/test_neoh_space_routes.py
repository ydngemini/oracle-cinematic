"""Neoh Space routes: cost guard, idempotency, customer-safe status, retry,
deletion, and signed streaming delivery.

The routes are called directly with a scripted connection, so each test
states exactly what the database holds and asserts what the route does.
"""
from __future__ import annotations

import asyncio
import types
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

import space_assets
import tour_api
from tenancy import Role, TenantContext

TENANT = "00000000-0000-0000-0000-0000000000aa"
LEAD = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _ctx(role=Role.AGENT):
    return TenantContext(agent_id="agent@x.test", tenant_id=TENANT, role=role)


def _job_row(**over):
    row = {
        "id": uuid.UUID("22222222-2222-2222-2222-222222222222"), "status": "queued",
        "stage": "queued", "provider": "runpod_pod", "provider_job_id": "pod-1",
        "progress": 0, "media_id": None, "error": "RunPod POST /pods failed (500): gpu",
        "diagnostics": {}, "quality_gate": None, "failure_category": None,
        "attempts": 0, "pipeline_version": "v", "cost_estimate_usd": 0.5,
        "gpu_seconds": 900.0, "output_bytes": None, "raw_output_key": None,
        "retry_of": None, "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc), "lead_id": LEAD, "listing_id": None,
    }
    row.update(over)
    return row


class _Conn:
    """Answers by SQL substring; records every statement."""

    def __init__(self, script):
        self.script = script
        self.sql = []

    def _answer(self, sql, kind):
        self.sql.append(sql)
        for needle, value in self.script:
            if needle in sql:
                return value(sql) if callable(value) else value
        return None if kind != "fetch" else []

    async def fetchval(self, sql, *a):
        return self._answer(sql, "val")

    async def fetchrow(self, sql, *a):
        return self._answer(sql, "row")

    async def fetch(self, sql, *a):
        return self._answer(sql, "fetch")

    async def execute(self, sql, *a):
        self.sql.append(sql)
        return "OK"


@pytest.fixture
def wire(monkeypatch):
    def _install(script):
        conn = _Conn(script)

        class _Tx:
            def __init__(self, ctx):
                pass

            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(tour_api, "tenant_tx", _Tx)
        monkeypatch.setattr(tour_api, "get_provider",
                            lambda: types.SimpleNamespace(available=lambda: (True, "")))
        monkeypatch.setattr(tour_api, "enqueue", lambda job: None)

        async def _no_audit(*a, **k):
            return None

        monkeypatch.setattr(tour_api, "audit_now", _no_audit)
        return conn
    return _install


def _enqueue(**kw):
    return asyncio.run(tour_api.enqueue_reconstruction(
        lead_id=kw.pop("lead_id", LEAD), listing_id=None,
        confirm_rebuild=kw.pop("confirm_rebuild", False),
        idempotency_key=kw.pop("idempotency_key", None), ctx=kw.pop("ctx", _ctx())))


def test_a_second_tap_returns_the_same_build_and_rents_nothing(wire):
    conn = wire([("SELECT 1 FROM leads", 1),
                 ("status IN ('queued', 'running')", _job_row(status="running", stage="reconstructing"))])
    out = _enqueue()
    assert out["deduplicated"] is True
    assert not any("INSERT INTO reconstruction_jobs" in s for s in conn.sql)


def test_the_same_idempotency_key_returns_the_same_job(wire):
    conn = wire([("SELECT 1 FROM leads", 1),
                 ("WHERE idempotency_key = $1", _job_row(status="succeeded", stage="ready"))])
    out = _enqueue(idempotency_key="tap-1")
    assert out["deduplicated"] is True and out["state"] == "ready"
    assert not any("INSERT INTO reconstruction_jobs" in s for s in conn.sql)


def test_replacing_a_published_space_needs_explicit_confirmation(wire):
    wire([("SELECT 1 FROM leads", 1), ("FROM property_media", 1)])
    with pytest.raises(HTTPException) as err:
        _enqueue()
    assert err.value.status_code == 409
    assert "stays visible" in err.value.detail


def test_the_daily_rebuild_limit_is_enforced(wire):
    wire([("SELECT 1 FROM leads", 1), ("FROM property_media", 1),
          ("interval '24 hours'", tour_api.RERUN_LIMIT_PER_DAY)])
    with pytest.raises(HTTPException) as err:
        _enqueue(confirm_rebuild=True)
    assert err.value.status_code == 429


def test_queues_are_bounded_per_brokerage_and_platform_wide(wire, monkeypatch):
    wire([("SELECT 1 FROM leads", 1), ("FROM property_media", 0),
          ("interval '24 hours'", 0), ("WHERE status = 'queued'", tour_api.RECON_QUEUE_MAX)])
    with pytest.raises(HTTPException) as err:
        _enqueue()
    assert err.value.status_code == 503
    monkeypatch.setattr(tour_api, "RECON_QUEUE_MAX", 10_000)
    wire([("SELECT 1 FROM leads", 1), ("FROM property_media", 0),
          ("interval '24 hours'", 0), ("WHERE status = 'queued'", tour_api.GLOBAL_QUEUE_MAX)])
    with pytest.raises(HTTPException) as err:
        _enqueue()
    assert err.value.status_code == 503


def test_a_fresh_build_is_inserted_queued_and_reported_by_stage(wire):
    conn = wire([("SELECT 1 FROM leads", 1), ("FROM property_media", 0),
                 ("interval '24 hours'", 0), ("WHERE status = 'queued'", 0),
                 ("INSERT INTO reconstruction_jobs", _job_row())])
    out = _enqueue()
    assert out["deduplicated"] is False
    assert out["state"] == "queued" and out["label"] == "Waiting to start"
    insert = next(s for s in conn.sql if "INSERT INTO reconstruction_jobs" in s)
    assert "'queued', 'queued'" in insert


def test_an_unavailable_provider_is_explained_in_product_language(wire, monkeypatch):
    wire([])
    monkeypatch.setattr(tour_api, "get_provider", lambda: types.SimpleNamespace(
        available=lambda: (False, "RunPod balance is $0.00, below the $1.00 minimum")))
    with pytest.raises(HTTPException) as err:
        _enqueue()
    assert err.value.status_code == 503
    assert "RunPod" not in err.value.detail and "balance" not in err.value.detail


def test_customers_never_see_provider_names_raw_errors_or_percentages(wire):
    wire([("FROM reconstruction_jobs WHERE id = $1",
           _job_row(status="failed", stage="failed", failure_category="provider"))])
    out = asyncio.run(tour_api.reconstruction_job_status(
        job_id=uuid.uuid4(), ctx=_ctx()))
    text = repr(out).lower()
    for word in ("runpod", "pod-1", "gpu", "/pods", "progress"):
        assert word not in text, word
    assert out["can_retry"] is True


def test_operators_get_the_diagnostics(wire):
    wire([("FROM reconstruction_jobs WHERE id = $1", _job_row(status="failed", stage="failed"))])
    out = asyncio.run(tour_api.reconstruction_job_diagnostics(
        job_id=uuid.uuid4(), ctx=_ctx(Role.BROKER_OWNER)))
    assert out["provider"] == "runpod_pod" and out["cost_estimate_usd"] == 0.5
    with pytest.raises(HTTPException) as err:
        asyncio.run(tour_api.reconstruction_job_diagnostics(job_id=uuid.uuid4(), ctx=_ctx()))
    assert err.value.status_code == 403


def test_retry_reconverts_without_a_gpu_when_raw_output_was_kept(wire):
    conn = wire([
        ("SELECT id, status", _job_row(status="needs_attention", stage="needs_attention",
                                       failure_category="conversion",
                                       raw_output_key="splats/t/raw/j.ply")),
        ("INSERT INTO reconstruction_jobs", _job_row()),
    ])
    out = asyncio.run(tour_api.retry_reconstruction(job_id=uuid.uuid4(), ctx=_ctx()))
    assert out["retry_kind"] == "conversion"
    insert = next(s for s in conn.sql if "INSERT INTO reconstruction_jobs" in s)
    assert "resume_from" in insert


def test_retry_refuses_when_there_is_nothing_to_resume(wire):
    wire([("SELECT id, status", _job_row(status="failed", stage="failed"))])
    with pytest.raises(HTTPException) as err:
        asyncio.run(tour_api.retry_reconstruction(job_id=uuid.uuid4(), ctx=_ctx()))
    assert err.value.status_code == 409


def test_deleting_a_space_removes_every_derived_object_and_keeps_photos(wire, monkeypatch):
    deleted = []
    monkeypatch.setattr(space_assets, "delete_objects",
                        lambda keys: (deleted.extend(keys), {"deleted": len(keys), "failed": 0})[1])
    conn = wire([
        ("SELECT id, s3_key FROM property_media", [{"id": uuid.uuid4(), "s3_key": "splats/t/a.sog"}]),
        ("SELECT raw_output_key", [{"raw_output_key": "splats/t/raw/j.ply"}]),
    ])
    with pytest.raises(HTTPException):
        asyncio.run(tour_api.delete_space(lead_id=LEAD, listing_id=None, confirm=False, ctx=_ctx()))
    out = asyncio.run(tour_api.delete_space(lead_id=LEAD, listing_id=None, confirm=True, ctx=_ctx()))
    assert out["photos_and_video"] == "kept"
    for key in ("splats/t/a.sog", "splats/t/a.sog.cameras.json", "splats/t/a.sog.points.ply",
                "splats/t/a.sog.scene.json", "splats/t/a.json", "splats/t/raw/j.ply"):
        assert key in deleted, key
    delete_sql = next(s for s in conn.sql if s.strip().startswith("DELETE FROM property_media"))
    assert "kind = 'splat'" in delete_sql  # photos and video rows are untouched


def test_a_failed_object_delete_keeps_the_rows(wire, monkeypatch):
    monkeypatch.setattr(space_assets, "delete_objects", lambda keys: {"deleted": 0, "failed": 1})
    conn = wire([("SELECT id, s3_key FROM property_media", [{"id": uuid.uuid4(), "s3_key": "k.sog"}])])
    with pytest.raises(HTTPException) as err:
        asyncio.run(tour_api.delete_space(lead_id=LEAD, listing_id=None, confirm=True, ctx=_ctx()))
    assert err.value.status_code == 503
    assert not any(s.strip().startswith("DELETE FROM property_media") for s in conn.sql)


# ── signed delivery ───────────────────────────────────────────────────────────
def test_signed_urls_are_scoped_short_lived_and_unforgeable(monkeypatch):
    monkeypatch.setenv("SPACE_ASSET_SIGNING_KEY", "test-key")
    exp, sig = space_assets.sign("m1", TENANT, now=1000)
    assert space_assets.verify("m1", TENANT, exp, sig, now=1001)
    assert not space_assets.verify("m2", TENANT, exp, sig, now=1001)        # other asset
    assert not space_assets.verify("m1", "other-tenant", exp, sig, now=1001)  # other tenant
    assert not space_assets.verify("m1", TENANT, exp, sig, now=exp + 1)     # expired
    assert not space_assets.verify("m1", TENANT, exp + 10_000, sig, now=1001)  # stretched
    assert not space_assets.verify("m1", TENANT, exp, sig[:-2] + "AA", now=1001)
    assert space_assets.signed_path("m1", TENANT).startswith("/api/space/assets/m1?")


def test_range_parsing():
    assert space_assets.parse_range(None, 100) is None
    assert space_assets.parse_range("bytes=0-9", 100) == (0, 9)
    assert space_assets.parse_range("bytes=90-", 100) == (90, 99)
    assert space_assets.parse_range("bytes=-10", 100) == (90, 99)
    assert space_assets.parse_range("bytes=0-1,5-6", 100) is None
    with pytest.raises(ValueError):
        space_assets.parse_range("bytes=200-300", 100)


def test_the_signed_route_streams_ranges_and_hides_everything_else(wire, monkeypatch):
    monkeypatch.setenv("SPACE_ASSET_SIGNING_KEY", "test-key")
    data = bytes(range(256)) * 4
    monkeypatch.setattr(space_assets, "read_range", lambda key, rng: (
        (data, len(data)) if rng is None else (data[rng[0]:rng[1] + 1], len(data))))
    wire([("SELECT s3_key FROM property_media", "splats/t/m.sog")])
    media = uuid.uuid4()
    exp, sig = space_assets.sign(str(media), TENANT)

    def _req(range_header=None):
        return types.SimpleNamespace(headers={"range": range_header} if range_header else {})

    whole = asyncio.run(tour_api.signed_space_asset(media, _req(), t=TENANT, exp=exp, sig=sig))
    assert whole.status_code == 200 and whole.body == data
    assert whole.headers["cache-control"].startswith("private")
    part = asyncio.run(tour_api.signed_space_asset(media, _req("bytes=10-19"), t=TENANT, exp=exp, sig=sig))
    assert part.status_code == 206 and part.body == data[10:20]
    assert part.headers["content-range"] == f"bytes 10-19/{len(data)}"
    bad = asyncio.run(tour_api.signed_space_asset(media, _req("bytes=9999-"), t=TENANT, exp=exp, sig=sig))
    assert bad.status_code == 416
    with pytest.raises(HTTPException) as err:
        asyncio.run(tour_api.signed_space_asset(uuid.uuid4(), _req(), t=TENANT, exp=exp, sig=sig))
    assert err.value.status_code == 404


def test_superseded_spaces_are_never_offered_by_the_resolver():
    import inspect

    src = inspect.getsource(tour_api.fetch_tour_rows)
    assert "superseded_at IS NOT NULL" in src


def test_the_newest_current_capture_is_the_one_opened():
    rows = [
        {"id": "a", "kind": "splat", "url": "/api/media/a", "sort_order": 1, "s3_key": "a.sog",
         "provenance": "captured"},
        {"id": "b", "kind": "splat", "url": "/api/media/b", "sort_order": 2, "s3_key": "b.sog",
         "provenance": "captured"},
    ]
    tour = tour_api.build_tour(rows, [], None)
    assert tour["splat_url"] == "/api/media/b"
