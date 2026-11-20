"""The shared workflow engine buys no AVM valuation that nobody can see (L03-16).

Its record frames (DATA_PULLED offers, LEGAL_PACKAGE contract prices,
PREDICTIVE_CACHE property lists) never reach the brokerage: the tenant-wide
publisher drops them (owner decision 2026-11-16). The analysis loop still ran
AnalystAgent.analyze_property every cycle — a paid AVM call through avm_client —
and built a contract package, for no audience. It now starts only when the
engine's transport delivers record frames.
"""

from __future__ import annotations

import asyncio

import pytest

import workflow_engine
from workflow_engine import WorkflowEngine


@pytest.fixture
def quiet_engine_loops(monkeypatch):
    """Loops that wait forever and record that they were started; no DB seed."""
    started: list[str] = []

    def _forever(name):
        async def loop(self=None):
            started.append(name)
            await asyncio.Event().wait()
        return loop

    async def _noop(self=None):
        return None

    monkeypatch.setattr(WorkflowEngine, "_seed_real_leads", _noop)
    for name in ("_analysis_loop", "_predictive_cache_loop", "_scout_loop", "_harvest_loop"):
        monkeypatch.setattr(WorkflowEngine, name, _forever(name))
    return started


def _started_loops(engine, started):
    async def scenario():
        await engine.start()
        await asyncio.sleep(0)            # let the created tasks begin
        names = list(started)
        await engine.stop()
        return names
    return asyncio.run(scenario())


def test_the_shared_tenant_engine_starts_no_paid_analysis(quiet_engine_loops):
    """server.py builds it with the tenant publisher, which drops record frames."""
    import server

    engine = WorkflowEngine(websocket=None, publish=server.make_engine_publisher("t"),
                            tenant_id="t", record_frames=False)
    names = _started_loops(engine, quiet_engine_loops)

    assert "_analysis_loop" not in names
    assert "_predictive_cache_loop" not in names
    assert "_scout_loop" in names          # the content-free loops still run


def test_the_server_builds_its_engine_without_a_record_audience():
    import inspect

    import server

    source = inspect.getsource(server)
    factory = source[source.index("factory=lambda: WorkflowEngine("):]
    factory = factory[:factory.index("),") + 2]
    assert "record_frames=False" in factory
    assert "websocket=None" in factory


def test_an_engine_whose_frames_reach_someone_still_analyses(quiet_engine_loops):
    for engine in (WorkflowEngine(websocket=object(), tenant_id="t"),
                   WorkflowEngine(publish=lambda _f: None, tenant_id="t", record_frames=True)):
        quiet_engine_loops.clear()
        names = _started_loops(engine, quiet_engine_loops)
        assert "_analysis_loop" in names and "_predictive_cache_loop" in names


def test_no_avm_call_is_made_by_the_shared_engine(monkeypatch):
    """End to end on the real loops: the analyst is never asked for a valuation."""
    import server

    calls: list[str] = []

    class _Analyst:
        async def analyze_property(self, hit):
            calls.append(hit.get("address", ""))
            return None

    async def _noop(self=None):
        return None

    real_sleep = asyncio.sleep

    async def _fast_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(WorkflowEngine, "_seed_real_leads", _noop)
    monkeypatch.setattr(workflow_engine.asyncio, "sleep", _fast_sleep)

    async def scenario():
        engine = WorkflowEngine(websocket=None, publish=server.make_engine_publisher("t"),
                                tenant_id="t", record_frames=False)
        engine.analyst = _Analyst()
        engine.scout = None
        await engine.start()
        for _ in range(50):
            await real_sleep(0)
        await engine.stop()

    asyncio.run(scenario())
    assert calls == []
