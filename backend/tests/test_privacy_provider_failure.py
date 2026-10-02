"""Offboarding/erasure when a provider is down: never claim what did not happen.

A Google revoke that fails (Google down, recovery mode) must leave the
credential row in place, disabled but still revocable on a retry, and report
`revoke_failed`. Deleting the row anyway would orphan a live grant at Google
that nobody could revoke again.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import commands_api
import crypto
import privacy_lifecycle as pl
from tenancy import Role, TenantContext


class _Conn:
    def __init__(self, rows):
        self.rows, self.deleted = rows, []

    async def fetch(self, *_a):
        return self.rows

    async def execute(self, sql, *args):
        if sql.startswith("DELETE FROM provider_credentials"):
            self.deleted.append(args[0])


def _run(monkeypatch, revoke):
    conn = _Conn([{"id": "cred-google", "provider": "google", "account_label": "a@x.test",
                   "token_ciphertext": b"c", "refresh_ciphertext": b"r"},
                  {"id": "cred-other", "provider": "fireworks", "account_label": "a@x.test",
                   "token_ciphertext": b"c", "refresh_ciphertext": None}])

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    async def decrypt(_conn, _cipher, _key):
        return "refresh-token"

    monkeypatch.setattr(pl, "tenant_tx", tx)
    monkeypatch.setattr(crypto, "decrypt_pii", decrypt)
    monkeypatch.setattr(commands_api, "revoke_google_token", revoke)
    monkeypatch.setattr(commands_api, "_provider_key", lambda _t: "test-key")
    ctx = TenantContext(agent_id="owner@x.test", tenant_id="00000000-0000-0000-0000-0000000000aa",
                        role=Role.BROKER_OWNER)
    out = asyncio.run(pl.revoke_personal_credentials(ctx, tenant_id=ctx.tenant_id, agent_id="a@x.test"))
    return out, conn


def test_google_down_keeps_the_credential_and_reports_it(monkeypatch):
    async def google_down(_token):
        raise TimeoutError("oauth2.googleapis.com timed out")

    out, conn = _run(monkeypatch, google_down)
    by_id = {o["credential_id"]: o for o in out}
    assert by_id["cred-google"]["status"] == "revoke_failed"
    assert "cred-google" not in conn.deleted            # still revocable on retry
    assert conn.deleted == ["cred-other"]               # nothing remote to revoke: deleted


def test_google_up_revokes_then_deletes(monkeypatch):
    async def revoked(_token):
        return "revoked"

    out, conn = _run(monkeypatch, revoked)
    assert {o["credential_id"]: o["status"] for o in out}["cred-google"] == "revoked"
    assert sorted(conn.deleted) == ["cred-google", "cred-other"]
