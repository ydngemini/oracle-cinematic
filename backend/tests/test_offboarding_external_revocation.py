"""Offboarding stays 'running' until the departed agent's Google grant is
confirmed revoked at Google (external audit 22.7).

It used to be created and finished 'succeeded' inside the transaction; the
revocation ran after commit, its failures were only appended to the result,
and nothing ever retried them. Now a durable `privacy:revoke_credentials` job
is queued in the same transaction, revokes only rows that are still disabled,
and on its final attempt fails the operation, emits
offboarding.external_cleanup_pending and marks the rows for the
external_cleanup alert. A reconnect cannot overwrite a row still waiting for
revocation (the token is the only handle on the old grant). Google is a stub
throughout.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import automation_jobs
import privacy_lifecycle as pl
from tenancy import Role, TenantContext

T = "00000000-0000-0000-0000-0000000000aa"
OWNER = TenantContext(agent_id="owner@x.test", tenant_id=T, role=Role.BROKER_OWNER)


def _events(caplog, name):
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "oracle.privacy.events" and json.loads(r.getMessage())["event"] == name]


# ── offboard_agent: running + a job queued in the same transaction ─────────

class _TxConn:
    def __init__(self, log):
        self.log = log

    async def fetchval(self, sql, *args):
        return "active"

    async def fetchrow(self, sql, *args):
        self.log.append(("fetchrow", sql, args))
        return {"id": "op-1"}

    async def execute(self, sql, *args):
        self.log.append(("execute", sql, args))
        return "UPDATE 1"


def _offboard(monkeypatch, *, credential_ids, settled=None, enqueue_error=None):
    log, state = [], {"in_tx": False}
    conn = _TxConn(log)

    @asynccontextmanager
    async def tx(_ctx):
        state["in_tx"] = True
        try:
            yield conn
        finally:
            state["in_tx"] = False

    plan = pl.OffboardPlan({"id": "u-a", "agent_id": "a@x.test", "role": "agent"},
                           {"id": "u-b", "agent_id": "b@x.test", "role": "agent"},
                           {"clients": 2}, {"command_executions": 1}, "none", len(credential_ids), [],
                           list(credential_ids))

    async def in_tx(*_a):
        return plan

    queued = []

    async def enqueue(c, ctx, **kw):
        if enqueue_error:
            raise enqueue_error
        queued.append({"conn": c, "in_tx": state["in_tx"], "ctx": ctx, **kw})
        return {"id": "job-1"}, True

    settles = []

    async def settle(ctx, op_id, ids, **kw):
        settles.append((ctx, op_id, list(ids), kw))
        return settled

    async def audit(*_a, **_k):
        return None

    monkeypatch.setattr(pl, "tenant_tx", tx)
    monkeypatch.setattr(pl, "_offboard_in_tx", in_tx)
    monkeypatch.setattr(automation_jobs, "enqueue_job_in_tx", enqueue)
    monkeypatch.setattr(pl, "settle_offboard_revocation", settle)
    monkeypatch.setattr(pl, "_audit", audit)
    out = asyncio.run(pl.offboard_agent(OWNER, departing_agent_id="a@x.test", successor_agent_id="b@x.test",
                                        reason="left", preview=False))
    return out, log, queued, settles


def test_a_failed_revocation_leaves_offboarding_running_with_a_job_and_says_pending(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="oracle.privacy.events")
    settled = {"state": "running", "external_revocation": "pending", "credentials_pending": 1,
               "credential_revocation": [{"credential_id": "c1", "provider": "google", "status": "revoke_failed"}]}
    before = datetime.now(timezone.utc)
    out, log, queued, settles = _offboard(monkeypatch, credential_ids=["c1"], settled=settled)

    insert = next(e for e in log if e[0] == "fetchrow" and "INSERT INTO privacy_operations" in e[1])
    assert insert[2][1:3] == ("offboard", "running")          # never created 'succeeded'
    finish = next(e for e in log if e[0] == "execute" and "UPDATE privacy_operations" in e[1])
    assert finish[2][1] == "running"
    assert json.loads(finish[2][2])["cleanup_pending"] == ["credential_revocation"]
    assert json.loads(finish[2][2])["access_disabled"] is True

    [job] = queued
    assert job["in_tx"] and job["conn"] is not None           # same transaction as the offboarding
    assert job["job_type"] == "privacy:revoke_credentials" == pl.JOB_REVOKE_CREDENTIALS
    assert job["payload"] == {"operation_id": "op-1", "credential_ids": ["c1"]}
    assert job["idempotency_key"] == "privacy:revoke-credentials:op-1"
    assert job["max_attempts"] == pl.REVOKE_MAX_ATTEMPTS and job["scheduled_at"] > before
    assert settles == [(OWNER, "op-1", ["c1"], {})]            # the inline attempt, not final

    assert out["state"] == "running" and out["external_revocation"] == "pending"
    assert out["credential_revocation"][0]["status"] == "revoke_failed"
    [event] = _events(caplog, "offboarding.completed")
    assert event["credential_revocations_failed"] == 1 and event["external_revocation_pending"] is True
    assert event["state"] == "running"


def test_an_unreadable_token_counts_as_a_failed_revocation_in_the_event(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="oracle.privacy.events")
    settled = {"state": "running", "external_revocation": "pending", "credentials_pending": 1,
               "credential_revocation": [{"credential_id": "c1", "provider": "google",
                                          "status": "revocation_unverified"}]}
    _offboard(monkeypatch, credential_ids=["c1"], settled=settled)
    assert _events(caplog, "offboarding.completed")[0]["credential_revocations_failed"] == 1


def test_a_confirmed_revocation_reports_complete(monkeypatch):
    settled = {"state": "succeeded", "external_revocation": "complete", "credentials_pending": 0,
               "credential_revocation": [{"credential_id": "c1", "provider": "google", "status": "revoked"}]}
    out, _log, queued, _ = _offboard(monkeypatch, credential_ids=["c1"], settled=settled)
    assert out["state"] == "succeeded" and out["external_revocation"] == "complete"
    assert len(queued) == 1   # queued anyway: it finds nothing left and stops


def test_no_personal_credentials_means_nothing_external_and_no_job(monkeypatch):
    out, log, queued, settles = _offboard(monkeypatch, credential_ids=[])
    insert = next(e for e in log if e[0] == "fetchrow" and "INSERT INTO privacy_operations" in e[1])
    assert insert[2][2] == "succeeded"
    assert queued == [] and settles == []
    assert out["external_revocation"] == "complete" and out["state"] == "succeeded"


def test_an_inline_attempt_that_raises_is_pending_not_an_error(monkeypatch):
    async def boom(*_a, **_k):
        raise ConnectionError("db blip")

    conn = _TxConn([])

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    plan = pl.OffboardPlan({"id": "u-a", "agent_id": "a@x.test", "role": "agent"},
                           {"id": "u-b", "agent_id": "b@x.test", "role": "agent"}, {}, {}, "none", 1, [], ["c1"])

    async def in_tx(*_a):
        return plan

    async def enqueue(*_a, **_k):
        return {"id": "j"}, True

    async def audit(*_a, **_k):
        return None

    monkeypatch.setattr(pl, "tenant_tx", tx)
    monkeypatch.setattr(pl, "_offboard_in_tx", in_tx)
    monkeypatch.setattr(automation_jobs, "enqueue_job_in_tx", enqueue)
    monkeypatch.setattr(pl, "settle_offboard_revocation", boom)
    monkeypatch.setattr(pl, "_audit", audit)
    out = asyncio.run(pl.offboard_agent(OWNER, departing_agent_id="a@x.test", successor_agent_id="b@x.test",
                                        reason="left", preview=False))
    assert out["external_revocation"] == "pending" and out["state"] == "running"


def test_a_job_that_cannot_be_queued_aborts_the_offboarding(monkeypatch):
    with pytest.raises(RuntimeError, match="queue down"):
        _offboard(monkeypatch, credential_ids=["c1"], enqueue_error=RuntimeError("queue down"))


# ── settle_offboard_revocation: the state machine ──────────────────────────

class _OpConn:
    def __init__(self, *, state="running", remaining=(), stored=None):
        self.op = {"state": state, "result": json.dumps(stored or {})}
        self.remaining = list(remaining)
        self.marked, self.finished, self.appended = [], None, None

    async def fetchrow(self, sql, *args):
        assert "FROM privacy_operations" in sql and "FOR UPDATE" in sql
        return self.op

    async def fetch(self, sql, *args):
        assert "disabled_at IS NOT NULL" in sql
        return [(i,) for i in self.remaining]

    async def execute(self, sql, *args):
        if sql.startswith("UPDATE provider_credentials"):
            self.marked.append((list(args[1]), args[2]))
        elif "SET state=$2" in sql:
            self.finished = {"state": args[1], "result": json.loads(args[2]), "error": args[4]}
        else:
            self.appended = json.loads(args[1])
        return "UPDATE 1"


def _settle(monkeypatch, conn, outcomes, *, final=False):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    calls = []

    async def revoke(ctx, **kw):
        calls.append(kw)
        return outcomes

    monkeypatch.setattr(pl, "tenant_tx", tx)
    monkeypatch.setattr(pl, "revoke_personal_credentials", revoke)
    out = asyncio.run(pl.settle_offboard_revocation(OWNER, "op-1", ["c1", "c2"], final_attempt=final))
    return out, calls


def test_everything_revoked_succeeds_the_offboarding(monkeypatch):
    conn = _OpConn(remaining=[])
    out, calls = _settle(monkeypatch, conn, [{"credential_id": "c1", "provider": "google", "status": "revoked"},
                                             {"credential_id": "c2", "provider": "smtp",
                                              "status": "deleted_no_remote_grant"}])
    assert calls == [{"tenant_id": T, "agent_id": None, "credential_ids": ["c1", "c2"]}]
    assert out["state"] == "succeeded" and out["external_revocation"] == "complete"
    assert conn.finished["state"] == "succeeded"
    assert conn.finished["result"]["cleanup_pending"] == [] and conn.finished["error"] is None
    assert conn.marked == []


def test_a_row_still_disabled_keeps_it_running_for_the_retry(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="oracle.privacy.events")
    conn = _OpConn(remaining=["c1"])
    out, _ = _settle(monkeypatch, conn, [{"credential_id": "c1", "provider": "google", "status": "revoke_failed"}])
    assert out["state"] == "running" and out["external_revocation"] == "pending"
    assert out["credentials_pending"] == 1
    assert conn.finished["state"] == "running"
    assert conn.finished["result"]["cleanup_pending"] == ["credential_revocation"]
    assert conn.marked == [] and not _events(caplog, "offboarding.external_cleanup_pending")


def test_the_final_attempt_fails_it_marks_the_rows_and_emits_the_event(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="oracle.privacy.events")
    conn = _OpConn(remaining=["c1"])
    out, _ = _settle(monkeypatch, conn, [{"credential_id": "c1", "provider": "google",
                                          "status": "revocation_unverified", "detail": "undecryptable"}],
                     final=True)
    assert out["state"] == "failed" and out["external_revocation"] == "failed"
    assert conn.finished["state"] == "failed"
    assert "third-party apps" in conn.finished["error"]
    assert conn.marked == [(["c1"], pl.REVOCATION_FAILED_MARK)]     # the external_cleanup alert counts these
    [event] = _events(caplog, "offboarding.external_cleanup_pending")
    assert event["operation_id"] == "op-1" and event["credential_revocations_failed"] == 1


def test_outcomes_merge_per_credential_across_attempts(monkeypatch):
    stored = {"credential_revocation": [{"credential_id": "c1", "provider": "google", "status": "revoke_failed"},
                                        {"credential_id": "c2", "provider": "google", "status": "revoked"}]}
    conn = _OpConn(remaining=[], stored=stored)
    _settle(monkeypatch, conn, [{"credential_id": "c1", "provider": "google", "status": "already_invalid"}])
    merged = {o["credential_id"]: o["status"] for o in conn.finished["result"]["credential_revocation"]}
    assert merged == {"c1": "already_invalid", "c2": "revoked"}


def test_a_finished_offboarding_is_never_reopened(monkeypatch):
    conn = _OpConn(state="succeeded", remaining=[], stored={"external_revocation": "complete"})
    out, _ = _settle(monkeypatch, conn, [], final=True)
    assert conn.finished is None and conn.marked == []
    assert conn.appended == {"credential_revocation": []}
    assert out["state"] == "succeeded" and out["external_revocation"] == "complete"


# ── the durable job ────────────────────────────────────────────────────────

class _Reporter:
    def __init__(self, attempt=1, max_attempts=12):
        self.job = {"id": "j1", "tenant_id": T, "attempt_count": attempt, "max_attempts": max_attempts}

    async def progress(self, *_a, **_k):
        return None


def _job(monkeypatch, settled, *, attempt=1, raises=None):
    seen, failed = [], []

    async def settle(ctx, op_id, ids, *, final_attempt):
        seen.append((ctx, op_id, list(ids), final_attempt))
        if raises:
            raise raises
        return settled

    async def fail(ctx, op_id, ids, error):
        failed.append((op_id, list(ids), error))

    monkeypatch.setattr(pl, "settle_offboard_revocation", settle)
    monkeypatch.setattr(pl, "_fail_offboard_revocation", fail)
    out = asyncio.run(pl._revoke_credentials_job({"operation_id": "op-1", "credential_ids": ["c1"]},
                                                 _Reporter(attempt=attempt)))
    return out, seen, failed


def test_the_job_retries_while_revocation_is_pending(monkeypatch):
    with pytest.raises(pl.CredentialRevocationPending):
        _job(monkeypatch, {"state": "running", "external_revocation": "pending", "credentials_pending": 1})


def test_the_job_runs_as_the_platform_in_the_jobs_tenant(monkeypatch):
    out, seen, _ = _job(monkeypatch, {"state": "succeeded", "external_revocation": "complete",
                                      "credentials_pending": 0})
    ctx, op_id, ids, final = seen[0]
    assert ctx.tenant_id == T and ctx.is_platform_admin and (op_id, ids, final) == ("op-1", ["c1"], False)
    assert out == {"operation_id": "op-1", "state": "succeeded", "external_revocation": "complete"}


def test_the_final_failure_ends_the_job_partial_not_dead(monkeypatch):
    out, seen, _ = _job(monkeypatch, {"state": "failed", "external_revocation": "failed",
                                      "credentials_pending": 1}, attempt=12)
    assert seen[0][3] is True and out["_terminal_state"] == "partial"


def test_the_final_attempt_failure_is_recorded_once(monkeypatch):
    failed = []

    async def settle(*_a, **_k):
        raise ConnectionError("db")

    async def fail(ctx, op_id, ids, error):
        failed.append((op_id, list(ids), error))

    monkeypatch.setattr(pl, "settle_offboard_revocation", settle)
    monkeypatch.setattr(pl, "_fail_offboard_revocation", fail)
    for attempt in (3, 12):
        with pytest.raises(ConnectionError):
            asyncio.run(pl._revoke_credentials_job({"operation_id": "op-1", "credential_ids": ["c1"]},
                                                   _Reporter(attempt=attempt)))
    assert failed == [("op-1", ["c1"], "credential revocation stopped on its final attempt: ConnectionError")]


def test_the_handler_is_registered_as_a_privacy_job():
    pl.register()
    assert pl.JOB_REVOKE_CREDENTIALS in automation_jobs.registered_handlers()
    assert pl.JOB_REVOKE_CREDENTIALS.startswith("privacy:")   # runs for a closing tenant, never cancelled


# ── a reconnect never overwrites a row waiting for revocation ──────────────

class _CallbackConn:
    def __init__(self, upserted):
        self.upserted, self.upsert_args = upserted, None

    async def fetchrow(self, sql, *args):
        if "oauth_authorization_states" in sql:
            return {"id": "s1", "tenant_id": T, "account_label": "a@x.test",
                    "code_verifier_ciphertext": b"v", "redirect_uri": "https://app.test/cb",
                    "return_path": "/settings", "scopes": ["openid"], "created_by": "a@x.test"}
        self.upsert_args = args
        assert "<> ALL(" in sql and "provider_credentials.validation_error" in sql
        return self.upserted


def _callback(monkeypatch, upserted):
    import commands_api

    conn = _CallbackConn(upserted)

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    revoked, recorded = [], []

    async def revoke(token):
        revoked.append(token)
        return "revoked"

    async def decrypt(*_a):
        return "verifier"

    async def encrypt(_c, value, _k):
        return b"enc:" + value.encode()

    async def exchange(**_kw):
        return {"access_token": "new-access-" + "a" * 4, "refresh_token": "new-refresh-" + "r" * 4,
                "expires_in": 3600, "scope": "openid"}

    async def record(**kw):
        recorded.append(kw)

    monkeypatch.setattr(commands_api, "tenant_tx", tx)
    monkeypatch.setattr(commands_api, "decrypt_pii", decrypt)
    monkeypatch.setattr(commands_api, "encrypt_pii", encrypt)
    monkeypatch.setattr(commands_api, "_provider_key", lambda _t: "k")
    monkeypatch.setattr(commands_api, "_google_oauth_settings", lambda: ("id", "secret", "https://app.test/cb"))
    monkeypatch.setattr(commands_api, "_exchange_google_oauth_code", exchange)
    monkeypatch.setattr(commands_api, "revoke_google_token", revoke)
    monkeypatch.setattr(commands_api.ledger, "record", record)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://app.test")
    state = "s" * 32
    binding = commands_api._oauth_browser_binding(hashlib.sha256(state.encode()).hexdigest())
    request = SimpleNamespace(cookies={commands_api._OAUTH_BINDING_COOKIE: binding})
    response = asyncio.run(commands_api.finish_google_oauth(request, state_token=state, code="c0de",
                                                            provider_error=None))
    return response, conn, revoked, recorded


def test_a_reconnect_while_revocation_is_pending_is_refused_and_its_new_grant_revoked(monkeypatch):
    response, conn, revoked, recorded = _callback(monkeypatch, upserted=None)
    assert "google=revocation_pending" in response.headers["location"]
    assert conn.upsert_args[-1] == list(pl.REVOCATION_MARKS)
    assert revoked == ["new-refresh-rrrr"]       # the unstored grant is not left at Google
    assert recorded == []                        # nothing was connected


def test_an_ordinary_reconnect_still_connects(monkeypatch):
    response, _conn, revoked, recorded = _callback(monkeypatch, upserted={"id": "cred-1"})
    assert "google=connected" in response.headers["location"]
    assert revoked == [] and recorded[0]["action"] == "google_oauth_connected"


def test_storing_a_credential_over_a_row_waiting_for_revocation_is_refused(monkeypatch):
    import commands_api

    class Conn:
        async def fetchrow(self, sql, *args):
            assert "<> ALL($9::text[])" in sql and args[-1] == list(pl.REVOCATION_MARKS)
            return None

    @asynccontextmanager
    async def tx(_ctx):
        yield Conn()

    async def encrypt(_c, value, _k):
        return b"enc"

    monkeypatch.setattr(commands_api, "tenant_tx", tx)
    monkeypatch.setattr(commands_api, "encrypt_pii", encrypt)
    monkeypatch.setattr(commands_api, "_provider_key", lambda _t: "k")
    monkeypatch.setattr(commands_api, "require_feature", lambda *_a: None)
    agent = TenantContext(agent_id="a@x.test", tenant_id=T, role=Role.AGENT)
    body = commands_api.ProviderCredentialInput(account_label="a@x.test", token="t" * 12)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(commands_api.store_provider_credential("smtp", body, agent))
    assert exc.value.status_code == 409


def test_offboarding_disables_and_marks_the_rows_and_returns_their_ids():
    """The marker is what the reconnect guard and the alert read."""
    seen = []

    class Conn:
        async def fetch(self, sql, *args):
            if "FROM users" in sql:
                return [{"id": "u-a", "agent_id": "a@x.test", "role": "agent", "is_active": True},
                        {"id": "u-b", "agent_id": "b@x.test", "role": "agent", "is_active": True}]
            if "provider_credentials" in sql:
                seen.append((sql, args))
                return [("c1",), ("c2",)]
            return []

        async def fetchrow(self, sql, *args):
            return None          # no telephony route

        async def fetchval(self, sql, *args):
            return 0

        async def execute(self, sql, *args):
            return "UPDATE 0"

    plan = asyncio.run(pl._offboard_in_tx(Conn(), OWNER, "a@x.test", "b@x.test", "left"))
    assert plan.credential_ids == ["c1", "c2"] and plan.credentials == 2
    [(sql, args)] = seen
    assert "disabled_at=COALESCE(disabled_at, now())" in sql and "validation_error=$3" in sql
    assert args == (T, "a@x.test", pl.REVOCATION_PENDING_MARK)
    assert pl.REVOCATION_MARKS == ("revocation_pending", "revocation_failed")
