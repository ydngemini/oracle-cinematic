"""Who may export, offboard, close and request deletion — and that every
irreversible action re-proves the password. Route functions called directly;
the lifecycle itself is proved in test_privacy_lifecycle_live.py."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import privacy_api as api
from tenancy import Role, TenantContext

TENANT = "11111111-1111-1111-1111-111111111111"
OWNER = TenantContext(agent_id="owner@b.test", tenant_id=TENANT, role=Role.BROKER_OWNER)
AGENT = TenantContext(agent_id="agent@b.test", tenant_id=TENANT, role=Role.AGENT)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def calls(monkeypatch):
    seen: dict[str, list] = {"password": [], "offboard": [], "closure": [], "export": [], "subject": []}

    async def confirm(ctx, password):
        seen["password"].append(password)
        if password != "right":
            raise HTTPException(403, "Password is incorrect.")

    async def offboard(ctx, **kw):
        seen["offboard"].append(kw)
        return {"preview": kw["preview"]}

    async def closure(ctx, **kw):
        seen["closure"].append(kw)
        return {"state": "closing"}

    async def export(ctx, **kw):
        seen["export"].append(kw)
        return {"operation_id": "op"}

    async def subject(ctx, **kw):
        seen["subject"].append(kw)
        return {"ok": True}

    import auth
    import privacy_export
    import privacy_lifecycle
    import privacy_requests

    monkeypatch.setattr(auth, "confirm_password", confirm)
    monkeypatch.setattr(privacy_lifecycle, "offboard_agent", offboard)
    monkeypatch.setattr(privacy_lifecycle, "request_closure", closure)
    monkeypatch.setattr(privacy_export, "request_export", export)
    monkeypatch.setattr(privacy_requests, "handle_subject_request", subject)
    return seen


def test_agents_cannot_reach_any_lifecycle_action(calls):
    for coro in (
        api.create_export(api.ExportRequest(password="right"), AGENT),
        api.offboard(api.OffboardRequest(agent_id="a@b.test", successor_agent_id="c@b.test",
                                         reason="leaving", preview=True), AGENT),
        api.close_brokerage(api.ClosureRequest(confirm_name="B", reason="closing", password="right"), AGENT),
        api.subject_request(api.SubjectRequest(kind="dsr_access", email="p@x.test", reason="asked"), AGENT),
    ):
        with pytest.raises(HTTPException) as exc:
            run(coro)
        assert exc.value.status_code == 403
    assert not any(calls[k] for k in ("offboard", "closure", "export", "subject"))


def test_export_requires_the_password(calls):
    with pytest.raises(HTTPException) as exc:
        run(api.create_export(api.ExportRequest(password="wrong"), OWNER))
    assert exc.value.status_code == 403 and not calls["export"]
    assert run(api.create_export(api.ExportRequest(password="right"), OWNER)) == {"operation_id": "op"}


def test_offboarding_preview_needs_no_password_but_the_real_run_does(calls):
    body = dict(agent_id="a@b.test", successor_agent_id="c@b.test", reason="leaving")
    assert run(api.offboard(api.OffboardRequest(**body, preview=True), OWNER)) == {"preview": True}
    assert calls["password"] == []
    with pytest.raises(HTTPException):
        run(api.offboard(api.OffboardRequest(**body, preview=False), OWNER))       # no password
    with pytest.raises(HTTPException):
        run(api.offboard(api.OffboardRequest(**body, preview=False, password="wrong"), OWNER))
    assert len(calls["offboard"]) == 1
    run(api.offboard(api.OffboardRequest(**body, preview=False, password="right"), OWNER))
    assert calls["offboard"][-1]["preview"] is False


def test_closure_requires_the_password(calls):
    with pytest.raises(HTTPException):
        run(api.close_brokerage(api.ClosureRequest(confirm_name="B", reason="closing", password="wrong"), OWNER))
    assert not calls["closure"]
    run(api.close_brokerage(api.ClosureRequest(confirm_name="B", reason="closing", password="right"), OWNER))
    assert calls["closure"][0]["confirm_name"] == "B"


def test_subject_deletion_requires_the_password_but_access_and_preview_do_not(calls):
    run(api.subject_request(api.SubjectRequest(kind="dsr_access", email="p@x.test", reason="asked",
                                               preview=False), OWNER))
    run(api.subject_request(api.SubjectRequest(kind="dsr_delete", email="p@x.test", reason="asked",
                                               preview=True), OWNER))
    assert calls["password"] == []
    with pytest.raises(HTTPException):
        run(api.subject_request(api.SubjectRequest(kind="dsr_delete", email="p@x.test", reason="asked",
                                                   preview=False), OWNER))
    with pytest.raises(HTTPException) as exc:
        run(api.subject_request(api.SubjectRequest(kind="dsr_access", reason="asked"), OWNER))
    assert exc.value.status_code == 422


def test_lifecycle_errors_become_their_status_codes(monkeypatch, calls):
    import privacy_lifecycle

    async def refuse(ctx, **kw):
        raise privacy_lifecycle.LifecycleError("This is the brokerage's last owner.", status_code=409)

    monkeypatch.setattr(privacy_lifecycle, "offboard_agent", refuse)
    with pytest.raises(HTTPException) as exc:
        run(api.offboard(api.OffboardRequest(agent_id="a@b.test", successor_agent_id="c@b.test",
                                             reason="leaving", preview=True), OWNER))
    assert exc.value.status_code == 409 and "last owner" in exc.value.detail


def test_bodies_reject_unknown_fields():
    with pytest.raises(Exception):
        api.ClosureRequest(confirm_name="B", reason="closing", password="x", erase_now=True)


def test_published_policy_lists_every_category():
    from retention_policy import RetentionCategory

    out = run(api.retention_policy(AGENT))
    assert {c["category"] for c in out["categories"]} == {c.value for c in RetentionCategory}
    assert out["backup_days"] == 7


def test_admin_routes_are_platform_only():
    import inspect

    for route in api.admin_router.routes:
        params = inspect.signature(route.endpoint).parameters
        dep = params["ctx"].default
        assert getattr(dep, "dependency", None).__name__ == "require_platform_admin", route.path
