"""Hosted calls outside a Neoh chat turn are held and metered under the
brokerage's AI token budget (T18.07, AC18.06), against real PostgreSQL with
FORCE RLS, as the application login (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

A scripted provider, no network. A route's unit of work (the CMA seam) and a
phone-call reply each settle their reported usage to the brokerage that pays,
under keys that name the feature and never the caller's number; the other
brokerage sees none of it. With a cap the next phone-call reply is refused
before the provider sees it and holds nothing, while another brokerage is not
stopped by it. A call that answered is metered and its hold dropped even when
its caller's deadline cancels the wait mid-settle. With no gateway the direct
Fireworks path is one held request metered by its own usage, and a paused
brokerage's is refused before it is sent. A walkthrough whose budget stays
unreadable is backed off per job across two real worker loops, and its last
attempt logs the transcript without extraction under the code.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import urllib.request
import uuid
from types import SimpleNamespace

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

CALLER = "+13025550100"


def test_hosted_calls_outside_chat_are_held_and_metered_for_the_brokerage_that_pays():
    asyncio.run(_scenario())


def _reply(content: str, prompt: int, completion: int):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


class _Provider:
    """A scripted litellm: counts the calls that actually reached it."""

    def __init__(self):
        self.calls = 0
        self.reply = _reply("", 0, 0)

    async def acompletion(self, **_kwargs):
        self.calls += 1
        return self.reply


class _HTTPResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


async def _scenario():  # noqa: C901 — one scenario, read top to bottom
    import asyncpg

    import ai_budget
    import ai_chat_agent
    import llm_gateway
    from db import connection
    from llm_gateway import SpendRefused
    from ml_forge import bedrock_client
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=2, max_size=6)
    tenant, other = str(uuid.uuid4()), str(uuid.uuid4())

    def ctx(t: str, role: Role, who: str) -> TenantContext:
        return TenantContext(agent_id=f"{who}@{t[:8]}.test", tenant_id=t, role=role)

    owner, agent = ctx(tenant, Role.BROKER_OWNER, "owner"), ctx(tenant, Role.AGENT, "agent")
    other_owner, other_agent = ctx(other, Role.BROKER_OWNER, "owner"), ctx(other, Role.AGENT, "agent")

    async def usage_keys(t: str) -> set[str]:
        return {r["idempotency_key"] for r in await admin.fetch(
            "SELECT idempotency_key FROM billing_usage_events WHERE tenant_id=$1", uuid.UUID(t))}

    async def held(t: str) -> int:
        return await admin.fetchval("SELECT count(*) FROM ai_token_reservations WHERE tenant_id=$1",
                                    uuid.UUID(t))

    async def settled() -> None:
        """Wait for the settles a phone-call reply leaves in the background."""
        while llm_gateway._METERING:
            await asyncio.gather(*list(llm_gateway._METERING), return_exceptions=True)
            await asyncio.sleep(0)

    fake = _Provider()
    sent: list = []

    def urlopen(request, timeout=None):
        sent.append(json.loads(request.data))
        return _HTTPResponse(json.dumps({"choices": [{"message": {"content": "drafted"}}],
                                         "usage": {"prompt_tokens": 40, "completion_tokens": 8}}).encode())

    saved = {
        (llm_gateway, "_litellm"): llm_gateway._litellm,
        (bedrock_client, "_gateway_enabled"): bedrock_client._gateway_enabled,
        (bedrock_client, "FIREWORKS_ENABLED"): bedrock_client.FIREWORKS_ENABLED,
        (ai_chat_agent, "_gateway_chat_providers"): ai_chat_agent._gateway_chat_providers,
        (urllib.request, "urlopen"): urllib.request.urlopen,
    }
    env = {name: os.environ.get(name) for name in
           ("ORACLE_FIREWORKS_API_KEY", "ORACLE_AI_BEDROCK_FALLBACK", "AZURE_AI_API_KEY",
            "ORACLE_FOUNDRY_API_KEY", "ORACLE_FOUNDRY_PROJECT_ENDPOINT")}
    llm_gateway._litellm = lambda: fake
    bedrock_client._gateway_enabled = lambda: True
    ai_chat_agent._gateway_chat_providers = lambda: [
        llm_gateway.Provider(name="fireworks", model="fireworks_ai/test")]
    urllib.request.urlopen = urlopen
    for name in env:
        os.environ.pop(name, None)
    os.environ["ORACLE_FIREWORKS_API_KEY"] = "fw-live-test"
    try:
        for t, name in ((tenant, "Hosted"), (other, "Other")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                                uuid.UUID(t), f"aih-{t[:8]}", name)

        # ── a route's unit of work: held, then metered for its brokerage ────
        fake.reply = _reply("## Retail value", 900, 120)
        async with ai_budget.spending(agent, "cma"):
            text = await bedrock_client.invoke_async(bedrock_client.PRIMARY_MODEL, "facts " * 80, 300)
        assert text == "## Retail value" and fake.calls == 1
        keys = await usage_keys(tenant)
        assert len(keys) == 2
        assert all(re.fullmatch(r"cma:[0-9a-f]{32}:fireworks:0:(prompt|completion)", k) for k in keys), keys
        assert (await ai_budget.state(owner))["used"]["total"] == 1020
        assert (await ai_budget.state(other_owner))["used"]["total"] == 0
        assert await held(tenant) == 0

        # ── a phone-call reply: the call's brokerage pays, by a service actor ─
        fake.reply = _reply("At four.", 300, 20)
        assert await ai_chat_agent._generate_voice_reply(CALLER, "what time is it?", tenant_id=tenant) \
            == "At four."
        # Said without waiting for its settle, which still commits.
        await settled()
        keys = await usage_keys(tenant)
        voice = sorted(k for k in keys if k.startswith("voice:"))
        assert len(voice) == 2 and all(re.fullmatch(r"voice:[0-9a-f]{32}:fireworks:0:\w+", k) for k in voice)
        assert not any("5550100" in k for k in keys), "a call key is stored: never the caller's number"
        assert (await ai_budget.state(owner))["used"]["total"] == 1340

        # ── capped: the next reply is refused before the provider sees it ───
        await ai_budget.set_budget(owner, cap=1350, warn_pct=80, paused=False)
        calls = fake.calls
        with pytest.raises(SpendRefused) as refused:
            await ai_chat_agent._generate_voice_reply(CALLER, "and the price?", tenant_id=tenant)
        assert refused.value.code == "AI_BUDGET_INSUFFICIENT" and fake.calls == calls
        assert await held(tenant) == 0 and (await ai_budget.state(owner))["used"]["total"] == 1340
        # Another brokerage is not stopped by it.
        fake.reply = _reply("## Other", 10, 5)
        async with ai_budget.spending(other_agent, "cma"):
            assert await bedrock_client.invoke_async(bedrock_client.PRIMARY_MODEL, "x", 300) == "## Other"
        assert fake.calls == calls + 1

        # ── the caller's deadline fires mid-settle: the answer is still metered
        # (it used to roll the usage row back and leave the hold to lapse) ──
        fake.reply = _reply("## Late", 70, 7)
        # A cap, so the call holds a real reservation row for its settle to drop.
        await ai_budget.set_budget(other_owner, cap=1_000_000, warn_pct=80, paused=False)
        real_settle, settling = ai_budget.settle, asyncio.Event()

        async def settle_signalled(*args):
            settling.set()
            await real_settle(*args)

        async def drafted_late():
            async with ai_budget.spending(other_agent, "comms_draft"):
                return await bedrock_client.invoke_async(bedrock_client.PRIMARY_MODEL, "late", 300)

        ai_budget.settle = settle_signalled
        try:
            task = asyncio.ensure_future(drafted_late())
            await settling.wait()
            assert await held(other) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await settled()
        finally:
            ai_budget.settle = real_settle
        late = {k for k in await usage_keys(other) if k.startswith("comms_draft:")}
        assert len(late) == 2 and all(k.split(":")[2:4] == ["fireworks", "0"] for k in late)
        assert (await ai_budget.state(other_owner))["used"]["total"] == 15 + 77
        assert await held(other) == 0

        # ── no gateway: one held direct request, metered by its own usage ───
        bedrock_client._gateway_enabled = lambda: False
        bedrock_client.FIREWORKS_ENABLED = True
        async with ai_budget.spending(other_agent, "comms_draft"):
            assert await bedrock_client.invoke_async(bedrock_client.PRIMARY_MODEL, "draft it", 600) == "drafted"
        assert len(sent) == 1
        drafted = {k for k in await usage_keys(other) if k.startswith("comms_draft:")} - late
        assert len(drafted) == 2 and all(k.split(":")[2:4] == ["fireworks", "0"] for k in drafted)
        assert (await ai_budget.state(other_owner))["used"]["total"] == 15 + 77 + 48
        assert await held(other) == 0

        # ── paused: refused before anything is sent ─────────────────────────
        await ai_budget.set_budget(other_owner, cap=None, warn_pct=80, paused=True)
        with pytest.raises(SpendRefused) as refused:
            async with ai_budget.spending(other_agent, "comms_draft"):
                await bedrock_client.invoke_async(bedrock_client.PRIMARY_MODEL, "draft it", 600)
        assert refused.value.code == "AI_SPEND_PAUSED" and len(sent) == 1
    finally:
        for (module, name), value in saved.items():
            setattr(module, name, value)
        for name, value in env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        await admin.execute("SET session_replication_role = replica")
        try:
            for t in (tenant, other):
                for table in ("ai_token_reservations", "billing_usage_events", "tenant_action_budgets"):
                    await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", t)
                await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", t)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()


def test_a_walkthrough_whose_budget_stays_unreadable_is_backed_off_per_job_and_never_dropped(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "live-test-walkthrough-master-key-0123456789")
    asyncio.run(_walkthrough_backed_off())


async def _walkthrough_backed_off():  # noqa: C901 — one scenario, read top to bottom
    """Two real worker loops (ORACLE_VOICE_WORKERS=2) against the real claim.

    The back-off used to be each worker's own sleep: while one slept, the
    other re-claimed the job at once, so its three attempts went in about one
    back-off and it ended 'failed' with its audio released and its
    transcript never logged. Now the job carries the back-off (its claim
    waits it out, whichever worker asks), and its last attempt logs the
    transcript without extraction under the budget's code."""
    import asyncpg

    import ai_budget
    import llm_gateway
    import voice_intel
    from db import connection

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=2, max_size=6)
    tenant, lead, job = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    loop = asyncio.get_running_loop()
    attempted: list[float] = []
    sent: list = []
    frames: list = []

    async def unreadable(*_args, **_kwargs):
        raise ConnectionError("budget tables unreachable")

    async def provider():
        sent.append(1)
        return None

    async def invoke(_model_id, _prompt, _max_tokens=2048, *, system=""):
        # The extraction's hosted call, held under its scope like any other.
        attempted.append(loop.time())
        return await llm_gateway.spend_guarded("fireworks", 50, provider)

    async def broadcast(_tenant, frame, **_kwargs):
        frames.append(frame)
        return 0

    saved = {name: getattr(voice_intel, name) for name in
             ("BUDGET_RETRY_SECONDS", "POLL_SECONDS", "invoke_async", "_transcribe")}
    saved_reserve, saved_broadcast = ai_budget._reserve, voice_intel.ws_hub.broadcast_record
    voice_intel.BUDGET_RETRY_SECONDS = 1.0
    voice_intel.POLL_SECONDS = 0.05
    voice_intel.invoke_async = invoke
    voice_intel._transcribe = lambda _path: "Walked the house, the roof needs work."
    voice_intel.ws_hub.broadcast_record = broadcast
    ai_budget._reserve = unreadable

    async def job_row():
        return await admin.fetchrow(
            "SELECT status, attempts, audio, error FROM voice_walkthrough_jobs WHERE id=$1", job)

    workers: list = []
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                            tenant, f"aiw-{tenant.hex[:8]}", "Walkthrough")
        await admin.execute(
            "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, address) "
            "VALUES ($1,$2,$3,'DE',50,'3 Walk Way')", lead, tenant, f"WALK-{lead.hex[:6]}")
        await admin.execute(
            """INSERT INTO voice_walkthrough_jobs
                   (id, tenant_id, lead_id, created_by, original_filename, suffix, audio, audio_bytes)
               VALUES ($1, $2, $3, 'agent@walk.test', 'walk.wav', '.wav', $4, 5)""",
            job, tenant, lead, b"audio")
        workers = [asyncio.ensure_future(voice_intel._worker_loop(n)) for n in range(2)]

        # Well inside one back-off: one attempt, queued again with its audio.
        await asyncio.sleep(0.6)
        row = await job_row()
        assert row["attempts"] == 1, f"{row['attempts']} attempts inside one back-off"
        assert row["status"] == "queued" and bytes(row["audio"]) == b"audio"
        assert row["error"].startswith("AI_BUDGET_UNAVAILABLE")

        deadline = loop.time() + 15
        while (await job_row())["status"] != "failed":
            assert loop.time() < deadline, "the walkthrough never finished"
            await asyncio.sleep(0.05)
        row = await job_row()
        assert row["attempts"] == voice_intel.MAX_ATTEMPTS == len(attempted)
        gaps = [later - earlier for earlier, later in zip(attempted, attempted[1:])]
        assert all(gap >= 0.9 for gap in gaps), f"attempts not backed off per job: {gaps}"
        assert row["audio"] is None and row["error"].startswith("AI_BUDGET_UNAVAILABLE")
        assert sent == [], "a model was asked with the budget unreadable"

        # The agent's own words were logged, without extraction, under the code.
        logs = await admin.fetch(
            "SELECT payload, content_ciphertext FROM interaction_logs WHERE lead_id=$1 "
            "AND interaction_type='voice_note'", lead)
        assert len(logs) == 1, "the transcript was dropped"
        payload = json.loads(logs[0]["payload"])
        assert payload["extraction_code"] == "AI_BUDGET_UNAVAILABLE"
        assert "transcript" not in payload and logs[0]["content_ciphertext"] is not None
        assert frames and frames[-1]["extraction_code"] == "AI_BUDGET_UNAVAILABLE"
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        for name, value in saved.items():
            setattr(voice_intel, name, value)
        ai_budget._reserve, voice_intel.ws_hub.broadcast_record = saved_reserve, saved_broadcast
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("interaction_logs", "voice_walkthrough_jobs", "leads"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1", tenant)
            await admin.execute("DELETE FROM tenants WHERE id = $1", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
