"""Offboarding/erasure when a provider is down: never claim what did not happen.

A Google revoke that fails (Google down, recovery mode) must leave the
credential row in place, disabled but still revocable on a retry, and report
`revoke_failed`. Deleting the row anyway would orphan a live grant at Google
that nobody could revoke again.

External audit 22.6: a token that cannot be READ is the same case. It used to
be deleted as `deleted_token_unreadable` without Google ever being asked; a
missing master key deleted every row that way, and (pgp_sym_decrypt runs in
the database) one corrupt row aborted the shared transaction so every later
row failed to decrypt and was deleted unrevoked too.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

import commands_api
import crypto
import privacy_lifecycle as pl
from tenancy import Role, TenantContext

CTX = TenantContext(agent_id="owner@x.test", tenant_id="00000000-0000-0000-0000-0000000000aa",
                    role=Role.BROKER_OWNER)


class _Conn:
    """A PostgreSQL connection as far as revocation needs it: a failed
    statement aborts the transaction (every later statement fails) unless it
    ran inside a savepoint, which rolls back to before it."""

    def __init__(self, rows, bad=()):
        self.rows, self.bad, self.deleted, self.aborted = rows, set(bad), [], False
        self.fetched = []

    def _check(self):
        if self.aborted:
            raise RuntimeError("current transaction is aborted, commands ignored until end of transaction block")

    async def fetch(self, sql, *args):
        self._check()
        self.fetched.append(args)
        return self.rows

    async def fetchrow(self, sql, *args):  # crypto.decrypt_pii's pgp_sym_decrypt
        self._check()
        if args[0] in self.bad:
            self.aborted = True
            raise RuntimeError("Wrong key or corrupt data")
        return {"pt": "token-for-" + args[0].decode()}

    async def execute(self, sql, *args):
        self._check()
        if sql.startswith("DELETE FROM provider_credentials"):
            self.deleted.append(args[0])
        return "DELETE 1"

    def transaction(self):
        conn = self

        class _Savepoint:
            async def __aenter__(self):
                conn._check()
                return self

            async def __aexit__(self, exc_type, exc, tb):
                if exc_type is not None:
                    conn.aborted = False   # ROLLBACK TO SAVEPOINT
                return False

        return _Savepoint()


def _row(cred_id, provider="google", cipher=None):
    return {"id": cred_id, "provider": provider, "account_label": "a@x.test",
            "token_ciphertext": b"access-" + cred_id.encode(),
            "refresh_ciphertext": cipher if cipher is not None else cred_id.encode()}


def _run(monkeypatch, revoke, rows=None, bad=(), provider_key=lambda _t: "test-key", decrypt=None,
         **kwargs):
    conn = _Conn(rows if rows is not None else [_row("cred-google"), _row("cred-other", "fireworks")], bad)

    @asynccontextmanager
    async def tx(_ctx):
        conn.aborted = False    # a new transaction
        yield conn

    async def fake_decrypt(_conn, _cipher, _key):
        return "refresh-token"

    monkeypatch.setattr(pl, "tenant_tx", tx)
    if decrypt is not False:     # False: the real crypto.decrypt_pii, against _Conn's pgp_sym_decrypt
        monkeypatch.setattr(crypto, "decrypt_pii", decrypt or fake_decrypt)
    monkeypatch.setattr(commands_api, "revoke_google_token", revoke)
    monkeypatch.setattr(commands_api, "_provider_key", provider_key)
    kwargs.setdefault("agent_id", "a@x.test")
    out = asyncio.run(pl.revoke_personal_credentials(CTX, tenant_id=CTX.tenant_id, **kwargs))
    return out, conn


async def _revoked(_token):
    return "revoked"


def test_google_down_keeps_the_credential_and_reports_it(monkeypatch):
    async def google_down(_token):
        raise TimeoutError("oauth2.googleapis.com timed out")

    out, conn = _run(monkeypatch, google_down)
    by_id = {o["credential_id"]: o for o in out}
    assert by_id["cred-google"]["status"] == "revoke_failed"
    assert "cred-google" not in conn.deleted            # still revocable on retry
    assert conn.deleted == ["cred-other"]               # nothing remote to revoke: deleted


def test_google_up_revokes_then_deletes(monkeypatch):
    out, conn = _run(monkeypatch, _revoked)
    assert {o["credential_id"]: o["status"] for o in out}["cred-google"] == "revoked"
    assert sorted(conn.deleted) == ["cred-google", "cred-other"]


def test_an_undecryptable_token_is_kept_and_never_called_revoked(monkeypatch):
    """22.6: decrypt fails -> kept (disabled), `revocation_unverified`, Google
    never asked; it used to be deleted as `deleted_token_unreadable`."""
    asked = []

    async def revoke(token):
        asked.append(token)
        return "revoked"

    async def bad(_conn, _cipher, _key):
        raise crypto.CryptoError("decrypt_pii: wrong key or corrupt data")

    out, conn = _run(monkeypatch, revoke, decrypt=bad)
    by_id = {o["credential_id"]: o for o in out}
    assert by_id["cred-google"] == {"credential_id": "cred-google", "provider": "google",
                                    "status": "revocation_unverified", "detail": "undecryptable"}
    assert "cred-google" not in conn.deleted and asked == []
    assert conn.deleted == ["cred-other"]
    assert "revocation_unverified" in pl.REVOCATION_FAILED


def test_a_missing_encryption_key_deletes_no_google_row(monkeypatch):
    """22.6: _provider_key raising (no master key) used to sit inside the
    per-row try, so EVERY Google row was deleted unrevoked."""
    asked = []

    async def revoke(token):
        asked.append(token)
        return "revoked"

    def no_key(_tenant):
        raise HTTPException(status_code=503, detail="Provider credential encryption is not configured.")

    rows = [_row("g1"), _row("g2"), _row("smtp1", "smtp")]
    out, conn = _run(monkeypatch, revoke, rows=rows, provider_key=no_key)
    statuses = {o["credential_id"]: (o["status"], o.get("detail")) for o in out}
    assert statuses == {"g1": ("revocation_unverified", "key_unavailable"),
                        "g2": ("revocation_unverified", "key_unavailable"),
                        "smtp1": ("deleted_no_remote_grant", None)}
    assert conn.deleted == ["smtp1"] and asked == []


def test_one_corrupt_row_does_not_cost_the_others_their_revocation(monkeypatch):
    """22.6: each decrypt runs in its own savepoint. Without it the corrupt
    first row aborts the transaction and the good second row cannot be read
    either. Uses the real crypto.decrypt_pii against _Conn's pgp_sym_decrypt."""
    asked = []

    async def revoke(token):
        asked.append(token)
        return "revoked"

    rows = [_row("bad", cipher=b"corrupt"), _row("good", cipher=b"fine")]
    out, conn = _run(monkeypatch, revoke, rows=rows, bad={b"corrupt"}, decrypt=False)
    statuses = {o["credential_id"]: o["status"] for o in out}
    assert statuses == {"bad": "revocation_unverified", "good": "revoked"}
    assert asked == ["token-for-fine"]
    assert conn.deleted == ["good"]


