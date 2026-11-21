"""Pending spoken STOPs on a REAL Valkey, across separate processes (external
audit 34.5; ledger L09-11). Skipped unless ORACLE_LIVE_VALKEY_URL points at a
THROWAWAY Valkey (only keys of this test's random tenants are touched).

Every "container" is its own OS process with its own spool file, sharing one
Valkey through a proxy the test can cut, as a server that dies is cut off.
The suppression write is faked (no database): "db down" fails it, "db up"
commits it and records whether the shared entry still existed at commit.

The same file is the replica command line for a drill that kills the Valkey
container itself (see the ledger entry):
    python tests/test_voice_opt_out_valkey_live.py honour|status|gate|replay_spool|replay_shared|health ...
with REDIS_URL, ORACLE_OPT_OUT_SPOOL, ORACLE_ENCRYPTION_MASTER_KEY, LIVE_DB=up|down and
LIVE_DB_LOG set.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import urllib.parse
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
CALLEE = "+13025550142"          # 555-01xx: reserved for fiction
SECOND = "+13025550143"


# ── the replica command line (runs in a child process) ──────────────────────
def _replica_main(argv: list[str]) -> None:
    sys.path.insert(0, str(BACKEND))
    import voice_opt_out
    from outreach_compliance import Channel, normalize_contact

    async def fake_write(state):
        if os.environ.get("LIVE_DB") != "up":
            raise voice_opt_out.OptOutNotRecorded("db down (faked)")
        key = (str(state["tenant_id"]), normalize_contact(str(state["callee"]), Channel.VOICE))
        try:
            client = await voice_opt_out._shared_client()
            present = bool(await client.exists(voice_opt_out.SHARED_PREFIX + voice_opt_out._member(key)))
        except Exception:  # noqa: BLE001 - unknown is recorded as such
            present = None
        with open(os.environ["LIVE_DB_LOG"], "a", encoding="utf-8") as log:
            log.write(json.dumps({"tenant_id": key[0], "member": voice_opt_out._member(key),
                                  "entry_present_at_commit": present}) + "\n")

    voice_opt_out.record_outbound_opt_out = fake_write
    voice_opt_out.RETRY_DELAYS = (3600.0,)   # this process exits right after; the retry is not under test

    async def run() -> object:
        action, args = argv[0], argv[1:]
        if action == "honour":
            await voice_opt_out.honour({"tenant_id": args[0], "callee": args[1], "direction": "outbound"},
                                       "live-drill")
            # Asked on a SEPARATE, blocking connection before this event loop
            # runs anything else: a shared write left to a background task
            # would not be there yet (review of PR #173).
            import redis

            key = (args[0], normalize_contact(args[1], Channel.VOICE))
            try:
                with redis.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=2) as probe:
                    at_return = bool(probe.exists(voice_opt_out.SHARED_PREFIX + voice_opt_out._member(key)))
            except redis.RedisError:
                at_return = None
            for task in list(voice_opt_out._IN_FLIGHT):
                task.cancel()
            return {"spooled": voice_opt_out.pending_count(), "shared_at_return": at_return}
        if action == "status":
            return await voice_opt_out.opt_out_status(args[0], normalize_contact(args[1], Channel.VOICE))
        if action == "gate":
            return await _gate(args[0], args[1], Channel(args[2]))
        if action == "replay_spool":
            return await voice_opt_out.replay_spool()
        if action == "replay_shared":
            return await voice_opt_out.replay_shared(args[0] if args else None)
        if action == "health":
            import component_health

            return await component_health._outbound_opt_outs()
        raise SystemExit(f"unknown action {action}")

    print(json.dumps(asyncio.run(run())))


async def _gate(tenant: str, contact: str, channel) -> dict:
    from contextlib import asynccontextmanager

    import outreach_compliance
    from tenancy import Role, TenantContext

    @asynccontextmanager
    async def no_db(ctx, conn):
        yield None

    async def clean_row(*_a, **_kw):   # nothing in PostgreSQL blocks this number
        return {"suppressed": False, "has_consent": True, "has_written_consent": True,
                "vp_consent": True, "recent_voice": 0}

    outreach_compliance._conn_or_tx = no_db
    outreach_compliance.ConsentLedger.gather_state = clean_row
    ctx = TenantContext(agent_id="drill@x.test", tenant_id=tenant, role=Role.AGENT)
    decision = await outreach_compliance.guard_outreach(ctx, channel=channel, contact=contact,
                                                        state_code="DE", log=False)
    return {"allowed": decision.allowed, "blockers": list(decision.blockers)}


if __name__ == "__main__":
    _replica_main(sys.argv[1:])
    sys.exit(0)


# ── the live test (runs under pytest) ───────────────────────────────────────
import pytest  # noqa: E402

LIVE_URL = os.environ.get("ORACLE_LIVE_VALKEY_URL", "")
pytestmark = pytest.mark.skipif(not LIVE_URL, reason="needs ORACLE_LIVE_VALKEY_URL (a throwaway Valkey)")


class CuttableProxy:
    """TCP proxy in front of Valkey. cut() drops every connection and refuses
    new ones, as a dead server does; restore() lets them through again."""

    def __init__(self, host: str, port: int):
        self.target = (host, port)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.up = threading.Event()
        self.up.set()
        self.lock = threading.Lock()
        self.conns: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            if not self.up.is_set():
                client.close()
                continue
            try:
                upstream = socket.create_connection(self.target, timeout=2)
            except OSError:
                client.close()
                continue
            with self.lock:
                self.conns += [client, upstream]
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    @staticmethod
    def _pump(a: socket.socket, b: socket.socket) -> None:
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()

    def cut(self) -> None:
        self.up.clear()
        with self.lock:
            for s in self.conns:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()
            self.conns.clear()

    def restore(self) -> None:
        self.up.set()

    def close(self) -> None:
        self.cut()
        self.listener.close()


@pytest.fixture
def world(tmp_path):
    import redis

    parts = urllib.parse.urlsplit(LIVE_URL)
    proxy = CuttableProxy(parts.hostname, parts.port or 6379)
    auth = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
    proxied = f"{parts.scheme}://{auth}127.0.0.1:{proxy.port}{parts.path}"
    direct = redis.Redis.from_url(LIVE_URL, socket_timeout=2, decode_responses=True)
    tenants = [str(uuid.uuid4()), str(uuid.uuid4())]
    base = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "PYTHONPATH")}
    base.update(ORACLE_SKIP_DOTENV="1", LITELLM_MODE="PRODUCTION", REDIS_URL=proxied,
                ORACLE_ENCRYPTION_MASTER_KEY=secrets.token_hex(32),   # one "deployment", made at run time
                LIVE_DB_LOG=str(tmp_path / "db.jsonl"))

    def replica(container: str, action: str, *args: str, db: str = "down"):
        (tmp_path / container).mkdir(exist_ok=True)
        env = {**base, "ORACLE_OPT_OUT_SPOOL": str(tmp_path / container / "spool.jsonl"), "LIVE_DB": db}
        proc = subprocess.run([sys.executable, __file__, action, *args], cwd=BACKEND, env=env,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr[-2000:]
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def entries(tenant=None) -> list[str]:
        return sorted(direct.keys(f"voice:opt_out_pending:{tenant or '*'}:*"))

    def committed() -> list[dict]:
        log = tmp_path / "db.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def lose_data():   # a restart without persistence: this test's entries are gone
        for tenant in tenants:
            for name in entries(tenant):
                direct.delete(name)
        for member in direct.smembers("voice:opt_out_pending:index"):
            if member.split(":", 1)[0] in tenants:
                direct.srem("voice:opt_out_pending:index", member)

    w = type("World", (), {})()
    w.replica, w.entries, w.committed, w.lose_data = replica, entries, committed, lose_data
    w.proxy, w.tenants, w.tmp = proxy, tenants, tmp_path
    try:
        yield w
    finally:
        proxy.restore()
        lose_data()
        proxy.close()
        direct.close()


def test_a_stop_heard_on_one_replica_blocks_the_other_until_the_row_lands(world):
    t = world.tenants[0]
    assert world.replica("api-1", "honour", t, CALLEE) == {"spooled": 1, "shared_at_return": True}
    [name] = world.entries(t)
    assert CALLEE[2:] not in name                                       # keyed hash, not the number

    assert world.replica("api-2", "status", t, CALLEE) == "pending"     # before any DB write
    for channel in ("sms", "voice"):
        decision = world.replica("api-2", "gate", t, CALLEE, channel)
        assert decision["allowed"] is False and any("opt-out list" in b for b in decision["blockers"])
    assert world.replica("api-2", "status", t, SECOND) == "clear"
    assert world.replica("api-2", "health")["detail"]["pending_shared"] >= 1

    assert world.replica("api-1", "replay_spool", db="up") == {"replayed": 1, "still_pending": 0}
    assert world.committed() == [{"tenant_id": t, "member": name[len("voice:opt_out_pending:"):],
                                  "entry_present_at_commit": True}]
    assert world.entries(t) == []                                       # removed after the commit
    assert world.replica("api-2", "status", t, CALLEE) == "clear"


def test_valkey_cut_mid_flight_the_spool_blocks_others_fail_closed_and_the_entry_returns(world):
    t = world.tenants[0]
    assert world.replica("api-1", "honour", t, CALLEE)["shared_at_return"] is True
    assert len(world.entries(t)) == 1

    world.proxy.cut()                                                   # Valkey gone...
    world.lose_data()                                                   # ...and its data with it
    assert world.replica("api-1", "status", t, CALLEE) == "pending"     # the owner's spool still blocks
    assert world.replica("api-2", "status", t, CALLEE) == "unknown"     # never guessed clear
    held = world.replica("api-2", "gate", t, SECOND, "sms")
    assert held["allowed"] is False and any("unavailable" in b for b in held["blockers"])
    email = world.replica("api-2", "gate", t, "owner@example.test", "email")
    assert not any("unavailable" in b for b in email["blockers"])      # email is not held
    assert "unreachable" in world.replica("api-1", "health")["summary"]
    assert world.replica("api-1", "replay_spool") == {"replayed": 0, "still_pending": 1}

    world.proxy.restore()                                               # Valkey back, empty
    assert world.replica("api-2", "status", t, CALLEE) == "clear"       # the documented window
    assert world.replica("api-1", "health")["detail"]["unmirrored"] == 1
    assert world.replica("api-1", "replay_spool") == {"replayed": 0, "still_pending": 1}
    assert len(world.entries(t)) == 1                                   # re-written by the sweep
    assert world.replica("api-2", "status", t, CALLEE) == "pending"

    assert world.replica("api-1", "replay_spool", db="up")["replayed"] == 1
    assert [c["entry_present_at_commit"] for c in world.committed()] == [True]
    assert world.entries(t) == [] and world.replica("api-2", "status", t, CALLEE) == "clear"


def test_the_worker_writes_a_stop_after_the_api_container_was_redeployed(world):
    t, other = world.tenants
    world.replica("api-1", "honour", t, CALLEE)
    world.replica("api-1", "honour", other, SECOND)
    (world.tmp / "api-1" / "spool.jsonl").unlink()                      # redeploy: the disk is gone
    assert world.replica("api-2", "status", t, CALLEE) == "pending"     # still held everywhere

    result = world.replica("worker", "replay_shared", t, db="up")       # targeted to one tenant
    assert result["replayed"] == 1 and result["unreadable"] == 0
    assert world.entries(t) == [] and len(world.entries(other)) == 1
    assert world.replica("api-2", "status", t, CALLEE) == "clear"
    assert world.replica("api-2", "status", other, SECOND) == "pending"
