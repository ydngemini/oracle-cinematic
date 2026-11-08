"""A brokerage's AI token budget stops hosted model calls BEFORE the spend
(0170, T18.07, AC18.05, AC18.06), against real PostgreSQL with FORCE RLS, as
the application login (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Failure injection through the real gateway (a scripted provider, no network):
with no cap every call passes and is metered; with a cap the warning shows,
the call that would pass the cap is refused before the provider sees it (as
"insufficient" while room is left, never "used up"), admission refuses a turn
once less is left than its smallest request, a call its caller cuts off drops
its hold, one made in a worker thread keeps it until the thread returns and is
then metered, admission and the smallest real first call agree at the floor,
and the brokerage's pause, the platform switch and the deploy switch each
refuse.
Also pinned: concurrent reservations serialise so exactly the calls that fit
are admitted; a lapsed hold stops counting; an unreadable budget holds the
call; an agent's connection cannot write a budget (42501), the missions cap
included; a platform admin's read is its brokerage's numbers only.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from types import SimpleNamespace

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_the_ai_token_budget_stops_spend_before_it_happens():
    asyncio.run(_scenario())


def _usage(prompt: int, completion: int):
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
                           choices=[])


class _Provider:
    """A scripted litellm: counts the calls that actually reached it."""

    def __init__(self):
        self.calls = 0
        self.reply = _usage(0, 0)

    async def acompletion(self, **_kwargs):
        self.calls += 1
        return self.reply


async def _scenario():  # noqa: C901 — one scenario, read top to bottom
    import asyncpg

    import ai_budget
    import llm_gateway
    from db import connection
    from llm_gateway import SpendRefused, SpendUnavailable
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=2, max_size=10)
    tenant, other, race = (str(uuid.uuid4()) for _ in range(3))

    def ctx(t: str, role: Role, who: str) -> TenantContext:
        return TenantContext(agent_id=f"{who}@{t[:8]}.test", tenant_id=t, role=role)

    owner, agent = ctx(tenant, Role.BROKER_OWNER, "owner"), ctx(tenant, Role.AGENT, "agent")
    platform = ctx(tenant, Role.PLATFORM_ADMIN, "platform")
    other_owner = ctx(other, Role.BROKER_OWNER, "owner")

    async def held(t: str) -> int:
        return await admin.fetchval("SELECT count(*) FROM ai_token_reservations WHERE tenant_id=$1", uuid.UUID(t))

    fake = _Provider()
    real_litellm = llm_gateway._litellm
    llm_gateway._litellm = lambda: fake
    provider = llm_gateway.Provider(name="fireworks", model="fireworks_ai/test")
    messages = [{"role": "user", "content": "what needs me today? " * 40}]

    async def round_(scope):
        token = llm_gateway.spend_scope.set(scope)
        try:
            return await llm_gateway.tool_call(messages, [], provider=provider)
        finally:
            llm_gateway.spend_scope.reset(token)

    estimate = llm_gateway.estimate_tokens(list(messages), [], completion=4096)
    try:
        for t, name in ((tenant, "Budget"), (other, "Other"), (race, "Race")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                                uuid.UUID(t), f"aib-{t[:8]}", name)

        # ── no cap is the default: every call passes and is metered ─────────
        scope = ai_budget.SpendScope(agent, "chat:turn-1")
        fake.reply = _usage(1200, 300)
        await round_(scope)
        assert fake.calls == 1 and scope.state == "unlimited"
        assert await held(tenant) == 0, "no cap, nothing to hold against"
        state = await ai_budget.state(owner)
        assert state["cap"] is None and state["state"] == "unlimited"
        assert state["used"] == {"prompt": 1200, "completion": 300, "total": 1500}
        keys = {r["idempotency_key"] for r in await admin.fetch(
            "SELECT idempotency_key FROM billing_usage_events WHERE tenant_id=$1", uuid.UUID(tenant))}
        assert keys == {"chat:turn-1:fireworks:0:prompt", "chat:turn-1:fireworks:0:completion"}
        # The other brokerage sees none of it.
        assert (await ai_budget.state(other_owner))["used"]["total"] == 0

        # ── only an owner or platform admin may write a budget (AC18.05) ────
        with pytest.raises(asyncpg.PostgresError) as denied:
            async with connection.tenant_tx(agent) as conn:
                await conn.execute(
                    "INSERT INTO tenant_action_budgets (tenant_id, ai_tokens_monthly_cap) VALUES ($1::uuid, 5)",
                    tenant)
        assert getattr(denied.value, "sqlstate", "") == "42501"
        await ai_budget.set_budget(owner, cap=10**9, warn_pct=80, paused=False)
        for sql in ("UPDATE tenant_action_budgets SET ai_tokens_monthly_cap = NULL",
                    "UPDATE tenant_action_budgets SET monthly_cap_cents = 999999",
                    "UPDATE tenant_action_budgets SET ai_inference_paused = true"):
            with pytest.raises(asyncpg.PostgresError) as denied:
                async with connection.tenant_tx(agent) as conn:
                    await conn.execute(sql)
            assert getattr(denied.value, "sqlstate", "") == "42501", sql
        row = await admin.fetchrow("SELECT * FROM tenant_action_budgets WHERE tenant_id=$1", uuid.UUID(tenant))
        assert row["ai_tokens_monthly_cap"] == 10**9 and row["monthly_cap_cents"] == 0
        assert row["updated_by"] == owner.agent_id

        # ── warn, then the hard stop BEFORE the provider ─────────────────────
        # Each call reports about one estimate of usage, so the cap admits
        # exactly two calls: the second crosses warn_pct, the third is refused.
        used = 1500
        cap = used + int(estimate * 2.5)
        await ai_budget.set_budget(platform, cap=cap, warn_pct=60, paused=False)
        assert await admin.fetchval("SELECT updated_by FROM tenant_action_budgets WHERE tenant_id=$1",
                                    uuid.UUID(tenant)) == platform.agent_id
        fake.reply = _usage(estimate - 100, 50)
        scope = ai_budget.SpendScope(agent, "chat:turn-2")
        await round_(scope)
        assert scope.state == "ok" and await held(tenant) == 0, "settled holds are dropped"
        await round_(scope)
        assert scope.state == "warning"
        assert (await ai_budget.state(owner))["state"] == "warning"
        with pytest.raises(SpendRefused) as refused:
            await round_(scope)
        # Room is left, only less than this call needs: said so, never "used up".
        assert refused.value.state == "insufficient" and refused.value.code == "AI_BUDGET_INSUFFICIENT"
        assert "broker" in refused.value.message                      # an agent is told to ask
        assert "used up" not in refused.value.message
        assert fake.calls == 3, "the refused call must never reach the provider"
        assert await held(tenant) == 0
        spent = (await ai_budget.state(owner))["used"]["total"]
        assert spent == used + 2 * (estimate - 50) <= cap
        # A new turn is admitted only while the smallest request still fits.
        room = cap - spent
        assert await ai_budget.admission(agent, floor=room) == "warning"
        with pytest.raises(SpendRefused) as refused:
            await ai_budget.admission(agent, floor=room + 1)
        assert refused.value.code == "AI_BUDGET_INSUFFICIENT"
        assert (await ai_budget.state(owner, floor=room + 1))["state"] == "insufficient"

        # ── a call cut off by its caller's deadline drops its hold ───────────
        await ai_budget.set_budget(owner, cap=10**9, warn_pct=80, paused=False)
        stalled = asyncio.Event()

        async def stall(**_kwargs):
            stalled.set()
            await asyncio.sleep(30)

        fake.acompletion = stall
        try:
            cut = asyncio.create_task(round_(ai_budget.SpendScope(agent, "chat:turn-cut")))
            await asyncio.wait_for(stalled.wait(), timeout=10)
            assert await held(tenant) == 1, "held while the call is in flight"
            cut.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cut
        finally:
            del fake.acompletion
        assert await held(tenant) == 0, "a cut-off call keeps no hold"
        assert (await ai_budget.state(owner))["used"]["total"] == spent, "and records no usage"

        # ── a thread-backed call cut off keeps its hold until it returns ─────
        # The Foundry SDK and boto3 run in a worker thread that a deadline
        # cannot stop: the provider still answers, and bills it.
        started, finish = threading.Event(), threading.Event()

        def converse(*_args):
            started.set()
            finish.wait(10)
            return {"usage": {"inputTokens": 700, "outputTokens": 30}}

        token = llm_gateway.spend_scope.set(ai_budget.SpendScope(agent, "chat:turn-thread"))
        try:
            cut = asyncio.ensure_future(llm_gateway.spend_guarded_thread("bedrock", 2_000, converse))
        finally:
            llm_gateway.spend_scope.reset(token)
        assert await asyncio.to_thread(started.wait, 10)
        cut.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cut
        assert await held(tenant) == 1, "held while its thread still runs"
        finish.set()
        for _ in range(500):
            if await held(tenant) == 0:
                break
            await asyncio.sleep(0.02)
        assert await held(tenant) == 0, "settled when its thread returned"
        spent += 730
        assert (await ai_budget.state(owner))["used"]["total"] == spent, "and metered"

        # ── the brokerage's pause, the platform switch, the deploy switch ────
        await ai_budget.set_budget(owner, cap=None, warn_pct=80, paused=True)
        with pytest.raises(SpendRefused) as refused:
            await round_(ai_budget.SpendScope(agent, "chat:turn-3"))
        assert refused.value.code == "AI_SPEND_PAUSED"
        with pytest.raises(SpendRefused):
            await ai_budget.admission(agent)
        await ai_budget.set_budget(owner, cap=None, warn_pct=80, paused=False)

        for who in (owner, agent):   # RLS lets nobody but a platform admin flip it
            assert await ai_budget.set_platform_halt(who, halted=True, reason="nope") is False
        assert await ai_budget.set_platform_halt(platform, halted=True, reason="provider incident") is True
        for who in (agent, other_owner):
            with pytest.raises(SpendRefused) as refused:
                await round_(ai_budget.SpendScope(who, "chat:turn-4"))
            assert refused.value.code == "AI_SPEND_HALTED"
        assert (await ai_budget.state(owner))["state"] == "platform_halted"
        assert await ai_budget.set_platform_halt(platform, halted=False, reason=None) is True

        os.environ["ORACLE_AI_INFERENCE_HALT"] = "1"
        try:
            with pytest.raises(SpendRefused) as refused:
                await round_(ai_budget.SpendScope(agent, "chat:turn-5"))
            assert refused.value.code == "AI_SPEND_HALTED"
        finally:
            del os.environ["ORACLE_AI_INFERENCE_HALT"]
        assert fake.calls == 3, "no switch let a call through"
        assert await ai_budget.admission(agent) == "unlimited"
        await round_(ai_budget.SpendScope(agent, "chat:turn-6"))   # every switch off: it passes
        assert fake.calls == 4

        # ── concurrent reservations: exactly the calls that fit ──────────────
        await ai_budget.set_budget(ctx(race, Role.BROKER_OWNER, "owner"), cap=3000, warn_pct=80, paused=False)
        racer = ctx(race, Role.AGENT, "agent")
        outcomes = await asyncio.gather(
            *(ai_budget.reserve(racer, f"chat:race:fireworks:{n}", 1000) for n in range(6)),
            return_exceptions=True)
        admitted = [o for o in outcomes if isinstance(o, ai_budget.Hold)]
        refusals = [o for o in outcomes if isinstance(o, SpendRefused)]
        assert len(admitted) == 3 and len(refusals) == 3, outcomes
        assert all(r.state == "exhausted" for r in refusals)
        assert (await ai_budget.state(ctx(race, Role.BROKER_OWNER, "owner")))["reserved"] == 3000
        # At the cap a new turn is refused at the socket, before anything is saved.
        with pytest.raises(SpendRefused) as refused:
            await ai_budget.admission(racer)
        assert refused.value.code == "AI_BUDGET_EXHAUSTED"
        # Holds are the brokerage's own: another brokerage's context sees none.
        async with connection.tenant_tx(other_owner) as conn:
            assert await conn.fetchval("SELECT count(*) FROM ai_token_reservations") == 0
        # A released hold frees its room; a lapsed one stops counting.
        await ai_budget.release_hold(racer, admitted[0])
        await admin.execute("UPDATE ai_token_reservations SET expires_at = now() - interval '1 second' "
                            "WHERE tenant_id=$1 AND call_key=$2", uuid.UUID(race), admitted[1].call_key)
        assert (await ai_budget.state(ctx(race, Role.BROKER_OWNER, "owner")))["reserved"] == 1000
        await ai_budget.reserve(racer, "chat:race:fireworks:6", 2000)
        assert await held(race) == 2, "the lapsed hold is swept on the next reservation"

        # ── a platform admin reads one brokerage, not the sum of all ─────────
        mine = await ai_budget.state(platform)
        assert mine["used"]["total"] == spent + (estimate - 50)    # turn-6 included
        assert mine["reserved"] == 0

        # ── admission and the first answering call agree at the floor ────────
        # The gateway is the first tier; the smallest turn there is ("hi", no
        # record, history or brokerage block, an agent with no channel set up)
        # runs through the real _generate, with the operator-memory block the
        # real SessionManager reads and renders for a new operator under RLS
        # (the operator id left empty, as the floor leaves it). The request
        # parser is off here; tests/test_ai_budget.py pins it with it on.
        import ai_chat_agent
        import tool_offer
        from memory_core.session_manager import SessionManager

        class _NewOperator(SessionManager):
            async def get_user_context(self, user_id):
                context = await super().get_user_context(user_id)
                assert not context["found"] and not context["recent_interactions"]
                return {**context, "user_id": ""}

        async def no_brokerage(_ctx):
            return None

        async def no_channels(_ctx):
            return {c: "not set up" for c in tool_offer.CHANNEL_TOOLS}

        patched = {(ai_chat_agent, "_gateway_chat_providers"): lambda: [provider],
                   (ai_chat_agent, "FIREWORKS_ENABLED"): False,
                   (ai_chat_agent, "AI_PROVIDER"): "fireworks",
                   (ai_chat_agent, "BEDROCK_FALLBACK_ENABLED"): False,
                   (ai_chat_agent, "_resolve_brokerage_context"): no_brokerage,
                   (ai_chat_agent, "SessionManager"): _NewOperator,
                   (tool_offer, "channel_gaps"): no_channels}
        saved = {key: getattr(*key) for key in patched}
        frames = os.environ.get("ORACLE_SEMANTIC_FRAME_ENABLED")
        os.environ["ORACLE_SEMANTIC_FRAME_ENABLED"] = "off"
        for (module, name), value in patched.items():
            setattr(module, name, value)

        async def smallest_turn(assistant_id):
            hi = {"messages": [{"role": "user", "content": "hi"}], "record": None, "attachments": [],
                  "assistant": {"context_type": None, "context_id": None}}
            token = llm_gateway.spend_scope.set(ai_budget.SpendScope(other_agent, f"chat:{assistant_id}"))
            try:
                return await ai_chat_agent._generate(other_agent, hi, assistant_id)
            finally:
                llm_gateway.spend_scope.reset(token)

        try:
            floor = ai_chat_agent.smallest_request_hold(words=True, files=False)
            other_agent = ctx(other, Role.AGENT, "agent")
            await ai_budget.set_budget(other_owner, cap=floor - 1, warn_pct=80, paused=False)
            with pytest.raises(SpendRefused) as refused:
                await ai_budget.admission(other_agent, floor=floor)
            assert refused.value.code == "AI_BUDGET_INSUFFICIENT"
            with pytest.raises(SpendRefused) as refused:
                await smallest_turn("turn-short")
            assert refused.value.code == "AI_BUDGET_INSUFFICIENT" and fake.calls == 4
            await ai_budget.set_budget(other_owner, cap=floor, warn_pct=80, paused=False)
            assert await ai_budget.admission(other_agent, floor=floor) == "ok"
            fake.reply = {"choices": [{"message": {"content": "answer"}}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
            text, _, _ = await smallest_turn("turn-floor")
            assert text == "answer" and fake.calls == 5, "the first call fits a cap at the floor"
            assert (await ai_budget.state(other_owner))["used"]["total"] == 12
        finally:
            for (module, name), value in saved.items():
                setattr(module, name, value)
            if frames is None:
                os.environ.pop("ORACLE_SEMANTIC_FRAME_ENABLED", None)
            else:
                os.environ["ORACLE_SEMANTIC_FRAME_ENABLED"] = frames

        # ── an unreadable budget holds the call; it never passes ─────────────
        await connection.close_pool()
        with pytest.raises(SpendUnavailable) as unknown:
            await round_(ai_budget.SpendScope(agent, "chat:turn-7"))
        assert unknown.value.code == "AI_BUDGET_UNAVAILABLE"
        with pytest.raises(SpendUnavailable):
            await ai_budget.state(owner)
        assert fake.calls == 5
    finally:
        llm_gateway._litellm = real_litellm
        os.environ.pop("ORACLE_AI_INFERENCE_HALT", None)
        await admin.execute("SET session_replication_role = replica")
        try:
            await admin.execute("UPDATE platform_spend_controls SET halted = false, reason = NULL, "
                                "updated_by = NULL WHERE channel = 'ai_tokens'")
            for t in (tenant, other, race):
                for table in ("ai_token_reservations", "billing_usage_events", "tenant_action_budgets"):
                    await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", t)
                await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", t)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