def test_scoped_revocation_reads_only_the_given_disabled_rows(monkeypatch):
    """The offboarding job names its credentials; a scoped run deletes only a
    row still disabled (a reconnect's re-enabled row is a new grant)."""
    seen = []

    class Rec(_Conn):
        async def execute(self, sql, *args):
            seen.append((sql, args))
            return await super().execute(sql, *args)

    rec = Rec([_row("cred-google")])

    @asynccontextmanager
    async def tx(_ctx):
        yield rec

    async def decrypt(_conn, _cipher, _key):
        return "t"

    monkeypatch.setattr(pl, "tenant_tx", tx)
    monkeypatch.setattr(crypto, "decrypt_pii", decrypt)
    monkeypatch.setattr(commands_api, "revoke_google_token", _revoked)
    monkeypatch.setattr(commands_api, "_provider_key", lambda _t: "k")
    asyncio.run(pl.revoke_personal_credentials(CTX, tenant_id=CTX.tenant_id, agent_id=None,
                                               credential_ids=["cred-google"]))
    assert rec.fetched == [(CTX.tenant_id, None, ["cred-google"], True)]
    delete_sql, delete_args = seen[-1]
    assert "disabled_at IS NOT NULL" in delete_sql and delete_args == ("cred-google", True)
    # erasure (no scope) reads every row and deletes regardless of disabled_at
    rec.fetched.clear()
    asyncio.run(pl.revoke_personal_credentials(CTX, tenant_id=CTX.tenant_id, agent_id=None))
    assert rec.fetched == [(CTX.tenant_id, None, None, False)]


