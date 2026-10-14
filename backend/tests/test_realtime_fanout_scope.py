"""Real-time pushes are an authorization layer too (CONTRACT C3.6, ledger
L03-08): a frame that carries a client's or contact's content goes only to the
members who may read that record, never to the whole brokerage.

Unit level (no database): the hub delivers a scoped frame to exactly the
named agents, mirrors only a content-free stub to the platform firehose,
crosses replicas in an envelope a previous-release replica drops instead of
fanning out, and fails closed when the audience cannot be computed. The
inventory test pins every tenant-wide fan-out in the backend to a reviewed
reason, so a new one cannot appear unclassified.

The database half (who the audience is) is
tests/test_realtime_record_scope_live.py.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

import ws_hub

BACKEND = Path(__file__).resolve().parents[1]

# Every call in backend/ (outside ws_hub.py and tests) that fans a frame out to
# a WHOLE tenant group, with why that is allowed. Record-content frames about
# clients/contacts use ws_hub.broadcast_record; private ones broadcast_user.
TENANT_WIDE = {
    ("voice_intel.py", "_process", "broadcast"):
        "VOICE_NOTE_LOGGED: a walkthrough note on a LEAD; leads are brokerage-wide (L03-03)",
    ("reconstruction_worker.py", "_process", "broadcast"):
        "SPLAT_READY: a lead/listing's captured space; property media is brokerage-wide",
    ("automation_jobs.py", "JobReporter.progress", "broadcast"):
        "JOB_PROGRESS: job id, type, percent and a fixed stage message; no record content",
    ("video_studio.py", "_process_job", "broadcast"):
        "VIDEO_READY: job and media ids of a property video; property media is brokerage-wide",
    ("disposition_enforcer.py", "sweep_once", "broadcast"):
        "CONTRACT_EXPIRED / CONTRACT_DANGER: a LEAD's contract clock (address, parcel); "
        "leads are brokerage-wide",
    ("lead_dossier.py", "move_dossier", "broadcast"):
        "DOSSIER_MOVED: a LEAD's pipeline stage; leads are brokerage-wide",
    ("server.py", "websocket_endpoint._publish_to_tenant", "deliver_local"):
        "workflow-engine ambient frames: public-record review candidates, simulated demo transcript",
}
FANOUT = {"broadcast", "deliver_local"}


def _tenant_wide_calls() -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith(("tests/", "scripts/")) or rel == "ws_hub.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack: list[str] = []

        class Visitor(ast.NodeVisitor):
            def _scope(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

            def visit_Call(self, node):
                func = node.func
                name = None
                if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                        and func.value.id == "ws_hub" and func.attr in FANOUT):
                    name = func.attr
                elif isinstance(func, ast.Name) and func.id in FANOUT:
                    name = func.id
                if name:
                    found.add((rel, ".".join(stack), name))
                self.generic_visit(node)

        Visitor().visit(tree)
    return found


def test_every_tenant_wide_fanout_is_classified():
    found = _tenant_wide_calls()
    assert found == set(TENANT_WIDE), (
        "a tenant-wide WebSocket fan-out was added or moved; if its frame carries a client's or "
        "contact's content use ws_hub.broadcast_record (C3.6), else classify it here: "
        f"new={sorted(found - set(TENANT_WIDE))} gone={sorted(set(TENANT_WIDE) - found)}")


def test_call_and_voice_frames_follow_the_record():
    import commands_api
    import inspect
    import voice_intel

    for fn in (commands_api.call_telemetry, commands_api.record_call_consent,
               commands_api.deliver_call_notes, voice_intel._broadcast_voice):
        src = inspect.getsource(fn)
        assert "ws_hub.broadcast_record(" in src, fn.__name__
        assert "ws_hub.broadcast(" not in src, f"{fn.__name__} fans a record's content out tenant-wide"


# ── the hub ────────────────────────────────────────────────────────────────


class FakeSocket:
    def __init__(self):
        self.frames: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.frames.append(json.loads(text))


@pytest.fixture()
def hub(monkeypatch):
    monkeypatch.setattr(ws_hub, "_sockets", ws_hub.defaultdict(set))
    monkeypatch.setattr(ws_hub, "_user_sockets", ws_hub.defaultdict(set))
    monkeypatch.setattr(ws_hub, "_pool", None)
    monkeypatch.setattr(ws_hub, "_listener_started", False)
    tenant = "11111111-1111-1111-1111-111111111111"
    socks = {name: FakeSocket() for name in ("owner", "agent-a", "agent-b", "anon", "firehose", "other")}
    ws_hub.register(tenant, socks["owner"], "owner@x.test")
    ws_hub.register(tenant, socks["agent-a"], "Agent-A@x.test")
    ws_hub.register(tenant, socks["agent-b"], "agent-b@x.test")
    ws_hub.register(tenant, socks["anon"])
    ws_hub.register(ws_hub.FIREHOSE_TENANT_ID, socks["firehose"], "ops")
    ws_hub.register("22222222-2222-2222-2222-222222222222", socks["other"], "agent-a@x.test")
    return tenant, socks


def _audience(monkeypatch, value):
    import record_access

    calls = []

    async def fake(tenant_id, client_id, contact_id):
        calls.append((tenant_id, client_id, contact_id))
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(record_access, "linked_audience", fake)
    return calls


FRAME = {"type": "NEGOTIATION_TELEMETRY", "counter_offer": 250000, "arv": 410000}


def test_a_private_clients_frame_reaches_only_its_audience(hub, monkeypatch):
    tenant, socks = hub
    calls = _audience(monkeypatch, ["agent-a@x.test", "owner@x.test"])
    reached = asyncio.run(ws_hub.broadcast_record(tenant, FRAME, client_id="c1", actor="agent-a@x.test"))
    assert reached == 2 and calls == [(tenant, "c1", None)], "audience computed once per frame"
    assert socks["agent-a"].frames == [FRAME], "agent ids compare case-insensitively"
    assert socks["owner"].frames == [FRAME]
    assert socks["agent-b"].frames == [] and socks["anon"].frames == []
    assert socks["other"].frames == [], "another brokerage's agent with the same id"
    assert socks["firehose"].frames == [
        {"type": "NEGOTIATION_TELEMETRY", "scoped": True, "source_tenant": tenant}
    ], "the firehose sees that it happened, never the offer"


def test_a_pool_frame_still_reaches_everyone(hub, monkeypatch):
    tenant, socks = hub
    _audience(monkeypatch, None)
    asyncio.run(ws_hub.broadcast_record(tenant, FRAME, client_id="pool-client"))
    for name in ("owner", "agent-a", "agent-b", "anon"):
        assert socks[name].frames == [FRAME], name
    assert socks["other"].frames == []
    assert socks["firehose"].frames == [{**FRAME, "source_tenant": tenant}], "unchanged for pool frames"


def test_only_narrows_and_never_widens(hub, monkeypatch):
    tenant, socks = hub
    _audience(monkeypatch, ["owner@x.test", "agent-a@x.test"])
    # The requester lost access (agent-b); the assignee keeps it.
    asyncio.run(ws_hub.broadcast_record(tenant, FRAME, client_id="c1",
                                        only=["agent-b@x.test", "agent-a@x.test"]))
    assert socks["agent-a"].frames == [FRAME]
    assert socks["agent-b"].frames == [] and socks["owner"].frames == []
    # A pool client: exactly `only`, not the brokerage.
    _audience(monkeypatch, None)
    asyncio.run(ws_hub.broadcast_record(tenant, FRAME, client_id="pool", only=["agent-b@x.test"]))
    assert socks["agent-b"].frames == [FRAME] and socks["owner"].frames == []


def test_an_unknown_audience_fails_closed_to_the_actor(hub, monkeypatch):
    tenant, socks = hub
    _audience(monkeypatch, RuntimeError("db down"))
    asyncio.run(ws_hub.broadcast_record(tenant, FRAME, client_id="c1", actor="agent-b@x.test"))
    assert socks["agent-b"].frames == [FRAME]
    assert socks["agent-a"].frames == [] and socks["owner"].frames == []
    asyncio.run(ws_hub.broadcast_record(tenant, FRAME, client_id="c1"))
    assert socks["owner"].frames == [] and socks["anon"].frames == [], "no actor: nobody"


def test_an_empty_audience_reaches_nobody(hub, monkeypatch):
    tenant, socks = hub
    _audience(monkeypatch, [])
    assert asyncio.run(ws_hub.broadcast_record(tenant, FRAME, client_id="gone")) == 0
    assert all(s.frames == [] for s in socks.values())


def test_a_voice_session_group_is_never_mirrored_to_the_firehose(hub):
    _, socks = hub
    group = "voice:t:s"
    watcher = FakeSocket()
    ws_hub.register(group, watcher)
    try:
        asyncio.run(ws_hub.broadcast_group(group, {"type": "VOICE_TELEMETRY", "transcript": {"text": "x"}}))
    finally:
        ws_hub.unregister(group, watcher)
    assert watcher.frames and socks["firehose"].frames == []


def test_scoped_frames_cross_replicas_to_the_named_agents_only(hub, monkeypatch):
    tenant, socks = hub
    sent: list[str] = []

    class Conn:
        async def execute(self, _sql, _channel, envelope):
            sent.append(envelope)

    class Pool:
        def acquire(self):
            pool = self

            class Ctx:
                async def __aenter__(self_inner):
                    return Conn()

                async def __aexit__(self_inner, *exc):
                    return False

            return Ctx()

    monkeypatch.setattr(ws_hub, "_pool", Pool())
    monkeypatch.setattr(ws_hub, "_listener_started", True)
    asyncio.run(ws_hub.broadcast_users(tenant, ["agent-a@x.test"], FRAME))
    assert len(sent) == 1, "one NOTIFY per frame, whatever the audience size"
    envelope = json.loads(sent[0])
    # No "payload" key: a replica still on the previous release reads
    # envelope["payload"] first, logs it malformed and delivers it to nobody
    # during a rolling deploy, instead of to the whole brokerage.
    assert envelope["user_ids"] == ["agent-a@x.test"] and "payload" not in envelope

    # Another replica receives it.
    for s in socks.values():
        s.frames.clear()
    asyncio.run(ws_hub._receive_notification(json.dumps({**envelope, "origin": "other-replica"})))
    assert socks["agent-a"].frames == [FRAME]
    assert socks["agent-b"].frames == [] and socks["owner"].frames == [] and socks["anon"].frames == []
    assert socks["firehose"].frames == [{"type": FRAME["type"], "scoped": True, "source_tenant": tenant}]


def test_malformed_scoped_envelopes_are_ignored(hub):
    tenant, socks = hub
    for bad in ({"tenant_id": tenant, "user_ids": "agent-a@x.test", "scoped_payload": FRAME},
                {"tenant_id": tenant, "user_ids": [1], "scoped_payload": FRAME},
                {"tenant_id": tenant, "user_ids": None, "scoped_payload": "x"}):
        asyncio.run(ws_hub._receive_notification(json.dumps({"origin": "o", **bad})))
    assert all(s.frames == [] for s in socks.values())
