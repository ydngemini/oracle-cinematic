"""What one WebSocket connection costs — inverted at P2.

This file was written during P0 to pin the *old* shape in executable form: every
`/ws` connection built its own `WorkflowEngine` (seeding up to 50 lead rows) and
ran its own `monologue_loop` making one LLM call every 6 seconds. Fifty agents in
one brokerage meant fifty engines, 2,500 seeded rows and ~500 ambient LLM calls a
minute — on a deployment where no replica runs a local model, so each call was a
failing health probe followed by a hosted request.

Those assertions were written to fail once ambient work was shared per tenant,
and that inversion was P2's acceptance criterion. They are inverted here rather
than deleted, so the old shape cannot quietly return.

The arithmetic went 500 calls/minute → 3. It is now **zero**: the ambient
monologue is deleted outright. Its text reached the browser and was stored in
the reducer, but the only component that rendered it — WalkerBubble — was
never mounted, so every one of those calls bought something no one could see.
The cheapest version of work nobody sees is still the wrong amount.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib

import pytest


BACKEND = pathlib.Path(__file__).resolve().parent.parent


class _FakeWebSocket:
    """Records frames; never touches a network."""

    def __init__(self):
        self.frames: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.frames.append(payload)


# ---------------------------------------------------------------------------
# The ambient cost, removed
# ---------------------------------------------------------------------------

def test_no_llm_call_happens_on_a_timer():
    """INVERTED again, and this time by deletion.

    The history is worth keeping because the arithmetic is the lesson. The
    original shape ran one LLM call per socket per 6s — 50 agents in one
    brokerage cost ~500 calls/minute. P2 made it one call per tenant per 20s,
    3/minute. Both were paying for the same thing: an ambient "thought" that
    reached the browser, was stored in the reducer, and was rendered by
    exactly one component — WalkerBubble, which was never mounted anywhere in
    the tree. The cheapest version of work nobody sees is still the wrong
    amount, so the producer, the frame, the reducer state and the component
    are all gone.

    What this guards: nothing may reintroduce a timer that generates text.
    """
    source = (BACKEND / "server.py").read_text(encoding="utf-8")

    assert "ambient_monologue_producer" not in source
    assert "AMBIENT_MONOLOGUE_INTERVAL" not in source
    assert "ORACLE_MONOLOGUE_INTERVAL" not in source
    assert "AGENT_THOUGHT" not in source, (
        "the frame is gone; nothing on the client listens for it any more"
    )
    assert "stream_monologue" not in source, (
        "server.py must not reach the monologue generator; mind_service keeps "
        "it for generate_command_draft's sake, which is a request path, not a timer"
    )


# ---------------------------------------------------------------------------
# Engine ownership
# ---------------------------------------------------------------------------

def test_the_engine_is_acquired_per_tenant_not_constructed_per_connection():
    """INVERTED at P2."""
    source = (BACKEND / "server.py").read_text(encoding="utf-8")
    handler = source.split("async def websocket_endpoint")[1]

    assert "tenant_engines.acquire" in handler, (
        "the socket handler no longer acquires a shared tenant engine"
    )
    assert "WorkflowEngine(\n            websocket=None" in handler or "websocket=None" in handler, (
        "the tenant engine must not bind a single socket"
    )


def test_the_engine_can_publish_without_a_websocket():
    """INVERTED at P2: the ctor took a socket, so its output could only reach
    one client. It now accepts a publish callable routed through ws_hub."""
    from workflow_engine import WorkflowEngine

    parameters = inspect.signature(WorkflowEngine.__init__).parameters
    assert "publish" in parameters, "WorkflowEngine cannot emit without a socket"

    # Every send must go through the seam, or a socketless engine silently drops
    # frames while its loops keep running.
    source = (BACKEND / "workflow_engine.py").read_text(encoding="utf-8")
    raw_sends = [
        line.strip()
        for line in source.splitlines()
        if "self.websocket.send" in line
    ]
    assert len(raw_sends) == 1, (
        f"expected the only raw socket send to be inside _send(); found {raw_sends}"
    )


def test_a_socketless_engine_with_no_publish_reports_no_transport():
    """Degradation must be explicit: an engine with neither transport reports
    it rather than pretending frames were delivered."""
    from workflow_engine import WorkflowEngine

    engine = WorkflowEngine(websocket=None, publish=None, tenant_id="t")
    assert engine._has_transport is False
    assert asyncio.run(engine._send({"type": "X"})) is False


def test_the_engine_still_seeds_the_graph_from_real_leads():
    """Unchanged by P2 — but now paid once per tenant rather than per socket."""
    from workflow_engine import WorkflowEngine

    assert hasattr(WorkflowEngine, "_seed_real_leads")
    start = inspect.getsource(WorkflowEngine.start)
    assert "_seed_real_leads" in start


def test_the_seed_query_uses_the_pipeline_rank_index():
    """INVERTED at P2.

    `ORDER BY random()` scanned every row the tenant owned — measured at
    6,156,309 rows to return 50 — once per connection. The new ordering matches
    idx_leads_pipeline_tenant_rank so the planner stops at LIMIT, and it is also
    the more useful answer: the seeded pipeline shows the highest-motivation
    leads deterministically.
    """
    # String *literals*, not raw file text: the comment above the query
    # explains what it replaced and quotes the old ordering, so a substring
    # search over the source matches the explanation rather than the SQL.
    tree = ast.parse((BACKEND / "real_leads.py").read_text(encoding="utf-8"))
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    sql = " ".join(lit for lit in literals if "FROM leads" in lit or "ORDER BY" in lit)

    assert sql, "no seed query literal found in real_leads.py"
    assert "ORDER BY random()" not in sql, "the full-scan seed query is back"
    assert "motivation_score DESC" in sql
    assert "NULLS LAST" in sql, (
        "without NULLS LAST a null motivation sorts first in a DESC ordering"
    )


# ---------------------------------------------------------------------------
# Reference counting
# ---------------------------------------------------------------------------

def test_a_second_socket_reuses_the_running_engine():
    import tenant_engines

    built = []

    class _Engine:
        def __init__(self):
            built.append(self)
            self.stopped = False

        async def start(self):
            pass

        async def stop(self):
            self.stopped = True

    async def scenario():
        tenant_engines._entries.clear()
        a = await tenant_engines.acquire("t1", factory=_Engine)
        b = await tenant_engines.acquire("t1", factory=_Engine)
        return a, b

    a, b = asyncio.run(scenario())
    assert a is b, "each socket built its own engine again"
    assert len(built) == 1
    tenant_engines._entries.clear()


def test_engines_are_not_shared_between_tenants():
    """The seeded graph is tenant-scoped data; sharing one engine across tenants
    would be a cross-tenant leak, not merely a caching bug."""
    import tenant_engines

    class _Engine:
        async def start(self):
            pass

        async def stop(self):
            pass

    async def scenario():
        tenant_engines._entries.clear()
        a = await tenant_engines.acquire("t1", factory=_Engine)
        b = await tenant_engines.acquire("t2", factory=_Engine)
        return a, b

    a, b = asyncio.run(scenario())
    assert a is not b
    tenant_engines._entries.clear()


def test_the_engine_survives_one_socket_leaving_while_another_remains():
    import tenant_engines

    class _Engine:
        def __init__(self):
            self.stopped = False

        async def start(self):
            pass

        async def stop(self):
            self.stopped = True

    async def scenario():
        tenant_engines._entries.clear()
        engine = await tenant_engines.acquire("t1", factory=_Engine)
        await tenant_engines.acquire("t1", factory=_Engine)
        await tenant_engines.release("t1")
        await asyncio.sleep(0)
        return engine

    engine = asyncio.run(scenario())
    assert engine.stopped is False, "one client disconnecting tore down everyone's pipeline"
    tenant_engines._entries.clear()


def test_a_reconnect_inside_the_linger_window_reclaims_the_engine(monkeypatch):
    """A page reload disconnects and reconnects within a second. Tearing the
    engine down only to immediately re-seed it is worse than keeping it."""
    import tenant_engines

    monkeypatch.setattr(tenant_engines, "ENGINE_LINGER_SECONDS", 5.0)

    class _Engine:
        def __init__(self):
            self.stopped = False

        async def start(self):
            pass

        async def stop(self):
            self.stopped = True

    async def scenario():
        tenant_engines._entries.clear()
        first = await tenant_engines.acquire("t1", factory=_Engine)
        await tenant_engines.release("t1")
        await asyncio.sleep(0)  # let the shutdown task be scheduled
        second = await tenant_engines.acquire("t1", factory=_Engine)
        await asyncio.sleep(0)
        return first, second

    first, second = asyncio.run(scenario())
    assert first is second, "a reload built a fresh engine and re-seeded the graph"
    assert first.stopped is False
    tenant_engines._entries.clear()


def test_the_last_release_stops_the_engine_after_the_linger(monkeypatch):
    """The linger must expire — otherwise a tenant that logged out an hour ago
    is still harvesting."""
    import tenant_engines

    monkeypatch.setattr(tenant_engines, "ENGINE_LINGER_SECONDS", 0.0)

    class _Engine:
        def __init__(self):
            self.stopped = False

        async def start(self):
            pass

        async def stop(self):
            self.stopped = True

    async def scenario():
        tenant_engines._entries.clear()
        engine = await tenant_engines.acquire("t1", factory=_Engine)
        await tenant_engines.release("t1")
        await asyncio.sleep(0.05)
        return engine

    engine = asyncio.run(scenario())
    assert engine.stopped is True, "the engine outlived its last viewer"
    assert tenant_engines.engine_count() == 0
    tenant_engines._entries.clear()


def test_the_replica_has_a_connection_ceiling():
    """Connections were uncapped, so overload degraded everyone rather than
    refusing the marginal client."""
    import ws_hub

    assert ws_hub.MAX_CONNECTIONS_PER_REPLICA > 0
    source = (BACKEND / "server.py").read_text(encoding="utf-8")
    assert "at_capacity()" in source
    assert "1013" in source, "the refusal must use a retryable close code"


# ---------------------------------------------------------------------------
# Making the load visible in production, not just in a test
# ---------------------------------------------------------------------------

def test_runtime_load_reports_pool_capacity_net_of_the_listener():
    """`max_size` overstates what a request can get.

    `ws_hub` holds one connection for the process lifetime, so a pool of 10 has
    9 usable. An operator sizing replicas off the raw number is off by one per
    replica, which matters most on the small pools this runs with.
    """
    import admin_ops
    from tenancy import Role, TenantContext

    ctx = TenantContext(
        agent_id="ops@tenant.test",
        tenant_id="11111111-1111-1111-1111-111111111111",
        role=Role.PLATFORM_ADMIN,
    )
    payload = asyncio.run(admin_ops.runtime_load(ctx=ctx))

    assert "usable_for_requests" in payload["db_pool"]
    assert "reserved_for_listener" in payload["db_pool"]
    assert payload["websockets"]["total"] == 0
    assert payload["instance"], "answers must be attributable to one replica"


def test_ambient_traffic_is_now_measured_rather_than_unknown():
    """This asserted `is None` until a gateway existed to count.

    That was the honest answer while nothing counted — zero would have read as
    "no ambient load", the opposite of true. P1's llm_gateway is that counter,
    so the endpoint now returns a real number, and the inversion of this
    assertion is what says the seam actually landed.
    """
    import admin_ops
    import llm_gateway

    before = admin_ops._ambient_llm_calls_last_minute()
    assert isinstance(before, int)

    llm_gateway.counter.record("analysis", "test", ok=True)
    assert admin_ops._ambient_llm_calls_last_minute() == before + 1
