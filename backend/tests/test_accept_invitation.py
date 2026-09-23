"""Accepting a brokerage invitation.

The interesting cases are all refusals: a replayed token, a lapsed one, a
withdrawn one, and an email that already belongs to somebody. Each has to fail
in a way that tells the person what to do next without telling an attacker
anything they did not already know.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, Response

import auth

TENANT_A = "aaaaaaaa-0000-0000-0000-00000000000a"
TENANT_B = "bbbbbbbb-0000-0000-0000-00000000000b"
NEW_USER = "cccccccc-1111-0000-0000-00000000000c"
FUTURE = datetime.now(timezone.utc) + timedelta(days=7)


class AcceptConn:
    def __init__(self, *, preview=None, existing=None, claimed="default", insert_raises=None):
        self.preview = preview
        self.existing = existing
        self._claimed = claimed
        self.insert_raises = insert_raises
        self.statements: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        self.statements.append((query, args))
        if "brokerage_invitation_preview" in query:
            return self.preview
        if "FROM users WHERE lower(agent_id)" in query:
            return self.existing
        if "INSERT INTO users" in query:
            if self.insert_raises:
                raise self.insert_raises
            return {"id": NEW_USER}
        if "consume_brokerage_invitation" in query:
            if self._claimed == "default":
                return {"tenant_id": TENANT_A, "email": "invitee@a.test", "invited_role": "agent"}
            return self._claimed
        if "SELECT name FROM tenants" in query:
            return {"name": "Brokerage A"}
        return None

    async def execute(self, query, *args):
        self.statements.append((query, args))
        return "INSERT 0 1"


def patch(monkeypatch, conn):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn
    monkeypatch.setattr("db.connection.tenant_tx", tx)


def preview(state="pending", tenant_id=TENANT_A, role="agent"):
    return {
        "tenant_id": tenant_id, "tenant_name": "Brokerage A",
        "email": "invitee@a.test", "invited_role": role,
        "invited_by_agent_id": "owner@a.test", "expires_at": FUTURE, "state": state,
    }


def body(**over):
    payload = {"token": "t" * 32, "password": "correct horse battery", "full_name": "Invitee"}
    payload.update(over)
    return auth.AcceptInviteRequest(**payload)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def test_preview_names_the_brokerage_without_any_session(monkeypatch):
    patch(monkeypatch, AcceptConn(preview=preview()))
    out = asyncio.run(auth.invitation_preview("t" * 32))
    assert out["brokerage"] == "Brokerage A"
    assert out["state"] == "pending"
    assert out["role"] == "agent"


def test_preview_leaks_no_internal_identifiers(monkeypatch):
    patch(monkeypatch, AcceptConn(preview=preview()))
    out = asyncio.run(auth.invitation_preview("t" * 32))
    assert "tenant_id" not in out and "token_hash" not in out
    assert TENANT_A not in str(out)


def test_preview_is_a_flat_404_for_anything_unknown(monkeypatch):
    """Same answer for malformed and never-existed: a probe learns nothing."""
    patch(monkeypatch, AcceptConn(preview=None))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.invitation_preview("nonsense"))
    assert exc.value.status_code == 404


def test_preview_hashes_the_token_before_looking_it_up(monkeypatch):
    conn = AcceptConn(preview=preview())
    patch(monkeypatch, conn)
    asyncio.run(auth.invitation_preview("secret-token-value"))
    args = [a for q, a in conn.statements if "brokerage_invitation_preview" in q][0]
    assert "secret-token-value" not in args[0]
    assert len(args[0]) == 64


# ---------------------------------------------------------------------------
# New user
# ---------------------------------------------------------------------------

def test_new_invitee_lands_in_the_inviting_brokerage(monkeypatch):
    conn = AcceptConn(preview=preview())
    patch(monkeypatch, conn)
    out = asyncio.run(auth.accept_invite(body(), Response()))
    assert out.tenant_id == TENANT_A
    assert out.role == "agent"
    assert out.agent_id == "invitee@a.test"


def test_the_account_is_created_with_the_invitations_role_not_the_bodys(monkeypatch):
    """An invitee must not be able to choose what they become."""
    conn = AcceptConn(preview=preview(role="broker_owner"))
    patch(monkeypatch, conn)
    out = asyncio.run(auth.accept_invite(body(), Response()))
    insert = [a for q, a in conn.statements if "INSERT INTO users" in q][0]
    assert insert[2] == "broker_owner"
    assert out.role == "broker_owner"
    assert "role" not in auth.AcceptInviteRequest.model_fields


def test_the_invitee_cannot_name_their_own_tenant():
    assert "tenant_id" not in auth.AcceptInviteRequest.model_fields
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        auth.AcceptInviteRequest(
            token="t" * 32, password="correct horse battery",
            full_name="X", tenant_id=TENANT_B,
        )


def test_the_email_comes_from_the_invitation_not_the_request(monkeypatch):
    conn = AcceptConn(preview=preview())
    patch(monkeypatch, conn)
    asyncio.run(auth.accept_invite(body(), Response()))
    insert = [a for q, a in conn.statements if "INSERT INTO users" in q][0]
    assert insert[1] == "invitee@a.test"
    assert "email" not in auth.AcceptInviteRequest.model_fields


def test_accepting_creates_an_active_roster_row(monkeypatch):
    """0027 reserved team_memberships.status='invited' for a flow never built.
    This is that flow — and a broker who sent the invitation has approved."""
    conn = AcceptConn(preview=preview())
    patch(monkeypatch, conn)
    asyncio.run(auth.accept_invite(body(), Response()))
    stmt, args = [(q, a) for q, a in conn.statements if "team_memberships" in q][0]
    assert "'active'" in stmt
    assert args[0] == TENANT_A and args[1] == NEW_USER


def test_the_user_is_created_before_the_claim(monkeypatch):
    """consumed_at and consumed_by are CHECK-bound to be set together, so the
    claim needs a real user id. If the claim then loses, the transaction takes
    the account with it."""
    conn = AcceptConn(preview=preview())
    patch(monkeypatch, conn)
    asyncio.run(auth.accept_invite(body(), Response()))
    order = [i for i, (q, _) in enumerate(conn.statements)
             if "INSERT INTO users" in q or "consume_brokerage_invitation" in q]
    first, second = conn.statements[order[0]][0], conn.statements[order[1]][0]
    assert "INSERT INTO users" in first
    assert "consume_brokerage_invitation" in second


def test_a_short_password_is_refused():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        auth.AcceptInviteRequest(token="t" * 32, password="short", full_name="X")


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state,fragment", [
    ("accepted", "already been used"),
    ("revoked", "withdrawn"),
    ("expired", "expired"),
])
def test_dead_invitations_say_which_kind_of_dead(monkeypatch, state, fragment):
    patch(monkeypatch, AcceptConn(preview=preview(state=state)))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.accept_invite(body(), Response()))
    assert exc.value.status_code == 409
    assert fragment in exc.value.detail


def test_replay_after_a_lost_race_is_refused(monkeypatch):
    """consume returns no row when another accept already claimed it."""
    conn = AcceptConn(preview=preview(), claimed=None)
    patch(monkeypatch, conn)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.accept_invite(body(), Response()))
    assert exc.value.status_code == 409
    assert "already been used" in exc.value.detail


def test_an_unknown_token_is_a_404(monkeypatch):
    patch(monkeypatch, AcceptConn(preview=None))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.accept_invite(body(), Response()))
    assert exc.value.status_code == 404


def test_an_existing_member_is_told_to_sign_in(monkeypatch):
    conn = AcceptConn(preview=preview(), existing={"id": NEW_USER, "tenant_id": TENANT_A})
    patch(monkeypatch, conn)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.accept_invite(body(), Response()))
    assert exc.value.status_code == 409
    assert "already a member" in exc.value.detail
    assert not any("INSERT INTO users" in q for q, _ in conn.statements)


def test_an_account_in_another_brokerage_is_an_honest_conflict(monkeypatch):
    """users.tenant_id is NOT NULL and lower(agent_id) is globally unique, so
    one account cannot be in two brokerages. Say so rather than moving them
    out of the brokerage that owns their data."""
    conn = AcceptConn(preview=preview(), existing={"id": NEW_USER, "tenant_id": TENANT_B})
    patch(monkeypatch, conn)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.accept_invite(body(), Response()))
    assert exc.value.status_code == 409
    assert "one brokerage at a time" in exc.value.detail
    assert not any("consume_brokerage_invitation" in q for q, _ in conn.statements)


def test_a_racing_duplicate_signup_is_a_409_not_a_500(monkeypatch):
    import asyncpg
    conn = AcceptConn(preview=preview(), insert_raises=asyncpg.UniqueViolationError("dup"))
    patch(monkeypatch, conn)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.accept_invite(body(), Response()))
    assert exc.value.status_code == 409


def test_the_new_session_requires_policy_acceptance(monkeypatch):
    """Same as /auth/register: a brand-new account has not accepted anything."""
    patch(monkeypatch, AcceptConn(preview=preview()))
    out = asyncio.run(auth.accept_invite(body(), Response()))
    assert out.policy_acceptance_required is True
