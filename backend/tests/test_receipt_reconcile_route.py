"""Only a platform admin settles contradicting delivery receipts, with a
closed vocabulary and a reason (external audit 25.8)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import messaging_data
import operator_api as op
from tenancy import Role, TenantContext, require_context

ADMIN = TenantContext(agent_id="ops@neoh.test", tenant_id="00000000-0000-0000-0000-000000000000",
                      role=Role.PLATFORM_ADMIN)
OWNER = TenantContext(agent_id="owner@a.test", tenant_id="aaaaaaaa-0000-0000-0000-00000000000a",
                      role=Role.BROKER_OWNER)
BODY = {"final_status": "failed", "reason": "checked the Telnyx message record"}


def _client(ctx):
    app = FastAPI()
    app.include_router(op.router)
    app.dependency_overrides[require_context] = lambda: ctx
    return TestClient(app)


@pytest.fixture
def reconcile(monkeypatch):
    seen = []

    async def fake(ctx, *, receipt_id, final_status):
        seen.append((ctx.agent_id, receipt_id, final_status))
        if receipt_id == 404:
            raise messaging_data.ReceiptNotFound("No open contradiction with that id")
        return {"tenant_id": "t1", "provider_message_id": "m1", "final_status": final_status,
                "status_changed": True, "receipts_reconciled": 1}

    monkeypatch.setattr(messaging_data, "reconcile_delivery_receipt", fake)
    import audit_ledger

    class _Ledger:
        async def record(self, *a, **kw):
            seen.append(("audit", a[1], kw["metadata"]["final_status"]))

    monkeypatch.setattr(audit_ledger, "ledger", _Ledger())
    return seen


def test_a_broker_owner_cannot_settle_receipts(reconcile):
    assert _client(OWNER).post("/api/admin/messaging/receipts/7/reconcile", json=BODY).status_code == 403
    assert reconcile == []


@pytest.mark.parametrize("patch", [{"final_status": "undelivered"}, {"reason": ""}, {"extra": 1}])
def test_the_body_is_a_closed_vocabulary(reconcile, patch):
    assert _client(ADMIN).post("/api/admin/messaging/receipts/7/reconcile",
                               json={**BODY, **patch}).status_code == 422


def test_a_platform_admin_settles_and_it_is_audited(reconcile):
    response = _client(ADMIN).post("/api/admin/messaging/receipts/7/reconcile", json=BODY)
    assert response.status_code == 200 and response.json()["status_changed"]
    assert reconcile == [("ops@neoh.test", 7, "failed"), ("audit", "admin.messaging.receipt_reconciled", "failed")]
    assert _client(ADMIN).post("/api/admin/messaging/receipts/404/reconcile", json=BODY).status_code == 404
