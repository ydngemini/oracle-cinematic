"""A spoken STOP on an outbound call survives a failed suppression write
(external audit 23.5 / 34.5): retried, then spooled, honoured by the outreach
gate while pending, replayed by reconciliation, reported by health.
No database, no carrier: the write is faked."""
from __future__ import annotations

import asyncio
import json
import stat
from contextlib import asynccontextmanager

import pytest

import component_health
import outreach_compliance
import voice_opt_out

TENANT = "11111111-1111-1111-1111-111111111111"
STATE = {"tenant_id": TENANT, "callee": "+1 (302) 555-0142", "direction": "outbound"}
NUMBER = outreach_compliance.normalize_contact(STATE["callee"], outreach_compliance.Channel.VOICE)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    # conftest._private_opt_out_spool already gives every test its own spool.
    monkeypatch.setattr(voice_opt_out, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(voice_opt_out, "SLOW_RETRY_FIRST", 0.0)
    monkeypatch.setattr(voice_opt_out, "SLOW_RETRY_CAP", 0.0)


def spool_line(tenant, contact):
    return json.dumps({"tenant_id": tenant, "contact": contact}) + "\n"


def honour_and_drain(state, ref):
    """Run honour() like a bridge, then let the after-call retries finish."""
    async def go():
        await voice_opt_out.honour(state, ref)
        for _ in range(100):
            await asyncio.sleep(0)  # let done-callbacks run (awaiting done tasks never yields)
            if not voice_opt_out._IN_FLIGHT:
                break
            await asyncio.gather(*list(voice_opt_out._IN_FLIGHT), return_exceptions=True)
        assert not voice_opt_out._IN_FLIGHT, "after-call retries did not finish"
    asyncio.run(go())


def fake_writer(monkeypatch, outcomes):
    calls = []

    async def write(state):
        if not outcomes:
            raise asyncio.CancelledError  # the test's "process exit": ends the retry loop
        calls.append(dict(state))
        if outcomes.pop(0) == "fail":
            raise voice_opt_out.OptOutNotRecorded("db down")

    monkeypatch.setattr(voice_opt_out, "record_outbound_opt_out", write)
    return calls


def test_a_transient_failure_is_retried_after_the_call_and_cleared(monkeypatch):
    calls = fake_writer(monkeypatch, ["fail", "ok"])
    honour_and_drain(STATE, "call-1")
    assert len(calls) == 2
    assert voice_opt_out.pending_count() == 0 and voice_opt_out.SPOOL_PATH.read_text() == ""


def test_a_persistent_failure_is_spooled_privately_and_blocks_outreach(monkeypatch):
    calls = fake_writer(monkeypatch, ["fail"] * 4)
    honour_and_drain(STATE, "call-2")
    assert len(calls) == 4                                      # 1 in-call + 3 after-call retries
    lines = voice_opt_out.SPOOL_PATH.read_text().splitlines()
    assert [json.loads(line) for line in lines] == [{"tenant_id": TENANT, "contact": NUMBER}]
    assert stat.S_IMODE(voice_opt_out.SPOOL_PATH.stat().st_mode) == 0o600
    assert "said" not in voice_opt_out.SPOOL_PATH.read_text()   # number + tenant only
    assert voice_opt_out.pending_opt_out(TENANT, NUMBER)
    assert component_health._outbound_opt_outs()["state"] == component_health.DEGRADED


def test_the_outreach_gate_treats_a_pending_stop_as_suppressed(monkeypatch):
    fake_writer(monkeypatch, ["fail"] * 4)
    honour_and_drain(STATE, "call-3")

    @asynccontextmanager
    async def no_db(ctx, conn):
        yield None

    async def state(_conn, _norm, _channel, _tomb, **_kw):
        return {"suppressed": False, "has_consent": True, "has_written_consent": True,
                "vp_consent": True, "recent_voice": 0}

    monkeypatch.setattr(outreach_compliance, "_conn_or_tx", no_db)
    monkeypatch.setattr(outreach_compliance.ConsentLedger, "gather_state", state)
    from tenancy import Role, TenantContext

    ctx = TenantContext(agent_id="a@x.test", tenant_id=TENANT, role=Role.AGENT)
    for channel in (outreach_compliance.Channel.SMS, outreach_compliance.Channel.VOICE):
        decision = asyncio.run(outreach_compliance.guard_outreach(
            ctx, channel=channel, contact=STATE["callee"], state_code="DE", log=False))
        assert not decision.allowed, channel
    other = asyncio.run(outreach_compliance.guard_outreach(
        ctx, channel=outreach_compliance.Channel.SMS, contact="+13025550199", state_code="DE", log=False))
    assert "suppressed" not in " ".join(other.blockers).lower()


def test_replay_writes_spooled_stops_and_keeps_only_failures(monkeypatch):
    fake_writer(monkeypatch, ["fail"] * 4)
    honour_and_drain(STATE, "call-4")
    second = {**STATE, "callee": "+13025550177"}
    fake_writer(monkeypatch, ["fail"] * 4)
    honour_and_drain(second, "call-5")
    assert voice_opt_out.pending_count() == 2

    calls = fake_writer(monkeypatch, ["ok", "fail"])           # DB back for one, not the other
    result = asyncio.run(voice_opt_out.replay_spool())
    assert result == {"replayed": 1, "still_pending": 1}
    assert len(calls) == 2 and voice_opt_out.pending_count() == 1

    fake_writer(monkeypatch, ["ok"])
    assert asyncio.run(voice_opt_out.replay_spool()) == {"replayed": 1, "still_pending": 0}
    assert voice_opt_out.pending_count() == 0
    assert component_health._outbound_opt_outs()["state"] == component_health.HEALTHY


def test_a_spool_write_failure_still_blocks_in_memory(monkeypatch):
    fake_writer(monkeypatch, ["fail"] * 4)

    def broken(_key):
        raise OSError("disk full")

    monkeypatch.setattr(voice_opt_out, "_spool", broken)
    honour_and_drain(STATE, "call-6")
    assert voice_opt_out.pending_opt_out(TENANT, NUMBER)


def test_reconciliation_sweep_replays_the_spool():
    import inspect

    import reconciliation

    assert "voice_opt_out.replay_spool()" in inspect.getsource(reconciliation.run_sweep)


def test_the_call_is_not_held_open_by_retries(monkeypatch):
    """honour() returns after ONE attempt; retries happen after hang-up."""
    import time

    calls = fake_writer(monkeypatch, ["fail"] * 4)
    monkeypatch.setattr(voice_opt_out, "RETRY_DELAYS", (30.0, 30.0, 30.0))

    async def go():
        started = time.monotonic()
        await voice_opt_out.honour(STATE, "call-7")
        elapsed = time.monotonic() - started
        for task in list(voice_opt_out._IN_FLIGHT):
            task.cancel()
        return elapsed

    assert asyncio.run(go()) < 1.0 and len(calls) == 1
    assert voice_opt_out.pending_opt_out(TENANT, NUMBER)        # spooled + blocked immediately


def test_a_restart_still_blocks_a_stop_spooled_by_an_earlier_process():
    """The spool on disk, not memory, is what counts (mutant: forget the file)."""
    voice_opt_out.SPOOL_PATH.write_text(spool_line(TENANT, NUMBER))
    assert voice_opt_out.pending_opt_out(TENANT, NUMBER)
    assert voice_opt_out.pending_count() == 1


def test_another_workers_stop_is_seen_after_this_worker_cached_the_spool():
    voice_opt_out.SPOOL_PATH.write_text(spool_line(TENANT, "+13025550001"))
    assert not voice_opt_out.pending_opt_out(TENANT, NUMBER)        # cached now
    with open(voice_opt_out.SPOOL_PATH, "a") as other_worker:
        other_worker.write(spool_line(TENANT, NUMBER))
    assert voice_opt_out.pending_opt_out(TENANT, NUMBER)


def test_a_stop_spooled_during_a_replay_is_never_dropped(monkeypatch):
    """Review of PR #84: replay rewrote the file from its snapshot and cleared
    memory, losing a STOP spooled while its writes were awaited."""
    voice_opt_out.SPOOL_PATH.write_text(spool_line(TENANT, "+13025550001"))
    release = None

    async def slow_ok(state):
        await release.wait()                                        # the DB is slow

    async def go():
        nonlocal release
        release = asyncio.Event()
        monkeypatch.setattr(voice_opt_out, "record_outbound_opt_out", slow_ok)
        replay = asyncio.ensure_future(voice_opt_out.replay_spool())
        await asyncio.sleep(0)

        async def failing(state):
            raise voice_opt_out.OptOutNotRecorded("db down")

        monkeypatch.setattr(voice_opt_out, "record_outbound_opt_out", failing)
        await voice_opt_out.honour(STATE, "call-during-replay")     # spooled mid-replay
        for task in list(voice_opt_out._IN_FLIGHT):
            task.cancel()
        monkeypatch.setattr(voice_opt_out, "record_outbound_opt_out", slow_ok)
        release.set()
        return await replay

    result = asyncio.run(go())
    assert result["replayed"] == 1
    assert voice_opt_out.pending_opt_out(TENANT, NUMBER), "the STOP said during the replay was lost"
    assert (TENANT, NUMBER) in set(voice_opt_out._read_spool())


def test_email_through_the_gate_is_unaffected_by_a_pending_phone_stop(monkeypatch):
    voice_opt_out.SPOOL_PATH.write_text(spool_line(TENANT, NUMBER))

    @asynccontextmanager
    async def no_db(ctx, conn):
        yield None

    async def state(_conn, _norm, _channel, _tomb, **_kw):
        return {"suppressed": False, "has_consent": True, "has_written_consent": True,
                "vp_consent": False, "recent_voice": 0}

    monkeypatch.setattr(outreach_compliance, "_conn_or_tx", no_db)
    monkeypatch.setattr(outreach_compliance.ConsentLedger, "gather_state", state)
    from tenancy import Role, TenantContext

    ctx = TenantContext(agent_id="a@x.test", tenant_id=TENANT, role=Role.AGENT)
    decision = asyncio.run(outreach_compliance.guard_outreach(
        ctx, channel=outreach_compliance.Channel.EMAIL, contact="owner@example.test", state_code="DE", log=False))
    assert "suppressed" not in " ".join(decision.blockers).lower()


def test_the_default_spool_path_is_absolute_and_outside_the_source_tree(monkeypatch):
    import importlib
    import pathlib

    monkeypatch.delenv("ORACLE_OPT_OUT_SPOOL", raising=False)
    fresh = importlib.reload(voice_opt_out)
    try:
        path = fresh.SPOOL_PATH
        assert path.is_absolute()
        assert pathlib.Path(__file__).resolve().parents[2] not in path.resolve().parents
    finally:
        importlib.reload(voice_opt_out)


def test_the_owner_keeps_retrying_past_the_fast_phase_until_it_lands(monkeypatch):
    """Production: the sweep runs in another container, so the owning api
    process must keep going (capped backoff) until the write succeeds."""
    calls = fake_writer(monkeypatch, ["fail"] * 7 + ["ok"])
    honour_and_drain(STATE, "call-slow")
    assert len(calls) == 8                                      # 1 in-call + 3 fast + 4 slow
    assert voice_opt_out.pending_count() == 0


def test_startup_resumes_a_leftover_spool(monkeypatch):
    voice_opt_out.SPOOL_PATH.write_text(spool_line(TENANT, NUMBER))
    calls = fake_writer(monkeypatch, ["fail", "ok"])

    async def go():
        assert voice_opt_out.resume_pending() == 1
        for _ in range(100):
            await asyncio.sleep(0)
            if not voice_opt_out._IN_FLIGHT:
                break
            await asyncio.gather(*list(voice_opt_out._IN_FLIGHT), return_exceptions=True)

    asyncio.run(go())
    assert len(calls) == 2 and voice_opt_out.pending_count() == 0


def test_the_api_lifespan_resumes_pending_stops_in_every_process_role():
    """Third review of PR #84: the call sat under `if config.RUNS_BACKGROUND_WORK`,
    which is False for the web/api role that owns the spool."""
    import ast
    import inspect
    import textwrap

    import server

    tree = ast.parse(textwrap.dedent(inspect.getsource(server.lifespan)))
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("resume_pending")]
    assert len(calls) == 1
    node = calls[0]
    while node is not None:
        if isinstance(node, (ast.If, ast.While)):
            assert "RUNS_BACKGROUND_WORK" not in ast.unparse(node.test), "resume is gated off for the api role"
        node = getattr(node, "parent", None)


def test_resume_never_raises_on_an_unreadable_spool(monkeypatch):
    def broken():
        raise PermissionError("no access")

    monkeypatch.setattr(voice_opt_out, "_spooled", broken)
    assert voice_opt_out.resume_pending() == 0
