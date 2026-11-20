"""Real-time pushes are an authorization layer too (CONTRACT C3.6, ledger
L03-08): a frame that carries a client's or contact's content goes only to the
members who may read that record, never to the whole brokerage.

Unit level (no database): the hub delivers a scoped frame to exactly the
named agents, mirrors one full copy to the platform-admin firehose (owner
2026-10-14) but never the call-notes frame or the voice-session stream,
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
# Lead frames (VOICE_NOTE_LOGGED, DOSSIER_MOVED, CONTRACT_EXPIRED/DANGER,
# SPLAT_READY, VIDEO_READY) use broadcast_record(lead_id=...) since 0151
# (test_lead_frames_follow_the_lead).
TENANT_WIDE = {
    ("automation_jobs.py", "JobReporter.progress", "broadcast"):
        "JOB_PROGRESS: job id, type, percent and a fixed stage message; no record content",
    ("server.py", "make_engine_publisher.publish", "deliver_local"):
        "workflow-engine frames, filtered by engine_frame_is_tenant_wide to STATUS_UPDATE / STAGE_PROPERTY "
        "(no record content); DATA_PULLED, TRANSCRIPT_LINE, LEGAL_PACKAGE, PREDICTIVE_CACHE are dropped (27.3)",
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
                stack.append(node.name)  # noqa: B023 - the visitor runs within this iteration
                self.generic_visit(node)
                stack.pop()  # noqa: B023 - the visitor runs within this iteration

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
                    found.add((rel, ".".join(stack), name))  # noqa: B023 - the visitor runs within this iteration
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
               commands_api.deliver_call_notes, commands_api.save_call_note,
               voice_intel._broadcast_voice):
        src = inspect.getsource(fn)
        assert "ws_hub.broadcast_record(" in src, fn.__name__
        assert "ws_hub.broadcast(" not in src, f"{fn.__name__} fans a record's content out tenant-wide"


def test_lead_frames_follow_the_lead():
    """A frame about a lead, or a row hanging off one, is delivered by the
    lead's audience (0151): broadcast_record(..., lead_id=...)."""
    import inspect

    import disposition_enforcer
    import lead_dossier
    import reconstruction_worker
    import video_studio
    import voice_intel

    for module, frame in ((voice_intel, "VOICE_NOTE_LOGGED"), (reconstruction_worker, "SPLAT_READY"),
                          (video_studio, "VIDEO_READY"), (disposition_enforcer, "CONTRACT_EXPIRED"),
                          (disposition_enforcer, "CONTRACT_DANGER"), (lead_dossier, "DOSSIER_MOVED")):
        src = inspect.getsource(module)
        at = src.index(f'"type": "{frame}"')
        call = src.rfind("broadcast", 0, at)
        assert src[call:call + len("broadcast_record(")] == "broadcast_record(", (module.__name__, frame)
        assert "lead_id=" in src[at:src.index("\n\n", at)], (module.__name__, frame)


def test_a_lead_frame_goes_to_the_lead_audience(hub, monkeypatch):
    """broadcast_record(lead_id=) asks for the lead's audience, and a lead with
    no client (None) still reaches the whole brokerage."""
    import record_access

    asked = []

    async def audience(tenant_id, client_id, contact_id, lead_id=None):
        asked.append((client_id, contact_id, lead_id))
        return ["agent-a@x.test"] if lead_id == "private" else None

    monkeypatch.setattr(record_access, "linked_audience", audience)
    tenant, socks = hub
    asyncio.run(ws_hub.broadcast_record(tenant, {"type": "SPLAT_READY"}, lead_id="private"))
    asyncio.run(ws_hub.broadcast_record(tenant, {"type": "SPLAT_READY"}, lead_id="prospect"))
    assert len(socks["agent-a"].frames) == 2, "the client's agent gets both"
    assert len(socks["agent-b"].frames) == 1 and len(socks["anon"].frames) == 1, "only the prospect's"
    assert asked == [(None, None, "private"), (None, None, "prospect")]



def test_call_and_voice_frames_pass_the_calls_lead():
    """Audit 27.1: a call opened on a lead with no client/contact sent its
    transcript, offer and MAO to the WHOLE brokerage because these callers
    never passed the lead. Every record broadcast in them names lead_id."""
    import inspect
    import textwrap

    import commands_api
    import voice_intel

    for fn, callee in ((commands_api.call_telemetry, "broadcast_record"),
                       (commands_api.record_call_consent, "broadcast_record"),
                       (voice_intel._broadcast_voice, "broadcast_record"),
                       (voice_intel.create_voice_session, "_broadcast_voice"),
                       (voice_intel.ingest_voice_telemetry, "_broadcast_voice")):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and getattr(node.func, "attr", getattr(node.func, "id", None)) == callee]
        assert calls, (fn.__name__, callee)
        for call in calls:
            value = {k.arg: k.value for k in call.keywords}.get("lead_id")
            assert value is not None, f"{fn.__name__} drops the call's lead"
            # The call's OWN lead: `lead_id` or `call["lead_id"]`, never a constant
            # (a `lead_id=None` regression would silently widen to everyone).
            source = ast.unparse(value)
            assert source in {"lead_id", "call['lead_id']", 'call["lead_id"]'}, \
                f"{fn.__name__} passes lead_id={source}"


