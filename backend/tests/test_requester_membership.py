"""L09-12 (external audit 26.2; owner instruction 2026-11-20): a queued send
leaves only for an ACTIVE member of the brokerage, or for a system pipeline
staging on its own authority under the reserved "system:" marker.

The command worker's check (_requester_may_contact) read a non-member
requester as "an agent with that id", so a suspended or offboarded person's
queued sends still reached their own former clients — and any unmarked name
passed as a pipeline. These pin the rule in memory; the same cases run
against real PostgreSQL in tests/test_outbox_sender_access_live.py.
"""
from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import commands_api as ca
from tenancy import (
    LEGACY_SPEED_TO_LEAD_REQUESTER,
    OUTREACH_PIPELINE_REQUESTERS,
    SPEED_TO_LEAD_REQUESTER,
    Role,
    is_reserved_pipeline_identity,
)

T = "11111111-1111-4111-8111-111111111111"
CLIENT = "33333333-3333-4333-8333-333333333333"
MEMBER = {"id": "u-1", "agent_id": "ava@brokerage.test", "role": "agent"}


# ── the marker ────────────────────────────────────────────────────────────────

def test_the_pipeline_marker_is_a_reserved_name():
    assert SPEED_TO_LEAD_REQUESTER == "system:speed-to-lead"
    assert OUTREACH_PIPELINE_REQUESTERS == frozenset({SPEED_TO_LEAD_REQUESTER})
    for name in ("system:speed-to-lead", " SYSTEM:Speed-To-Lead ", "system:anything@x.test",
                 LEGACY_SPEED_TO_LEAD_REQUESTER, "Speed-To-Lead-Worker"):
        assert is_reserved_pipeline_identity(name), name
    for name in ("ava@brokerage.test", "systematic@x.test", "system@x.test", "", None):
        assert not is_reserved_pipeline_identity(name), name


def test_account_creation_refuses_the_pipeline_namespace(monkeypatch):
    import auth
    import db.connection

    for name in ("system:speed-to-lead@x.test", "System:Other@X.test", "speed-to-lead-worker"):
        assert auth._is_reserved_identity(name), name
    assert not auth._is_reserved_identity("someone@x.test")

    @asynccontextmanager
    async def never(_ctx):
        raise AssertionError("a reserved signup reached the database")
        yield  # pragma: no cover

    monkeypatch.setattr(db.connection, "tenant_tx", never)
    from fastapi import HTTPException, Response

    with pytest.raises(HTTPException) as refused:
        asyncio.run(auth.register(auth.RegisterRequest(
            email="system:speed-to-lead@x.test", password=secrets.token_urlsafe(24),  # built at run time
            full_name="Not a pipeline"), Response()))
    assert refused.value.status_code == 409


def test_an_invitation_to_the_pipeline_namespace_is_refused():
    from pydantic import ValidationError

    import brokerage_onboarding

    with pytest.raises(ValidationError, match="reserved"):
        brokerage_onboarding.InviteCreate(emails=["System:Speed-To-Lead@x.test"])
    assert brokerage_onboarding.InviteCreate(emails=["ava@x.test"]).emails == ["ava@x.test"]


def test_the_ai_agent_onboarding_script_refuses_the_namespace_before_writing():
    # Read, not imported: the script loads the repository .env at import.
    src = (Path(__file__).resolve().parents[1] / "scripts" / "onboard_ai_agent.py").read_text()
    body = src[src.index("async def onboard("):]
    assert body.index("is_reserved_pipeline_identity(agent_id)") < body.index("INSERT INTO users")


# ── pre-marker rows: every origin signal or nothing ──────────────────────────

LEGACY = {"created_by": LEGACY_SPEED_TO_LEAD_REQUESTER, "idempotency_key": "speed-to-lead:evt-1",
          "draft": {"content": {"body": "Hi"}, "context": {"source": "speed-to-lead"}}}


@pytest.mark.parametrize("requester, changes, expected", [
    (LEGACY_SPEED_TO_LEAD_REQUESTER, {}, SPEED_TO_LEAD_REQUESTER),
    # the draft is stored as JSON text by some readers
    (LEGACY_SPEED_TO_LEAD_REQUESTER,
     {"draft": '{"content": {}, "context": {"source": "speed-to-lead"}}'}, SPEED_TO_LEAD_REQUESTER),
    # an edit replaced the approval: the editor is the requester now
    ("ava@brokerage.test", {}, None),
    (LEGACY_SPEED_TO_LEAD_REQUESTER, {"created_by": "ava@brokerage.test"}, None),
    (LEGACY_SPEED_TO_LEAD_REQUESTER, {"idempotency_key": "parse:evt-1"}, None),
    (LEGACY_SPEED_TO_LEAD_REQUESTER, {"draft": {"content": {}, "context": {"source": "smart-plan"}}}, None),
    (LEGACY_SPEED_TO_LEAD_REQUESTER, {"draft": {"content": {}}}, None),
    (LEGACY_SPEED_TO_LEAD_REQUESTER, {"draft": None}, None),
    ("Speed-To-Lead-Worker", {}, None),            # exact spelling only
    ("speed-to-lead", {}, None),
])
def test_a_pre_marker_speed_to_lead_row_is_read_as_the_pipeline_only_when_unambiguous(
        requester, changes, expected):
    assert ca._legacy_pipeline_requester(requester, {**LEGACY, **changes}) == expected


