"""The tenant engine's start() must return.

The defect this pins was silent and total: `WorkflowEngine.start()` ended by
awaiting its own background loops, and `tenant_engines.acquire()` awaits
start() inside its lock, and the /ws handler awaits acquire() BEFORE it enters
`asyncio.gather(listen_for_client_messages(), idle_watchdog())`.

So the first socket to connect blocked forever at acquire, every later socket
blocked on the lock behind it, and no inbound frame on any socket was ever
read — AI chat sends, deal-pipeline requests, manual comps, voice transcripts,
and the client's own PONG. Server→client broadcasts kept working (they go
through ws_hub from the engine's loops), so the product looked alive: a chat
message sat at "Neoh is working…" forever instead of failing, and the server
never sent a PING because idle_watchdog was never started either.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

import tenant_engines
from workflow_engine import WorkflowEngine


def _engine(monkeypatch) -> WorkflowEngine:
    """A real engine whose loops run forever and whose seed touches no DB.

    The loops must genuinely never finish — that is the condition under which
    the old start() never returned.
    """
    engine = WorkflowEngine(websocket=None, publish=None, tenant_id="t")

    async def forever(self=None):
        await asyncio.Event().wait()

    async def noop(self=None):
        return None

    monkeypatch.setattr(WorkflowEngine, "_seed_real_leads", noop, raising=True)
    for loop in ("_analysis_loop", "_predictive_cache_loop", "_scout_loop", "_harvest_loop"):
        monkeypatch.setattr(WorkflowEngine, loop, forever, raising=True)
    return engine


def test_start_returns_while_the_loops_are_still_running(monkeypatch):
    async def scenario():
        engine = _engine(monkeypatch)
        # A regression fails here as a timeout, which is the honest signal:
        # the old code did not error, it simply never came back.
        await asyncio.wait_for(engine.start(), timeout=5)
        running = engine._background_tasks()
        assert running, "start() returned without spawning anything"
        assert all(not t.done() for t in running), (
            "start() returned only because its loops finished; the defect "
            "reproduces only while they are still running"
        )
        await engine.stop()
        assert all(t.done() for t in engine._background_tasks() or running)

    asyncio.run(scenario())


def test_acquire_returns_so_the_socket_can_start_reading(monkeypatch):
    """acquire() is on the path between accepting a socket and reading it."""

    async def scenario():
        engine = _engine(monkeypatch)
        tenant = "tenant-under-test"
        tenant_engines._entries.pop(tenant, None)
        try:
            got = await asyncio.wait_for(
                tenant_engines.acquire(tenant, factory=lambda: engine), timeout=5
            )
            assert got is engine
            # A second caller must not queue behind a lock held by the first.
            again = await asyncio.wait_for(
                tenant_engines.acquire(tenant, factory=lambda: engine), timeout=5
            )
            assert again is engine
        finally:
            entry = tenant_engines._entries.pop(tenant, None)
            if entry is not None:
                if entry.shutdown_handle is not None:
                    entry.shutdown_handle.cancel()
                await engine.stop()

    asyncio.run(scenario())


def test_only_wait_awaits_the_loops():
    """The blocking form still exists for a standalone runner — it is just no
    longer what acquire() calls."""
    start = inspect.getsource(WorkflowEngine.start)
    assert "asyncio.gather" not in start, (
        "start() must not await its own loops; that is the whole defect"
    )
    assert hasattr(WorkflowEngine, "wait")
    assert "asyncio.gather" in inspect.getsource(WorkflowEngine.wait)

    acquire = inspect.getsource(tenant_engines.acquire)
    assert "engine.start()" in acquire, "acquire must still start the engine"
    assert "engine.wait()" not in acquire, "acquire must never block on the loops"


def test_a_dying_loop_is_logged_not_swallowed(monkeypatch, caplog):
    """Nothing awaits the loops now, so a crash must reach the log itself."""

    async def scenario():
        engine = _engine(monkeypatch)

        async def boom(self=None):
            raise RuntimeError("analysis loop fell over")

        monkeypatch.setattr(WorkflowEngine, "_analysis_loop", boom, raising=True)
        await asyncio.wait_for(engine.start(), timeout=5)
        await asyncio.sleep(0)  # let the failing task run its callback
        await asyncio.sleep(0)
        await engine.stop()

    with caplog.at_level("ERROR"):
        asyncio.run(scenario())
    messages = [r.getMessage() for r in caplog.records]
    assert any("analysis loop fell over" in m for m in messages), \
        f"a loop crash was swallowed; records={messages}"


@pytest.mark.parametrize("handler", ["AI_CHAT_SEND", "REQUEST_DEAL_PIPELINE", "PONG"])
def test_the_socket_loop_is_reachable(handler):
    """These branches were dead code in production for the life of the bug."""
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")
    body = source.split("async def websocket_endpoint")[1]
    assert handler in body
    assert body.index("tenant_engines.acquire") < body.index("listen_for_client_messages()"), (
        "acquire runs before the receive loop, so acquire must never block"
    )
