"""Audit A02 / ledger L11-02 / AC11.03: a failed property lookup is never a
verified zero.

The public-record property-candidate lookup in client reconciliation used to
catch every error and return `[]`, and the reconcile set `degraded` from the
model alone — so a timed-out or failed lookup read, to the agent and to the
AI, as "no matching property". These tests inject each failure and assert it
is reported as `timed_out` / `unavailable` (CONTRACT C6), never as `checked`.

Covers: a verified zero, a timeout (three shapes), a provider error, valid
matches, not-applicable, stale results, an idempotent retry, the persisted and
displayed status, the AI-facing answers (Neoh's shortlist, buyer matching) and
the public-records enrichment path.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

import client_ai_automation
from client_ai_automation import automation_state_json

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

_RECORD_ROW = {
    "id": "aaaaaaaa-0000-4000-8000-000000000000", "parcel_id": "P1",
    "address": "1 Main St", "city": "Dover", "state": "DE", "zip_code": "19901",
    "owner_name": "Jane Doe", "source_name": "county", "record_refreshed_at": NOW,
}


class _QueryCanceled(Exception):
    """What asyncpg raises when `SET LOCAL statement_timeout` expires."""
    sqlstate = "57014"


class _LookupConn:
    """Just enough connection for `_property_candidates_bounded`."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.executed = []
        self.lookups = 0

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, sql, *args):
        self.executed.append(sql)

    async def fetch(self, sql, *args):
        self.lookups += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _lookup(outcome, name="Jane Doe", linked=0, **kw):
    conn = _LookupConn(outcome)
    candidates, status = asyncio.run(
        client_ai_automation._property_candidates_bounded(conn, name, [], linked, **kw)
    )
    return conn, candidates, status


# ── the lookup itself ──────────────────────────────────────────────────────

def test_a_verified_zero_is_checked_with_a_timestamp():
    conn, candidates, status = _lookup([])
    assert candidates == []
    assert status["status"] == "checked" and status["matched"] == 0
    assert status["checked_at"] and status["last_checked_at"] == status["checked_at"]
    assert status["required"] is True
    assert any("statement_timeout" in q for q in conn.executed)


def test_valid_matches_are_checked_and_counted():
    _, candidates, status = _lookup([_RECORD_ROW])
    assert len(candidates) == 1 and candidates[0]["owner_name"] == "Jane Doe"
    assert candidates[0]["requires_review"] is True
    assert status["status"] == "checked" and status["matched"] == 1


@pytest.mark.parametrize("exc", [TimeoutError(), asyncio.TimeoutError(), _QueryCanceled()])
def test_a_timeout_is_timed_out_not_zero(exc):
    """Still bounded — the old bare TimeoutError() dead-lettered the client —
    but it says it timed out instead of returning a checked-empty list."""
    _, candidates, status = _lookup(exc, last_checked_at="2026-10-01T00:00:00+00:00")
    assert candidates == []
    assert status["status"] == "timed_out"
    assert status["matched"] is None and status["checked_at"] is None
    # The last real check is carried forward: "when was this last read" survives.
    assert status["last_checked_at"] == "2026-10-01T00:00:00+00:00"
    assert "ms" in status["reason"]


def test_a_provider_error_is_unavailable_and_logs_no_name(caplog):
    with caplog.at_level("WARNING", logger="oracle.client_ai"):
        _, candidates, status = _lookup(RuntimeError("relation missing for Jane Doe"))
    assert candidates == [] and status["status"] == "unavailable"
    assert status["reason"] == "lookup error (RuntimeError)"
    assert "Jane Doe" not in caplog.text and "Jane Doe" not in json.dumps(status)


@pytest.mark.parametrize("name, linked", [("Jane Doe", 1), ("Al", 0)])
def test_a_lookup_that_cannot_apply_is_not_applicable_and_runs_no_query(name, linked):
    conn, candidates, status = _lookup([_RECORD_ROW], name=name, linked=linked)
    assert candidates == [] and status["status"] == "not_applicable"
    assert conn.lookups == 0 and conn.executed == []


def test_automation_json_carries_source_status_and_old_rows_read_as_unrecorded():
    assert automation_state_json(None)["source_status"] == {}
    row = {k: None for k in (
        "enabled", "status", "score_mode", "stage_mode", "score_version", "summary",
        "model_id", "last_evaluated_at", "last_error_code",
    )}
    row.update(score_breakdown="[]", normalized_preferences="{}", next_actions="[]",
               data_gaps="[]", evidence="[]", property_candidates="[]")
    # A row read before migration 0135 has no such column at all.
    assert automation_state_json(row)["source_status"] == {}
    row["source_status"] = '{"public_property_records": {"status": "timed_out"}}'
    assert automation_state_json(row)["source_status"]["public_property_records"]["status"] == "timed_out"


