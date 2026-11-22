"""Offboarding revokes a departed agent's Google grant durably, against real
PostgreSQL (external audits 22.6, 22.7).

* 22.6: an undecryptable token is kept, never deleted as if revoked, and a
  corrupt row no longer costs the rows after it their revocation (each
  decrypt has its own savepoint; pgp_sym_decrypt runs in the database, so one
  failure used to abort the shared transaction).
* 22.7: the offboarding stays 'running' with a privacy:revoke_credentials job
  queued in the same transaction until Google confirms; a reconnect cannot
  overwrite a row waiting for revocation; the job's final failure fails the
  operation and raises the external_cleanup alert.

Google is a stub. Skipped unless ORACLE_LIVE_DB_ADMIN_DSN is set (see
test_privacy_lifecycle_live).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

MASTER = "e" * 64


def test_offboarding_revocation_against_real_postgres(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="oracle.privacy.events")
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    asyncio.run(_scenario(monkeypatch))
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "oracle.privacy.events"]
    assert any(e["event"] == "offboarding.external_cleanup_pending" for e in events), events
    first = next(e for e in events if e["event"] == "offboarding.completed")
    assert first["credential_revocations_failed"] == 1 and first["external_revocation_pending"] is True


class _Reporter:
    def __init__(self, job: dict):
        self.job = job

    async def progress(self, *_a, **_k):
        return None


async def _connect(admin, monkeypatch, tenant: str, agent: str, key: str) -> str:
    """Finish a Google connect for `agent` through the real callback: an
    unconsumed state row like oauth/start writes, Google's token exchange
    stubbed. Returns the redirect location."""
    import hashlib
    from types import SimpleNamespace

    import commands_api

    state = secrets.token_urlsafe(32)
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    await admin.execute(
        "INSERT INTO oauth_authorization_states (tenant_id, provider, state_hash, account_label, "
        "code_verifier_ciphertext, redirect_uri, return_path, scopes, expires_at, created_by) "
        "VALUES ($1,'google',$2,$3, pgp_sym_encrypt('verifier', $4), 'https://app.test/cb', '/', "
        "'{openid}', now() + interval '10 minutes', $3)", tenant, state_hash, agent, key)

    async def exchange(**_kw):
        return {"access_token": "fresh-access", "refresh_token": "fresh-refresh", "expires_in": 3600,
                "scope": "openid"}

    monkeypatch.setattr(commands_api, "_google_oauth_settings", lambda: ("id", "secret", "https://app.test/cb"))
    monkeypatch.setattr(commands_api, "_exchange_google_oauth_code", exchange)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://app.test")
    request = SimpleNamespace(cookies={commands_api._OAUTH_BINDING_COOKIE:
                                       commands_api._oauth_browser_binding(state_hash)})
    response = await commands_api.finish_google_oauth(request, state_token=state, code="c0de",
                                                      provider_error=None)
    return response.headers["location"]


async def _scenario(monkeypatch):
    import asyncpg

    import commands_api
    import component_health
    import privacy_lifecycle as pl
    from crypto import derive_tenant_key
    from db import connection
    from fastapi import HTTPException
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner, a, b, c, d = (f"{who}-{s}@live.test" for who in ("owner", "a", "b", "c", "d"))
    key = derive_tenant_key(T, MASTER)
    google = {"up": False, "seen": []}

    async def fake_revoke(token):
        google["seen"].append(token)
        if not google["up"]:
            raise TimeoutError("oauth2.googleapis.com timed out")
        return "revoked"

    monkeypatch.setattr(commands_api, "revoke_google_token", fake_revoke)
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                            T, f"revoke-{s}", f"Brokerage {s}")
        for agent, role in ((owner, "broker_owner"), (a, "agent"), (b, "agent"), (c, "agent"), (d, "agent")):
            await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                                "VALUES ($1,$2,$3,$2,'scrypt$x$y')", T, agent, role)

        async def seed(label, refresh, *, cred_id=None, enc_key=key, disabled=False):
            return str(await admin.fetchval(
                "INSERT INTO provider_credentials (id, tenant_id, provider, account_label, token_ciphertext, "
                "refresh_ciphertext, created_by, disabled_at) VALUES "
                "($1, $2, 'google', $3, pgp_sym_encrypt('access', $5), pgp_sym_encrypt($4, $5), $3, "
                " CASE WHEN $6 THEN now() END) RETURNING id",
                cred_id or str(uuid.uuid4()), T, label, refresh, enc_key, disabled))

        # ── 22.6: a corrupt row first, a good row after it ────────────────
        # The ids, labels and insertion order all put the corrupt row first
        # whichever index or scan the planner picks.
        bad = await seed(f"0-corrupt-{s}", "refresh-bad", cred_id="00000000" + str(uuid.uuid4())[8:],
                         enc_key="another-key", disabled=True)
        good = await seed(f"1-good-{s}", "refresh-good", cred_id="ffffffff" + str(uuid.uuid4())[8:],
                          disabled=True)
        google["up"] = True
        pctx = TenantContext(agent_id="privacy-offboarding", tenant_id=T, role=Role.PLATFORM_ADMIN)
        out = await pl.revoke_personal_credentials(pctx, tenant_id=T, agent_id=None, credential_ids=[bad, good])
        assert [(o["credential_id"], o["status"]) for o in out] == [
            (bad, "revocation_unverified"), (good, "revoked")], out
        assert google["seen"] == ["refresh-good"]              # read after the corrupt row failed
        assert await admin.fetchval("SELECT count(*) FROM provider_credentials WHERE id=$1", bad) == 1
        assert await admin.fetchval("SELECT count(*) FROM provider_credentials WHERE id=$1", good) == 0
        await admin.execute("DELETE FROM provider_credentials WHERE id=$1", bad)
        # A listed id that is no longer disabled (re-enabled: a new grant) is
        # never revoked or deleted (the scoped SELECT's disabled_at filter).
        live_b = await seed(b, "refresh-b-live")
        assert await pl.revoke_personal_credentials(pctx, tenant_id=T, agent_id=None,
                                                    credential_ids=[live_b]) == []
        assert "refresh-b-live" not in google["seen"]
        assert await admin.fetchval("SELECT count(*) FROM provider_credentials WHERE id=$1", live_b) == 1
        google["up"], google["seen"] = False, []

        # ── 22.7: Google down → running, job queued, row marked ───────────
        cred_a = await seed(a, "refresh-a")
        octx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)
        done = await pl.offboard_agent(octx, departing_agent_id=a, successor_agent_id=b,
                                       reason="leaving", preview=False)
        assert done["state"] == "running" and done["external_revocation"] == "pending"
        assert done["credential_revocation"][0]["status"] == "revoke_failed"
        op = await admin.fetchrow("SELECT state, result FROM privacy_operations WHERE id=$1", done["operation_id"])
        assert op["state"] == "running"
        assert json.loads(op["result"])["cleanup_pending"] == ["credential_revocation"]
        cred = await admin.fetchrow("SELECT disabled_at, validation_error FROM provider_credentials WHERE id=$1",
                                    cred_a)
        assert cred["disabled_at"] is not None and cred["validation_error"] == "revocation_pending"
        job = dict(await admin.fetchrow(
            "SELECT * FROM automation_jobs WHERE tenant_id=$1 AND job_type='privacy:revoke_credentials'", T))
        assert job["state"] == "queued" and job["max_attempts"] == pl.REVOKE_MAX_ATTEMPTS
        assert json.loads(job["payload"]) == {"operation_id": done["operation_id"], "credential_ids": [cred_a]}
        assert await admin.fetchval("SELECT scheduled_at > now() FROM automation_jobs WHERE id=$1", job["id"])

        # A reconnect (here the store route; the OAuth callback has the same
        # ON CONFLICT ... WHERE) cannot overwrite the row waiting for
        # revocation, even once the agent is reinstated (so the refusal is
        # the marker's, not the inactive-member check's).
        await admin.execute("UPDATE users SET is_active=true WHERE tenant_id=$1 AND agent_id=$2", T, a)
        body = commands_api.ProviderCredentialInput(account_label=a, token="t" * 12)
        monkeypatch.setattr(commands_api, "require_feature", lambda *_a: None)
        with pytest.raises(HTTPException) as refused:
            await commands_api.store_provider_credential("google", body, octx)
        assert refused.value.status_code == 409 and "still being removed" in refused.value.detail
        assert await admin.fetchval("SELECT validation_error FROM provider_credentials WHERE id=$1",
                                    cred_a) == "revocation_pending"

        # The job, attempt 2: Google still down → retried, still running.
        job["attempt_count"] = 2
        with pytest.raises(pl.CredentialRevocationPending):
            await pl._revoke_credentials_job(json.loads(job["payload"]), _Reporter(job))
        assert await admin.fetchval("SELECT state FROM privacy_operations WHERE id=$1",
                                    done["operation_id"]) == "running"
        # Attempt 3: Google is back → revoked, row deleted, offboarding succeeded.
        google["up"] = True
        job["attempt_count"] = 3
        result = await pl._revoke_credentials_job(json.loads(job["payload"]), _Reporter(job))
        assert result["state"] == "succeeded" and result["external_revocation"] == "complete"
        assert await admin.fetchval("SELECT count(*) FROM provider_credentials WHERE id=$1", cred_a) == 0
        op = await admin.fetchrow("SELECT state, result, completed_at FROM privacy_operations WHERE id=$1",
                                  done["operation_id"])
        stored = json.loads(op["result"])
        assert op["state"] == "succeeded" and op["completed_at"] is not None
        assert stored["cleanup_pending"] == [] and stored["credential_revocation"][0]["status"] == "revoked"
        # Now the (reinstated) account can connect again.
        stored_cred = await commands_api.store_provider_credential("google", body, octx)
        assert stored_cred["account_label"] == a

        # ── the final attempt fails it and raises the alert ───────────────
        google["up"] = False
        cred_c = await seed(c, "refresh-c")
        gone = await pl.offboard_agent(octx, departing_agent_id=c, successor_agent_id=b,
                                       reason="leaving", preview=False)
        cjob = dict(await admin.fetchrow(
            "SELECT * FROM automation_jobs WHERE tenant_id=$1 AND job_type='privacy:revoke_credentials' "
            "AND payload->>'operation_id' = $2", T, gone["operation_id"]))
        cjob["attempt_count"] = cjob["max_attempts"]
        result = await pl._revoke_credentials_job(json.loads(cjob["payload"]), _Reporter(cjob))
        assert result["_terminal_state"] == "partial" and result["state"] == "failed"
        op = await admin.fetchrow("SELECT state, error FROM privacy_operations WHERE id=$1", gone["operation_id"])
        assert op["state"] == "failed" and "third-party apps" in op["error"]
        cred = await admin.fetchrow("SELECT disabled_at, validation_error FROM provider_credentials WHERE id=$1",
                                    cred_c)
        assert cred["disabled_at"] is not None and cred["validation_error"] == "revocation_failed"
        health = (await component_health._db_components())["external_cleanup"]
        assert health["state"] == component_health.DEGRADED
        assert health["detail"]["credentials_unrevoked"] >= 1

        # ── a connect finishing after the agent was offboarded ───────────
        # D has no credential, so offboarding has no row to mark; a callback
        # (or a store) landing afterwards must not create an active one.
        await pl.offboard_agent(octx, departing_agent_id=d, successor_agent_id=b, reason="leaving", preview=False)
        with pytest.raises(HTTPException) as inactive:
            await commands_api.store_provider_credential(
                "google", commands_api.ProviderCredentialInput(account_label=d, token="t" * 12), octx)
        assert inactive.value.status_code == 409
        assert await admin.fetchval("SELECT count(*) FROM provider_credentials WHERE tenant_id=$1 "
                                    "AND account_label=$2", T, d) == 0
        google["up"], google["seen"] = True, []
        assert "member_inactive" in await _connect(admin, monkeypatch, T, d, key)
        assert await admin.fetchval("SELECT count(*) FROM provider_credentials WHERE tenant_id=$1 "
                                    "AND account_label=$2", T, d) == 0
        assert google["seen"] == ["fresh-refresh"]           # the unstored grant was revoked
        # an active agent's connect (the member lock under the agent's own
        # context) still works
        assert "google=connected" in await _connect(admin, monkeypatch, T, b, key)
        assert await admin.fetchval("SELECT disabled_at IS NULL AND validation_status='valid' "
                                    "FROM provider_credentials WHERE tenant_id=$1 AND account_label=$2", T, b)
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in await admin.fetch(
                    "SELECT c.table_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t USING (table_schema, table_name) "
                    "WHERE c.table_schema='public' AND c.column_name='tenant_id' "
                    "AND t.table_type='BASE TABLE' AND c.table_name <> 'tenants'"):
                await admin.execute(f'DELETE FROM "{table[0]}" WHERE tenant_id::text = $1', T)
            await admin.execute("DELETE FROM tenants WHERE id=$1", T)
        finally:
            await admin.execute("SET session_replication_role = origin")
        await admin.close()
        await connection.close_pool()