CALL_CONTENT_FRAMES = ("VOICE_", "NEGOTIATION_TELEMETRY", "CALL_CONSENT", "CALL_TRANSCRIPT")


def test_every_call_content_broadcast_names_its_lead():
    """By frame type, across the whole backend (review of PR #77): any
    function that builds a call-content frame and sends it with
    broadcast_record must pass the lead AND sensitive=True, so no site can fall
    back to the whole brokerage (owner decision 2026-11-16)."""
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith(("tests/", "scripts/")) or rel == "ws_hub.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            frames = [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant)
                      and isinstance(n.value, str) and n.value.startswith(CALL_CONTENT_FRAMES)
                      and n.value.isupper()]
            if not frames:
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "broadcast_record":
                    keywords = {k.arg: k.value for k in node.keywords}
                    sensitive = keywords.get("sensitive")
                    if "lead_id" not in keywords or not (isinstance(sensitive, ast.Constant)
                                                         and sensitive.value is True):
                        offenders.append(f"{rel}:{node.lineno} {fn.name} ({frames[0]})")
    assert not offenders, offenders


def test_a_lead_only_voice_frame_never_reaches_the_whole_brokerage(hub, monkeypatch):
    """Behaviour of 27.1: a voice frame for a call with neither client nor
    contact goes to the lead's audience, not to every member."""
    import record_access
    import voice_intel
    from tenancy import Role, TenantContext

    asked = []

    async def audience(tenant_id, client_id, contact_id, lead_id=None):
        asked.append((client_id, contact_id, lead_id))
        return ["agent-a@x.test"]

    monkeypatch.setattr(record_access, "linked_audience", audience)
    tenant, socks = hub
    ctx = TenantContext(agent_id="agent-a@x.test", tenant_id=tenant, role=Role.AGENT)
    frame = {"type": "VOICE_TELEMETRY", "transcript_excerpt": "my lowest is 240", "mao": 231000}
    asyncio.run(voice_intel._broadcast_voice(ctx, "session-1", frame, client_id=None,
                                             contact_id=None, lead_id="lead-private"))
    assert asked == [(None, None, "lead-private")]
    assert socks["agent-a"].frames == [frame]
    assert socks["agent-b"].frames == [] and socks["anon"].frames == [] and socks["owner"].frames == []


@pytest.mark.parametrize("audience, actor, expected", [
    (None, "agent-a@x.test", {"agent-a"}),          # pool / erased client's lead: the caller only
    (None, None, set()),                            # pool and no actor: nobody, never everyone
    ([], "agent-a@x.test", set()),                  # missing or foreign lead: nobody
    (["agent-b@x.test"], "agent-a@x.test", {"agent-b"}),  # a private record: exactly its audience
])
def test_a_sensitive_call_frame_never_reaches_the_whole_brokerage(hub, monkeypatch, audience, actor, expected):
    """Owner decision 2026-11-16: transcript / offer frames are never
    broadcast tenant-wide, whatever the record's pool status."""
    import record_access

    async def resolve(tenant_id, client_id, contact_id, lead_id=None):
        return audience

    monkeypatch.setattr(record_access, "linked_audience", resolve)
    tenant, socks = hub
    asyncio.run(ws_hub.broadcast_record(tenant, FRAME, lead_id="lead-1", actor=actor, sensitive=True))
    got = {name for name in ("owner", "agent-a", "agent-b", "anon") if socks[name].frames}
    assert got == expected


@pytest.mark.parametrize("only, expected", [(["agent-b@x.test", "owner@x.test"], set()),
                                            (["agent-a@x.test", "agent-b@x.test"], {"agent-a"})])
def test_a_sensitive_pool_frame_with_only_still_reaches_the_caller_only(hub, monkeypatch, only, expected):
    """Privacy review of #77: `only` must not widen a sensitive pool frame."""
    import record_access

    async def resolve(tenant_id, client_id, contact_id, lead_id=None):
        return None

    monkeypatch.setattr(record_access, "linked_audience", resolve)
    tenant, socks = hub
    asyncio.run(ws_hub.broadcast_record(tenant, FRAME, lead_id="pool", actor="agent-a@x.test",
                                        only=only, sensitive=True))
    assert {n for n in ("owner", "agent-a", "agent-b", "anon") if socks[n].frames} == expected


def test_a_non_sensitive_pool_frame_keeps_its_pool_audience(hub, monkeypatch):
    """Not a regression for ordinary pool frames (assignment notices etc.)."""
    import record_access

    async def resolve(tenant_id, client_id, contact_id, lead_id=None):
        return None

    monkeypatch.setattr(record_access, "linked_audience", resolve)
    tenant, socks = hub
    asyncio.run(ws_hub.broadcast_record(tenant, {"type": "DOSSIER_MOVED"}, lead_id="pool", actor="agent-a@x.test"))
    assert all(socks[n].frames for n in ("owner", "agent-a", "agent-b", "anon"))


