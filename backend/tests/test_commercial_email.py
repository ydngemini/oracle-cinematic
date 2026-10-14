"""Commercial email basics (L17-08, crosswalk T-CS-1): unsubscribe token, the
RFC 8058 one-click headers, the postal-address footer, the public endpoint and
which mail is classified as commercial. No database: test_email_unsubscribe_live.py
proves the opt-out against real PostgreSQL."""

from __future__ import annotations

import ast
import asyncio
import email
import inspect
import uuid
from email import policy
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import commercial_email as ce

BACKEND = Path(__file__).resolve().parents[1]
TENANT = "6f1c2b9e-0a4d-4c8e-9b7a-2d3e4f5a6b7c"
ADDRESS = "Pat.Client+deals@Example.com"


@pytest.fixture(autouse=True)
def _base_url(monkeypatch):
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://app.neoh.test")
    monkeypatch.delenv("ORACLE_DOMAIN", raising=False)
    monkeypatch.delenv("ORACLE_BASE_URL", raising=False)


# ── the token ───────────────────────────────────────────────────────────────

def test_a_token_names_the_brokerage_and_the_normalized_address():
    assert ce.read_token(ce.make_token(TENANT, ADDRESS)) == (TENANT, ADDRESS.lower())


def test_the_link_carries_no_readable_personal_data():
    url = ce.unsubscribe_url(ce.make_token(TENANT, ADDRESS))
    low = url.lower()
    for leak in ("pat.client", "example.com", "deals", TENANT, TENANT.replace("-", "")):
        assert leak not in low, leak
    assert url.startswith("https://app.neoh.test/api/public/email/unsubscribe/u1")


def test_two_links_for_the_same_person_differ_and_both_work():
    a, b = ce.make_token(TENANT, ADDRESS), ce.make_token(TENANT, ADDRESS)
    assert a != b
    assert ce.read_token(a) == ce.read_token(b)


def test_an_altered_or_foreign_token_reads_as_nothing(monkeypatch):
    token = ce.make_token(TENANT, ADDRESS)
    flipped = token[:-3] + ("A" if token[-3] != "A" else "B") + token[-2:]
    for bad in (flipped, token[:-6], "u2" + token[2:], "", "u1", "u1!!!!", "u1" + "A" * 600,
                token + "=", None, 42):
        assert ce.read_token(bad) is None, bad
    import auth

    monkeypatch.setattr(auth, "SECRET_KEY", "a-different-deployment-secret")
    assert ce.read_token(token) is None, "a token minted under another key must not decrypt"


def test_a_non_address_cannot_be_tokenized():
    with pytest.raises(ce.UnsubscribeTokenError):
        ce.make_token(TENANT, "+13025550100")


def test_the_key_is_derived_not_the_app_secret_itself():
    import auth

    assert ce._key() != auth.SECRET_KEY.encode()
    assert len(ce._key()) == 32  # AES-256


# ── footer ──────────────────────────────────────────────────────────────────

def test_the_footer_carries_the_postal_address_and_the_link():
    prepared = ce.compose(tenant_id=TENANT, recipient=ADDRESS, body="Hi Pat,\nThe house is ready.\n",
                          brokerage_name="Lockwood  Realty", postal_address="1 Loockerman St\nDover, DE 19901")
    assert prepared.body.startswith("Hi Pat,\nThe house is ready.\n\n--\nLockwood Realty\n")
    assert "1 Loockerman St\nDover, DE 19901" in prepared.body
    assert prepared.body.rstrip().endswith(prepared.unsubscribe_url)
    assert prepared.postal_address_configured is True
    assert prepared.headers == {"List-Unsubscribe": f"<{prepared.unsubscribe_url}>",
                                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


def test_without_an_address_the_footer_invents_none():
    prepared = ce.compose(tenant_id=TENANT, recipient=ADDRESS, body="Hello", brokerage_name="Lockwood Realty",
                          postal_address="   ")
    assert prepared.postal_address_configured is False
    footer = prepared.body.split("\n--\n", 1)[1].splitlines()
    assert footer[0] == "Lockwood Realty"
    assert footer[1].startswith("You are receiving this email from Lockwood Realty")
    assert "Unsubscribe" in footer[2]


def test_prepare_reads_the_brokerage_row(monkeypatch):
    from contextlib import asynccontextmanager

    seen = {}

    class Conn:
        async def fetchrow(self, sql, *args):
            seen["sql"], seen["args"] = sql, args
            return {"name": "Lockwood Realty", "postal_address": "PO Box 7, Dover, DE 19903"}

    @asynccontextmanager
    async def tx(ctx):
        seen["ctx"] = ctx
        yield Conn()

    import db.connection

    monkeypatch.setattr(db.connection, "tenant_tx", tx)
    prepared = asyncio.run(ce.prepare(TENANT, recipient=ADDRESS, body="b"))
    assert "PO Box 7, Dover, DE 19903" in prepared.body
    assert seen["args"] == (TENANT,) and seen["ctx"].tenant_id == TENANT
    assert "postal_address" in seen["sql"] and "tenant_id" not in seen["sql"]


# ── the headers on the wire ─────────────────────────────────────────────────

def _raw(**kw) -> bytes:
    import smtp_mailer

    msg = smtp_mailer.build_message(sender="mail@brokerage.test", sender_name="Lockwood Realty",
                                    recipient="pat@example.com", subject="Your showing", text="hello", **kw)
    return msg.as_bytes()


def test_the_list_unsubscribe_headers_survive_serialization_verbatim():
    """The default email policy folds a 78+ column header with no whitespace into
    RFC 2047 encoded words (=?utf-8?q?=3Chttps=3A//...), which no mailbox
    provider reads as a URI. Found while building this; the raw bytes are the
    test, not the EmailMessage object."""
    url = ce.unsubscribe_url(ce.make_token(TENANT, ADDRESS))
    assert len(url) > 78
    raw = _raw(list_unsubscribe_url=url)
    lines = raw.decode("ascii").split("\n")
    assert f"List-Unsubscribe: <{url}>" in [line.rstrip("\r") for line in lines]
    assert "List-Unsubscribe-Post: List-Unsubscribe=One-Click" in [line.rstrip("\r") for line in lines]
    assert b"=?utf-8?" not in raw
    parsed = email.message_from_bytes(raw, policy=policy.default)
    assert str(parsed["List-Unsubscribe"]) == f"<{url}>"
    assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert max(len(line) for line in lines) <= 998


def test_transactional_mail_has_no_list_headers():
    raw = _raw()
    assert b"List-Unsubscribe" not in raw


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "https://x.test/a\r\nBcc: victim@x.test",
                                 "https://x.test/a b", "https://x.test/<a>", "ftp://x.test/a"])