# ── requester_contact_refusal ─────────────────────────────────────────────────

class _Conn:
    def __init__(self, world):
        self.world = world

    async def fetchval(self, sql, *args):
        if "FROM users" in sql:                    # an account carrying the pipeline's name
            return 1 if self.world["named_account"] else None
        if "app_linked_access" in sql:
            self.world["access_checked_as"].append(self.world["current"])
            return self.world["access"]
        raise AssertionError(sql)


def _world(monkeypatch, *, member=None, access=True, named_account=False):
    import record_access

    world = {"member": member, "access": access, "named_account": named_account,
             "access_checked_as": [], "current": None}

    @asynccontextmanager
    async def tx(ctx):
        world["current"] = (ctx.agent_id, ctx.role)
        yield _Conn(world)

    async def find_member(_conn, _tenant, ref):
        world["looked_up"] = ref
        return world["member"]

    monkeypatch.setattr(ca, "tenant_tx", tx)
    monkeypatch.setattr(record_access, "find_member", find_member)
    return world


def _refusal(requester, client=CLIENT, **kw):
    return asyncio.run(ca.requester_contact_refusal(T, requester, client, None, **kw))


def test_a_member_is_checked_as_themselves_in_their_current_role(monkeypatch):
    world = _world(monkeypatch, member={**MEMBER, "role": "broker_owner"})
    assert _refusal("AVA@brokerage.test", allow_pipeline=True) is None
    assert world["access_checked_as"] == [("ava@brokerage.test", Role.BROKER_OWNER)]
    world["access"] = False
    assert _refusal("ava@brokerage.test") == ca.NO_RECORD_ACCESS


@pytest.mark.parametrize("allow_pipeline", [False, True])
def test_a_non_member_person_is_refused_even_on_their_own_former_client(monkeypatch, allow_pipeline):
    """Was: checked as "an agent with that id", which still reached the
    clients assigned to that id."""
    world = _world(monkeypatch, member=None, access=True)
    assert _refusal("ava@brokerage.test", allow_pipeline=allow_pipeline) == ca.NOT_A_MEMBER
    assert world["access_checked_as"] == []


def test_a_non_member_is_refused_with_no_record_to_check(monkeypatch):
    _world(monkeypatch, member=None)
    assert _refusal("ava@brokerage.test", client=None, allow_pipeline=True) == ca.NOT_A_MEMBER


def test_a_member_with_no_record_to_check_may_send(monkeypatch):
    world = _world(monkeypatch, member=MEMBER)
    assert _refusal("ava@brokerage.test", client=None) is None
    assert world["access_checked_as"] == []


def test_the_marked_pipeline_is_judged_by_its_own_rule(monkeypatch):
    world = _world(monkeypatch, member=None, access=True)
    assert _refusal(SPEED_TO_LEAD_REQUESTER, allow_pipeline=True) is None
    # an agent with that id: what app_linked_access gives it is the pool
    assert world["access_checked_as"] == [(SPEED_TO_LEAD_REQUESTER, Role.AGENT)]
    world["access"] = False                     # the pool contact was assigned since
    assert _refusal(SPEED_TO_LEAD_REQUESTER, allow_pipeline=True) == ca.NO_RECORD_ACCESS


@pytest.mark.parametrize("requester", [
    "System:Speed-To-Lead",          # not the pipeline's exact spelling
    "system:other",                  # not a pipeline that stages outreach
    "speed-to-lead",                 # unmarked: it used to pass as one
    LEGACY_SPEED_TO_LEAD_REQUESTER,  # unmarked here; only _requester_may_contact maps a whole legacy row
    "mission:42",
])
def test_no_other_non_member_name_passes_as_a_pipeline(monkeypatch, requester):
    world = _world(monkeypatch, member=None, access=True)
    assert _refusal(requester, allow_pipeline=True) == ca.NOT_A_MEMBER
    assert world["access_checked_as"] == []


def test_the_pipeline_is_not_admitted_where_only_people_queue(monkeypatch):
    _world(monkeypatch, member=None, access=True)
    assert _refusal(SPEED_TO_LEAD_REQUESTER) == ca.NOT_A_MEMBER          # allow_pipeline defaults False
    assert asyncio.run(ca.requester_may_contact_record(T, SPEED_TO_LEAD_REQUESTER, CLIENT, None)) is False


def test_an_account_carrying_the_pipelines_name_is_not_the_pipeline(monkeypatch):
    world = _world(monkeypatch, member=None, access=True, named_account=True)
    assert _refusal(SPEED_TO_LEAD_REQUESTER, allow_pipeline=True) == ca.NOT_A_MEMBER
    assert world["access_checked_as"] == []


