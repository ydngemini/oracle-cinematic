"""MLS licensing and feed health — the gate that decides what a customer is told.

The defect these exist to prevent shipped once already. Classification was a
denylist of three developer dataset names that DEFAULTED TO LICENSED, so every
other slug became `licensed_property_listing` with no human ever saying so. The
configured feed was `actris_ref` — a Bridge reference dataset — and its 52,622
rows carried licensed provenance. Their newest source_modified_at is 2020-11-08:
frozen sample data six years old, labelled live inventory.

So the tests below are mostly about refusing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import mls_health as health
from mls_licensing import (
    DEVELOPER, LICENSED, classify_dataset, classify_from_env,
    looks_like_reference_dataset, valid_agreement_ref,
)

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Classification fails closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug", ["test", "test_sd", "test_sf", "TEST_SD"])
def test_bridge_synthetic_sets_are_developer_data(slug):
    assert classify_dataset(slug).classification == DEVELOPER


@pytest.mark.parametrize("slug", ["actris_ref", "bright-ref", "foo_reference", "x_sample", "y_demo"])
def test_reference_datasets_are_developer_data(slug):
    assert looks_like_reference_dataset(slug)
    assert classify_dataset(slug).classification == DEVELOPER


def test_a_reference_dataset_stays_developer_even_when_declared_licensed():
    """The exact live misconfiguration. A reference dataset is frozen sample
    inventory; no agreement makes it live data."""
    result = classify_dataset("actris_ref", declared_licensed=True, agreement_ref="ACTRIS-2026")
    assert result.classification == DEVELOPER
    assert not result.may_report_ready
    assert "reference" in result.reason


def test_an_unknown_dataset_is_developer_data_by_default():
    """The old rule made this licensed. Fail closed: silence is not consent."""
    result = classify_dataset("brightmls")
    assert result.classification == DEVELOPER
    assert "not been declared licensed" in result.reason


def test_declaring_licensed_without_an_agreement_is_refused():
    result = classify_dataset("brightmls", declared_licensed=True)
    assert result.classification == DEVELOPER
    assert "no agreement reference" in result.reason


def test_a_declared_licensed_feed_with_an_agreement_is_licensed():
    result = classify_dataset("brightmls", declared_licensed=True, agreement_ref="BRIGHT-2026-001")
    assert result.classification == LICENSED
    assert result.may_report_ready
    assert result.agreement_ref == "BRIGHT-2026-001"


def test_empty_configuration_is_developer_data():
    assert classify_dataset("").classification == DEVELOPER
    assert classify_dataset(None).classification == DEVELOPER


def test_env_declaration_needs_both_switches(monkeypatch):
    monkeypatch.delenv("ORACLE_MLS_LICENSED", raising=False)
    monkeypatch.delenv("ORACLE_MLS_AGREEMENT_REF", raising=False)
    assert classify_from_env("brightmls").classification == DEVELOPER

    monkeypatch.setenv("ORACLE_MLS_LICENSED", "1")
    assert classify_from_env("brightmls").classification == DEVELOPER

    monkeypatch.setenv("ORACLE_MLS_AGREEMENT_REF", "BRIGHT-2026-001")
    assert classify_from_env("brightmls").classification == LICENSED


@pytest.mark.parametrize("ref,ok", [
    ("BRIGHT-2026-001", True), ("a/b_c 1.2", True),
    ("", False), ("ab", False), ("x" * 200, False), ("drop; --", False),
])
def test_agreement_refs_are_shape_checked(ref, ok):
    assert valid_agreement_ref(ref) is ok


# ---------------------------------------------------------------------------
# Feed health
# ---------------------------------------------------------------------------

def feed(**over):
    base = {
        "last_success_at": NOW, "backfill_complete": True,
        "consecutive_failures": 0, "stale_after_minutes": 1440,
    }
    base.update(over)
    return base


@pytest.mark.parametrize("row,expected", [
    (None, health.NOT_CONFIGURED),
    ({"stale_after_minutes": 1440}, health.CONFIGURED),
    (feed(backfill_complete=False), health.BACKFILLING),
    (feed(), health.READY),
    (feed(last_success_at=NOW - timedelta(days=3)), health.STALE),
    (feed(consecutive_failures=3), health.DEGRADED),
    (feed(last_error_class="auth"), health.AUTH_ERROR),
    (feed(last_error_class="rate_limit"), health.RATE_LIMITED),
])
def test_health_is_derived_from_what_happened(row, expected):
    assert health.compute_health(row, now=NOW) == expected


def test_an_incomplete_backfill_never_reads_as_ready():
    """Half a board looks exactly like a whole board to a searcher, which is
    how a brokerage concludes a listing does not exist."""
    assert health.compute_health(feed(backfill_complete=False), now=NOW) == health.BACKFILLING


def test_auth_failure_outranks_staleness():
    row = feed(last_success_at=NOW - timedelta(days=9), last_error_class="auth")
    assert health.compute_health(row, now=NOW) == health.AUTH_ERROR


def test_staleness_thresholds_are_per_feed():
    """Boards publish at different cadences; one global threshold would call a
    nightly feed broken every morning."""
    six_hours = feed(last_success_at=NOW - timedelta(hours=6))
    assert health.compute_health({**six_hours, "stale_after_minutes": 1440}, now=NOW) == health.READY
    assert health.compute_health({**six_hours, "stale_after_minutes": 60}, now=NOW) == health.STALE


def test_never_synced_is_not_stale():
    assert health.is_stale({"stale_after_minutes": 60}, now=NOW) is False


def test_stale_and_degraded_feeds_still_serve():
    """§32: a provider outage leaves cached listings readable and marked — it
    must not turn into "no listings"."""
    for row in (feed(last_success_at=NOW - timedelta(days=5)), feed(consecutive_failures=4)):
        assert health.feed_is_usable(row)


def test_an_unconfigured_feed_is_not_usable():
    assert not health.feed_is_usable(None)


# ---------------------------------------------------------------------------
# Entitlement and readiness
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager          # noqa: E402
from tenancy import Role, TenantContext             # noqa: E402

TENANT_A = "aaaaaaaa-0000-0000-0000-00000000000a"
TENANT_B = "bbbbbbbb-0000-0000-0000-00000000000b"
CTX_A = TenantContext(agent_id="a@x.test", tenant_id=TENANT_A, role=Role.BROKER_OWNER)


class FeedConn:
    def __init__(self, entitlements=(), feeds=()):
        self._ent = list(entitlements)
        self._feeds = list(feeds)
        self.queries = []

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        if "mls_feed_entitlements" in query:
            return [{"mls_id": m} for m in self._ent]
        if "mls_sync_status" in query:
            wanted = set(args[0]) if args else set()
            return [f for f in self._feeds if f["mls_id"] in wanted]
        return []


def licensed_feed(mls_id="brightmls", **over):
    base = {
        "mls_id": mls_id, "mls_name": "Bright MLS", "provider": "bridge",
        "dataset": "brightmls", "license_classification": LICENSED,
        "license_reason": "declared", "health": "READY",
        "last_success_at": NOW, "last_attempt_at": NOW, "last_error": None,
        "consecutive_failures": 0, "backfill_complete": True,
        "backfill_records": 100, "listings_synced": 100, "stale_after_minutes": 1440,
    }
    base.update(over)
    return base


def dev_feed(mls_id="actris", **over):
    return licensed_feed(mls_id=mls_id, license_classification=DEVELOPER,
                         mls_name="ACTRIS reference", **over)


def test_no_entitlement_means_not_started():
    conn = FeedConn()
    out = asyncio.run(health.mls_capability(conn, CTX_A))
    assert out["status"] == "NOT_STARTED"
    assert out["feeds"] == []


def test_developer_data_can_never_report_ready():
    """The whole point of the licensing split. This feed is green, complete,
    synced seconds ago — and is a reference dataset."""
    conn = FeedConn(entitlements=["actris"], feeds=[dev_feed()])
    out = asyncio.run(health.mls_capability(conn, CTX_A))
    assert out["status"] == "BLOCKED"
    assert "developer/reference" in out["detail"]
    assert out["feeds"][0]["licensed"] is False


def test_a_licensed_fresh_feed_reports_ready():
    conn = FeedConn(entitlements=["brightmls"], feeds=[licensed_feed()])
    out = asyncio.run(health.mls_capability(conn, CTX_A))
    assert out["status"] == "READY"


def test_a_licensed_feed_still_backfilling_is_in_progress():
    conn = FeedConn(entitlements=["brightmls"], feeds=[licensed_feed(backfill_complete=False)])
    assert asyncio.run(health.mls_capability(conn, CTX_A))["status"] == "IN_PROGRESS"


def test_a_licensed_feed_that_cannot_authenticate_is_an_error():
    conn = FeedConn(entitlements=["brightmls"], feeds=[licensed_feed(last_error_class="auth")])
    assert asyncio.run(health.mls_capability(conn, CTX_A))["status"] == "ERROR"


def test_stale_licensed_data_is_an_error_not_ready():
    conn = FeedConn(entitlements=["brightmls"],
                    feeds=[licensed_feed(last_success_at=NOW - timedelta(days=4))])
    assert asyncio.run(health.mls_capability(conn, CTX_A))["status"] == "ERROR"


def test_a_licensed_feed_beside_developer_data_still_reports_ready():
    conn = FeedConn(entitlements=["brightmls", "actris"],
                    feeds=[licensed_feed(), dev_feed()])
    assert asyncio.run(health.mls_capability(conn, CTX_A))["status"] == "READY"


def test_entitlement_is_read_for_the_session_tenant_only():
    conn = FeedConn(entitlements=["brightmls"])
    asyncio.run(health.entitled_feed_ids(conn, CTX_A))
    query, args = conn.queries[0]
    assert "tenant_id = $1::uuid" in query
    assert args[0] == TENANT_A


def test_a_browser_may_narrow_feeds_but_never_widen_them():
    """A request may ask for fewer feeds. Asking for another tenant's feed
    returns nothing rather than granting it."""
    conn = FeedConn(entitlements=["brightmls", "actris"])
    assert asyncio.run(health.narrow_to_entitled(conn, CTX_A, ["brightmls"])) == ["brightmls"]
    assert asyncio.run(health.narrow_to_entitled(conn, CTX_A, ["someone_elses_feed"])) == []
    assert asyncio.run(health.narrow_to_entitled(conn, CTX_A, None)) == ["actris", "brightmls"]


# ---------------------------------------------------------------------------
# Telling a caller WHY a result set is empty (§18)
# ---------------------------------------------------------------------------

def test_no_coverage_is_distinguished_from_no_matches():
    assert health.coverage_note([])["state"] == "no_coverage"


def test_developer_only_coverage_is_labelled():
    note = health.coverage_note([{"licensed": False, "health": "READY"}])
    assert note["state"] == "developer_data_only"
    assert "not live MLS inventory" in note["message"]


def test_backfilling_coverage_warns_results_are_incomplete():
    note = health.coverage_note([{"licensed": True, "health": "BACKFILLING"}])
    assert note["state"] == "backfilling"


def test_stale_coverage_reports_its_age():
    note = health.coverage_note([
        {"licensed": True, "health": "STALE", "age_seconds": 9 * 3600},
    ])
    assert note["state"] == "stale"
    assert "9h ago" in note["message"]


def test_fresh_licensed_coverage_says_nothing():
    note = health.coverage_note([{"licensed": True, "health": "READY"}])
    assert note["state"] == "fresh"
    assert note["message"] == ""
