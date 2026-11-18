"""Quarantined Stripe usage waits on a person, and only a platform admin
settles it (external audits 24.5, 24.6). The drain itself is proven against
PostgreSQL in test_usage_report_claims_live."""
from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import billing_usage
import component_health as ch
import operator_api as op
from tenancy import Role, TenantContext, require_context

ADMIN = TenantContext(agent_id="ops@neoh.test", tenant_id="00000000-0000-0000-0000-000000000000",
                      role=Role.PLATFORM_ADMIN)
OWNER = TenantContext(agent_id="owner@a.test", tenant_id="aaaaaaaa-0000-0000-0000-00000000000a",
                      role=Role.BROKER_OWNER)
AGENT = TenantContext(agent_id="agent@a.test", tenant_id="aaaaaaaa-0000-0000-0000-00000000000a",
                      role=Role.AGENT)
T1 = "11111111-1111-1111-1111-111111111111"
NOW = datetime.now(timezone.utc)
U1 = "33333333-3333-3333-3333-333333333333"
BODY = {"tenant_id": T1, "report_state": "uncertain", "resolution": "billed",
        "reason": "found on the Stripe meter", "usage_ids": [U1]}


def _client(ctx):
    app = FastAPI()
    app.include_router(op.router)
    app.dependency_overrides[require_context] = lambda: ctx
    return TestClient(app)


@pytest.mark.parametrize("ctx", [AGENT, OWNER], ids=["agent", "broker_owner"])
def test_only_a_platform_admin_may_resolve_quarantined_usage(ctx, monkeypatch):
    async def must_not(*_a, **_k):
        raise AssertionError("reached the database")

    monkeypatch.setattr(billing_usage, "resolve_quarantined_usage", must_not)
    assert _client(ctx).post("/api/admin/billing/usage/resolve", json=BODY).status_code == 403


@pytest.mark.parametrize("patch", [
    {"resolution": "refunded"}, {"report_state": "reported"}, {"reason": ""},
    {"usage_ids": []}, {"extra": 1}, {"tenant_id": "not-a-uuid"},
    {"usage_ids": None},   # an uncertain row is a per-row fact: never resolved in bulk
])
def test_a_resolution_outside_the_vocabulary_is_refused(patch, monkeypatch):
    async def must_not(*_a, **_k):
        raise AssertionError("reached the database")

    monkeypatch.setattr(billing_usage, "resolve_quarantined_usage", must_not)
    resp = _client(ADMIN).post("/api/admin/billing/usage/resolve", json={**BODY, **patch})
    assert resp.status_code == 422


def test_a_resolution_is_recorded_and_audited(monkeypatch):
    seen, audited = {}, []

    async def resolve(ctx, **kw):
        seen.update(kw, actor=ctx.agent_id)
        return 3

    class _Ledger:
        async def record(self, category, action, **kw):
            audited.append((category, action, kw))

    import audit_ledger

    monkeypatch.setattr(billing_usage, "resolve_quarantined_usage", resolve)
    monkeypatch.setattr(audit_ledger, "ledger", _Ledger())
    resp = _client(ADMIN).post("/api/admin/billing/usage/resolve", json=BODY)
    assert resp.status_code == 200 and resp.json()["resolved"] == 3
    assert seen == {"tenant_id": T1, "report_state": "uncertain", "resolution": "billed",
                    "note": "found on the Stripe meter", "usage_ids": [U1], "actor": "ops@neoh.test"}
    (category, action, kw), = audited
    assert action == "admin.billing.usage_resolved" and category == audit_ledger.AuditCategory.ADMIN_ACTION
    assert kw["tenant_id"] == T1 and kw["metadata"]["rows"] == 3 and kw["metadata"]["usage_ids"] == [U1]
    # expired usage (a billing decision) may be resolved for the whole brokerage
    bulk = {**BODY, "report_state": "expired", "resolution": "written_off", "usage_ids": None}
    assert _client(ADMIN).post("/api/admin/billing/usage/resolve", json=bulk).status_code == 200


