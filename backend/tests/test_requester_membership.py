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
    is_reserved_account_name,
    is_reserved_pipeline_identity,
)

T = "11111111-1111-4111-8111-111111111111"
CLIENT = "33333333-3333-4333-8333-333333333333"
#: What missions/executor._stage writes as created_by: "mission:" + the
#: missions row's uuid primary key.
MISSION_CREATOR = "mission:7c9e6679-7425-40de-944b-e07fc1f90ae7"
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


def test_the_mission_namespace_is_a_reserved_account_name_but_not_a_pipeline():
    """A reviewer registered "mission:ops@example.com": the worker read that
    creator as a mission and never checked their membership (review of
    PR #175). No account may take the prefix; the pipeline rule (which judges
    a REQUESTER as a pipeline) is unchanged, because a mission is not one."""
    for name in ("mission:ops@example.com", " Mission:Ops@Example.com ", MISSION_CREATOR, "MISSION:42",
                 "system:speed-to-lead", "System:Other@X.test", LEGACY_SPEED_TO_LEAD_REQUESTER):
        assert is_reserved_account_name(name), name
    for name in ("ava@brokerage.test", "missionary@x.test", "mission@x.test", "my-mission:x@x.test", "", None):
        assert not is_reserved_account_name(name), name
    for name in ("mission:ops@example.com", MISSION_CREATOR):
        assert not is_reserved_pipeline_identity(name), name


def test_account_creation_refuses_the_pipeline_namespace(monkeypatch):
    import auth
    import db.connection

    for name in ("system:speed-to-lead@x.test", "System:Other@X.test", "speed-to-lead-worker",
                 "mission:ops@example.com", "MISSION:Ops@Example.com"):
        assert auth._is_reserved_identity(name), name
    assert not auth._is_reserved_identity("someone@x.test")

    @asynccontextmanager
    async def never(_ctx):
        raise AssertionError("a reserved signup reached the database")
        yield  # pragma: no cover

    monkeypatch.setattr(db.connection, "tenant_tx", never)
    from fastapi import HTTPException, Response

    for email in ("system:speed-to-lead@x.test", "mission:ops@example.com"):
        with pytest.raises(HTTPException) as refused:
            asyncio.run(auth.register(auth.RegisterRequest(
                email=email, password=secrets.token_urlsafe(24),  # built at run time
                full_name="Not a pipeline"), Response()))
        assert refused.value.status_code == 409, email


def test_an_invitation_to_the_pipeline_namespace_is_refused():
    from pydantic import ValidationError

    import brokerage_onboarding

    for email in ("System:Speed-To-Lead@x.test", "Mission:Ops@Example.com"):
        with pytest.raises(ValidationError, match="reserved"):
            brokerage_onboarding.InviteCreate(emails=[email])
    assert brokerage_onboarding.InviteCreate(emails=["ava@x.test"]).emails == ["ava@x.test"]


def test_the_ai_agent_onboarding_script_refuses_the_namespace_before_writing():
    # Read, not imported: the script loads the repository .env at import.
    src = (Path(__file__).resolve().parents[1] / "scripts" / "onboard_ai_agent.py").read_text()
    body = src[src.index("async def onboard("):]
    # The account-name rule (system: AND mission:), not only the pipeline one.
    assert body.index("is_reserved_account_name(agent_id)") < body.index("INSERT INTO users")


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
        if "FROM users" in sql:                    # an account carrying one of the names asked about
            names = args[1] if isinstance(args[1], list) else [str(args[1]).lower()]
            self.world["names_checked"].append(names)
            if self.world["named_account"] and SPEED_TO_LEAD_REQUESTER in names:
                return 1
            return 1 if set(names) & self.world["accounts"] else None
        if "app_linked_access" in sql:
            self.world["access_checked_as"].append(self.world["current"])
            return self.world["access"]
        raise AssertionError(sql)