def test_a_malformed_unsubscribe_url_is_refused(bad):
    import smtp_mailer

    with pytest.raises(smtp_mailer.SmtpConfigurationError):
        _raw(list_unsubscribe_url=bad)


def test_the_approved_sender_passes_the_url_to_the_relay(monkeypatch):
    import command_providers
    import smtp_mailer

    seen = {}

    def fake_send(**kw):
        seen.update(kw)
        return "<mid@brokerage.test>"

    monkeypatch.setattr(smtp_mailer, "send", fake_send)
    asyncio.run(command_providers.send_smtp_email(
        {"target": {"email": "pat@example.com"}, "subject": "s", "body": "b"},
        list_unsubscribe_url="https://app.neoh.test/api/public/email/unsubscribe/u1x"))
    assert seen["list_unsubscribe_url"] == "https://app.neoh.test/api/public/email/unsubscribe/u1x"


# ── classification: which mail is commercial ────────────────────────────────

def _direct_smtp_callers() -> set[str]:
    callers = set()
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith(("tests/", "venv/")) or "site-packages" in rel:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == "send"
                    and isinstance(node.value, ast.Name) and node.value.id == "smtp_mailer"):
                callers.add(rel)
    return callers


def test_every_direct_smtp_caller_is_classified():
    """A new module that mails smtp_mailer.send directly must be listed as
    transactional (EXEMPT_SENDERS) or route through commercial_email.prepare."""
    unclassified = _direct_smtp_callers() - ce.EXEMPT_SENDERS
    assert not unclassified, f"classify these senders in commercial_email.EXEMPT_SENDERS: {unclassified}"


def test_both_commercial_senders_footer_every_email():
    import commands_api
    import email_outbox

    deliver = inspect.getsource(email_outbox.deliver)
    assert "commercial_email.prepare" in deliver and "list_unsubscribe_url=commercial.unsubscribe_url" in deliver
    execute = inspect.getsource(commands_api._execute_command_job)
    assert "commercial_email.prepare" in execute and "list_unsubscribe_url=commercial.unsubscribe_url" in execute
    assert '"body": commercial.body' in execute


def test_the_outbox_rechecks_the_opt_out_list_at_send_time():
    import email_outbox

    deliver = inspect.getsource(email_outbox.deliver)
    assert deliver.index("guard_outreach") < deliver.index("await sender(")


# ── the public endpoint ─────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    import server
    from csrf_middleware import CSRFMiddleware
    from email_unsubscribe_api import router

    recorded: list[tuple[str, str]] = []

    async def record(tenant_id, address):
        recorded.append((tenant_id, address))

    async def name(tenant_id):
        return "Lockwood <Realty>"

    monkeypatch.setattr(ce, "record_unsubscribe", record)
    monkeypatch.setattr(ce, "tenant_name", name)
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(CSRFMiddleware, enabled=True)
    app.middleware("http")(server._security_headers)
    c = TestClient(app, raise_server_exceptions=False)
    c.recorded = recorded
    return c


