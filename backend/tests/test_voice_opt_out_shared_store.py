"""A pending spoken STOP is shared across containers (external audit 34.5;
ledger L09-11 owner blocker, decided 2026-11-20).

The local spool is per container: the other api replica and the worker did
not see a pending STOP, and a redeploy wiped it. Now the STOP is also written
to Valkey before honour() returns; every reader checks both; an unreadable
Valkey holds SMS and voice (fail closed); the entry is removed only after the
suppression row commits. Each "replica" here is its own spool and memory
sharing one fake Valkey; no database, no carrier (the write is faked).
tests/test_voice_opt_out_valkey_live.py runs the same story on a real Valkey.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

import component_health
import outreach_compliance
import voice_opt_out
from outreach_compliance import Channel

TENANT = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"
STATE = {"tenant_id": TENANT, "callee": "+1 (302) 555-0142", "direction": "outbound"}
NUMBER = outreach_compliance.normalize_contact(STATE["callee"], Channel.VOICE)
DIGITS = "3025550142"
MONTH = 30 * 24 * 3600


class FakeValkey:
    """The commands voice_opt_out uses. `down` makes every command fail as a
    dead server does; `stall` makes them hang."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.sets: dict[str, set] = {}
        self.down = False
        self.stall = False
        self.ops = 0

    async def _cmd(self):
        self.ops += 1
        if self.stall:
            await asyncio.sleep(30)
        if self.down:
            raise RedisConnectionError("Connection refused")

    def lose_everything(self):
        self.kv.clear(), self.ttl.clear(), self.sets.clear()

    async def exists(self, name):
        await self._cmd()
        return int(name in self.kv)

    async def get(self, name):
        await self._cmd()
        return self.kv.get(name)

    async def smembers(self, name):
        await self._cmd()
        return set(self.sets.get(name, set()))

    async def scard(self, name):
        await self._cmd()
        return len(self.sets.get(name, set()))

    async def srem(self, name, *members):
        await self._cmd()
        self.sets.get(name, set()).difference_update(members)
        return 1

    async def expire(self, name, seconds):
        await self._cmd()
        if name in self.kv or name in self.sets:
            self.ttl[name] = seconds
        return 1

    def pipeline(self, transaction=True):
        assert transaction, "entry and index must change together"
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, owner):
        self.owner, self.queued = owner, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def set(self, name, value, ex=None):
        self.queued.append(("set", name, value, ex))
        return self

    def sadd(self, name, *members):
        self.queued.append(("sadd", name, members))
        return self

    def expire(self, name, seconds):
        self.queued.append(("expire", name, seconds))
        return self

    def delete(self, *names):
        self.queued.append(("delete", names))
        return self

    def srem(self, name, *members):
        self.queued.append(("srem", name, members))
        return self

    async def execute(self):
        o = self.owner
        await o._cmd()   # all or nothing, like MULTI/EXEC
        for op in self.queued:
            if op[0] == "set":
                o.kv[op[1]], o.ttl[op[1]] = op[2], op[3]
            elif op[0] == "sadd":
                o.sets.setdefault(op[1], set()).update(op[2])
            elif op[0] == "expire":
                o.ttl[op[1]] = op[2]
            elif op[0] == "delete":
                for name in op[1]:
                    o.kv.pop(name, None), o.ttl.pop(name, None)
            elif op[0] == "srem":
                o.sets.get(op[1], set()).difference_update(op[2])
        return [True] * len(self.queued)


