"""A platform admin sees that calls exist, never what was said (ledger L03-22,
follow-up to L03-21 / external audit 27.5; owner instruction 2026-11-20).

The voice-session socket's hydrate and GET /api/voice/telemetry used to hand a
platform admin the call's transcript, counter offer, MAO, threshold and
objection draft for any brokerage (the socket skipped the content rule,
the REST feed set readable = visible). Now the same rule as for everyone
decides content (record_access.may_read_call_content); outside the record's
audience a platform admin gets operational metadata only: event type, time,
the firehose's type-checked flags. The ops console's socket counts no longer
list live voice:{tenant}:{session} group names either.

Unit level with fakes; the real-PostgreSQL half is
tests/test_voice_pull_access_live.py.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

import record_access
import voice_intel
import ws_hub
from tenancy import Role, TenantContext

TENANT = "00000000-0000-0000-0000-0000000000d1"
OLDER = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
NEWER = uuid.UUID("00000000-0000-4000-8000-0000000000a2")
CONTENT = ("Pat", "251357", "410000", "41733", "233911", "AMBER", "verbal", "302-555", "roof")
CONTENT_COLUMNS = ("transcript", "counter_offer", "arv", "rehab", "mao", "threshold")
ALLOWED_EVENT_KEYS = {"type", "version", "session_id", "event_id", "event_type", "created_at",
                      "content_withheld", "consent_recorded", "requires_agent_approval", "started_at",
                      "transcript_status", "extraction_code", "mao_unavailable"}


def _rows():
    at = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)
    return [
        {"id": 7, "event_type": "consent", "payload": json.dumps({"consent_recorded": True, "basis": "verbal Pat"}),
         "created_at": at, "transcript_excerpt": None, "counter_offer": None, "arv": None, "rehab": None,
         "mao": None, "threshold": None},
        {"id": 9, "event_type": "counter_offer",
         "payload": {"speaker": "CLIENT", "is_final": True, "guidance": {"objection_draft": "Pat, the roof"},
                     "mao_unavailable_reason": "Pat Owner 302-555-0147"},
         "created_at": at, "transcript_excerpt": "Pat Owner: 251357 is my number", "counter_offer": 251357,
         "arv": 410000, "rehab": 41733, "mao": 233911, "threshold": "AMBER"},
    ]


class World:
    def __init__(self):
        self.visible: list = [NEWER, OLDER]
        self.readable: list[str] = []
        self.event_sql: list[str] = []
        self.unsealed = 0


@pytest.fixture
def world(monkeypatch):
    w = World()

    class Conn:
        async def fetch(self, sql, *args):
            if "FROM live_call_sessions" in sql:
                return [{"id": v} for v in w.visible]
            w.event_sql.append(sql)
            return _rows()

        async def fetchval(self, sql, session_id):
            return session_id in w.visible

    @asynccontextmanager
    async def tx(_ctx):
        yield Conn()

    async def readable(tenant_id, session_ids, agent_id):
        assert tenant_id == TENANT
        return [str(s) for s in session_ids if str(s) in w.readable]

    async def open_rows(_conn, rows, _kind):
        w.unsealed += 1
        return rows

    monkeypatch.setattr(voice_intel, "tenant_tx", tx)
    monkeypatch.setattr(record_access, "may_read_call_content_many", readable)
    monkeypatch.setattr(voice_intel.sealed_text, "open_rows", open_rows)
    return w


def _read(role, session_id, *, after=0, limit=100):
    ctx = TenantContext(agent_id="ops@platform.test", tenant_id=TENANT, role=role)
    return asyncio.run(voice_intel.read_voice_telemetry(session_id=session_id, after_event_id=after, limit=limit,
                                                        ctx=ctx))


def _assert_metadata_feed(out, session_id):
    assert out["content_withheld"] is True and out["session_id"] == str(session_id)
    assert [e["event_type"] for e in out["events"]] == ["consent", "counter_offer"]
    for event in out["events"]:
        assert set(event) <= ALLOWED_EVENT_KEYS, set(event) - ALLOWED_EVENT_KEYS
        assert event["content_withheld"] is True and event["session_id"] == str(session_id)
    assert out["events"][0]["consent_recorded"] is True and out["events"][1]["mao_unavailable"] is True
    assert out["next_event_id"] == 9
    leaked = [c for c in CONTENT if c in json.dumps(out)]
    assert not leaked, leaked


def test_a_platform_admin_outside_the_audience_reads_metadata_only(world):
    out = _read(Role.PLATFORM_ADMIN, OLDER)
    _assert_metadata_feed(out, OLDER)
    # Nothing unsealed, and the query never selects content or amounts.
    assert world.unsealed == 0
    (sql,) = world.event_sql
    selected = sql.split("FROM")[0]
    assert not [c for c in CONTENT_COLUMNS if c in selected], selected


def test_a_platform_admin_in_the_audience_reads_the_content(world):
    """By the normal rule (for example an admin whose account is a member of
    the brokerage with access): the same content feed as any reader."""
    world.readable = [str(OLDER)]
    out = _read(Role.PLATFORM_ADMIN, OLDER)
    assert "content_withheld" not in out and world.unsealed == 1
    assert out["events"][1]["counter_offer"] == 251357.0
    assert out["events"][1]["transcript"]["text"] == "Pat Owner: 251357 is my number"


@pytest.mark.parametrize("readable, content", [([], False), ([str(OLDER)], False), ([str(NEWER)], True)])
def test_a_platform_admins_latest_call_is_the_newest_it_sees(world, readable, content):
    """No session_id: the newest call the admin can see (as before), with its
    content only if the rule allows; a readable OLDER call does not make the
    newer one readable, nor replace it."""
    world.readable = readable
    out = _read(Role.PLATFORM_ADMIN, None)
    assert out["session_id"] == str(NEWER)
    if content:
        assert "content_withheld" not in out and out["events"][1]["counter_offer"] == 251357.0
    else:
        _assert_metadata_feed(out, NEWER)


def test_members_keep_the_fail_closed_rule(world):
    with pytest.raises(HTTPException) as refused:
        _read(Role.AGENT, OLDER)
    assert refused.value.status_code == 404
    assert _read(Role.BROKER_OWNER, None) == {"session_id": None, "events": [], "next_event_id": 0}
    world.readable = [str(OLDER)]
    assert _read(Role.AGENT, None)["session_id"] == str(OLDER), "a member's latest: the newest readable"
    world.visible = []
    with pytest.raises(HTTPException):
        _read(Role.PLATFORM_ADMIN, OLDER)


def test_event_metadata_drops_content_put_in_any_key():
    smuggled = {"id": True, "event_type": "transcript Pat Owner", "created_at": "Pat Owner 302-555-0147",
                "payload": {"consent_recorded": "Pat said yes", "requires_agent_approval": 1,
                            "transcript_status": "active: Pat", "extraction_code": "call 302-555-0147",
                            "started_at": "2026-11-20T09:59:00+00:00 pat@owner.test", "basis": "verbal"},
                "transcript_excerpt": "Pat Owner: 251357", "counter_offer": 251357, "mao": 233911}
    meta = voice_intel.event_metadata(smuggled)
    assert meta == {"event_id": None, "event_type": None, "created_at": None, "content_withheld": True}
    assert voice_intel.event_metadata({**smuggled, "payload": "not json"})["content_withheld"] is True


def test_the_hydrate_frame_for_metadata_only():
    rows = list(reversed(_rows()))  # newest first, as the loader returns them
    frame = voice_intel.hydrate_metadata_frame(str(OLDER), {"consent_recorded": True,
                                                            "transcript_status": "active"}, rows)
    assert frame["type"] == "VOICE_SESSION_HYDRATED" and frame["content_withheld"] is True
    assert (frame["consent_recorded"], frame["transcript_status"]) == (True, "active")
    assert [(e["id"], e["event_type"]) for e in frame["events"]] == [(7, "consent"), (9, "counter_offer")]
    assert not [c for c in CONTENT if c in json.dumps(frame)]


def test_the_metadata_loader_reads_no_content_column():
    seen = []

    class Conn:
        async def fetch(self, sql, *args):
            seen.append((sql, args))
            return []

    assert asyncio.run(voice_intel.load_session_event_metadata(Conn(), str(OLDER), limit=5)) == []
    (sql, args), = seen
    selected = sql.split("FROM")[0]
    assert not [c for c in CONTENT_COLUMNS if c in selected], selected
    assert args == (str(OLDER), 5)


# ── the ops console's socket counts ───────────────────────────────────────


class _Sock:
    async def send_text(self, _text):
        pass


@pytest.fixture
def live_calls(monkeypatch):
    monkeypatch.setattr(ws_hub, "_sockets", ws_hub.defaultdict(set))
    monkeypatch.setattr(ws_hub, "_user_sockets", ws_hub.defaultdict(set))
    ws_hub.register(TENANT, _Sock(), "agent@x.test")
    ws_hub.register(ws_hub.FIREHOSE_TENANT_ID, _Sock(), "ops@platform.test")
    sessions = [str(uuid.uuid4()) for _ in range(3)]
    for session in sessions:
        ws_hub.register(f"voice:{TENANT}:{session}", _Sock(), "agent@x.test")
    return sessions


def _assert_no_session_named(payload, sessions):
    text = json.dumps(payload, default=str)
    assert "voice:" not in text and not [s for s in sessions if s in text], text


def test_admin_system_counts_voice_streams_without_naming_them(live_calls, monkeypatch):
    import admin_ops

    @asynccontextmanager
    async def offline(_ctx):
        raise ConnectionError("no database in this test")
        yield  # pragma: no cover

    monkeypatch.setattr(admin_ops, "tenant_tx", offline)
    ctx = TenantContext(agent_id="ops@platform.test", tenant_id=ws_hub.FIREHOSE_TENANT_ID,
                        role=Role.PLATFORM_ADMIN)
    out = asyncio.run(admin_ops.system(ctx=ctx))
    _assert_no_session_named(out, live_calls)
    assert out["ws_groups"] == {TENANT: 1, ws_hub.FIREHOSE_TENANT_ID: 1, "voice": 3}
    assert out["firehose_watchers"] == 1


def test_admin_overview_counts_voice_streams_without_naming_them(live_calls, monkeypatch):
    import admin_ops

    async def no_tenants(*_a, **_k):
        return []

    monkeypatch.setattr(admin_ops, "_fetch", no_tenants)
    ctx = TenantContext(agent_id="ops@platform.test", tenant_id=ws_hub.FIREHOSE_TENANT_ID,
                        role=Role.PLATFORM_ADMIN)
    out = asyncio.run(admin_ops.overview(ctx=ctx))
    _assert_no_session_named(out, live_calls)
    assert out["ws_groups"]["voice"] == 3


def test_runtime_load_keeps_voice_streams_out_of_the_tenant_list(live_calls):
    import admin_ops

    ctx = TenantContext(agent_id="ops@platform.test", tenant_id=ws_hub.FIREHOSE_TENANT_ID,
                        role=Role.PLATFORM_ADMIN)
    out = asyncio.run(admin_ops.runtime_load(ctx=ctx))
    _assert_no_session_named(out, live_calls)
    sockets = out["websockets"]
    assert sockets["by_tenant"] == {TENANT: 1, ws_hub.FIREHOSE_TENANT_ID: 1}
    assert (sockets["tenants"], sockets["voice_streams"], sockets["total"]) == (2, 3, 5)
