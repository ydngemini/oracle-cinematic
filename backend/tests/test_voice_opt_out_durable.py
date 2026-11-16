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
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(voice_opt_out, "SPOOL_PATH", tmp_path / "spool.jsonl")
    monkeypatch.setattr(voice_opt_out, "_PENDING", set())
    monkeypatch.setattr(voice_opt_out, "_SPOOL_LOADED", False)
    monkeypatch.setattr(voice_opt_out, "RETRY_DELAYS", (0.0, 0.0, 0.0))


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

    async def state(_conn, _norm, _channel, _tomb):
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
