"""A brokerage erasure says "succeeded" only when it did (external audit 22.2,
22.3, 22.4, 29.4).

It used to:
* count object storage that was not configured, or a prefix that could not be
  listed, as zero objects left, i.e. clean (22.2);
* mark the operation succeeded while the restore directive was unwritten, so a
  restore from an older backup would bring the brokerage back and nothing
  would re-erase it (22.3, 29.4);
* answer "succeeded" from run_erasure whatever the receipt had recorded, and
  complete the job, so cleanup that a retry could finish never got one (22.4).

These drive the real phases and run_erasure over an in-memory operation row
and a fake object store.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

import object_storage
import privacy_lifecycle as pl

TENANT = "00000000-0000-0000-0000-0000000000e1"
OP = "00000000-0000-0000-0000-00000000e0e1"


class Store:
    def __init__(self):
        self.configured = True
        self.objects: dict[str, bytes] = {}
        self.list_fails: set[str] = set()
        self.put_fails = False
        self.corrupt_reads = False

    def install(self, monkeypatch):
        monkeypatch.setattr(object_storage, "is_configured", lambda: self.configured)
        monkeypatch.setattr(object_storage, "delete_object", self.delete_object)
        monkeypatch.setattr(object_storage, "delete_prefix", self.delete_prefix)
        monkeypatch.setattr(object_storage, "list_prefix", self.list_prefix)
        monkeypatch.setattr(object_storage, "put_bytes", self.put_bytes)
        monkeypatch.setattr(object_storage, "get_bytes", self.get_bytes)

    def delete_object(self, key):
        return self.objects.pop(key, None) is not None

    def delete_prefix(self, prefix):
        keys = [k for k in self.objects if k.startswith(prefix)]
        for k in keys:
            del self.objects[k]
        return len(keys)

    def list_prefix(self, prefix, limit=100_000):
        if prefix in self.list_fails:
            raise TimeoutError("listing timed out")
        return [k for k in self.objects if k.startswith(prefix)][:limit]

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        if self.put_fails:
            raise ConnectionError("store unavailable")
        self.objects[key] = data
        return key

    def get_bytes(self, key):
        data = self.objects[key]
        return data + b"x" if self.corrupt_reads else data


class Db:
    def __init__(self):
        self.op = {"id": OP, "kind": "erasure", "state": "running", "tenant_id": TENANT,
                   "progress": {}, "result": {}, "receipt": None, "error": None}

    def conn(self):
        db = self

        class Conn:
            async def fetchrow(self, sql, *args):
                assert "FROM privacy_operations" in sql
                return dict(db.op, progress=json.dumps(db.op["progress"]))

            async def fetchval(self, sql, *args):
                if sql.startswith("SELECT state FROM privacy_operations"):
                    return db.op["state"]
                raise AssertionError(sql)

            async def fetch(self, sql, *args):
                return []

            async def execute(self, sql, *args):
                if "SET progress=" in sql:
                    db.op["progress"] = json.loads(args[1])
                elif "SET state=$2" in sql:          # _finish_operation
                    db.op.update(state=args[1], error=args[4])
                    db.op["result"].update(json.loads(args[2]))
                    if args[3] is not None:
                        db.op["receipt"] = json.loads(args[3])
                elif "SET result = result ||" in sql:
                    db.op["result"].update(json.loads(args[1]))
                    db.op["error"] = args[2]
                elif "SET state='failed'" in sql:     # _fail_if_running
                    if db.op["state"] == "running":
                        db.op.update(state="failed", error=args[2])
                        db.op["result"].update(json.loads(args[1]))
                elif sql.lstrip().startswith("INSERT INTO erasure_ledger"):
                    pass
                else:
                    raise AssertionError(sql)

        return Conn()


@pytest.fixture
def world(monkeypatch):
    store, db = Store(), Db()
    store.install(monkeypatch)

    @asynccontextmanager
    async def tx(_ctx):
        yield db.conn()

    async def catalog(_conn):
        return [], []

    ran: list[str] = []

    def stub(name, result=None):
        async def handler(ctx, op_id, tenant_id, progress):
            ran.append(name)
            return dict(result or {})
        return handler

    monkeypatch.setattr(pl, "tenant_tx", tx)
    monkeypatch.setattr(pl, "_erasure_catalog", catalog)
    monkeypatch.setattr(pl, "emit_event", lambda *a, **k: None)
    handlers = dict(pl._PHASE_HANDLERS)
    for name in ("freeze", "providers", "tombstones", "rows", "pseudonymize", "caches", "tenant_row"):
        handlers[name] = stub(name, {"failed": 0} if name == "providers" else {})
    monkeypatch.setattr(pl, "_PHASE_HANDLERS", handlers)
    return store, db, ran, handlers


def _verify(progress=None):
    return asyncio.run(pl._phase_verify(None, OP, TENANT, progress or {}))


# ── 22.2: verification that proves nothing is not "clean" ──────────────────

def test_unconfigured_storage_is_not_verified_clean(world):
    store, *_ = world
    store.configured = False
    out = _verify()
    assert out["clean"] is False
    assert out["objects_unverified"] == pl.tenant_prefixes(TENANT)


def test_a_prefix_that_cannot_be_listed_is_not_clean(world):
    store, *_ = world
    prefix = pl.tenant_prefixes(TENANT)[0]
    store.list_fails.add(prefix)
    out = _verify()
    assert out["clean"] is False and out["objects_unverified"] == [prefix]


def test_a_reachable_empty_store_is_clean(world):
    assert _verify()["clean"] is True


# ── 22.3 / 29.4: the restore directive is part of success ─────────────────

def test_a_directive_that_was_not_read_back_is_not_written(world):
    store, *_ = world
    store.corrupt_reads = True
    assert pl._write_erasure_directive(OP, TENANT, {"erased_at": "t"}) is False
    store.corrupt_reads = False
    assert pl._write_erasure_directive(OP, TENANT, {"erased_at": "t"}) is True


def test_an_unwritten_directive_keeps_the_erasure_running_and_retries(world):
    store, db, ran, _ = world
    store.put_fails = True
    with pytest.raises(pl.ErasureIncomplete):
        asyncio.run(pl.run_erasure(OP))
    assert db.op["state"] == "running"                        # not "succeeded"
    assert db.op["result"]["cleanup_pending"] == ["directive"]
    progress = db.op["progress"]
    assert progress["receipt"]["done"] is False and progress["verify"]["done"] is False
    assert progress["rows"]["done"] is True                   # not redone needlessly

    store.put_fails = False                                    # the store is back
    ran.clear()
    result = asyncio.run(pl.run_erasure(OP))
    assert result["state"] == "succeeded" == db.op["state"]
    assert ran == []                                           # only verify + receipt re-ran
    assert f"privacy/erasure-directives/{OP}.json" in store.objects


# ── 22.4: stored state, retry, then an honest terminal state ──────────────

def test_storage_down_retries_the_objects_and_carries_failed_keys(world, monkeypatch):
    store, db, _, handlers = world
    store.configured = False
    keys = iter([(["property-media/elsewhere/a.jpg"], []), ([], [])])  # rows gone on retry

    async def object_keys(_conn, _tenant):
        return next(keys)

    monkeypatch.setattr(pl, "_object_keys", object_keys)
    with pytest.raises(pl.ErasureIncomplete):
        asyncio.run(pl.run_erasure(OP))
    assert db.op["state"] == "running"
    assert db.op["result"]["cleanup_pending"] == ["objects", "directive"]
    assert db.op["progress"]["objects"]["failed_media_keys"] == ["property-media/elsewhere/a.jpg"]

    store.configured = True
    store.objects["property-media/elsewhere/a.jpg"] = b"photo"
    assert asyncio.run(pl.run_erasure(OP))["state"] == "succeeded"
    assert "property-media/elsewhere/a.jpg" not in store.objects  # the carried key was deleted


def test_the_final_attempt_fails_the_operation_instead_of_succeeding(world):
    store, db, *_ = world
    store.configured = False
    result = asyncio.run(pl.run_erasure(OP, final_attempt=True))
    assert result["state"] == "failed" == db.op["state"]
    assert db.op["error"].startswith("cleanup pending")


def test_a_provider_failure_is_terminal_and_reported_failed(world):
    """Route rows are gone after the rows phase, so a retry cannot release the
    number: the operation fails for a person to finish (no endless retry)."""
    _, db, _, handlers = world

    async def providers(ctx, op_id, tenant_id, progress):
        return {"steps": [{"provider": "twilio", "action": "release_number", "status": "failed"}],
                "failed": 1}

    handlers["providers"] = providers
    result = asyncio.run(pl.run_erasure(OP))
    assert result["state"] == "failed" == db.op["state"]
    assert db.op["result"]["cleanup_pending"] == ["providers"]


def test_a_finished_operation_answers_its_stored_state(world):
    _, db, *_ = world
    db.op["state"] = "failed"
    assert asyncio.run(pl.run_erasure(OP))["state"] == "failed"


def test_the_job_runs_the_last_attempt_as_final_and_never_reports_success(monkeypatch):
    seen = {}

    async def run_erasure(op, *, final_attempt=False):
        seen["final"] = final_attempt
        return {"state": "failed", "operation_id": op}

    monkeypatch.setattr(pl, "run_erasure", run_erasure)

    class Reporter:
        job = {"attempt_count": 20, "max_attempts": 20}

    out = asyncio.run(pl._erase_job({"operation_id": OP}, Reporter()))
    assert seen["final"] is True
    assert out["_terminal_state"] == "partial"                 # the job is not "succeeded"

    Reporter.job = {"attempt_count": 3, "max_attempts": 20}
    asyncio.run(pl._erase_job({"operation_id": OP}, Reporter()))
    assert seen["final"] is False


def test_leftover_rows_are_terminal_not_retried(world):
    """tenant_row has moved the tenant to 'erased', and the erase functions
    refuse anything but 'erasing': a rows retry could only crash (review)."""
    _, db, _, handlers = world

    async def dirty_verify(ctx, op_id, tenant_id, progress):
        return {"rows_left": {"clients": 1}, "objects_left": 0, "objects_unverified": [],
                "provider_failures": 0, "object_failures": 0, "clean": False}

    handlers["verify"] = dirty_verify
    result = asyncio.run(pl.run_erasure(OP))
    assert result["state"] == "failed" == db.op["state"]
    assert db.op["result"]["cleanup_pending"] == ["rows"]
    assert db.op["progress"]["rows"]["done"] is True               # never reopened


def test_a_phase_that_raises_on_the_final_attempt_fails_the_operation(world):
    _, db, _, handlers = world

    async def broken(ctx, op_id, tenant_id, progress):
        raise ConnectionError("database blip")

    handlers["tombstones"] = broken
    with pytest.raises(ConnectionError):
        asyncio.run(pl.run_erasure(OP))
    assert db.op["state"] == "running"                             # not final: the job retries
    with pytest.raises(ConnectionError):
        asyncio.run(pl.run_erasure(OP, final_attempt=True))
    assert db.op["state"] == "failed" and "final attempt" in db.op["error"]


def test_a_retry_left_by_a_crash_is_not_reopened_on_the_final_attempt(world):
    _, db, *_ = world
    db.op["progress"] = {p: {"done": True} for p in pl.PHASES}
    db.op["progress"]["receipt"].update(retry=["verify", "receipt"], cleanup_pending=["directive"])
    result = asyncio.run(pl.run_erasure(OP, final_attempt=True))
    assert result["state"] == "failed" == db.op["state"]
    assert db.op["result"]["cleanup_pending"] == ["directive"]


def test_a_finished_operation_is_never_overwritten_by_the_failure_path(world):
    _, db, *_ = world
    db.op["state"] = "succeeded"
    asyncio.run(pl._fail_if_running(None, OP, ["incomplete"], "x"))
    assert db.op["state"] == "succeeded"
