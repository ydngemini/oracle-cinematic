"""Every reader of a 3D space's quality reads its certification (T12.10, AC12.06).

The consumer audit (T12.10) found each place a label was derived from a space
existing or from a non-blocked verdict: the tour badge, label and note, the
asset's `walkable`, the capture panel's job view, the operator view, the
homeowner's portal, and the AI's tour answers. Each is pinned here to the
certification state (space_certification), and AC12.06 is asserted across all
of them at once: missing or invalid metrics, disconnected rooms, unregistered
captures and stale evidence never produce "verified", "professional" or the
tier-3 "Full 3D Walkthrough".

No database: the reads are driven through fake connections. The SQL that
decides freshness runs against real PostgreSQL in
test_space_certification_live.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import re
import uuid
from uuid import UUID

import pytest

import space_certification as sc
import space_status
import tour_api
from tenancy import Role, TenantContext

TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT)
OWNER = TenantContext(agent_id="owner@tenant.test", tenant_id=TENANT_ID, role=Role.BROKER_OWNER)
LEAD_ID = "22222222-2222-4222-8222-222222222222"
SPLAT_ID = "33333333-3333-4333-8333-333333333333"

GOOD = {"held_out_psnr": 28.8, "held_out_ssim": 0.89, "held_out_lpips": 0.086,
        "registration_ratio": 0.95, "mean_reprojection_error_px": 0.5, "input_images": 150}


def _job_evidence(*, limitations=(), metrics=None, fresh=True, status="succeeded", unread=0):
    return {"evidence_job_id": "44444444-4444-4444-8444-444444444444",
            "evidence_job_status": status, "quality_limitations": list(limitations),
            "quality_metrics": metrics, "evidence_fresh": fresh, "evidence_unread": unread}


#: Room connectivity proven and compared with the home's rooms (T12.06's
#: producer will write this; nothing does yet).
CONNECTED = {"rooms": 3, "components": 1, "edges": 2, "verified_edges": 2, "expected_rooms": 3}


#: What AC12.06 names, each as a stored record would carry it.
AC12_06 = {
    "no build behind it (phone scan)": {},
    "metrics missing": _job_evidence(metrics=None),
    "metrics missing, no caveat either": _job_evidence(limitations=[], metrics={}),
    "invalid metric": _job_evidence(metrics={**GOOD, "held_out_ssim": 7.0}),
    "invalid metric flagged by the gate": _job_evidence(metrics={**GOOD, "invalid": ["held_out_psnr"]}),
    "unregistered capture": _job_evidence(metrics={k: v for k, v in GOOD.items()
                                                   if k.startswith("held_out")}),
    "registration failed": _job_evidence(metrics={**GOOD, "registration_ratio": 0.1}),
    "partly registered": _job_evidence(limitations=["partial_registration"],
                                       metrics={**GOOD, "registration_ratio": 0.45}),
    "disconnected rooms": _job_evidence(metrics={**GOOD, "room_graph": {
        "rooms": 2, "components": 2, "edges": 1, "verified_edges": 0}}),
    "one room of a larger home": _job_evidence(metrics={**GOOD, "room_graph": {
        "rooms": 1, "components": 1, "edges": 0, "verified_edges": 0, "expected_rooms": 4}}),
    "an impossible room graph": _job_evidence(metrics={**GOOD, "room_graph": {
        "rooms": 3, "components": 1, "edges": 0, "verified_edges": 0, "expected_rooms": 3}}),
    "a boolean measurement": _job_evidence(metrics={**GOOD, "registration_ratio": True,
                                                    "room_graph": CONNECTED}),
    "capture the build could not read": _job_evidence(metrics={**GOOD, "room_graph": CONNECTED},
                                                      unread=1),
    "stale evidence": _job_evidence(metrics=GOOD, fresh=False),
    "freshness unknown": _job_evidence(metrics=GOOD, fresh=None),
    "the best evidence stored today": _job_evidence(metrics=GOOD, fresh=True),
}


def _splat(**evidence):
    return {"id": SPLAT_ID, "kind": "splat", "url": f"/api/media/{SPLAT_ID}",
            "sort_order": 0, "provenance": "captured", **evidence}


def _tour(row):
    return tour_api.build_tour([row], [], None, lead_id=LEAD_ID)


def _texts(tour: dict) -> list[str]:
    out = [tour["badge"], tour["honest_note"], tour["splat_label"] or "", *tour["splat_caveats"]]
    cert = tour.get("splat_certification") or {}
    out += [cert.get("label") or "", *(cert.get("reasons") or [])]
    for asset in tour["assets"]:
        if asset["kind"] == "exterior":
            # Google's licensed photoreal tiles of the street, not a capture.
            continue
        out += [asset["label"], asset.get("note") or "", *(asset.get("caveats") or [])]
        a_cert = asset.get("certification") or {}
        out += [a_cert.get("label") or "", *(a_cert.get("reasons") or [])]
    return out


def _claims_quality(text: str) -> bool:
    lowered = text.lower().replace("not verified", "").replace("has not been verified", "")
    return any(word in lowered for word in ("verified", "professional", "photoreal")) or "Full" in text


# ── AC12.06 across the tour ──────────────────────────────────────────────────
@pytest.mark.parametrize("case", list(AC12_06))
def test_no_record_without_evidence_reads_as_verified_professional_or_full(case):
    tour = _tour(_splat(**AC12_06[case]))
    assert tour["best_tier"] == 3
    assert tour["splat_quality_state"] != "verified"
    assert tour["whole_home_verified"] is False
    assert tour["badge"] != "Full 3D Walkthrough"
    claims = [t for t in _texts(tour) if _claims_quality(t)]
    assert claims == [], (case, claims)
    splat = next(a for a in tour["assets"] if a["kind"] == "splat")
    assert splat["quality_state"] == splat["certification"]["state"] == tour["splat_quality_state"]
    assert splat["whole_home_verified"] is False


@pytest.mark.parametrize("evidence, state, badge, label", [
    ({}, "pending", "3D Walkthrough · quality not verified", "3D walkthrough · quality not verified"),
    (_job_evidence(metrics=GOOD, fresh=False), "pending",
     "3D Walkthrough · quality not verified", "3D walkthrough · quality not verified"),
    (_job_evidence(metrics=GOOD), "incomplete",
     "3D Walkthrough · quality not verified", "3D walkthrough · quality not verified"),
    (_job_evidence(limitations=["partial_registration"], metrics={**GOOD, "registration_ratio": 0.45}),
     "incomplete", "Partial 3D Walkthrough", "Partial 3D walkthrough"),
    (_job_evidence(limitations=["quality_low"], metrics={**GOOD, "held_out_psnr": 20.5}),
     "degraded", "3D Walkthrough · reduced quality", "3D walkthrough · reduced quality"),
    (_job_evidence(metrics={**GOOD, "held_out_psnr": 13.0}),
     "rejected", "3D Walkthrough · failed quality checks", "3D walkthrough · failed quality checks"),
])
def test_the_badge_label_and_note_follow_the_certification(evidence, state, badge, label):
    tour = _tour(_splat(**evidence))
    assert (tour["splat_quality_state"], tour["badge"], tour["splat_label"]) == (state, badge, label)
    assert tour["splat_certification"]["state"] == state
    assert "not been verified" in tour["honest_note"]
    splat = next(a for a in tour["assets"] if a["kind"] == "splat")
    assert splat["label"] == label and splat["note"] == tour["honest_note"]


def test_stale_evidence_tells_the_viewer_why():
    tour = _tour(_splat(**_job_evidence(metrics=GOOD, fresh=False)))
    assert any("added after this space was built" in c for c in tour["splat_caveats"])


def test_caveats_are_not_said_twice():
    tour = _tour(_splat(**_job_evidence(limitations=["partial_registration"],
                                        metrics={**GOOD, "registration_ratio": 0.45})))
    assert len(tour["splat_caveats"]) == len(set(tour["splat_caveats"]))
    assert any("couldn't be placed" in c for c in tour["splat_caveats"])


def test_only_a_verified_space_is_called_full_and_photoreal(monkeypatch):
    """The label table is bound to the state: when (and only when) the policy
    says verified, the tier-3 badge and the photoreal note are allowed."""
    verified = sc.certify(
        job_found=True, job_status="succeeded", fresh=True, limitations=[],
        quality={**GOOD, "room_graph": {"rooms": 2, "components": 1, "edges": 1, "verified_edges": 1,
                                        "expected_rooms": 2}},
        media_id=SPLAT_ID,
        device_runs=[{"platform": "ios_safari", "real_device": True, "steady_fps": 40.0,
                      "media_id": SPLAT_ID},
                     {"platform": "android_chrome", "real_device": True, "steady_fps": 33.0,
                      "media_id": SPLAT_ID}])
    assert verified.state == "verified"
    monkeypatch.setattr(sc, "from_tour_row", lambda _row: verified)
    tour = _tour(_splat(**_job_evidence(metrics=GOOD)))
    assert tour["badge"] == "Full 3D Walkthrough"
    assert tour["whole_home_verified"] is True
    assert "photoreal" in tour["honest_note"]
    assert tour["splat_certification"]["state"] == "verified"


def test_walkable_is_navigability_not_quality():
    tour = _tour(_splat())
    splat = next(a for a in tour["assets"] if a["kind"] == "splat")
    assert splat["walkable"] is True and tour["walkable_interior"] is True
    assert splat["whole_home_verified"] is False


def test_a_demo_space_carries_no_certification():
    row = {**_splat(**_job_evidence(metrics=GOOD)), "provenance": "synthetic"}
    tour = _tour(row)
    splat = next(a for a in tour["assets"] if a["kind"] == "splat")
    assert splat["certification"] is None and splat["whole_home_verified"] is False
    assert tour["splat_certification"] is None


# ── the evidence read ────────────────────────────────────────────────────────
class _CaptureConn:
    def __init__(self, media=None, scenes=None, plan=None, rows=None):
        self.media, self.scenes, self.plan = media or [], scenes or [], plan
        self.queries: list[str] = []
        self.rows = rows or {}

    async def fetch(self, query, *_a):
        self.queries.append(query)
        return self.scenes if "property_pano_scenes" in query else self.media

    async def fetchrow(self, query, *_a):
        self.queries.append(query)
        for needle, row in self.rows.items():
            if needle in query:
                return row
        return self.plan

    async def fetchval(self, query, *_a):
        self.queries.append(query)
        return 0


def _flat(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)


def test_the_tour_reads_evidence_and_freshness_in_one_query():
    import reconstruction_worker

    conn = _CaptureConn()
    asyncio.run(tour_api.fetch_tour_rows(conn, UUID(LEAD_ID), None))
    media_sql = _flat(next(q for q in conn.queries if "reconstruction_jobs" in q))
    assert "LEFT JOIN LATERAL" in media_sql
    for column in ("quality_limitations", "quality_metrics", "evidence_job_id",
                   "evidence_job_status", "evidence_fresh", "evidence_unread"):
        assert f"AS {column}" in media_sql, column
    assert "'input_media_ids'" in media_sql and "'skipped_media_ids'" in media_sql
    # The same definition of "capture input" as the build that gathered it.
    assert _flat(reconstruction_worker.capture_input_sql("src")) in media_sql
    # Tenancy is RLS's job (CONTRACT C3): no duplicated tenant predicate.
    assert "tenant_id" not in media_sql


def test_generated_media_is_never_capture_input():
    """An AI listing reel is stored as kind='video' with an object key. It is
    not a capture: the build must not reconstruct it as the house, and the
    freshness check must not count it as capture the build never read."""
    import reconstruction_worker

    sql = _flat(reconstruction_worker.capture_input_sql("src"))
    assert "COALESCE(src.provenance, 'captured') = 'captured'" in sql


def test_unreviewed_client_uploads_are_never_capture_input():
    """A client-link upload is held 'pending' until an agent reviews it, and
    may be 'rejected' (migration 0066: it must not appear on anything public).
    It may not even be this house: the build must not reconstruct it as the
    home, and the freshness check must not call a published space stale
    because of it."""
    import reconstruction_worker

    sql = _flat(reconstruction_worker.capture_input_sql("src"))
    assert "src.review_status = 'approved'" in sql


def _summary(published, ctx):
    conn = _CaptureConn(rows={"FROM property_media pm": published})

    @contextlib.asynccontextmanager
    async def _tx(_ctx):
        yield conn

    mp = pytest.MonkeyPatch()
    mp.setattr(tour_api, "tenant_tx", _tx)
    try:
        return asyncio.run(tour_api.space_summary(lead_id=UUID(LEAD_ID), listing_id=None, ctx=ctx))
    finally:
        mp.undo()


def test_the_space_summary_gives_check_detail_only_to_operators():
    published = {"id": UUID(SPLAT_ID), "s3_key": None, "provenance": "captured",
                 "created_at": dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc),
                 **_job_evidence(metrics=GOOD, fresh=True)}
    assert "checks" not in _summary(published, CTX)["published"]["certification"]
    cert = _summary(published, OWNER)["published"]["certification"]
    assert set(cert["checks"]) == set(sc.REQUIRED_CHECKS)
    assert cert["fresh"] is True


def test_the_space_summary_reports_the_published_certification():
    published = {"id": UUID(SPLAT_ID), "s3_key": None, "provenance": "captured",
                 "created_at": dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc),
                 **_job_evidence(metrics=GOOD, fresh=False)}
    conn = _CaptureConn(rows={"FROM property_media pm": published})

    @contextlib.asynccontextmanager
    async def _tx(_ctx):
        yield conn

    mp = pytest.MonkeyPatch()
    mp.setattr(tour_api, "tenant_tx", _tx)
    try:
        out = asyncio.run(tour_api.space_summary(lead_id=UUID(LEAD_ID), listing_id=None, ctx=CTX))
    finally:
        mp.undo()
    cert = out["published"]["certification"]
    assert cert["state"] == "pending"
    assert any("added after this space was built" in r for r in cert["reasons"])
    assert "checks" not in cert, "check detail is for operators"
    sql = _flat(next(q for q in conn.queries if "FROM property_media pm" in q))
    assert "LEFT JOIN LATERAL" in sql and "evidence_fresh" in sql


# ── the build view: ready to view is not verified ───────────────────────────
def _job_row(status="succeeded", stage="ready", diagnostics=None, **extra):
    return {"id": uuid.uuid4(), "status": status, "stage": stage, "provider": "runpod_pod",
            "provider_job_id": "pod-1", "progress": 100, "media_id": UUID(SPLAT_ID), "error": None,
            "diagnostics": diagnostics if diagnostics is not None else
            {"limitations": [], "quality": GOOD},
            "quality_gate": None, "failure_category": None, "attempts": 1,
            "pipeline_version": "v", "cost_estimate_usd": 0.3, "gpu_seconds": 900.0,
            "output_bytes": 1, "raw_output_key": None, "retry_of": None,
            "created_at": None, "updated_at": None, **extra}


def test_a_ready_build_is_ready_to_view_and_not_verified():
    view = space_status.public_view(_job_row())
    assert view["state"] == "ready"
    cert = view["certification"]
    assert cert["state"] == "incomplete"
    assert set(cert) == {"state", "label", "reasons"}
    assert not _claims_quality(view["message"]) and not _claims_quality(view["label"])
    # The build view does not compare media, so it does not claim it can't.
    assert not any("can't tell" in r for r in cert["reasons"])


def test_a_build_view_says_which_capture_it_could_not_read():
    diagnostics = {"limitations": [], "quality": GOOD,
                   "capture": {"input_media_ids": ["a", "b"], "skipped_media_ids": ["b"]}}
    cert = space_status.public_view(_job_row(diagnostics=diagnostics))["certification"]
    assert any("couldn't be read" in r for r in cert["reasons"])


def test_a_build_refused_by_the_quality_gate_is_rejected():
    view = space_status.public_view(_job_row(status="failed_quality_gate", stage="failed",
                                             quality_gate="reconstruction"))
    assert view["certification"]["state"] == "rejected"


def test_a_running_build_is_pending():
    view = space_status.public_view(_job_row(status="running", stage="reconstructing",
                                             diagnostics={}))
    assert view["certification"]["state"] == "pending"


def test_the_operator_sees_each_check_its_values_and_thresholds():
    view = space_status.operator_view(_job_row())
    checks = view["certification"]["checks"]
    assert set(checks) == set(sc.REQUIRED_CHECKS)
    assert checks["registration"]["status"] == "passed"
    assert checks["registration"]["values"]["ratio"] == 0.95
    assert checks["room_connectivity"]["status"] == "missing"
    assert checks["device_performance"]["status"] == "missing"


def test_the_job_payload_gives_the_operator_form_only_to_operators():
    row = _job_row()
    assert "checks" not in tour_api._job_payload(row, CTX)["certification"]
    assert "checks" in tour_api._job_payload(row, OWNER)["certification"]


# ── the homeowner's portal ───────────────────────────────────────────────────
class _PortalConn:
    def __init__(self, splat_row):
        self.splat_row = splat_row

    async def fetchrow(self, query, *args):
        if "FROM client_portals" in query:
            return {"asset_scope": {"tour": True}, "link_kind": "seller", "watermark_text": None}
        if "FROM leads" in query:
            return {"id": UUID(LEAD_ID), "parcel_id": "P-1", "state": "DE", "address": "1 Test Lane",
                    "asking_price": None, "dossier_status": "draft", "contract_execution_date": None,
                    "contract_expires_at": None,
                    "updated_at": dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc)}
        return None   # no floor plan

    async def fetch(self, query, *args):
        if "property_pano_scenes" in query:
            return []
        if "FROM property_media" in query:
            return [self.splat_row]
        raise AssertionError(f"unexpected fetch: {query}")


def _portal(monkeypatch, row):
    import client_portal

    conn = _PortalConn(row)

    @contextlib.asynccontextmanager
    async def _tx(_ctx):
        yield conn

    async def _quiet(*_a, **_k):
        return None

    monkeypatch.setattr(client_portal, "tenant_tx", _tx)
    monkeypatch.setattr(client_portal, "_record_portal_activity", _quiet)
    monkeypatch.setattr(client_portal, "_portal_session_claims", lambda _h: {
        "portal_id": str(uuid.uuid4()), "tenant_id": TENANT_ID, "lead_id": LEAD_ID,
        "link_kind": "seller"})
    return asyncio.run(client_portal.read_scoped_dossier(authorization="Bearer x"))


def test_the_homeowner_sees_the_quality_label_and_its_caveats(monkeypatch):
    payload = _portal(monkeypatch, _splat(**_job_evidence(metrics=GOOD, fresh=False)))
    tour = payload["assets"]["tour"]
    assert tour["splat_quality_state"] == "pending"
    assert tour["splat_label"] == "3D walkthrough · quality not verified"
    assert any("added after this space was built" in c for c in tour["splat_caveats"])
    # Product language only: no check detail, no job id.
    text = json.dumps(tour)
    assert "checks" not in tour and "44444444-4444-4444-8444-444444444444" not in text
    assert "thresholds" not in text


# ── the AI's answers ─────────────────────────────────────────────────────────
class _AiConn(_CaptureConn):
    async def fetchval(self, query, *_a):
        return 1


def test_the_agent_is_told_the_quality_state_and_caveats():
    import ai_tools_read

    conn = _AiConn(media=[_splat(**_job_evidence(limitations=["partial_registration"],
                                                 metrics={**GOOD, "registration_ratio": 0.45}))])
    out = asyncio.run(ai_tools_read.execute(conn, CTX, "get_property_tour", {"lead_id": LEAD_ID}))
    splat = next(a for a in out["assets"] if a["kind"] == "splat")
    assert splat["quality_state"] == "incomplete"
    assert any("couldn't be placed" in c for c in splat["caveats"])
    assert splat["whole_home_verified"] is False
    note = out["note"].lower()
    assert "quality_state" in note and "verified" in note and "navig" in note


def test_a_share_request_shows_the_approver_the_quality_label(monkeypatch):
    import ai_tools_gated

    staged = {}

    async def _create_approval(ctx, **kwargs):
        staged.update(kwargs)
        return {"id": "approval-1"}

    monkeypatch.setattr("approval_service.create_approval", _create_approval)
    conn = _AiConn(media=[_splat(**_job_evidence(metrics=GOOD, fresh=False))])
    out = asyncio.run(ai_tools_gated.execute(
        conn, CTX, "share_property_tour", {"lead_id": LEAD_ID}, user_id="u1",
        message_id="m1", context_type="lead", context_id=LEAD_ID))
    assert out["ok"] is True
    assert out["quality_label"] == "3D walkthrough · quality not verified"
    assert staged["draft_payload"]["quality_label"] == out["quality_label"]
    assert "quality not verified" in out["detail"]


# ── the worker records the input set the freshness check compares against ──
def test_the_gather_names_every_media_row_it_considered(monkeypatch, tmp_path):
    import reconstruction_worker as worker

    readable, unreadable = uuid.uuid4(), uuid.uuid4()
    newest = dt.datetime(2026, 10, 17, 9, 30, tzinfo=dt.timezone.utc)
    rows = [
        {"id": readable, "kind": "photo", "s3_key": "a.jpg", "bytes": None,
         "content_type": "image/jpeg", "created_at": newest - dt.timedelta(hours=1)},
        {"id": unreadable, "kind": "photo", "s3_key": "b.jpg", "bytes": None,
         "content_type": "image/jpeg", "created_at": newest},
    ]
    queries = []

    class _Conn:
        async def fetch(self, query, *_a):
            queries.append(query)
            return rows

    @contextlib.asynccontextmanager
    async def _tx(_ctx):
        yield _Conn()

    async def _load(row):
        if row["id"] == unreadable:
            raise OSError("object missing")
        return b"\xff\xd8jpeg"

    monkeypatch.setattr(worker, "tenant_tx", _tx)
    monkeypatch.setattr(worker.media_storage, "load_media_bytes", _load)
    job = worker.ReconstructionJob(ctx=CTX, job_id=str(uuid.uuid4()), lead_id=LEAD_ID, listing_id=None)
    images, counts = asyncio.run(worker._gather_source_images(job, tmp_path / "in"))
    assert len(images) == 1
    assert counts == {
        "photos": 1, "videos": 0, "frames": 0,
        "input_media_ids": sorted([str(readable), str(unreadable)]),
        "skipped_media_ids": [str(unreadable)],
        "newest_input_at": newest.isoformat(),
    }
    assert _flat(worker.capture_input_sql("pm")) in _flat(queries[0])


# ── the golden-capture proof script (scripts/golden_capture.py) ─────────────
def _golden_capture():
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "golden_capture.py"
    if not script.exists():
        pytest.skip("scripts/ is not mounted next to backend/")
    spec = importlib.util.spec_from_file_location("golden_capture", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_golden_capture_script_reports_the_certification_not_full_3d():
    """An operator running the proof capture read "the tour should offer
    'Full 3D'" as the success signal; only `verified` may be called that."""
    golden = _golden_capture()
    final = {**space_status.public_view(_job_row()), "status": "succeeded"}
    text = "\n".join(golden.closing_note(final))
    assert "Full 3D" not in text and not _claims_quality(text)
    assert final["certification"]["label"] in text
    assert all(reason in text for reason in final["certification"]["reasons"])
    assert golden.closing_note({"status": "failed"}) == []