@pytest.fixture
def valkey(monkeypatch):
    fake = FakeValkey()
    monkeypatch.setenv("REDIS_URL", "redis://valkey.invalid:6379/0")
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", secrets.token_hex(32))   # built at run time

    async def client():
        return fake

    monkeypatch.setattr(voice_opt_out, "_shared_client", client)
    monkeypatch.setattr(voice_opt_out, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(voice_opt_out, "SLOW_RETRY_FIRST", 0.0)
    monkeypatch.setattr(voice_opt_out, "SLOW_RETRY_CAP", 0.0)
    return fake


@pytest.fixture
def be(monkeypatch, tmp_path):
    """be("api-1") makes the test run as that container: its own spool file
    and memory. The fake Valkey is shared by all of them."""
    def switch(name):
        (tmp_path / name).mkdir(exist_ok=True)
        monkeypatch.setattr(voice_opt_out, "SPOOL_PATH", tmp_path / name / "spool.jsonl")
        monkeypatch.setattr(voice_opt_out, "_PENDING", set())
        monkeypatch.setattr(voice_opt_out, "_SPOOL_SEEN", (None, None))
        return voice_opt_out.SPOOL_PATH
    return switch


def writer(monkeypatch, outcomes, *, on_call=None):
    """The suppression write: 'fail' raises OptOutNotRecorded, 'ok' commits.
    Out of outcomes = the process exits (ends a retry loop)."""
    calls = []

    async def write(state):
        if not outcomes:
            raise asyncio.CancelledError
        calls.append(dict(state))
        if on_call:
            on_call(len(calls))
        if outcomes.pop(0) == "fail":
            raise voice_opt_out.OptOutNotRecorded("db down")

    monkeypatch.setattr(voice_opt_out, "record_outbound_opt_out", write)
    return calls


def honour_now(state=STATE, ref="call"):
    """honour() as a bridge calls it; the after-call retries are held off and
    then cancelled, so what is asserted is what honour() did BEFORE it
    returned."""
    async def go():
        await voice_opt_out.honour(state, ref)
        for task in list(voice_opt_out._IN_FLIGHT):
            task.cancel()
        await asyncio.gather(*list(voice_opt_out._IN_FLIGHT), return_exceptions=True)

    saved = voice_opt_out.RETRY_DELAYS
    voice_opt_out.RETRY_DELAYS = (30.0, 30.0, 30.0)
    try:
        asyncio.run(go())
    finally:
        voice_opt_out.RETRY_DELAYS = saved


def honour_and_drain(state=STATE, ref="call"):
    async def go():
        await voice_opt_out.honour(state, ref)
        for _ in range(100):
            await asyncio.sleep(0)
            if not voice_opt_out._IN_FLIGHT:
                break
            await asyncio.gather(*list(voice_opt_out._IN_FLIGHT), return_exceptions=True)
    asyncio.run(go())


def status(number=NUMBER, tenant=TENANT):
    return asyncio.run(voice_opt_out.opt_out_status(tenant, number))


def entry_name(tenant=TENANT, number=NUMBER):
    from privacy_lifecycle import contact_hmac

    return f"{voice_opt_out.SHARED_PREFIX}{tenant}:{contact_hmac(tenant, number)}"


def gate(monkeypatch, channel, contact=STATE["callee"]):
    @asynccontextmanager
    async def no_db(ctx, conn):
        yield None

    async def clean_row(_conn, _norm, _channel, _tomb, **_kw):
        return {"suppressed": False, "has_consent": True, "has_written_consent": True,
                "vp_consent": True, "recent_voice": 0}

    monkeypatch.setattr(outreach_compliance, "_conn_or_tx", no_db)
    monkeypatch.setattr(outreach_compliance.ConsentLedger, "gather_state", clean_row)
    from tenancy import Role, TenantContext

    ctx = TenantContext(agent_id="a@x.test", tenant_id=TENANT, role=Role.AGENT)
    return asyncio.run(outreach_compliance.guard_outreach(
        ctx, channel=channel, contact=contact, state_code="DE", log=False))


class _LedgerConn:
    async def fetchval(self, *_a):
        return False   # nothing in outreach_suppression


def ledger(channel, contact=STATE["callee"]):
    return asyncio.run(outreach_compliance.ledger_suppressed(_LedgerConn(), TENANT, contact, channel))


def health():
    return asyncio.run(component_health._outbound_opt_outs())


# ── written before honour() returns, keyed by the keyed hash ────────────────
def test_a_pending_stop_is_in_the_shared_store_before_honour_returns_and_never_as_the_number(
        monkeypatch, valkey, be):
    be("api-1")
    writer(monkeypatch, ["fail"])
    honour_now()

    name = entry_name()
    assert list(valkey.kv) == [name]
    assert valkey.sets[voice_opt_out.SHARED_INDEX] == {name[len(voice_opt_out.SHARED_PREFIX):]}
    assert valkey.ttl[name] == MONTH and valkey.ttl[voice_opt_out.SHARED_INDEX] == MONTH
    raw = valkey.kv[name]
    assert DIGITS not in name and DIGITS not in raw          # the number is sealed, the key is a keyed hash
    assert voice_opt_out._unseal(raw) == (TENANT, NUMBER)  # yet the worker can write the row
    assert (TENANT, NUMBER) in set(voice_opt_out._read_spool())   # and the local spool still has it


def test_another_replica_and_the_worker_see_a_stop_heard_elsewhere(monkeypatch, valkey, be):
    be("api-1")
    writer(monkeypatch, ["fail"])
    honour_now()

    for container in ("api-2", "worker"):
        be(container)
        assert not voice_opt_out.pending_opt_out(TENANT, NUMBER)   # not in THIS container's spool
        assert status() == voice_opt_out.PENDING                  # but shared
        for channel in (Channel.SMS, Channel.VOICE):
            decision = gate(monkeypatch, channel)
            assert not decision.allowed, (container, channel)
            assert "opt-out list" in " ".join(decision.blockers)
        assert ledger(Channel.SMS)
    assert status("+13025550199") == voice_opt_out.CLEAR
    other = gate(monkeypatch, Channel.SMS, "+13025550199")
    assert not any("opt-out" in b for b in other.blockers)
    assert status(tenant=OTHER_TENANT) == voice_opt_out.CLEAR   # keyed per brokerage


# ── Valkey unreachable: fail closed for texts and calls, not email ───────────
def test_an_unreadable_shared_store_holds_sms_and_voice_but_not_email(monkeypatch, valkey, be):
    be("api-2")
    valkey.down = True
    assert status("+13025550199") == voice_opt_out.UNKNOWN
    for channel in (Channel.SMS, Channel.VOICE):
        decision = gate(monkeypatch, channel, "+13025550199")
        assert not decision.allowed
        assert outreach_compliance.OPT_OUT_STATUS_UNAVAILABLE in decision.blockers
        assert not any("opt-out list" in b for b in decision.blockers)   # not claimed to be on it
    email = gate(monkeypatch, Channel.EMAIL, "owner@example.test")
    assert outreach_compliance.OPT_OUT_STATUS_UNAVAILABLE not in email.blockers
    assert ledger(Channel.SMS, "+13025550199")
    assert not ledger(Channel.EMAIL, "owner@example.test")


def test_a_client_that_never_connected_is_unavailable_not_clear(monkeypatch):
    """REDIS_URL set, the shared client could not connect (get_redis_client
    returned None): UNKNOWN, through the real _shared_client."""
    import rate_limit_middleware

    async def none():
        return None

    monkeypatch.setenv("REDIS_URL", "redis://valkey.invalid:6379/0")
    monkeypatch.setattr(rate_limit_middleware, "get_redis_client", none)
    assert status() == voice_opt_out.UNKNOWN


def test_a_stalled_valkey_is_bounded(monkeypatch, valkey, be):
    be("api-1")
    monkeypatch.setattr(voice_opt_out, "SHARED_TIMEOUT_SECONDS", 0.05)
    valkey.stall = True
    writer(monkeypatch, ["fail"])
    import time

    started = time.monotonic()
    honour_now()
    assert status("+13025550199") == voice_opt_out.UNKNOWN
    assert time.monotonic() - started < 2.0
    assert voice_opt_out.pending_opt_out(TENANT, NUMBER)   # the spool still holds it


def test_no_shared_store_configured_is_this_containers_spool_only(monkeypatch, valkey, be):
    monkeypatch.delenv("REDIS_URL")
    be("api-1")
    writer(monkeypatch, ["fail"])
    honour_now()
    assert valkey.ops == 0
    assert status() == voice_opt_out.PENDING
    be("api-2")
    assert status() == voice_opt_out.CLEAR and valkey.ops == 0
    assert health()["detail"]["shared_store"] == "not_configured"


# ── Valkey down when the STOP is heard, then back (empty) ────────────────────
def test_valkey_down_at_honour_the_spool_still_blocks_and_the_sweep_re_writes_it(monkeypatch, valkey, be):
    be("api-1")
    valkey.down = True
    writer(monkeypatch, ["fail"])
    honour_now()
    assert status() == voice_opt_out.PENDING                       # the local spool still blocks
    assert not gate(monkeypatch, Channel.SMS).allowed
    comp = health()
    assert comp["state"] == component_health.DEGRADED and "unreachable" in comp["summary"]

    be("api-2")
    assert status() == voice_opt_out.UNKNOWN                       # held, not guessed clear

    valkey.down = False                                             # back, with nothing in it
    assert status() == voice_opt_out.CLEAR                         # the documented window
    be("api-1")
    assert health()["detail"]["unmirrored"] == 1
    writer(monkeypatch, ["fail"])
    assert asyncio.run(voice_opt_out.replay_spool()) == {"replayed": 0, "still_pending": 1}
    assert entry_name() in valkey.kv and valkey.ttl[entry_name()] == MONTH
    be("api-2")
    assert status() == voice_opt_out.PENDING


def test_the_owner_retry_loop_re_writes_an_entry_valkey_lost(monkeypatch, valkey, be):
    be("api-1")

    def lose_on_second_attempt(n):
        if n == 2:
            valkey.lose_everything()                                # Valkey restarted without its data

    writer(monkeypatch, ["fail", "fail", "fail"], on_call=lose_on_second_attempt)
    honour_and_drain()
    assert entry_name() in valkey.kv                                # re-asserted by a later retry
    assert voice_opt_out._member((TENANT, NUMBER)) in valkey.sets[voice_opt_out.SHARED_INDEX]


# ── removed only after the row commits ──────────────────────────────────────
def test_the_shared_entry_is_removed_only_after_the_row_commits(monkeypatch, valkey, be):
    be("api-1")
    writer(monkeypatch, ["fail"])
    honour_now()
    seen_at_commit = []
    writer(monkeypatch, ["ok"], on_call=lambda _n: seen_at_commit.append(entry_name() in valkey.kv))
    assert asyncio.run(voice_opt_out.replay_spool()) == {"replayed": 1, "still_pending": 0}
    assert seen_at_commit == [True]                                 # still blocking while the row was written
    assert valkey.kv == {} and valkey.sets[voice_opt_out.SHARED_INDEX] == set()
    assert voice_opt_out.pending_count() == 0
    be("api-2")
    assert status() == voice_opt_out.CLEAR
    assert health()["state"] == component_health.HEALTHY


def test_the_owner_retry_removes_the_entry_after_its_write_lands(monkeypatch, valkey, be):
    be("api-1")
    seen_at_commit = []
    writer(monkeypatch, ["fail", "fail", "ok"],
           on_call=lambda n: seen_at_commit.append(entry_name() in valkey.kv) if n == 3 else None)
    honour_and_drain()
    assert seen_at_commit == [True]
    assert valkey.kv == {} and voice_opt_out.pending_count() == 0


def test_an_entry_the_sweep_cannot_write_keeps_blocking_with_its_ttl_reset(monkeypatch, valkey, be):
    be("api-1")
    writer(monkeypatch, ["fail"])
    honour_now()
    valkey.ttl[entry_name()] = 5                                    # nearly lapsed
    be("worker")
    writer(monkeypatch, ["fail"])
    result = asyncio.run(voice_opt_out.replay_shared())
    assert result["still_pending"] == 1 and result["replayed"] == 0
    assert valkey.ttl[entry_name()] == MONTH
    assert status() == voice_opt_out.PENDING


# ── the worker writes a STOP whose api container is gone ────────────────────
def test_the_worker_writes_a_stop_whose_api_container_was_redeployed(monkeypatch, valkey, be):
    spool = be("api-1")
    writer(monkeypatch, ["fail"])
    honour_now()
    spool.unlink()                                                  # redeploy: the container disk is gone
    be("worker")
    calls = writer(monkeypatch, ["ok"])
    result = asyncio.run(voice_opt_out.replay_shared())
    assert result == {"replayed": 1, "still_pending": 0, "unreadable": 0, "gone": 0}
    assert calls == [{"tenant_id": TENANT, "callee": NUMBER}]
    assert valkey.kv == {}
    be("api-2")
    assert status() == voice_opt_out.CLEAR


def test_the_sweep_never_writes_an_entry_it_cannot_read_or_that_was_moved(monkeypatch, valkey, be):
    be("worker")
    good_member = voice_opt_out._member((TENANT, NUMBER))
    other_key = secrets.token_hex(32)
    real_key = voice_opt_out._master_key()
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", other_key)
    foreign = voice_opt_out._seal((TENANT, NUMBER))                 # sealed under another master key
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", real_key)
    moved = voice_opt_out._seal((OTHER_TENANT, "+13025550111"))     # a value copied under this key
    second_member = voice_opt_out._member((TENANT, "+13025550123"))
    for member, value in ((good_member, foreign), (second_member, moved)):
        valkey.kv[voice_opt_out.SHARED_PREFIX + member] = value
        valkey.sets.setdefault(voice_opt_out.SHARED_INDEX, set()).add(member)
    valkey.sets[voice_opt_out.SHARED_INDEX].add(f"{TENANT}:lapsed")   # its key expired
    calls = writer(monkeypatch, ["ok", "ok"])
    result = asyncio.run(voice_opt_out.replay_shared())
    assert calls == [] and result == {"replayed": 0, "still_pending": 0, "unreadable": 2, "gone": 1}
    assert len(valkey.kv) == 2                                      # kept: they still block
    assert f"{TENANT}:lapsed" not in valkey.sets[voice_opt_out.SHARED_INDEX]
    assert status() == voice_opt_out.PENDING


def test_a_targeted_sweep_touches_only_its_tenant(monkeypatch, valkey, be):
    be("api-1")
    writer(monkeypatch, ["fail", "fail"])
    honour_now()
    honour_now({**STATE, "tenant_id": OTHER_TENANT})
    be("worker")
    calls = writer(monkeypatch, ["ok", "ok"])
    assert asyncio.run(voice_opt_out.replay_shared(OTHER_TENANT))["replayed"] == 1
    assert calls == [{"tenant_id": OTHER_TENANT, "callee": NUMBER}]
    assert status() == voice_opt_out.PENDING and status(tenant=OTHER_TENANT) == voice_opt_out.CLEAR


# ── health is no longer per replica ─────────────────────────────────────────
def test_health_on_any_replica_reports_a_stop_pending_elsewhere(monkeypatch, valkey, be):
    be("api-1")
    writer(monkeypatch, ["fail"])
    honour_now()
    be("api-2")
    comp = health()
    assert comp["state"] == component_health.DEGRADED
    assert comp["detail"]["pending"] == 0 and comp["detail"]["pending_shared"] == 1
    valkey.down = True
    assert "unreachable" in health()["summary"]


def test_the_sweep_runs_the_shared_replay_for_its_tenant_scope():
    import inspect

    import reconciliation

    source = inspect.getsource(reconciliation.run_sweep)
    assert "voice_opt_out.replay_shared(only_tenant)" in source
    assert source.index("replay_spool()") < source.index("replay_shared(")


def test_without_a_master_key_the_entry_is_what_the_local_spool_holds(monkeypatch, valkey, be):
    """Development only (production requires the master key): no key to hash
    or seal with, so the entry carries the tenant and number exactly as the
    local spool does."""
    monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY")
    be("api-1")
    writer(monkeypatch, ["fail"])
    honour_now()
    name = f"{voice_opt_out.SHARED_PREFIX}{TENANT}:{NUMBER}"
    assert list(valkey.kv) == [name]
    assert json.loads(valkey.kv[name]) == {"v": 1, "tenant_id": TENANT, "contact": NUMBER}
    be("api-2")
    assert status() == voice_opt_out.PENDING
