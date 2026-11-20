"""The approved-EMAIL branch of the command worker, end to end against real
PostgreSQL as the application role (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Owed by PR #146 and PR #147, which changed this branch with no test running it:

* Before #146 a function-local ``from agent_profile import load_agent_identity``
  (in the call branch) made that name local to the whole of
  ``_execute_command_job``, so every approved email raised UnboundLocalError
  before it was sent.
* Before #147 ``submission_started`` was set before the sender, its credential
  and the drafter's identity were loaded, so a failure loading any of them
  ended the command 'reconciliation_required' (a person must check whether it
  went) instead of 'failed' (definitely not sent; retryable).

Real code paths throughout: staging, the outreach gate, the commercial footer,
the encrypted credential vault, the drafter's identity (load_agent_identity is
NOT patched: the name itself was the regression) and every state write. Only
the network edge is replaced: the configured sender in ``_EMAIL_SENDERS`` is a
stub that records what it was handed. Nothing is sent.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import types
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

APPROVED_SUBJECT = "About 12 Elm St"
APPROVED_BODY = "Hi Pat,\n\nThe inspection is booked for Tuesday at 10."
BROKERAGE = "Elm Street Realty"
POSTAL = "1 Main St, Dover, DE 19901"
RECIPIENT = "pat@example.test"


class _StubSender:
    """The network edge. Records every submission, then returns a provider
    result or raises what it was told to."""

    def __init__(self, reference: str) -> None:
        self.reference = reference
        self.calls: list[dict] = []
        self.fail_with: Exception | None = None

    async def __call__(self, draft, *, credentials=None, reply_to=None, list_unsubscribe_url=None):
        from command_providers import ProviderResult

        self.calls.append({"draft": dict(draft), "credentials": credentials, "reply_to": reply_to,
                           "list_unsubscribe_url": list_unsubscribe_url})
        if self.fail_with is not None:
            raise self.fail_with
        return ProviderResult(provider="smtp", reference=self.reference, status="accepted", detail={})


class _Reporter:
    def __init__(self, job: dict) -> None:
        self.job = job
        self.steps: list[str] = []

    async def progress(self, _pct, message="", *_a, **_k):
        self.steps.append(message)


def test_an_approved_email_is_sent_with_its_footer_and_the_drafters_identity(monkeypatch):
    asyncio.run(_with_brokerage(monkeypatch, _sent))


def test_a_credential_that_cannot_be_loaded_ends_failed_and_retryable(monkeypatch):
    asyncio.run(_with_brokerage(monkeypatch, _credential_unreadable))


def test_a_provider_rejection_ends_failed(monkeypatch):
    asyncio.run(_with_brokerage(monkeypatch, _rejected))


def test_an_uncertain_provider_error_ends_reconciliation_required(monkeypatch):
    asyncio.run(_with_brokerage(monkeypatch, _uncertain))


# ── scenarios ─────────────────────────────────────────────────────────────────

async def _sent(w):
    import agent_profile
    import commands_api
    import commercial_email
    import config

    # the module-level import is the one in use, and nothing replaced it
    assert commands_api.load_agent_identity is agent_profile.load_agent_identity
    gate_before = await w.allowed_email_attempts()
    command_id, payload, reporter = await w.stage("sent")

    result = await commands_api._execute_command_job(payload, reporter)

    assert result["provider"] == "smtp" and result["reference"] == w.sender.reference
    assert "submitting approved email" in reporter.steps
    row = await w.state(command_id)
    assert row["state"] == "succeeded", dict(row)
    assert row["provider"] == "smtp" and row["provider_reference"] == w.sender.reference
    assert row["provider_submitted_at"] is not None and row["last_error"] is None
    # the outreach gate really ran, and allowed it
    assert await w.allowed_email_attempts() == gate_before + 1

    assert len(w.sender.calls) == 1
    call = w.sender.calls[0]
    assert call["draft"]["subject"] == APPROVED_SUBJECT
    assert call["draft"]["target"]["email"] == RECIPIENT
    # the approved body unchanged, then the fixed commercial footer: brokerage,
    # postal address, and an unsubscribe link bound to this brokerage + recipient
    url = call["list_unsubscribe_url"]
    prefix = config.public_base_url() + commercial_email.UNSUBSCRIBE_PATH
    assert url and url.startswith(prefix)
    assert commercial_email.read_token(url[len(prefix):]) == (w.tenant, RECIPIENT)
    body = call["draft"]["body"]
    assert body.startswith(APPROVED_BODY + "\n\n--\n" + BROKERAGE + "\n" + POSTAL + "\n"), body
    assert body.endswith(f"Unsubscribe from {BROKERAGE} emails: {url}"), body
    # the drafter's own SMTP identity, decrypted from the vault...
    assert call["credentials"] == w.credentials
    # ...and their Reply-To, which only load_agent_identity(drafter) can supply
    # (the worker itself is "command-worker", who has no profile)
    assert call["reply_to"] == w.reply_to


async def _credential_unreadable(w):
    import commands_api
    import crypto

    command_id, payload, reporter = await w.stage("vault")
    # The drafter's stored SMTP identity can no longer be decrypted: loading it
    # raises a plain CryptoError (not a provider error), before any sender call.
    await w.admin.execute("UPDATE provider_credentials SET token_ciphertext=$2 WHERE id=$1",
                          w.credential_id, b"\xc3\x0dnot a pgp message")

    with pytest.raises(crypto.CryptoError) as exc:
        await commands_api._execute_command_job(payload, reporter)

    # the job runner retries it (only job_terminal errors are dead-lettered)
    assert not getattr(exc.value, "job_terminal", False)
    row = await w.state(command_id)
    # before PR #147: 'reconciliation_required', though nothing had been sent
    assert row["state"] == "failed", dict(row)
    assert row["reconciliation_reason"] is None
    assert row["provider"] is None and row["provider_reference"] is None
    assert row["last_error"]
    assert w.sender.calls == []

    # 'failed' really is retryable: once the identity reads again, the same job
    # sends the approved email, once
    await w.store_credential()
    result = await commands_api._execute_command_job(payload, reporter)
    assert result["reference"] == w.sender.reference
    assert len(w.sender.calls) == 1
    assert (await w.state(command_id))["state"] == "succeeded"


async def _rejected(w):
    import commands_api
    from command_providers import ProviderRejectedError

    command_id, payload, reporter = await w.stage("rejected")
    w.sender.fail_with = ProviderRejectedError("550 5.1.1 recipient address rejected")

    with pytest.raises(ProviderRejectedError):
        await commands_api._execute_command_job(payload, reporter)

    assert len(w.sender.calls) == 1  # the provider was asked, and refused before accepting it
    row = await w.state(command_id)
    assert row["state"] == "failed", dict(row)
    assert row["reconciliation_reason"] is None
    assert row["provider"] is None and row["provider_reference"] is None
    assert "550 5.1.1" in row["last_error"]


async def _uncertain(w):
    import commands_api

    command_id, payload, reporter = await w.stage("uncertain")
    w.sender.fail_with = TimeoutError("SMTP server stopped answering after DATA")

    # Not raised: a retry could send it twice.
    result = await commands_api._execute_command_job(payload, reporter)

    assert result["status"] == "reconciliation_required"
    assert result["detail"] == {"duplicate_submission_suppressed": True}
    assert len(w.sender.calls) == 1
    row = await w.state(command_id)
    assert row["state"] == "reconciliation_required", dict(row)
    assert row["reconciliation_reason"].startswith("Provider submission began")
    assert row["provider_reference"] is None
    assert "after DATA" in row["last_error"]
    # a second delivery of the same job does not send it again
    with pytest.raises(RuntimeError, match="not executable from state reconciliation_required"):
        await commands_api._execute_command_job(payload, reporter)
    assert len(w.sender.calls) == 1


# ── one throwaway brokerage per test ─────────────────────────────────────────

async def _with_brokerage(monkeypatch, scenario):  # noqa: C901 — setup, scenario, cleanup
    import asyncpg

    import commands_api
    from commands_api import CommandCreate, CommandType, ProviderCredentialInput
    from db import connection
    from tenancy import Role, TenantContext

    if not os.environ.get("ORACLE_ENCRYPTION_MASTER_KEY"):
        monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "live-test-master-key-" + secrets.token_hex(16))
    # the default provider (smtp), resolved by the real _resolve_email_provider;
    # only the function it hands back is the stub
    monkeypatch.delenv("ORACLE_EMAIL_PROVIDER", raising=False)
    tag = uuid.uuid4().hex[:8]
    sender = _StubSender(f"<email-live-{tag}@smtp.stub.test>")
    monkeypatch.setitem(commands_api._EMAIL_SENDERS, "smtp", (sender, "smtp"))

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant = str(uuid.uuid4())
    owner = f"owner-{tag}@mail.test"
    ctx = TenantContext(agent_id=owner, tenant_id=tenant, role=Role.BROKER_OWNER)
    client = uuid.uuid4()
    credentials = {"host": "smtp.brokerage.test", "port": 587, "username": owner,
                   "password": "pw-" + secrets.token_hex(8), "from_email": owner}
    w = types.SimpleNamespace(admin=admin, tenant=tenant, owner=owner, sender=sender, credentials=credentials,
                              reply_to=f"olive-{tag}@elmstreet.test", credential_id=None)

    async def store_credential():
        stored = await commands_api.store_provider_credential(
            "smtp", ProviderCredentialInput(account_label=owner, token=json.dumps(credentials)), ctx=ctx)
        w.credential_id = uuid.UUID(stored["id"])

    async def stage(key: str):
        staged = await commands_api.create_command(CommandCreate(
            command_type=CommandType.EMAIL, target={"email": RECIPIENT, "client_id": str(client)},
            draft={"subject": APPROVED_SUBJECT, "body": APPROVED_BODY}, idempotency_key=f"mail-{tag}-{key}"),
            ctx=ctx)
        assert staged["created"]
        command_id = staged["command"]["id"]
        # Approved and released: the state _queue_execution_in_tx leaves. The
        # job row itself is not enqueued, so no running worker can claim it.
        row = await admin.fetchrow(
            "UPDATE command_executions SET state='queued' WHERE id=$1 RETURNING target, draft, approval_id",
            uuid.UUID(command_id))
        stored = commands_api._decode(row["draft"])
        payload = commands_api._execution_payload(
            command_id=command_id, command_type=CommandType.EMAIL, target=commands_api._decode(row["target"]),
            draft=dict(stored.get("content") or {}), context=dict(stored.get("context") or {}), tenant_id=tenant)
        assert payload["draft"]["body"] == APPROVED_BODY
        reporter = _Reporter({"tenant_id": tenant, "created_by": owner, "approval_id": str(row["approval_id"])})
        return command_id, payload, reporter

    async def state(command_id: str):
        return await admin.fetchrow(
            "SELECT state, provider, provider_reference, provider_submitted_at, last_error, "
            "reconciliation_reason FROM command_executions WHERE id=$1", uuid.UUID(command_id))

    async def allowed_email_attempts() -> int:
        return await admin.fetchval(
            "SELECT count(*) FROM outreach_attempt_log WHERE tenant_id=$1 AND channel='email' AND allowed",
            uuid.UUID(tenant))

    w.store_credential, w.stage, w.state, w.allowed_email_attempts = (
        store_credential, stage, state, allowed_email_attempts)
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name, postal_address) VALUES ($1,$2,$3,$4)",
                            tenant, f"mail-{tag}", BROKERAGE, POSTAL)
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash, is_active) "
                            "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y',true)", tenant, owner)
        # the drafter's identity: load_agent_identity reads public_email for Reply-To
        await admin.execute("INSERT INTO user_profiles (user_id, tenant_id, display_name, public_email) "
                            "VALUES ($1,$2,'Olive Owner',$3)", owner, tenant, w.reply_to)
        # entitled even where billing enforcement is on (it is off under ORACLE_ENV=dev)
        await admin.execute("INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id, "
                            "status) VALUES ($1,$2,$3,'active')", tenant, f"cus_live_{tag}", f"sub_live_{tag}")
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name, email) "
                            "VALUES ($1,$2,'Pat Seller','Pat@Example.TEST')", client, tenant)
        # the drafter's own SMTP identity, stored through the real vault endpoint
        await store_credential()
        await scenario(w)
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            tables = await admin.fetch(
                """SELECT c.table_name, c.data_type FROM information_schema.columns c
                     JOIN information_schema.tables t
                       ON t.table_name = c.table_name AND t.table_schema = c.table_schema
                    WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
                      AND t.table_type = 'BASE TABLE'""")
            for row in tables:
                cast = "uuid" if row["data_type"] == "uuid" else "text"
                await admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", tenant)
            await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(tenant))
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