def test_the_resolver_refuses_an_unknown_state_or_resolution():
    for kw in ({"report_state": "reported", "resolution": "billed"},
               {"report_state": "expired", "resolution": "refunded"}):
        with pytest.raises(ValueError):
            asyncio.run(billing_usage.resolve_quarantined_usage(ADMIN, tenant_id=T1, note="x", **kw))


class _Conn:
    def __init__(self, row, quarantined):
        self.row, self.quarantined = row, quarantined

    async def fetchrow(self, query, *a):
        return self.row if "FROM billing_usage_events" in query else None

    async def fetch(self, query, *a):
        return self.quarantined if "report_quarantined_at" in query else []

    async def fetchval(self, query, *a):
        return None

    async def execute(self, *a):
        return "SET"

    def transaction(self):
        @asynccontextmanager
        async def tx():
            yield self
        return tx()


def test_the_billing_card_names_usage_that_waits_on_a_person(monkeypatch):
    conn = _Conn({"backlog": 4, "oldest": NOW, "erroring": 0, "uncertain": 2, "expired": 1, "aging": 0},
                 [{"tenant_id": T1, "name": "Lockwood", "uncertain": 2, "expired": 1, "since": NOW}])

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    monkeypatch.setattr(op, "tenant_tx", tx)
    monkeypatch.setattr(billing_usage, "STRIPE_METERED_PRICE_ID", "price_x")
    usage = asyncio.run(op.billing_exceptions(ctx=ADMIN))["usage_metering"]
    assert usage["state"] == "attention"
    assert (usage["uncertain"], usage["expired"], usage["unreported"]) == (2, 1, 4)
    assert usage["quarantined_by_tenant"] == [
        {"tenant_id": T1, "name": "Lockwood", "uncertain": 2, "expired": 1, "since": NOW.isoformat()}]


def test_usage_reporting_health_pages_until_quarantined_usage_is_resolved(monkeypatch):
    monkeypatch.setattr(billing_usage, "STRIPE_METERED_PRICE_ID", "price_x")
    assert ch._usage_reporting({"uncertain": 0, "expired": 0, "aging": 0})["state"] == ch.HEALTHY
    held = ch._usage_reporting({"uncertain": 1, "expired": 2, "aging": 0})
    assert held["state"] == ch.DEGRADED and held["detail"]["uncertain"] == 1
    assert ch._usage_reporting({"uncertain": 0, "expired": 0, "aging": 5})["state"] == ch.DEGRADED
    assert "usage_reporting" in ch._DB_KEYS
    assert ch.DEGRADED in ch.ALERTING                       # ops_alerts emails it
    monkeypatch.setattr(billing_usage, "STRIPE_METERED_PRICE_ID", "")
    assert ch._usage_reporting({"uncertain": 0, "expired": 0, "aging": 5})["state"] == ch.NOT_CONFIGURED
    # quarantined usage still pages with metering switched off afterwards
    assert ch._usage_reporting({"uncertain": 1, "expired": 0, "aging": 0})["state"] == ch.DEGRADED


def test_the_ai_billing_tools_do_not_count_quarantined_usage_as_the_next_bill():
    import ai_tools_read

    status = inspect.getsource(ai_tools_read._get_billing_status)
    assert "report_state IS NULL" in status and "usage_events_held_for_billing_review" in status
    assert "report_state IS NULL" in inspect.getsource(ai_tools_read._list_billing_invoices)


def test_each_send_is_one_http_attempt_under_a_stable_idempotency_key():
    """A refusal is trusted as 'not recorded' only because the SDK does not
    retry inside the call (its retries use a fresh key; a refusal could then
    answer a retry of an attempt Stripe recorded)."""
    drain = inspect.getsource(billing_usage.drain_usage_to_stripe)
    assert "max_network_retries=0" in drain and "idempotency_key=_idempotency_key(identifier)" in drain
    assert billing_usage._idempotency_key("k") == billing_usage._idempotency_key("k")
