"""The shared workflow engine does no work that nobody can see (L03-16).

Every product of the engine is a record frame — DATA_PULLED offers,
LEGAL_PACKAGE contract prices, PREDICTIVE_CACHE property lists, scout parcels —
and the shared tenant engine's publisher drops every record frame (owner
decision 2026-11-16). It still seeded up to 50 of the first user's leads (owner
names included) into its graph, ran AnalystAgent.analyze_property every cycle (a
paid AVM call through avm_client) and built contract packages, for no audience.
With no record audience it now stays idle and says so.
"""

from __future__ import annotations

import asyncio

import pytest

import workflow_engine
from workflow_engine import WorkflowEngine

LOOPS = ("_analysis_loop", "_predictive_cache_loop", "_scout_loop", "_harvest_loop")


@pytest.fixture
def engine_spy(monkeypatch):
    """Loops that wait forever and record that they started; a seed that only
    records that it ran (no DB)."""
    seen: list[str] = []

    def _forever(name):
        async def loop(self=None):
            seen.append(name)
            await asyncio.Event().wait()
        return loop

    async def _seed(self=None):
        seen.append("_seed_real_leads")

    monkeypatch.setattr(WorkflowEngine, "_seed_real_leads", _seed)
    for name in LOOPS:
        monkeypatch.setattr(WorkflowEngine, name, _forever(name))
    return seen


def _run(engine, seen):
    async def scenario():
        await engine.start()
        await asyncio.sleep(0)            # let any created task begin
        names = list(seen)
        await engine.stop()
        return names
    return asyncio.run(scenario())


def test_the_shared_tenant_engine_stays_idle_and_says_so(engine_spy):
    """server.py builds it with the tenant publisher, which drops record frames."""
    import server

    delivered: list[dict] = []

    async def publish(frame):
        if server.engine_frame_is_tenant_wide(frame):
            delivered.append(frame)

    engine = WorkflowEngine(websocket=None, publish=publish, tenant_id="t", record_frames=False)
    names = _run(engine, engine_spy)

    assert names == []                    # no seed, no loop of any kind
    assert engine._background_tasks() == []
    statuses = [f["agent"] for f in delivered]
    assert "WORKFLOW ENGINE — ambient analysis off (no audience)" in statuses
    assert "WORKFLOW ENGINE — all agents online" not in statuses


def test_the_server_builds_its_engine_without_a_record_audience():
    import inspect

    import server

    source = inspect.getsource(server)
    factory = source[source.index("factory=lambda: WorkflowEngine("):]
    factory = factory[:factory.index("),") + 2]
    assert "record_frames=False" in factory
    assert "websocket=None" in factory


def test_an_engine_whose_frames_reach_someone_still_works(engine_spy):
    for engine in (WorkflowEngine(websocket=object(), tenant_id="t"),
                   WorkflowEngine(publish=lambda _f: None, tenant_id="t", record_frames=True)):
        engine_spy.clear()
        names = _run(engine, engine_spy)
        assert {"_seed_real_leads", "_analysis_loop", "_predictive_cache_loop",
                "_scout_loop"} <= set(names)


class _Graph:
    """One review candidate with every field the analysis cycle reads."""

    async def calculate_novelty_score(self):
        yield {"address": "1 Main St", "novelty_score": 91, "sqft": 1400,
               "market_value": 250000, "bedrooms": 3, "bathrooms": 2,
               "owner_name": "Owner of Record", "equity_pct": 60, "life_event": None}


def _avm_calls(monkeypatch, *, record_frames: bool) -> list[str]:
    """Run the real start() and loops against a graph with a candidate and an
    analyst that records every valuation request."""
    calls: list[str] = []

    class _Analyst:
        async def analyze_property(self, hit):
            calls.append(hit["address"])
            return None

    async def _noop(self=None):
        return None

    real_sleep = asyncio.sleep

    async def _fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(WorkflowEngine, "_seed_real_leads", _noop)
    monkeypatch.setattr(WorkflowEngine, "_scout_loop", _noop)
    monkeypatch.setattr(workflow_engine.asyncio, "sleep", _fast_sleep)

    async def publish(_frame):
        return None

    async def scenario():
        engine = WorkflowEngine(websocket=None, publish=publish, tenant_id="t",
                                record_frames=record_frames)
        engine.graph = _Graph()
        engine.analyst = _Analyst()
        await engine.start()
        for _ in range(200):
            await real_sleep(0)
        await engine.stop()

    asyncio.run(scenario())
    return calls


def test_no_avm_call_is_made_by_the_shared_engine(monkeypatch):
    """End to end on the real loops, with a candidate the analyst would value."""
    assert _avm_calls(monkeypatch, record_frames=False) == []


def test_the_same_scenario_with_an_audience_does_value_the_candidate(monkeypatch):
    """The control that makes the test above able to fail."""
    assert "1 Main St" in _avm_calls(monkeypatch, record_frames=True)