def _world(monkeypatch, *, member=None, access=True, named_account=False, accounts=()):
    import record_access

    world = {"member": member, "access": access, "named_account": named_account,
             "accounts": {a.lower() for a in accounts}, "names_checked": [],
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


def test_an_account_carrying_an_unclaimed_old_name_is_not_the_pipeline(monkeypatch):
    """A pre-marker row is read as the pipeline only while no account of the
    brokerage carries its OLD name either (review of PR #172): such a row,
    written outside account creation, makes the row's origin ambiguous."""
    world = _world(monkeypatch, member=None, access=True, accounts=("Speed-To-Lead-Worker",))
    assert _refusal(SPEED_TO_LEAD_REQUESTER, allow_pipeline=True,
                    also_unclaimed=(LEGACY_SPEED_TO_LEAD_REQUESTER,)) == ca.NOT_A_MEMBER
    assert world["names_checked"] == [sorted([SPEED_TO_LEAD_REQUESTER, LEGACY_SPEED_TO_LEAD_REQUESTER])]
    assert world["access_checked_as"] == []
    # with no such account the pipeline's own rule applies, as before
    world["accounts"] = set()
    assert _refusal(SPEED_TO_LEAD_REQUESTER, allow_pipeline=True,
                    also_unclaimed=(LEGACY_SPEED_TO_LEAD_REQUESTER,)) is None


# ── _requester_may_contact (the command worker's check) ──────────────────────

class _WorkerConn:
    def __init__(self, requested_by):
        self.requested_by = requested_by

    async def fetchval(self, sql, *args):
        assert "requested_by FROM action_approvals" in sql
        return self.requested_by

    async def fetchrow(self, sql, *args):
        raise AssertionError("no lead named")


def _worker_check(monkeypatch, command, requested_by, verdict, *, members=None, unclaimed=None):
    import record_access

    seen = []

    @asynccontextmanager
    async def tx(_ctx):
        yield _WorkerConn(requested_by)

    async def refusal(tenant_id, requester, client_id, contact_id, *, allow_pipeline=False, also_unclaimed=()):
        seen.append((requester, client_id, contact_id, allow_pipeline))
        if unclaimed is not None:
            unclaimed.append(also_unclaimed)
        return verdict

    async def find_member(_conn, _tenant, ref):     # the sender lookup (created_by)
        active = members if members is not None else {ref.lower()}
        return {**MEMBER, "agent_id": ref} if ref.lower() in active else None

    monkeypatch.setattr(ca, "tenant_tx", tx)
    monkeypatch.setattr(ca, "requester_contact_refusal", refusal)
    monkeypatch.setattr(record_access, "find_member", find_member)
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


def test_the_worker_asks_that_a_legacy_rows_old_name_is_unclaimed_too(monkeypatch):
    whole = {"approval_id": "a-1", "client_id": CLIENT, "contact_id": None, "target": {}, **LEGACY}
    unclaimed: list = []
    _worker_check(monkeypatch, whole, LEGACY_SPEED_TO_LEAD_REQUESTER, None, unclaimed=unclaimed)
    assert unclaimed == [(LEGACY_SPEED_TO_LEAD_REQUESTER,)]
    unclaimed.clear()
    marked = {**whole, "created_by": SPEED_TO_LEAD_REQUESTER}
    _worker_check(monkeypatch, marked, SPEED_TO_LEAD_REQUESTER, None, unclaimed=unclaimed)
    assert unclaimed == [()]


class _LegacyWorldConn:
    """One fake for the whole check: the approval's requester, the active
    members and every account name the brokerage has (active or not)."""

    def __init__(self, accounts):
        self.accounts = {a.lower() for a in accounts}

    async def fetchval(self, sql, *args):
        if "requested_by FROM action_approvals" in sql:
            return LEGACY_SPEED_TO_LEAD_REQUESTER
        if "FROM users" in sql:
            names = args[1] if isinstance(args[1], list) else [str(args[1]).lower()]
            return 1 if set(names) & self.accounts else None
        if "app_linked_access" in sql:
            return True                              # a pool client
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if "FROM users" in sql:                      # find_member: no ACTIVE member has either name
            return None
        raise AssertionError(sql)


@pytest.mark.parametrize("accounts, allowed", [
    ((), True),
    (("Speed-To-Lead-Worker",), False),             # an inactive or stray account with the old name
    (("system:speed-to-lead",), False),
])
def test_a_legacy_row_is_refused_when_an_account_carries_either_name(monkeypatch, accounts, allowed):
    @asynccontextmanager
    async def tx(_ctx):
        yield _LegacyWorldConn(accounts)

    monkeypatch.setattr(ca, "tenant_tx", tx)
    command = {"approval_id": "a-1", "client_id": CLIENT, "contact_id": None, "target": {}, **LEGACY}
    reasons: list[str] = []
    assert asyncio.run(ca._requester_may_contact(T, command, reasons=reasons)) is allowed
    assert reasons == ([] if allowed else [ca.NOT_A_MEMBER])


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

    async def refuse(_tenant, _command, *, reasons, calendar_account=None):
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


# ── the sender identity: whose number, caller ID or mailbox it goes out under ─

class _SendConn:
    """The worker's command row (created_by = the draft's creator, whose
    identity the send uses) and the approval's requester (the editor)."""

    def __init__(self, command_type, created_by, requested_by="editor@brokerage.test"):
        self.requested_by = requested_by
        self.row = {"state": "queued", "approval_id": "a-1", "created_by": created_by,
                    "command_type": command_type, "client_id": CLIENT, "contact_id": None,
                    "target": {}, "draft": {}, "idempotency_key": "execute:1",
                    "provider": None, "provider_reference": None}
        self.executed = []

    async def fetchrow(self, sql, *args):
        assert "FROM command_executions" in sql, sql
        return self.row

    async def fetchval(self, sql, *args):
        assert "requested_by FROM action_approvals" in sql, sql
        return self.requested_by

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


SEND_PAYLOADS = {
    "EMAIL": {"target": {"email": "x@y.test", "client_id": CLIENT}, "draft": {"subject": "Hi", "body": "Hello"}},
    "SMS": {"target": {"phone": "+13025550142", "client_id": CLIENT}, "draft": {"body": "Hello"}},
    "CALL": {"target": {"phone": "+13025550142", "client_id": CLIENT}, "draft": {"script": "Hello"}},
    "CALENDAR": {"target": {"client_id": CLIENT}, "draft": {"event": {"summary": "Walkthrough"}}},
}


class _ReachedTheDestinationCheck(Exception):
    pass


def _send(monkeypatch, command_type, created_by, *, active, job_creator="editor@brokerage.test"):
    """The real _execute_command_job and _requester_may_contact. The
    requester (the editor) may send; `active` is who is an active member;
    `job_creator` approved it (a calendar event is created on their Google
    account). Stops at the destination check, the first step after the
    identity checks."""
    import billing
    import record_access

    conn = _SendConn(command_type, created_by)
    looked_up: list[str] = []

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    async def none(*_a, **_k):
        return None

    async def find_member(_conn, _tenant, ref):
        looked_up.append(ref)
        return {**MEMBER, "agent_id": ref} if ref.lower() in active else None

    async def destination(*_a, **_k):
        raise _ReachedTheDestinationCheck

    async def never(*_a, **_k):
        raise AssertionError("a refused sender reached the gate or a provider")

    monkeypatch.setattr(ca, "tenant_tx", tx)
    monkeypatch.setattr(billing, "entitlement_refusal", none)
    monkeypatch.setattr(ca, "requester_contact_refusal", none)        # the requester may send
    monkeypatch.setattr(record_access, "find_member", find_member)
    monkeypatch.setattr(ca, "_destination_still_on_record", destination)
    for name in ("guard_outreach", "_resolve_email_provider", "_load_provider_credential",
                 "_load_google_access_token", "create_google_calendar_event"):
        monkeypatch.setattr(ca, name, never)

    class Reporter:
        job = {"tenant_id": T, "created_by": job_creator}

        async def progress(self, *_a, **_k):
            return None

    payload = {"command_id": "c-1", "command_type": command_type, **SEND_PAYLOADS[command_type]}
    return conn, looked_up, ca._execute_command_job(payload, Reporter())


@pytest.mark.parametrize("command_type", ["SMS", "EMAIL", "CALL"])
def test_a_send_under_a_departed_creators_identity_is_refused_terminally(monkeypatch, command_type):
    """The worker checked the approval's requester but sent as created_by:
    after a colleague edited (so requested) a departed agent's draft, the text
    left from the departed agent's business number, the call showed their
    caller ID, the email used their SMTP identity and Reply-To (review of
    PR #172). Now: refused, 'failed', never retried, never 'maybe sent'."""
    conn, looked_up, run = _send(monkeypatch, command_type, "gone@brokerage.test",
                                 active={"editor@brokerage.test"})
    with pytest.raises(ca.RequesterMayNotContact,
                       match="this draft's sender is no longer an active member; "
                             "create it again from your own account") as refused:
        asyncio.run(run)
    assert refused.value.job_terminal is True
    assert refused.value.job_error_code == "SENDER_NOT_A_MEMBER"
    assert looked_up == ["gone@brokerage.test"]
    states = [args[2] for sql, args in conn.executed if "SET state=$3" in sql]
    assert states == ["failed"]                                   # a definite "not sent"
    (stored,) = [args[1] for sql, args in conn.executed if "SET state=$3" in sql]
    assert "sender is no longer an active member" in stored


@pytest.mark.parametrize("command_type", ["SMS", "EMAIL", "CALL"])
def test_an_active_creator_still_sends_after_a_colleagues_edit(monkeypatch, command_type):
    _conn, looked_up, run = _send(monkeypatch, command_type, "ava@brokerage.test",
                                  active={"editor@brokerage.test", "ava@brokerage.test"})
    with pytest.raises(_ReachedTheDestinationCheck):
        asyncio.run(run)
    assert looked_up == ["ava@brokerage.test"]


@pytest.mark.parametrize("created_by", [MISSION_CREATOR, SPEED_TO_LEAD_REQUESTER,
                                        LEGACY_SPEED_TO_LEAD_REQUESTER])
def test_a_mission_or_pipeline_creator_is_not_a_person_to_look_up(monkeypatch, created_by):
    _conn, looked_up, run = _send(monkeypatch, "SMS", created_by, active={"editor@brokerage.test"})
    with pytest.raises(_ReachedTheDestinationCheck):
        asyncio.run(run)
    assert looked_up == []


@pytest.mark.parametrize("created_by", [
    "mission:ops@example.com",       # an account a reviewer registered before the prefix was reserved
    "mission:42",                    # not a mission id
    MISSION_CREATOR.upper(),         # missions write the uuid lower-case
    MISSION_CREATOR + "x",
    " " + MISSION_CREATOR,
])
def test_only_the_exact_mission_shape_skips_the_sender_check(monkeypatch, created_by):
    """Was: any created_by starting "mission:" skipped it, so an offboarded
    person whose account was named that way kept sending (review of PR #175).
    Now anything but "mission:<uuid>" is a person: looked up, and refused."""
    conn, looked_up, run = _send(monkeypatch, "SMS", created_by, active={"editor@brokerage.test"})
    with pytest.raises(ca.SenderNotAMember) as refused:
        asyncio.run(run)
    assert refused.value.job_terminal is True
    assert refused.value.job_error_code == "SENDER_NOT_A_MEMBER"
    assert looked_up == [created_by.strip()]
    assert [args[2] for sql, args in conn.executed if "SET state=$3" in sql] == ["failed"]
    assert ca._human_sender(MISSION_CREATOR) is None


def test_a_calendar_event_is_checked_against_the_approvers_account_not_the_drafts(monkeypatch):
    """A calendar event is created on the approving job creator's Google
    account, not the draft creator's: that is the identity looked up."""
    _conn, looked_up, run = _send(monkeypatch, "CALENDAR", "gone@brokerage.test",
                                  active={"editor@brokerage.test"})
    with pytest.raises(_ReachedTheDestinationCheck):
        asyncio.run(run)
    assert looked_up == ["editor@brokerage.test"]


def test_the_calendar_token_is_loaded_for_the_account_the_membership_check_cleared(monkeypatch):
    """Review of PR #176: the worker computes calendar_account once, for the
    membership check and for the Google token's label, but replacing the
    label with the draft creator's id (account_label=command['created_by'])
    passed every test. Run the CALENDAR path to the token load, with a
    draft creator who is not the approver: the token is loaded for exactly
    the account the membership check was asked about, and looked up."""
    checked: list = []
    labels: list = []
    real_check = ca._requester_may_contact

    async def check(tenant_id, command, *, reasons=None, calendar_account=None):
        checked.append(calendar_account)
        return await real_check(tenant_id, command, reasons=reasons, calendar_account=calendar_account)

    class _TokenLoaded(Exception):
        pass

    async def load_token(_ctx, account_label):
        labels.append(account_label)
        raise _TokenLoaded

    async def on_record(*_a, **_k):
        return None

    conn, looked_up, run = _send(monkeypatch, "CALENDAR", "ava@brokerage.test",
                                 active={"ava@brokerage.test", "editor@brokerage.test",
                                         "approver@brokerage.test"},
                                 job_creator="approver@brokerage.test")
    monkeypatch.setattr(ca, "_requester_may_contact", check)
    monkeypatch.setattr(ca, "_destination_still_on_record", on_record)
    monkeypatch.setattr(ca, "_load_google_access_token", load_token)
    with pytest.raises(_TokenLoaded):
        asyncio.run(run)
    assert checked == ["approver@brokerage.test"]
    assert looked_up == ["approver@brokerage.test"]       # the membership lookup itself
    assert labels == checked                              # the token: the same account
    assert conn.row["created_by"] == "ava@brokerage.test" not in labels


def test_a_calendar_event_on_a_departed_approvers_account_is_refused_terminally(monkeypatch):
    """Was: only EMAIL/SMS/CALL checked the identity they go out under, so an
    event approved by someone offboarded since was still created on their
    Google account, its invitations sent from it (review of PR #175). Now:
    refused before any token is loaded, 'failed', never retried."""
    conn, looked_up, run = _send(monkeypatch, "CALENDAR", "ava@brokerage.test",
                                 active={"ava@brokerage.test", "editor@brokerage.test"},
                                 job_creator="gone@brokerage.test")
    with pytest.raises(ca.SenderNotAMember,
                       match="the person whose Google Calendar this event would be created on "
                             r"\(who approved it\) is no longer an active member") as refused:
        asyncio.run(run)
    assert refused.value.job_terminal is True
    assert refused.value.job_error_code == "SENDER_NOT_A_MEMBER"
    assert looked_up == ["gone@brokerage.test"]
    states = [args[2] for sql, args in conn.executed if "SET state=$3" in sql]
    assert states == ["failed"]                                   # a definite "not sent"
    (stored,) = [args[1] for sql, args in conn.executed if "SET state=$3" in sql]
    assert "Google Calendar" in stored and "no longer an active member" in stored
