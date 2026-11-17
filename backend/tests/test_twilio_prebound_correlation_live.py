"""The command worker, driven for real against PostgreSQL, places a Twilio
call whose webhooks beat the create response (external audit 24.2).

Three cases the audit's remediation names:
- the answer webhook arrives while calls.create has not returned: the call is
  connected, not hung up as "unmanaged";
- provider submission is uncertain (Twilio placed the call, then the create
  request timed out): the call is still answered, its CallSid reaches the
  command through the correlation, and the reconciliation sweep can confirm
  it at Twilio instead of leaving it unknown;
- no usable Twilio account with a custom call URL configured: the custom call
  fallback still runs (the correlation write must not pre-empt it).

Redis is an in-memory fake; Twilio and the custom call API are fakes.
Skipped unless ORACLE_LIVE_DB_ADMIN_DSN is set (see test_privacy_lifecycle_live).
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from types import SimpleNamespace

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

ACCOUNT = "AC" + "2" * 32


class _Redis:
    def __init__(self):
        self.values = {}

    async def ping(self):
        return True

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        self.values.pop(key, None)


class _Request:
    def __init__(self, form, query):
        from starlette.datastructures import FormData

        self._form = FormData(form)
        self.headers = {"X-Twilio-Signature": "sig", "host": "internal.invalid"}
        self.query_params = dict(p.split("=", 1) for p in query.split("&") if p)
        self.url = SimpleNamespace(netloc="internal.invalid", scheme="https", query=query)

    async def form(self):
        return self._form


def test_the_worker_survives_webhooks_that_beat_the_create_response(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import billing
    import command_providers
    import commands_api as ca
    import inbound_voice
    import reconciliation
    import twilio_call_handler as tch
    from db import connection

    redis = _Redis()

    async def get_redis():
        return redis

    async def allowed(*_a, **_k):
        return SimpleNamespace(allowed=True, blockers=[])

    async def nothing(*_a, **_k):
        return None

    async def yes(*_a, **_k):
        return True

    async def caller_id(_ctx):
        return "+13025550100"

    async def route(_ctx):
        return {"provider": "twilio"}

    async def spoken(text, **_k):
        return f"<Say>{text}</Say>"

    monkeypatch.setattr(tch, "_get_redis", get_redis)
    monkeypatch.setattr(tch, "_STATE_WAIT_SECONDS", 0.001)
    monkeypatch.setattr(billing, "entitlement_refusal", nothing)
    monkeypatch.setattr(ca, "_requester_may_contact", yes)
    monkeypatch.setattr(ca, "guard_outreach", allowed)
    monkeypatch.setattr(ca, "_load_provider_credential", nothing)
    monkeypatch.setattr(ca, "_validate_twilio_webhook_signature", lambda *a, **k: None)
    monkeypatch.setattr(ca, "_spoken", spoken)
    monkeypatch.setattr(inbound_voice, "get_verified_caller_id", caller_id)
    monkeypatch.setattr(inbound_voice, "get_telephony_route", route)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT)
    monkeypatch.setenv("ORACLE_TWILIO_QWEN_REALTIME_ENABLED", "false")

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner = f"owner-{s}@live.test"
    client = str(uuid.uuid4())
    answers = []

    async def answer(call_sid, correlation):
        response = await ca.twilio_webhook(_Request({"CallSid": call_sid, "To": "+13025550142"},
                                                    f"neoh_ref={correlation}"))
        answers.append(response.body)

    class Reporter:
        job = {"tenant_id": T, "created_by": owner}

        async def progress(self, *_a, **_k):
            return None

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)

    async def command(key):
        return str(await admin.fetchval(
            "INSERT INTO command_executions (tenant_id, command_type, classification, risk_class, target, "
            "draft, state, idempotency_key, created_by) "
            "VALUES ($1,'CALL','external','outreach',$2::jsonb,'{}'::jsonb,'queued',$3,$4) RETURNING id",
            T, json.dumps({"phone": "+13025550142", "client_id": client}), f"call-{s}-{key}", owner))

    async def run(command_id):
        return await ca._execute_command_job(
            {"command_type": "CALL", "command_id": command_id,
             "target": {"phone": "+13025550142", "client_id": client}, "draft": {"reason": "follow up"}},
            Reporter())

    async def row(command_id):
        return await admin.fetchrow("SELECT state, provider, provider_reference FROM command_executions "
                                    "WHERE id=$1::uuid", command_id)

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"tw2-{s}", f"B {s}")
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                            "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name) VALUES ($1,$2,$3)", client, T, f"X {s}")

        # 1. The answer arrives while calls.create has not returned.
        sid = "CA" + secrets.token_hex(16)

        async def answered_before_ack(draft, *, credentials=None, correlation=""):
            assert correlation and tch._correlation_key(correlation) in redis.values   # written first
            await answer(sid, correlation)
            return command_providers.ProviderResult("twilio", sid, "queued", {})

        monkeypatch.setattr(ca, "place_twilio_call", answered_before_ack)
        first = await command("early")
        await run(first)
        assert b"cannot be connected safely" not in answers[-1]
        assert tuple(await row(first)) == ("succeeded", "twilio", sid)
        assert await admin.fetchval("SELECT count(*) FROM live_call_sessions WHERE provider_call_id=$1", sid) == 1
        assert json.loads(redis.values[tch._state_key(sid)])["via"] == "correlation"   # the worker kept it

        # 2. Twilio placed the call, then the create request timed out.
        sid2 = "CA" + secrets.token_hex(16)
        seen = {}

        async def placed_then_timed_out(draft, *, credentials=None, correlation=""):
            seen["correlation"] = correlation
            await answer(sid2, correlation)
            raise command_providers.ProviderRequestError("Twilio call request timed out")

        monkeypatch.setattr(ca, "place_twilio_call", placed_then_timed_out)
        second = await command("uncertain")
        result = await run(second)
        assert result["status"] == "reconciliation_required"
        assert b"cannot be connected safely" not in answers[-1]           # the customer was not hung up on
        assert tuple(await row(second)) == ("reconciliation_required", "twilio", sid2)   # linked by the answer
        status = await ca.twilio_status_webhook(_Request(
            {"CallSid": sid2, "CallStatus": "completed", "CallDuration": "30"}, f"neoh_ref={seen['correlation']}"))
        assert status.status_code == 204
        stored = await admin.fetchrow("SELECT tenant_id::text AS t, call_aliases FROM provider_call_outcomes "
                                      "WHERE provider_call_id=$1", sid2)
        assert stored["t"] == T and seen["correlation"] in stored["call_aliases"]

        asked = []

        async def ask(r):
            asked.append(str(r["provider_reference"]))
            return "happened"

        monkeypatch.setattr(reconciliation, "_ask_provider", ask)
        await reconciliation._reconcile_commands(only_tenant=T)
        assert asked == [sid2]                                            # asked at Twilio by its CallSid
        assert (await row(second))["state"] == "succeeded"

        # 3. No usable Twilio account, custom call URL configured: the fallback runs.
        monkeypatch.delenv("TWILIO_ACCOUNT_SID")
        monkeypatch.setenv("ORACLE_CUSTOM_CALL_API_URL", "https://calls.example.test/place")

        async def must_not_place(*_a, **_k):
            raise AssertionError("Twilio must not be asked without an account")

        async def custom(draft):
            return command_providers.ProviderResult("custom_http", f"custom-{s}", "queued", {})

        monkeypatch.setattr(ca, "place_twilio_call", must_not_place)
        monkeypatch.setattr(ca, "place_custom_http_call", custom)
        third = await command("fallback")
        await run(third)
        assert tuple(await row(third)) == ("succeeded", "custom_http", f"custom-{s}")
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