def test_opening_the_link_changes_nothing(client):
    """Link scanners and previewers fetch every URL in an email."""
    token = ce.make_token(TENANT, ADDRESS)
    r = client.get(f"/api/public/email/unsubscribe/{token}")
    assert r.status_code == 200
    assert client.recorded == []
    assert "Lockwood &lt;Realty&gt;" in r.text and "<Realty>" not in r.text
    assert 'method="post"' in r.text and f'action="/api/public/email/unsubscribe/{token}"' in r.text
    assert ADDRESS.lower() not in r.text.lower()
    csp = r.headers["content-security-policy"]
    assert "form-action 'self'" in csp and "script-src" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["cache-control"] == "no-store"


def test_one_click_post_unsubscribes_without_login_or_csrf_token(client):
    token = ce.make_token(TENANT, ADDRESS)
    r = client.post(f"/api/public/email/unsubscribe/{token}", content="List-Unsubscribe=One-Click",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200, r.text
    assert client.recorded == [(TENANT, ADDRESS.lower())]
    again = client.post(f"/api/public/email/unsubscribe/{token}", headers={"Accept": "application/json"})
    assert again.status_code == 200 and again.json() == {"unsubscribed": True, "channel": "email"}


def test_a_forged_token_is_not_found_and_records_nothing(client):
    for method in ("get", "post"):
        r = getattr(client, method)("/api/public/email/unsubscribe/u1" + "A" * 80,
                                    headers={"Accept": "application/json"})
        assert r.status_code == 404 and r.json()["valid"] is False
    assert client.recorded == []


def test_a_failed_write_is_a_retryable_503_never_a_false_success(client, monkeypatch):
    async def down(tenant_id, address):
        raise ConnectionError("db down")

    monkeypatch.setattr(ce, "record_unsubscribe", down)
    r = client.post(f"/api/public/email/unsubscribe/{ce.make_token(TENANT, ADDRESS)}")
    assert r.status_code == 503
    assert "You are unsubscribed" not in r.text


def test_the_csrf_exemption_covers_only_the_tokenized_route():
    from csrf_middleware import CSRFMiddleware

    mw = CSRFMiddleware(app=None)
    assert mw._is_exempt("/api/public/email/unsubscribe/u1abc")
    assert not mw._is_exempt("/api/public/email/other")
    assert not mw._is_exempt("/api/public/email/unsubscribe")


def test_the_route_has_its_own_rate_limit_bucket_and_masked_logs():
    import rate_limit_middleware as rl
    from log_redaction import redact_path

    path = "/api/public/email/unsubscribe/u1secret"
    assert rl._get_bucket_for_path(path) == "/api/public/email/unsubscribe/"
    assert rl._get_limit_for_path(path) == 120
    assert "u1secret" not in redact_path(path)


def test_the_server_mounts_the_route():
    import server

    paths = {getattr(r, "path", "") for r in server.app.routes}
    assert "/api/public/email/unsubscribe/{token}" in paths


def test_record_unsubscribe_writes_the_brokerage_wide_email_suppression(monkeypatch):
    import outreach_compliance

    seen = {}

    async def suppress(ctx, **kw):
        seen.update(kw, tenant=ctx.tenant_id, role=ctx.role.value)

    monkeypatch.setattr(outreach_compliance.ConsentLedger, "suppress", staticmethod(suppress))
    asyncio.run(ce.record_unsubscribe(TENANT, ADDRESS))
    assert seen == {"contact": ADDRESS.lower(), "channel": "email", "reason": "stop_keyword",
                    "source_text": "email unsubscribe link", "tenant": TENANT, "role": "platform_admin"}


def test_the_tenant_id_round_trips_as_a_uuid():
    tid = str(uuid.uuid4())
    assert ce.read_token(ce.make_token(tid, "a@b.co"))[0] == tid


def test_no_postal_address_holds_commercial_email(monkeypatch):
    # Owner 2026-10-14: no postal address, no client email (CAN-SPAM). The
    # sender refuses before any provider call; system mail is not affected.
    import asyncio

    import commercial_email

    async def no_address(_tenant_id):
        return {"name": "Northstar", "postal_address": None}

    monkeypatch.setattr(commercial_email, "_sender", no_address)
    with pytest.raises(commercial_email.PostalAddressRequired) as held:
        asyncio.run(commercial_email.prepare("00000000-0000-0000-0000-0000000000a1",
                                             recipient="x@example.test", body="Hi"))
    assert "postal mailing address" in str(held.value)
    with pytest.raises(commercial_email.PostalAddressRequired):
        asyncio.run(commercial_email.require_postal_address("00000000-0000-0000-0000-0000000000a1"))

    async def with_address(_tenant_id):
        return {"name": "Northstar", "postal_address": "1 Main St, Dover, DE 19901"}

    monkeypatch.setattr(commercial_email, "_sender", with_address)
    asyncio.run(commercial_email.require_postal_address("00000000-0000-0000-0000-0000000000a1"))
    prepared = asyncio.run(commercial_email.prepare("00000000-0000-0000-0000-0000000000a1",
                                                    recipient="x@example.test", body="Hi"))
    assert "1 Main St" in prepared.body