def test_migration_0135_adds_an_object_typed_source_status():
    sql = (Path(__file__).parents[1] / "db" / "migrations"
           / "0135_client_ai_source_status.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS source_status jsonb NOT NULL DEFAULT '{}'::jsonb" in sql
    assert "jsonb_typeof(source_status) = 'object'" in sql
    assert "CREATE FUNCTION" not in sql.upper().replace("OR REPLACE ", "")


# ── through reconcile_client ───────────────────────────────────────────────

class _ReconcileConn(_LookupConn):
    """A client_ai_state round trip through reconcile_client, in memory."""

    CLIENT_ID = "bbbbbbbb-0000-4000-8000-000000000001"
    TENANT_ID = "cccccccc-0000-4000-8000-000000000001"
    _STATE_COLS = (
        "client_id", "tenant_id", "status", "score_version", "score_breakdown",
        "normalized_preferences", "summary", "next_actions", "data_gaps", "evidence",
        "property_candidates", "model_id", "input_fingerprint", "last_error_code",
        "source_status",
    )

    def __init__(self, outcome):
        super().__init__(outcome)
        self.state = None
        self.tasks = []
        self.activities = []
        self.shortcuts = 0
        self.client = {
            "id": self.CLIENT_ID, "tenant_id": self.TENANT_ID, "full_name": "Jane Doe",
            "email": "jane@example.test", "phone": "+13025550100", "client_type": "buyer",
            "stage": "lead", "lead_score": 0, "assignee_id": None, "company": None,
            "preferences": {}, "source": "referral", "last_contacted_at": None,
            "archived_at": None, "created_at": NOW, "updated_at": NOW,
        }

    async def fetchrow(self, sql, *args):
        text = " ".join(sql.split())
        if text.startswith("SELECT id,tenant_id,full_name"):
            return self.client
        if text.startswith("SELECT * FROM client_ai_state"):
            return self.state
        if text.startswith("SELECT updated_at FROM clients"):
            return {"updated_at": self.client["updated_at"]}
        if text.startswith("SELECT enabled,score_mode,stage_mode"):
            return {"enabled": True, "score_mode": "auto", "stage_mode": "auto"}
        if text.startswith("UPDATE client_ai_state"):
            self.shortcuts += 1
            self.state = {
                **self.state,
                "status": "complete" if self.state["last_error_code"] is None else "degraded",
            }
            return self.state
        if text.startswith("UPDATE clients SET"):
            return {"stage": self.client["stage"], "lead_score": self.client["lead_score"]}
        if text.startswith("INSERT INTO client_ai_state ( client_id,tenant_id,enabled"):
            self.state = {
                **dict(zip(self._STATE_COLS, args)), "enabled": True, "score_mode": "auto",
                "stage_mode": "auto", "last_evaluated_at": datetime.now(timezone.utc),
            }
            return self.state
        raise AssertionError(f"unexpected fetchrow: {text[:80]}")

    async def fetch(self, sql, *args):
        if "public_property_records" in sql:
            return await super().fetch(sql, *args)
        return []

    async def fetchval(self, sql, *args):
        marker = args[1].rstrip("%")
        return 1 if any(t.startswith(marker) for t in self.tasks) else None

    async def execute(self, sql, *args):
        if "INSERT INTO client_tasks" in sql:
            self.tasks.append(args[3])
        elif "INSERT INTO client_activities" in sql:
            self.activities.append(args)
        else:
            self.executed.append(sql)


def _reconcile(monkeypatch, conn, *, force=False):
    from tenancy import Role, TenantContext

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(client_ai_automation, "tenant_tx", fake_tx)
    monkeypatch.setenv("ORACLE_CLIENT_AI_MODEL_ENABLED", "0")
    ctx = TenantContext(agent_id="client-ai", tenant_id=conn.TENANT_ID, role=Role.AGENT)
    return asyncio.run(client_ai_automation.reconcile_client(ctx, conn.CLIENT_ID, force=force))


def _timeline_summary(conn):
    # INSERT INTO client_activities ... VALUES ($1,$2,'system',$4,$3,...)
    return conn.activities[-1][3]


def test_a_reconcile_whose_lookup_times_out_is_degraded_not_complete(monkeypatch):
    conn = _ReconcileConn(TimeoutError())
    automation = _reconcile(monkeypatch, conn)["automation"]

    assert automation["status"] == "degraded"
    assert automation["last_error_code"] == "PROPERTY_LOOKUP_TIMED_OUT"
    assert automation["property_candidates"] == []
    assert automation["source_status"]["public_property_records"]["status"] == "timed_out"
    assert any(g["code"] == "property-lookup" for g in automation["data_gaps"])
    assert "not checked" in _timeline_summary(conn)
    assert json.loads(conn.activities[-1][2])["property_lookup"] == "timed_out"


def test_a_reconcile_whose_lookup_errors_is_degraded_unavailable(monkeypatch):
    conn = _ReconcileConn(RuntimeError("boom"))
    automation = _reconcile(monkeypatch, conn)["automation"]
    assert automation["status"] == "degraded"
    assert automation["last_error_code"] == "PROPERTY_LOOKUP_UNAVAILABLE"
    assert automation["source_status"]["public_property_records"]["status"] == "unavailable"


def test_a_reconcile_with_a_verified_zero_is_complete(monkeypatch):
    conn = _ReconcileConn([])
    automation = _reconcile(monkeypatch, conn)["automation"]
    assert automation["status"] == "complete" and automation["last_error_code"] is None
    lookup = automation["source_status"]["public_property_records"]
    assert lookup["status"] == "checked" and lookup["matched"] == 0
    assert not any(g["code"] == "property-lookup" for g in automation["data_gaps"])
    assert _timeline_summary(conn) == "AI client reconciliation completed"


def test_stale_candidates_are_not_reserved_and_the_retry_is_idempotent(monkeypatch):
    # Pass 1: a real match, and one review task.
    conn = _ReconcileConn([_RECORD_ROW])
    first = _reconcile(monkeypatch, conn)["automation"]
    assert first["status"] == "complete" and len(first["property_candidates"]) == 1
    checked_at = first["source_status"]["public_property_records"]["checked_at"]
    assert len(conn.tasks) == 1

    # Pass 2 (forced): the lookup times out. The old candidates are not
    # re-presented as current matches; when the source was last read is kept.
    conn.outcome = TimeoutError()
    second = _reconcile(monkeypatch, conn, force=True)["automation"]
    assert second["status"] == "degraded" and second["property_candidates"] == []
    lookup = second["source_status"]["public_property_records"]
    assert lookup["status"] == "timed_out" and lookup["last_checked_at"] == checked_at

    # Pass 3, same inputs, not forced: a failed pass is not "unchanged", so
    # the lookup is retried instead of the failure being re-served for 24 h.
    conn.outcome = [_RECORD_ROW]
    lookups = conn.lookups
    third = _reconcile(monkeypatch, conn)["automation"]
    assert conn.lookups == lookups + 1 and conn.shortcuts == 0
    assert third["status"] == "complete" and len(third["property_candidates"]) == 1
    assert len(conn.tasks) == 1  # the review task is not duplicated

    # Pass 4, same inputs after a successful check: the 24-hour shortcut holds.
    _reconcile(monkeypatch, conn)
    assert conn.shortcuts == 1 and conn.lookups == lookups + 1


# ── what Neoh says about the shortlist ─────────────────────────────────────

def test_neoh_never_calls_a_failed_lookup_an_empty_shortlist():
    import neoh_render as render

    failed = render.properties_for("Sarah", [], {
        "status": "timed_out", "last_checked_at": "2026-10-01T00:00:00+00:00",
    })
    assert "couldn't check public records" in failed["spoken"]
    assert "not a 'no matches'" in failed["spoken"] and "2026-10-01" in failed["spoken"]
    assert "Nothing is on" not in failed["spoken"]

    unavailable = render.properties_for("Sarah", [], {"status": "unavailable"})
    assert "failed" in unavailable["spoken"] and "Nothing is on" not in unavailable["spoken"]

    unrecorded = render.properties_for("Sarah", [], {})
    assert "has not been checked" in unrecorded["spoken"]

    checked = render.properties_for("Sarah", [], {"status": "checked", "matched": 0})
    assert "Nothing is on Sarah's shortlist" in checked["spoken"]


def test_neoh_shortlist_reads_the_recorded_lookup_status(monkeypatch):
    import db.connection
    import neoh_intents
    from tenancy import Role, TenantContext

    row = {
        "enabled": True, "status": "degraded", "score_mode": "auto", "stage_mode": "auto",
        "score_version": None, "score_breakdown": "[]", "normalized_preferences": "{}",
        "summary": None, "next_actions": "[]", "data_gaps": "[]", "evidence": "[]",
        "property_candidates": "[]", "model_id": None, "last_evaluated_at": None,
        "last_error_code": "PROPERTY_LOOKUP_TIMED_OUT",
        "source_status": json.dumps({"public_property_records": {"status": "timed_out"}}),
    }

    class _Conn:
        async def fetchrow(self, sql, *args):
            return row

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield _Conn()

    monkeypatch.setattr(db.connection, "tenant_tx", fake_tx)
    ctx = TenantContext(agent_id="a@x.test", tenant_id=_ReconcileConn.TENANT_ID, role=Role.AGENT)
    candidates, lookup = asyncio.run(neoh_intents._candidates_for(ctx, _ReconcileConn.CLIENT_ID))
    assert candidates == [] and lookup["status"] == "timed_out"
    assert asyncio.run(neoh_intents._candidates_for(ctx, None)) == ([], {})


# ── buyer matching: "not searched" is not "nobody fits" ────────────────────

def test_buyer_matching_skipped_for_a_platform_admin_is_not_a_zero(monkeypatch):
    import ai_tools_read
    import buyer_matching
    from tenancy import Role, TenantContext

    async def fake_load(conn, ctx, *, lead_id=None, listing_id=None):
        return {"address": "1 Main St", "lead_id": lead_id, "listing_id": None}

    monkeypatch.setattr(buyer_matching, "load_owned_property", fake_load)
    admin = TenantContext(agent_id="ops@x.test", tenant_id=_ReconcileConn.TENANT_ID,
                          role=Role.PLATFORM_ADMIN)
    assert admin.is_platform_admin
    out = asyncio.run(ai_tools_read._suggest_client_matches(
        object(), admin, {"lead_id": "dddddddd-0000-4000-8000-000000000001"},
    ))
    assert out["buyer_search"]["status"] == "not_applicable"
    assert "did not run" in out["note"] and "not a result that no buyer fits" in out["note"]
    assert "No buyer's recorded criteria fit" not in out["note"]


def test_buyer_matching_that_ran_and_found_nobody_is_a_checked_zero(monkeypatch):
    import ai_tools_read
    import buyer_matching
    from tenancy import Role, TenantContext

    async def fake_load(conn, ctx, *, lead_id=None, listing_id=None):
        return {"address": "1 Main St", "lead_id": lead_id, "listing_id": None}

    class _Conn:
        async def fetch(self, sql, *args):
            return []

    monkeypatch.setattr(buyer_matching, "load_owned_property", fake_load)
    agent = TenantContext(agent_id="a@x.test", tenant_id=_ReconcileConn.TENANT_ID, role=Role.AGENT)
    out = asyncio.run(ai_tools_read._suggest_client_matches(
        _Conn(), agent, {"lead_id": "dddddddd-0000-4000-8000-000000000001"},
    ))
    assert out["buyer_search"]["status"] == "checked"
    assert "No buyer's recorded criteria fit" in out["note"]


# ── public records: a failed detail lookup is "not checked" ────────────────

@pytest.mark.parametrize("exc, expected", [
    (asyncio.TimeoutError(), "timed_out"),
    (RuntimeError("county API 500 at 1 Main St"), "unavailable"),
])
def test_a_failed_official_source_lookup_is_reported_not_hidden(monkeypatch, exc, expected):
    import mls_portal
    from harvesters import fl_fdor
    from tenancy import Role, TenantContext

    class _Harvester:
        def __init__(self, *a, **kw):
            self.metrics = {}

        async def lookup_parcel(self, parcel_id):
            raise exc

    monkeypatch.setattr(fl_fdor, "FloridaFDORHarvester", _Harvester)
    row = {"state": "FL", "source_key": "firehose:FL", "parcel_id": "12-34",
           "match_score": 100, "beds": None, "source_metadata": None}
    failures: list = []
    ctx = TenantContext(agent_id="a@x.test", tenant_id=_ReconcileConn.TENANT_ID, role=Role.AGENT)
    changed = asyncio.run(mls_portal._reconcile_sparse_public_record(
        rows=[row], ctx=ctx, exact_match_only=True, failures=failures,
    ))
    assert changed is False
    assert failures == [{
        "source": "fl_fdor_parcel", "status": expected, "reason": failures[0]["reason"],
    }]
    assert "1 Main St" not in failures[0]["reason"]