@pytest.mark.parametrize("frame, tenant_wide", [
    ({"type": "STATUS_UPDATE", "agent": "AGENT ANALYST — scoring novelty across graph"}, True),
    ({"type": "STATUS_UPDATE", "agent": "WORKFLOW ENGINE — seeded 12 real parcels into graph"}, True),
    ({"type": "STAGE_PROPERTY"}, True),
    ({"type": "STATUS_UPDATE", "agent": "SCOUT → UNDERWRITER (DE) — parcel 1-23-45 score 91"}, False),
    ({"type": "STATUS_UPDATE", "agent": "AGENT LEGAL — generating contract package"}, False),
    ({"type": "STATUS_UPDATE", "agent": "AGENT ANALYST — scoring novelty across graph", "data": {"x": 1}}, False),
    ({"type": "STAGE_PROPERTY", "data": {"address": "1 Main St"}}, False),
    ({"type": "DATA_PULLED", "data": {"address": "1 Main St", "investorOffer": 180000}}, False),
    ({"type": "TRANSCRIPT_LINE", "data": {"text": "Initiating contact with Pat Owner..."}}, False),
    ({"type": "LEGAL_PACKAGE", "data": {"price": 180000}}, False),
    ({"type": "PREDICTIVE_CACHE", "properties": [{"propertyId": "p1"}]}, False),
    ({"type": "SOMETHING_NEW"}, False),        # allowlist: unknown types are not broadcast
    ({}, False), ("not a dict", False),
])
def test_workflow_engine_record_frames_never_reach_the_whole_brokerage(frame, tenant_wide):
    """Audit 27.3: the shared engine's offers, owner names, contract prices and
    scout parcel ids (seeded from the first user's own scope) went to every socket."""
    import server

    assert server.engine_frame_is_tenant_wide(frame) is tenant_wide


def test_the_engine_publisher_delivers_only_content_free_frames(monkeypatch):
    """Behaviour, not shape (review of #85): the real publisher sends a status
    frame and drops a DATA_PULLED frame — an inverted guard fails here."""
    import server

    sent = []

    async def deliver(tenant_id, payload):
        sent.append((tenant_id, payload["type"]))
        return 1

    monkeypatch.setattr(server.ws_hub, "deliver_local", deliver)
    publish = server.make_engine_publisher("t-1")
    asyncio.run(publish({"type": "DATA_PULLED", "data": {"investorOffer": 180000}}))
    asyncio.run(publish({"type": "LEGAL_PACKAGE", "data": {"price": 180000}}))
    asyncio.run(publish({"type": "STATUS_UPDATE", "agent": "WORKFLOW ENGINE — all agents online"}))
    assert sent == [("t-1", "STATUS_UPDATE")]


def test_the_shared_engine_is_built_with_the_filtering_publisher():
    """A factory passing deliver_local (or anything else) straight in would
    bypass the filter: the engine must get make_engine_publisher's publisher."""
    import inspect
    import textwrap

    import server

    tree = ast.parse(textwrap.dedent(inspect.getsource(server.websocket_endpoint)))
    engines = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == "WorkflowEngine"]
    assert len(engines) == 1
    publish = {k.arg: k.value for k in engines[0].keywords}["publish"]
    assert ast.unparse(publish) == "_publish_to_tenant"
    assigned = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "_publish_to_tenant" for t in n.targets)]
    assert len(assigned) == 1 and ast.unparse(assigned[0].value) == "make_engine_publisher(tenant_id)"

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
    assert socks["firehose"].frames == [{**FRAME, "source_tenant": tenant}], \
        "the platform-admin firehose keeps its full copy (owner 2026-10-14)"


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
    assert socks["firehose"].frames == [{**FRAME, "source_tenant": tenant}]


def test_malformed_scoped_envelopes_are_ignored(hub):
    tenant, socks = hub
    for bad in ({"tenant_id": tenant, "user_ids": "agent-a@x.test", "scoped_payload": FRAME},
                {"tenant_id": tenant, "user_ids": [1], "scoped_payload": FRAME},
                {"tenant_id": tenant, "user_ids": None, "scoped_payload": "x"}):
        asyncio.run(ws_hub._receive_notification(json.dumps({"origin": "o", **bad})))
    assert all(s.frames == [] for s in socks.values())


def test_an_unmirrored_record_frame_never_reaches_the_firehose(hub, monkeypatch):
    # CALL_NOTES (mirror=False) was broadcast_user before 0145: private to its
    # recipients, never copied to the ops console. Pool or not.
    tenant, socks = hub
    _audience(monkeypatch, ["agent-a@x.test"])
    asyncio.run(ws_hub.broadcast_record(tenant, FRAME, client_id="c1",
                                        only=["agent-a@x.test"], mirror=False))
    assert socks["agent-a"].frames == [FRAME] and socks["firehose"].frames == []
    socks["agent-a"].frames.clear()
    _audience(monkeypatch, None)
    asyncio.run(ws_hub.broadcast_record(tenant, FRAME, client_id="pool",
                                        only=["agent-a@x.test"], mirror=False))
    assert socks["agent-a"].frames == [FRAME] and socks["agent-b"].frames == []
    assert socks["firehose"].frames == []