@pytest.mark.parametrize("status,failed", [
    ("revoked", False), ("already_invalid", False), ("deleted_no_remote_grant", False),
    ("released", False), ("no_subscription", False), ("renewal_cancelled", False),
    ("revoke_failed", True), ("revocation_unverified", True), ("stripe_not_configured", True),
    ("failed", True), ("failed:TimeoutError", True),
])
def test_the_erasure_provider_phase_counts_every_step_that_did_not_happen(status, failed):
    assert pl._provider_step_failed(status) is failed


def test_the_erasure_provider_phase_fails_on_an_unreadable_token_and_unconfigured_stripe(monkeypatch):
    class Conn:
        async def fetch(self, *_a):
            return []

    @asynccontextmanager
    async def tx(_ctx):
        yield Conn()

    async def revoke(_ctx, **_kw):
        return [{"credential_id": "g1", "provider": "google", "status": "revocation_unverified",
                 "detail": "undecryptable"},
                {"credential_id": "g2", "provider": "google", "status": "revoked"}]

    async def no_stripe(_tenant, cancel, **_kw):
        assert cancel is True
        return "stripe_not_configured"

    monkeypatch.setattr(pl, "tenant_tx", tx)
    monkeypatch.setattr(pl, "revoke_personal_credentials", revoke)
    monkeypatch.setattr(pl, "_converge_subscription", no_stripe)
    out = asyncio.run(pl._phase_providers(pl._platform_ctx(), "op1", CTX.tenant_id, {}))
    assert out["failed"] == 2
    unverified = next(s for s in out["steps"] if s["status"] == "revocation_unverified")
    assert "third-party apps" in unverified["note"]          # what a person does instead
    assert "note" not in next(s for s in out["steps"] if s["status"] == "revoked")


def test_an_expired_access_token_alone_never_proves_the_grant_gone(monkeypatch):
    """No refresh token held: Google answers invalid_token for an access token
    that has merely expired, which says nothing about the grant (review of
    22.6). Kept and unverified, not deleted as already_invalid."""
    async def invalid(_token):
        return "already_invalid"

    rows = [_row("access-only", cipher=b""), _row("with-refresh")]
    out, conn = _run(monkeypatch, invalid, rows=rows)
    statuses = {o["credential_id"]: (o["status"], o.get("detail")) for o in out}
    assert statuses == {"access-only": ("revocation_unverified", "access_token_rejected"),
                        "with-refresh": ("already_invalid", None)}
    assert conn.deleted == ["with-refresh"]
    # a live access token Google accepts does end the grant
    out, conn = _run(monkeypatch, _revoked, rows=[_row("access-only", cipher=b"")])
    assert out[0]["status"] == "revoked" and conn.deleted == ["access-only"]


def test_a_scoped_run_selects_only_rows_still_disabled():
    """The SQL filter itself (review: removing it survived every fake-based
    test); test_offboarding_revocation_live proves a listed id that was
    re-enabled is never revoked against PostgreSQL."""
    import inspect

    src = inspect.getsource(pl.revoke_personal_credentials)
    assert "AND ($4::boolean IS FALSE OR disabled_at IS NOT NULL)" in src
