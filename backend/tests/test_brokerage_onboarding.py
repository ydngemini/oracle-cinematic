"""Brokerage onboarding — invitations, roles, tenant isolation, setup state.

The repo's route tests run on fakes rather than a live Postgres (see
tests/conftest.py and .github/workflows/ci.yml), so these drive the handlers
directly with a hand-rolled connection keyed on SQL substrings. That covers
the application's decisions.

It does NOT cover the decisions Postgres makes — RLS isolation, the WITH CHECK
on a cross-tenant insert, and the single-use UPDATE that makes replay
impossible. Those are proved against a real server in tests/rls_brokerage.sql,
because asserting them against a fake would only prove the fake agrees with me.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import brokerage_onboarding as bo
from tenancy import Role, TenantContext

TENANT_A = "aaaaaaaa-0000-0000-0000-00000000000a"
TENANT_B = "bbbbbbbb-0000-0000-0000-00000000000b"
USER_A = "aaaaaaaa-1111-0000-0000-00000000000a"

OWNER = TenantContext(agent_id="owner@a.test", tenant_id=TENANT_A, role=Role.BROKER_OWNER)
AGENT = TenantContext(agent_id="agent@a.test", tenant_id=TENANT_A, role=Role.AGENT)
ADMIN = TenantContext(agent_id="ops@neoh", tenant_id=TENANT_A, role=Role.PLATFORM_ADMIN)

FUTURE = datetime.now(timezone.utc) + timedelta(days=7)
PAST = datetime.now(timezone.utc) - timedelta(days=1)


class FakeConn:
    """Answers by SQL substring. Records every statement so a test can assert
    on what was actually sent, which is how the tenant-predicate checks work."""

    def __init__(self, *, rows=None, fetch_rows=None):
        self.rows = rows or {}
        self.fetch_rows = fetch_rows or {}
        self.statements: list[tuple[str, tuple]] = []

    def _match(self, table, query):
        for needle, value in table.items():
            if needle in query:
                return value() if callable(value) else value
        return None

    async def fetchrow(self, query, *args):
        self.statements.append((query, args))
        return self._match(self.rows, query)

    async def fetch(self, query, *args):
        self.statements.append((query, args))
        return self._match(self.fetch_rows, query) or []

    async def execute(self, query, *args):
        self.statements.append((query, args))
        return "UPDATE 1"

    async def fetchval(self, query, *args):
        self.statements.append((query, args))
        return self._match(self.rows, query)


def fake_tx(conn):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn
    return tx


def invite_row(**over):
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "email": "invitee@a.test",
        "invited_role": "agent",
        "invited_by_agent_id": "owner@a.test",
        "expires_at": FUTURE,
        "consumed_at": None,
        "revoked_at": None,
        "created_at": datetime.now(timezone.utc),
        "last_sent_at": datetime.now(timezone.utc),
        "send_count": 1,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def test_token_is_stored_only_as_a_digest():
    raw, digest = bo.new_invitation_token()
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    # The raw token must not be recoverable from what we persist.
    assert raw not in digest
    assert bo.hash_invitation_token(raw) == digest


def test_tokens_are_unique_and_high_entropy():
    tokens = {bo.new_invitation_token()[0] for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 32 for t in tokens)


def test_link_uses_the_documented_public_origin(monkeypatch):
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://app.neoh.test/")
    raw = "tok123"
    assert bo.invitation_link(raw) == "https://app.neoh.test/accept-invite?token=tok123"


def test_link_degrades_to_a_relative_path_without_an_origin(monkeypatch):
    monkeypatch.delenv("ORACLE_PUBLIC_BASE_URL", raising=False)
    assert bo.invitation_link("t").startswith("/accept-invite?token=")


# ---------------------------------------------------------------------------
# Invitation email
# ---------------------------------------------------------------------------

def test_invitation_email_names_brokerage_inviter_link_and_expiry():
    subject, text, html = bo.build_invitation_email(
        brokerage="Lockwood Realty", inviter="nat@lockwood.test",
        link="https://x.test/accept-invite?token=abc", expires_at=FUTURE,
    )
    assert "Lockwood Realty" in subject and "nat@lockwood.test" in subject
    for body in (text, html):
        assert "Lockwood Realty" in body
        assert "https://x.test/accept-invite?token=abc" in body
        assert FUTURE.strftime("%d %b %Y") in body


def test_invitation_email_exposes_no_internal_ids():
    _, text, html = bo.build_invitation_email(
        brokerage="B", inviter="i@x.test", link="https://x.test/a?token=t", expires_at=FUTURE,
    )
    for body in (text, html):
        assert TENANT_A not in body and USER_A not in body


# ---------------------------------------------------------------------------
# Role model
# ---------------------------------------------------------------------------

def test_an_invitation_can_never_mint_platform_admin():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        bo.InviteCreate(emails=["x@y.test"], role="platform_admin")


def test_invite_defaults_to_agent():
    assert bo.InviteCreate(emails=["x@y.test"]).role == "agent"


def test_bulk_invites_are_capped():
    from pydantic import ValidationError
    many = [f"a{i}@y.test" for i in range(bo.MAX_BULK_INVITES + 1)]
    with pytest.raises(ValidationError):
        bo.InviteCreate(emails=many)


def test_malformed_addresses_are_rejected():
    from pydantic import ValidationError
    for bad in ("nope", "no@domain", "@y.test", ""):
        with pytest.raises(ValidationError):
            bo.InviteCreate(emails=[bad])


def test_duplicate_addresses_collapse_case_insensitively():
    body = bo.InviteCreate(emails=["Sam@Y.test", "sam@y.test", "  SAM@y.TEST "])
    assert body.emails == ["sam@y.test"]


# ---------------------------------------------------------------------------
# Capability derivation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "counts,expected",
    [
        ({"verified": 0, "pending": 0, "total": 0}, "NOT_STARTED"),
        ({"verified": 1, "pending": 0, "total": 1}, "READY"),
        ({"verified": 0, "pending": 1, "total": 1}, "IN_PROGRESS"),
        ({"verified": 0, "pending": 0, "total": 1}, "NEEDS_ACTION"),
    ],
)
def test_phone_status_follows_verified_caller_id(counts, expected):
    conn = FakeConn(rows={"FROM telephony_routes": counts})
    assert asyncio.run(bo._phone_status(conn, TENANT_A)) == expected


@pytest.mark.parametrize(
    "counts,expected",
    [
        ({"active": 0, "working": 0, "total": 0}, "NOT_STARTED"),
        ({"active": 1, "working": 0, "total": 1}, "READY"),
        ({"active": 0, "working": 1, "total": 1}, "IN_PROGRESS"),
        ({"active": 0, "working": 0, "total": 1}, "NEEDS_ACTION"),
    ],
)
def test_messaging_status_is_tracked_apart_from_calls(counts, expected):
    conn = FakeConn(rows={"FROM messaging_routes": counts})
    assert asyncio.run(bo._messaging_status(conn, TENANT_A)) == expected


@pytest.mark.parametrize(
    "status,expected",
    [(None, "NOT_STARTED"), ("active", "READY"), ("trialing", "READY"),
     ("past_due", "READY"), ("canceled", "NEEDS_ACTION")],
)
def test_billing_status_matches_billing_pys_definition(status, expected):
    rows = {"FROM subscriptions": ({"status": status} if status else None)}
    assert asyncio.run(bo._billing_status(FakeConn(rows=rows), TENANT_A)) == expected


def test_invites_status_reports_needs_action_for_a_lone_owner():
    conn = FakeConn(rows={"AS members": {"members": 1, "pending": 0, "accepted": 0}})
    state, summary = asyncio.run(bo._invites_status(conn, TENANT_A))
    assert state == "NEEDS_ACTION"
    assert summary == {"active_members": 1, "pending_invitations": 0, "accepted_invitations": 0}


def test_invites_status_is_in_progress_while_invitations_are_outstanding():
    conn = FakeConn(rows={"AS members": {"members": 1, "pending": 3, "accepted": 0}})
    state, summary = asyncio.run(bo._invites_status(conn, TENANT_A))
    assert state == "IN_PROGRESS" and summary["pending_invitations"] == 3


def test_invites_status_is_ready_once_somebody_joined():
    conn = FakeConn(rows={"AS members": {"members": 2, "pending": 0, "accepted": 1}})
    state, _ = asyncio.run(bo._invites_status(conn, TENANT_A))
    assert state == "READY"


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

def _setup_conn(**over):
    tenant = {
        "id": TENANT_A, "slug": "a", "name": "Brokerage A", "org_type": "brokerage",
        "primary_state": None, "website": None, "profile_completed_at": None,
    }
    tenant.update(over.pop("tenant", {}))
    rows = {
        "FROM tenants WHERE id": tenant,
        "AS members": over.pop("counts", {"members": 1, "pending": 0, "accepted": 0}),
        "FROM telephony_routes": over.pop("phone", {"verified": 0, "pending": 0, "total": 0}),
        "FROM messaging_routes": over.pop("messaging", {"active": 0, "working": 0, "total": 0}),
        "FROM subscriptions": over.pop("subscription", None),
    }
    return FakeConn(rows=rows, fetch_rows={"FROM brokerage_setup_progress": over.pop("progress", [])})


def test_setup_state_recommends_the_first_outstanding_required_step():
    state = asyncio.run(compute := bo.compute_setup_state(_setup_conn(), OWNER))
    assert state["capabilities"]["brokerage_profile"] == "NEEDS_ACTION"
    assert state["recommended_next"] == "brokerage_profile"


def test_setup_state_moves_on_once_the_profile_is_done():
    conn = _setup_conn(tenant={"profile_completed_at": datetime.now(timezone.utc)})
    state = asyncio.run(bo.compute_setup_state(conn, OWNER))
    assert state["capabilities"]["brokerage_profile"] == "READY"
    assert state["recommended_next"] == "agent_invites"


def test_optional_capabilities_never_become_the_next_step():
    """§17: a missing MLS feed must not stop somebody managing contacts."""
    conn = _setup_conn(
        tenant={"profile_completed_at": datetime.now(timezone.utc)},
        counts={"members": 3, "pending": 0, "accepted": 2},
        phone={"verified": 1, "pending": 0, "total": 1},
        subscription={"status": "active"},
    )
    state = asyncio.run(bo.compute_setup_state(conn, OWNER))
    assert state["capabilities"]["mls"] == "NOT_STARTED"
    assert state["capabilities"]["contact_import"] == "NOT_STARTED"
    # Everything REQUIRED is ready, so readiness is ready and nothing is next.
    assert state["capabilities"]["readiness"] == "READY"
    assert state["recommended_next"] is None


def test_readiness_is_blocked_while_anything_required_is_outstanding():
    state = asyncio.run(bo.compute_setup_state(_setup_conn(), OWNER))
    assert state["capabilities"]["readiness"] == "BLOCKED"


def test_setup_state_covers_every_declared_capability():
    state = asyncio.run(bo.compute_setup_state(_setup_conn(), OWNER))
    assert set(state["capabilities"]) == set(bo.CAPABILITIES)


def test_stored_progress_is_honoured_for_capabilities_with_no_live_source():
    conn = _setup_conn(progress=[{"capability": "contact_import", "status": "IN_PROGRESS", "detail": {}}])
    state = asyncio.run(bo.compute_setup_state(conn, OWNER))
    assert state["capabilities"]["contact_import"] == "IN_PROGRESS"


def test_calls_and_texts_are_reported_separately():
    conn = _setup_conn(
        phone={"verified": 1, "pending": 0, "total": 1},
        messaging={"active": 0, "working": 1, "total": 1},
    )
    state = asyncio.run(bo.compute_setup_state(conn, OWNER))
    assert state["capabilities"]["phone"] == "READY"
    assert state["messaging"] == "IN_PROGRESS"


def test_setup_state_404s_when_the_tenant_is_gone():
    conn = FakeConn(rows={"FROM tenants WHERE id": None})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(bo.compute_setup_state(conn, OWNER))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Creating invitations
# ---------------------------------------------------------------------------

def _invite_conn(*, member=None, prior=None, created=None):
    return FakeConn(rows={
        "SELECT id, agent_id FROM users": {"id": USER_A, "agent_id": "owner@a.test"},
        "SELECT name FROM tenants": {"name": "Brokerage A"},
        "SELECT 1 FROM users WHERE tenant_id": member,
        "SELECT token_hash, send_count": prior,
        "INSERT INTO brokerage_invitations": created or invite_row(),
    })


def test_create_invitation_stores_only_the_digest():
    conn = _invite_conn()
    asyncio.run(bo.create_invitations(conn, OWNER, ["invitee@a.test"], "agent"))
    insert = [(q, a) for q, a in conn.statements if "INSERT INTO brokerage_invitations" in q][0]
    token_hash = insert[1][0]
    assert len(token_hash) == 64 and all(c in "0123456789abcdef" for c in token_hash)


def test_create_invitation_binds_the_row_to_the_session_tenant():
    conn = _invite_conn()
    asyncio.run(bo.create_invitations(conn, OWNER, ["invitee@a.test"], "agent"))
    insert = [(q, a) for q, a in conn.statements if "INSERT INTO brokerage_invitations" in q][0]
    assert insert[1][1] == TENANT_A          # never from a request body
    assert insert[1][4] == USER_A            # inviter resolved server-side


def test_create_skips_somebody_who_is_already_a_member():
    conn = _invite_conn(member={"?column?": 1})
    out = asyncio.run(bo.create_invitations(conn, OWNER, ["already@a.test"], "agent"))
    assert out["created"] == []
    assert out["skipped"] == [{"email": "already@a.test", "reason": "already_a_member"}]
    assert not any("INSERT INTO brokerage_invitations" in q for q, _ in conn.statements)


def test_reinviting_revokes_the_previous_live_invitation():
    """Double-click, resend, two tabs: one live invitation, never two."""
    conn = _invite_conn(prior={"token_hash": "f" * 64, "send_count": 2})
    out = asyncio.run(bo.create_invitations(conn, OWNER, ["invitee@a.test"], "agent"))
    revokes = [a for q, a in conn.statements if "SET revoked_at = now()" in q]
    assert len(revokes) == 1 and revokes[0][0] == "f" * 64
    insert = [(q, a) for q, a in conn.statements if "INSERT INTO brokerage_invitations" in q][0]
    assert insert[1][7] == 3          # send_count carried forward
    assert out["created"]


def test_bulk_invite_issues_one_row_per_address():
    conn = _invite_conn()
    out = asyncio.run(bo.create_invitations(
        conn, OWNER, ["a@x.test", "b@x.test", "c@x.test"], "agent"))
    inserts = [q for q, _ in conn.statements if "INSERT INTO brokerage_invitations" in q]
    assert len(inserts) == 3 and len(out["_deliveries"]) == 3


def test_create_refuses_a_caller_who_is_not_in_the_brokerage():
    conn = FakeConn(rows={"SELECT id, agent_id FROM users": None})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(bo.create_invitations(conn, OWNER, ["x@y.test"], "agent"))
    assert exc.value.status_code == 403


def test_public_invite_shape_never_carries_the_token_hash():
    row = invite_row()
    public = bo._invite_row(row)
    assert "token_hash" not in public
    assert set(public) == {
        "id", "email", "role", "state", "invited_by",
        "expires_at", "created_at", "last_sent_at", "send_count",
    }


@pytest.mark.parametrize("over,state", [
    ({}, "pending"),
    ({"revoked_at": datetime.now(timezone.utc)}, "revoked"),
    ({"consumed_at": datetime.now(timezone.utc)}, "accepted"),
    ({"expires_at": PAST}, "expired"),
])
def test_invitation_state_is_derived_from_the_row(over, state):
    assert bo._invite_row(invite_row(**over))["state"] == state


def test_revoked_beats_expired_in_the_reported_state():
    row = invite_row(revoked_at=datetime.now(timezone.utc), expires_at=PAST)
    assert bo._invite_row(row)["state"] == "revoked"


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

def _patch_tx(monkeypatch, conn):
    monkeypatch.setattr(bo, "tenant_tx", fake_tx(conn))


@pytest.mark.parametrize("call", [
    lambda: bo.post_invitations(bo.InviteCreate(emails=["x@y.test"]), AGENT),
    lambda: bo.list_invitations(AGENT),
    lambda: bo.revoke_invitation("11111111-1111-1111-1111-111111111111", AGENT),
    lambda: bo.resend_invitation("11111111-1111-1111-1111-111111111111", AGENT),
    lambda: bo.update_profile(bo.BrokerageProfileUpdate(name="Nope"), AGENT),
    lambda: bo.set_progress(bo.SetupProgressUpdate(capability="contact_import", status="READY"), AGENT),
])
def test_an_ordinary_agent_cannot_administer_the_brokerage(monkeypatch, call):
    """An agent must not promote themselves, invite, or rewrite the business."""
    _patch_tx(monkeypatch, _invite_conn())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(call())
    assert exc.value.status_code in (401, 403)


def test_an_ordinary_agent_may_still_read_the_setup_screen(monkeypatch):
    _patch_tx(monkeypatch, _setup_conn())
    state = asyncio.run(bo.get_setup(AGENT))
    assert "capabilities" in state


def test_a_platform_admin_may_administer(monkeypatch):
    _patch_tx(monkeypatch, _invite_conn())
    out = asyncio.run(bo.post_invitations(bo.InviteCreate(emails=["x@y.test"]), ADMIN))
    assert out["created"]


# ---------------------------------------------------------------------------
# Tenant isolation at the statement level
# ---------------------------------------------------------------------------

def test_revoke_is_scoped_to_the_session_tenant(monkeypatch):
    """Brokerage A must not be able to revoke Brokerage B's invitation. RLS
    enforces this in Postgres; the statement carries the predicate too, so a
    loosened policy would not silently open it."""
    conn = FakeConn(rows={"UPDATE brokerage_invitations": None})
    _patch_tx(monkeypatch, conn)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(bo.revoke_invitation("22222222-2222-2222-2222-222222222222", OWNER))
    assert exc.value.status_code == 404
    stmt, args = [(q, a) for q, a in conn.statements if "UPDATE brokerage_invitations" in q][0]
    assert "tenant_id = $2::uuid" in stmt
    assert args[1] == TENANT_A


def test_profile_update_targets_the_session_tenant_only(monkeypatch):
    conn = _setup_conn()
    conn.rows["UPDATE tenants"] = {"id": TENANT_A}
    _patch_tx(monkeypatch, conn)
    asyncio.run(bo.update_profile(bo.BrokerageProfileUpdate(name="Renamed"), OWNER))
    stmt, args = [(q, a) for q, a in conn.statements if "UPDATE tenants" in q][0]
    assert "WHERE id = $1::uuid" in stmt and args[0] == TENANT_A
    # The body cannot name a tenant at all.
    assert "tenant_id" not in bo.BrokerageProfileUpdate.model_fields


def test_profile_rejects_unknown_fields():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        bo.BrokerageProfileUpdate(name="X", tenant_id=TENANT_B)


def test_profile_normalises_state_to_uppercase(monkeypatch):
    conn = _setup_conn()
    conn.rows["UPDATE tenants"] = {"id": TENANT_A}
    _patch_tx(monkeypatch, conn)
    asyncio.run(bo.update_profile(bo.BrokerageProfileUpdate(primary_state="de"), OWNER))
    _, args = [(q, a) for q, a in conn.statements if "UPDATE tenants" in q][0]
    assert "DE" in args


def test_empty_profile_update_is_rejected(monkeypatch):
    _patch_tx(monkeypatch, _setup_conn())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(bo.update_profile(bo.BrokerageProfileUpdate(), OWNER))
    assert exc.value.status_code == 422


def test_progress_cannot_forge_a_derived_capability():
    """Hand-writing "phone: READY" would make setup disagree with the call
    path, so the model only accepts the three with no live source."""
    from pydantic import ValidationError
    # "mls" joined this list once feed health became its real source: an
    # operator hand-writing "mls: READY" would tell a brokerage a reference
    # dataset was live inventory.
    for forged in ("phone", "billing", "mls", "agent_invites", "brokerage_profile", "readiness"):
        with pytest.raises(ValidationError):
            bo.SetupProgressUpdate(capability=forged, status="READY")


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def test_dev_capture_returns_the_link_when_no_mail_server_is_configured(monkeypatch):
    monkeypatch.setenv("ORACLE_ENV", "dev")
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://app.test")
    import smtp_mailer
    monkeypatch.setattr(smtp_mailer, "is_configured", lambda *a, **k: False)
    link = asyncio.run(bo._send_invitation(
        email="x@y.test", brokerage="B", inviter="i@x.test",
        raw_token="tok", expires_at=FUTURE))
    assert link == "https://app.test/accept-invite?token=tok"


def test_prod_never_captures_even_without_a_mail_server(monkeypatch):
    """An invitation nobody receives is not an invitation; in prod a missing
    mail server must fail loudly rather than quietly hand the link back."""
    monkeypatch.setenv("ORACLE_ENV", "prod")
    import smtp_mailer
    monkeypatch.setattr(smtp_mailer, "is_configured", lambda *a, **k: False)
    assert bo._dev_capture_enabled() is False


def test_a_failed_send_does_not_lose_the_invitation(monkeypatch):
    """The row is already committed. Losing it because the mail server
    hiccuped is worse than an owner clicking Resend."""
    monkeypatch.setenv("ORACLE_ENV", "dev")
    import smtp_mailer
    monkeypatch.setattr(smtp_mailer, "is_configured", lambda *a, **k: True)
    def boom(**_kw):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(smtp_mailer, "send", boom)
    assert asyncio.run(bo._send_invitation(
        email="x@y.test", brokerage="B", inviter="i@x.test",
        raw_token="tok", expires_at=FUTURE)) is None


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------

class MissingTableConn(FakeConn):
    """A database that is behind on migrations for one capability's table."""

    def __init__(self, missing: str, **kw):
        super().__init__(**kw)
        self.missing = missing

    async def fetchrow(self, query, *args):
        if self.missing in query:
            import asyncpg
            raise asyncpg.UndefinedTableError(f"relation {self.missing} does not exist")
        return await super().fetchrow(query, *args)


@pytest.mark.parametrize("table,capability", [
    ("messaging_routes", "phone"),
    ("telephony_routes", "phone"),
    ("subscriptions", "billing"),
])
def test_a_capability_table_that_is_behind_does_not_500_the_whole_screen(table, capability):
    """§17: Neoh stays useful while integrations are incomplete, and that has
    to include the screen reporting on them. A missing carrier table was a
    real 500 against a dev database one migration behind."""
    base = _setup_conn()
    conn = MissingTableConn(table, rows=base.rows, fetch_rows=base.fetch_rows)
    state = asyncio.run(bo.compute_setup_state(conn, OWNER))
    assert state["capabilities"][capability] in ("NOT_STARTED", "NEEDS_ACTION")
    assert set(state["capabilities"]) == set(bo.CAPABILITIES)
