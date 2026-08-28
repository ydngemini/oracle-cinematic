"""One person must not be able to approve their own role override.

The control exists because creating another broker owner should take two
people. It was defeated by capitalisation: `_lookup_user` matches on
`lower(agent_id)`, so "Me@x.com" and "me@x.com" are one account — but login
signed the *typed* spelling into the JWT, so they were two identities to every
string comparison downstream.

A single broker could therefore sign in as "me@x.com", request the change, sign
in as "Me@x.com", and approve it: `existing["requested_by"] == ctx.agent_id`
compared two different strings. The audit trail showed two approvers.

Fixed at the root (the token now carries the row's canonical agent_id) and at
the control point (the comparison case-folds), because this is the line the
guarantee actually lives on.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

import approval_service
from approval_service import ActionRisk
from tenancy import Role, TenantContext

TENANT = "11111111-1111-1111-1111-111111111111"
APPROVAL_ID = "33333333-3333-3333-3333-333333333333"


def _ctx(agent_id):
    return TenantContext(agent_id=agent_id, tenant_id=TENANT, role=Role.BROKER_OWNER)


class _Conn:
    def __init__(self, requested_by):
        self.row = {
            "id": APPROVAL_ID,
            "risk_class": ActionRisk.ROLE_OVERRIDE.value,
            "requested_by": requested_by,
            "status": "pending",
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        }

    async def fetchrow(self, _query, *_args):
        return self.row


def _install(monkeypatch, requested_by):
    conn = _Conn(requested_by)

    @asynccontextmanager
    async def tx(_ctx_):
        yield conn

    monkeypatch.setattr(approval_service, "tenant_tx", tx)
    return conn


@pytest.mark.parametrize(
    "requested_by,approver",
    [
        ("me@x.com", "me@x.com"),        # the obvious case, always caught
        ("me@x.com", "Me@X.com"),        # the bypass: same account, new spelling
        ("Me@X.com", "me@x.com"),        # and the other direction
        ("me@x.com", "  ME@X.COM  "),    # padding is not identity either
    ],
)
def test_the_requester_cannot_approve_under_any_spelling(monkeypatch, requested_by, approver):
    _install(monkeypatch, requested_by)

    with pytest.raises(ValueError, match="different approving broker"):
        asyncio.run(
            approval_service.decide_approval(
                _ctx(approver), APPROVAL_ID, decision="approved", reason="promoting per board minute"
            )
        )


def test_a_genuinely_different_broker_is_not_blocked_by_this_check(monkeypatch):
    """Case-folding must not collapse two real people into one."""
    _install(monkeypatch, "alice@x.com")

    # Gets past the two-person gate; whatever it fails on later is not this rule.
    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            approval_service.decide_approval(
                _ctx("bob@x.com"), APPROVAL_ID, decision="approved", reason="promoting per board minute"
            )
        )
    assert "different approving broker" not in str(excinfo.value)


def test_login_signs_the_canonical_agent_id_not_what_was_typed():
    """The root cause: identity came from the request body, not the user row."""
    import inspect

    import auth

    source = inspect.getsource(auth.login)
    assert 'agent_identity = str(row["agent_id"])' in source
    assert "_issue_jwt(\n        agent_identity," in source, (
        "the JWT subject must be the row's agent_id"
    )
