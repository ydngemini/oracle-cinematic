"""Audit 30.3: the operator test-alert routes. The code goes only to the
recipient, never into a response or the audit ledger; a refused or unsent
test says so; a wrong code is counted (its transaction commits) before the
refusal is raised."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import admin_ops
import ops_alert_test
import ops_alerts

CTX = SimpleNamespace(agent_id="operator-1", tenant_id="00000000-0000-0000-0000-000000000000")


@pytest.fixture
def world(monkeypatch):
    w = SimpleNamespace(tx_exits=[], ledger=[], sent=[], finished=[], begin=None, confirm=None)

    @asynccontextmanager
    async def tenant_tx(ctx):
        try:
            yield object()
        except BaseException as exc:
            w.tx_exits.append(type(exc).__name__)
            raise
        w.tx_exits.append("commit")

    async def record(**kw):
        w.ledger.append(kw)

    async def begin(conn, requested_by, recipient):
        return w.begin

    async def finish(conn, row_id, error):
        w.finished.append((row_id, error))

    async def send_email(subject, body, *, to=None):
        w.sent.append((subject, body, to))
        return w.send_result

    async def confirm(conn, code, by, recipient):
        assert recipient == "ops@example.test", "a code is checked against today's recipient"
        return w.confirm

    monkeypatch.setattr(admin_ops, "tenant_tx", tenant_tx)
    monkeypatch.setattr(admin_ops.ledger, "record", record)
    monkeypatch.setattr(ops_alert_test, "begin", begin)
    monkeypatch.setattr(ops_alert_test, "finish", finish)
    monkeypatch.setattr(ops_alert_test, "confirm", confirm)
    monkeypatch.setattr(ops_alerts, "send_email", send_email)
    monkeypatch.setenv("ORACLE_ALERT_EMAIL", "ops@example.test")
    w.send_result = None
    return w


def test_a_sent_test_alert_carries_its_code_only_to_the_recipient(world):
    world.begin = ops_alert_test.Started(id=7, code="ABCD2345", recipient="ops@example.test")
    out = asyncio.run(admin_ops.send_alert_test(ctx=CTX))
    assert out == {"outcome": "sent", "error": None}
    [(subject, body, to)] = world.sent
    assert to == "ops@example.test" and "ABCD-2345" in body
    assert world.finished == [(7, None)]
    assert "ABCD" not in str(out) and "ABCD" not in str(world.ledger)


def test_an_unsent_or_refused_test_alert_says_so(world):
    world.begin = ops_alert_test.Started(id=8, code="ABCD2345", recipient="ops@example.test")
    world.send_result = "SmtpConfigurationError"
    assert asyncio.run(admin_ops.send_alert_test(ctx=CTX)) == {
        "outcome": "delivery_failed", "error": "SmtpConfigurationError"}
    assert world.finished == [(8, "SmtpConfigurationError")]

    world.sent.clear()
    world.begin = ops_alert_test.Started()
    out = asyncio.run(admin_ops.send_alert_test(ctx=CTX))
    assert out["outcome"] == "recipient_missing" and world.sent == []

    world.begin = ops_alert_test.Started(retry_after=42)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_ops.send_alert_test(ctx=CTX))
    assert exc.value.status_code == 429 and exc.value.headers["Retry-After"] == "42"


def test_a_wrong_code_is_counted_before_it_is_refused(world):
    world.confirm = {"result": "wrong_code", "attempts_left": 3}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_ops.confirm_alert_test(admin_ops.AlertTestConfirm(code="ZZZZ-ZZZZ"), ctx=CTX))
    assert exc.value.status_code == 400 and exc.value.detail["attempts_left"] == 3
    assert world.tx_exits == ["commit"], "the attempt counter must commit, not roll back with the refusal"
    world.confirm = {"result": "too_many_attempts"}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_ops.confirm_alert_test(admin_ops.AlertTestConfirm(code="ZZZZ-ZZZZ"), ctx=CTX))
    assert exc.value.status_code == 409
    world.confirm = {"result": "confirmed"}
    assert asyncio.run(admin_ops.confirm_alert_test(admin_ops.AlertTestConfirm(code="abcd-2345"), ctx=CTX)) == {
        "result": "confirmed"}
    assert "abcd" not in str(world.ledger).lower()


def test_the_routes_are_platform_admin_only():
    routes = {(r.path, tuple(sorted(r.methods))): r for r in admin_ops.router.routes}
    for key in (("/api/admin/alerts/test", ("GET",)), ("/api/admin/alerts/test", ("POST",)),
                ("/api/admin/alerts/test/confirm", ("POST",))):
        deps = [d.call for d in routes[key].dependant.dependencies]
        assert admin_ops.require_platform_admin in deps, key