# ── _requester_may_contact (the command worker's check) ──────────────────────

class _WorkerConn:
    def __init__(self, requested_by):
        self.requested_by = requested_by

    async def fetchval(self, sql, *args):
        assert "requested_by FROM action_approvals" in sql
        return self.requested_by

    async def fetchrow(self, sql, *args):
        raise AssertionError("no lead named")


def _worker_check(monkeypatch, command, requested_by, verdict):
    seen = []

    @asynccontextmanager
    async def tx(_ctx):
        yield _WorkerConn(requested_by)

    async def refusal(tenant_id, requester, client_id, contact_id, *, allow_pipeline=False):
        seen.append((requester, client_id, contact_id, allow_pipeline))
        return verdict

    monkeypatch.setattr(ca, "tenant_tx", tx)
    monkeypatch.setattr(ca, "requester_contact_refusal", refusal)
    reasons: list[str] = []
    ok = asyncio.run(ca._requester_may_contact(T, command, reasons=reasons))
    return ok, reasons, seen


def test_the_worker_checks_membership_even_for_a_draft_about_no_record(monkeypatch):
    """Was: True at once for a draft naming no client, contact or lead, so a
    departed person's email to a bare address still left."""
    command = {"approval_id": "a-1", "created_by": "ava@brokerage.test", "client_id": None,
               "contact_id": None, "target": {"email": "x@y.test"}}
    ok, reasons, seen = _worker_check(monkeypatch, command, "ava@brokerage.test", ca.NOT_A_MEMBER)
    assert (ok, reasons) == (False, [ca.NOT_A_MEMBER])
    assert seen == [("ava@brokerage.test", None, None, True)]


def test_the_worker_reads_a_whole_legacy_row_as_the_pipeline(monkeypatch):
    command = {"approval_id": "a-1", "client_id": CLIENT, "contact_id": None, "target": {}, **LEGACY}
    ok, reasons, seen = _worker_check(monkeypatch, command, LEGACY_SPEED_TO_LEAD_REQUESTER, None)
    assert (ok, reasons) == (True, [])
    assert seen == [(SPEED_TO_LEAD_REQUESTER, CLIENT, None, True)]


def test_the_worker_keeps_a_partial_legacy_row_unmarked(monkeypatch):
    command = {"approval_id": "a-1", "client_id": CLIENT, "contact_id": None, "target": {},
               **LEGACY, "idempotency_key": "execute:1"}
    _ok, _reasons, seen = _worker_check(monkeypatch, command, LEGACY_SPEED_TO_LEAD_REQUESTER, ca.NOT_A_MEMBER)
    assert seen[0][0] == LEGACY_SPEED_TO_LEAD_REQUESTER


# ── the worker stops, finally, with the reason ───────────────────────────────

class _RowConn:
    def __init__(self):
        self.executed = []

    async def fetchrow(self, sql, *args):
        return {"state": "queued", "approval_id": None, "created_by": "ava@brokerage.test",
                "provider": None, "provider_reference": None}

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


@pytest.mark.parametrize("reason, words", [
    (ca.NOT_A_MEMBER, "not an active member of this brokerage"),
    (ca.NO_RECORD_ACCESS, "can no longer contact this client or contact"),
])
def test_a_refused_requester_ends_failed_and_is_never_retried(monkeypatch, reason, words):
    import billing

    conn = _RowConn()

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    async def none(*_a, **_k):
        return None

    async def refuse(_tenant, _command, *, reasons):
        reasons.append(reason)
        return False

    async def never(*_a, **_k):
        raise AssertionError("a refused requester reached the destination check, the gate or a sender")

    monkeypatch.setattr(ca, "tenant_tx", tx)
    monkeypatch.setattr(billing, "entitlement_refusal", none)
    monkeypatch.setattr(ca, "_requester_may_contact", refuse)
    for name in ("_destination_still_on_record", "guard_outreach", "_resolve_email_provider"):
        monkeypatch.setattr(ca, name, never)

    class Reporter:
        job = {"tenant_id": T, "created_by": "owner@brokerage.test"}

        async def progress(self, *_a, **_k):
            return None

    payload = {"command_id": "c-1", "command_type": "EMAIL", "target": {"email": "x@y.test"},
               "draft": {"subject": "Hi", "body": "Hello"}}
    with pytest.raises(ca.RequesterMayNotContact, match=words) as refused:
        asyncio.run(ca._execute_command_job(payload, Reporter()))
    # terminal: the job worker dead-letters it instead of retrying into a
    # send after a reactivation or a hand-back
    assert refused.value.job_terminal is True
    assert refused.value.job_error_code == "REQUESTER_NOT_AUTHORIZED"
    states = [args[2] for sql, args in conn.executed if "SET state=$3" in sql]
    assert states == ["failed"]
