"""Logs carry ids, never a customer's name, email, phone or message (audit A06,
CONTRACT C5.4, prompt 17 T17.14 / AC17.05).

Two layers:
- a source scan: no logging call in backend/ passes a value whose name says it
  is contact PII or communication content (a `len()`/`bool()` of one is fine);
- failure-path tests: the paths that used to log a name or a number now log
  only ids, checked on the captured log text.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
# Identifiers that hold a customer's identity or words. Matched exactly against
# variable names, attribute names and string subscript keys.
_PII_NAMES = {
    "full_name", "owner_name", "first_name", "last_name", "client_name", "contact_name",
    "seller_name", "buyer_name", "entity_name",
    "email", "to_email", "from_email", "recipient_email",
    "phone", "phone_number", "from_number", "to_number", "caller_phone", "callee_phone",
    "contact", "note", "notes", "transcript", "speech_text",
    # A property address identifies a homeowner as surely as a name (A06, owner 2026-10-14).
    "address", "street", "street_address", "address_line", "full_address", "addr", "situs_address",
    # agent_id IS the user's email (docs/privacy-data-map.md §2): log
    # log_redaction.actor_ref(agent_id) instead (L17-11, 2026-10-14).
    "agent_id", "agent_identity", "current_agent_id",
}
# Wrapping a value in one of these logs a fact about it, not the value.
# actor_ref() is a keyed pseudonym of an account id.
_SAFE_WRAPPERS = {"len", "bool", "type", "id", "actor_ref"}
_ACCOUNT_NAMES = {"agent_id", "agent_identity", "current_agent_id"}
# crypto.py logs ctx.agent_id at three sites (pii_context, access_pii_field).
# A parallel slice (platform-admin read logging) owns crypto.py on 2026-10-14,
# so those sites move to actor_ref() at that merge. Account ids only: any other
# PII name in crypto.py still fails. Remove this entry once they are fixed.
_ACCOUNT_ID_DEFERRED = {"crypto.py"}


def _pii_in(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _SAFE_WRAPPERS:
        return []
    # A field access is judged by the field: contact["id"] and lead.id are ids,
    # body.contact and row["full_name"] are not.
    if isinstance(node, ast.Attribute):
        return [node.attr] if node.attr in _PII_NAMES else []
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
            and isinstance(node.slice.value, str):
        return [node.slice.value] if node.slice.value in _PII_NAMES else []
    found = []
    if isinstance(node, ast.Name) and node.id in _PII_NAMES:
        found.append(node.id)
    for child in ast.iter_child_nodes(node):
        found.extend(_pii_in(child))
    return found


def _logging_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LOG_METHODS
                and "log" in ast.unparse(node.func.value).lower()):
            yield node


def test_no_logging_call_interpolates_customer_pii():
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] in {"tests", "venv", ".venv"}:
            continue
        tree = ast.parse(path.read_text(), filename=str(rel))
        for call in _logging_calls(tree):
            message = call.args[:1] if call.args and not isinstance(call.args[0], ast.Constant) else []
            names = [n for arg in [*message, *call.args[1:], *(k.value for k in call.keywords)]
                     for n in _pii_in(arg)]
            if str(rel) in _ACCOUNT_ID_DEFERRED:
                names = [n for n in names if n not in _ACCOUNT_NAMES]
            if names:
                offenders.append(f"{rel}:{call.lineno} logs {sorted(set(names))}")
    assert not offenders, (
        "Log an id or an outcome code instead of contact PII / message content "
        "(CONTRACT C5.4):\n  " + "\n  ".join(offenders))


def test_the_scan_catches_what_it_is_for():
    """Guard the guard: the pre-fix A06 line must trip it, a len() must not."""
    bad = ast.parse('logger.warning("lookup skipped for %r", full_name)')
    ok = ast.parse('logger.info("notes=%d client=%s", len(notes), contact["id"])')
    assert [n for c in _logging_calls(bad) for a in c.args[1:] for n in _pii_in(a)] == ["full_name"]
    assert [n for c in _logging_calls(ok) for a in c.args[1:] for n in _pii_in(a)] == []


# ── failure paths ────────────────────────────────────────────────────────

def test_failed_property_lookup_logs_the_client_id_not_the_name(caplog):
    import client_ai_automation

    class _TimeoutConn:
        @asynccontextmanager
        async def transaction(self):
            yield self

        async def execute(self, *_a):
            return None

        async def fetch(self, *_a, **_kw):
            raise TimeoutError()

    with caplog.at_level(logging.WARNING, logger="oracle.client_ai"):
        result, status = asyncio.run(client_ai_automation._property_candidates_bounded(
            _TimeoutConn(), "Margaret Customer-Example", [], 0, client_id="client-1234"))
    assert result == [] and status["status"] == "timed_out"  # a failure, never a verified zero (A02)
    assert "client-1234" in caplog.text and "TimeoutError" in caplog.text
    assert "Margaret" not in caplog.text and "Customer-Example" not in caplog.text


def test_consent_and_opt_out_logs_carry_no_contact(caplog, monkeypatch):
    import outreach_compliance as oc
    from tenancy import Role, TenantContext

    async def _record(*_a, **_kw):
        return "consent-1"

    async def _suppress(*_a, **_kw):
        return None

    monkeypatch.setattr(oc.ConsentLedger, "record_consent", _record)
    monkeypatch.setattr(oc.ConsentLedger, "suppress", _suppress)
    ctx = TenantContext(agent_id="agent-a", tenant_id="00000000-0000-0000-0000-0000000000aa",
                        role=Role.AGENT)
    with caplog.at_level(logging.INFO):
        asyncio.run(oc.record_consent(oc.ConsentRecord(
            contact="+1 (302) 555-0142", channel="sms", consent_type="express_written"), ctx))
        asyncio.run(oc.record_opt_out(oc.OptOutRecord(
            contact="Jane.Customer@example.test", channel="*", reason="manual_dnc"), ctx))
        asyncio.run(oc.handle_inbound(oc.InboundMessage(
            contact="+13025550142", channel="sms", body="STOP"), ctx))
    assert "Consent recorded" in caplog.text and "STOP honoured" in caplog.text
    for leaked in ("555", "0142", "jane.customer", "example.test"):
        assert leaked not in caplog.text.lower(), leaked


def test_failed_incoming_call_logs_no_caller_number(caplog, monkeypatch):
    import sys
    import types

    import commands_api

    async def _boom(_context, _from_number):
        raise RuntimeError("handler exploded")

    fake = types.ModuleType("acs_call_handler")
    fake.answer_incoming_call = _boom
    monkeypatch.setitem(sys.modules, "acs_call_handler", fake)
    with caplog.at_level(logging.ERROR, logger="oracle.acs_webhook"):
        asyncio.run(commands_api._handle_incoming_acs_call("ctx-1", "+13025550199"))
    assert "Incoming call handler failed" in caplog.text
    assert "3025550199" not in caplog.text


# ── account ids and exception text (L17-11) ─────────────────────────────

def test_account_ids_are_logged_as_a_pseudonym():
    from log_redaction import actor_ref

    ref = actor_ref("Jane.Agent@Example.test")
    assert ref.startswith("actor:") and "jane" not in ref.lower()
    assert ref == actor_ref("jane.agent@example.test ")  # one account, one reference
    assert ref != actor_ref("someone.else@example.test")
    assert actor_ref(None) == "actor:-"
    bad = ast.parse('log.info("login for %r", ctx.agent_id)')
    ok = ast.parse('log.info("login for %s", actor_ref(ctx.agent_id))')
    assert [n for c in _logging_calls(bad) for a in c.args[1:] for n in _pii_in(a)] == ["agent_id"]
    assert [n for c in _logging_calls(ok) for a in c.args[1:] for n in _pii_in(a)] == []


def _scrubbed_logger():
    import io

    from log_redaction import install_pii_scrub

    stream = io.StringIO()
    logger = logging.getLogger("test.pii_scrub")
    logger.handlers[:] = [logging.StreamHandler(stream)]
    logger.propagate = False
    install_pii_scrub(logger)
    return logger, stream


# str() of the asyncpg errors the local PostgreSQL raised on 2026-10-14 for a
# unique and a check violation (L17-11 evidence), with a phone and an
# address added to the refused row.
_UNIQUE = ('duplicate key value violates unique constraint "t_email_key"\n'
           'DETAIL:  Key (email)=(jane@example.test) already exists.')
_CHECK = ('new row for relation "t" violates check constraint "t_phone_check"\n'
          'DETAIL:  Failing row contains (bob@example.test, +13025550142, 12 Elm St (rear)).')


def test_database_error_text_is_scrubbed_in_messages_and_tracebacks():
    import asyncpg.exceptions as pg

    logger, stream = _scrubbed_logger()
    logger.warning("insert failed: %s", pg.UniqueViolationError(_UNIQUE))
    try:
        raise pg.CheckViolationError(_CHECK)
    except pg.CheckViolationError:
        logger.exception("write failed")
    logger.error("callback from %s failed for %s", "+13025550199", "(302) 555-0142")
    out = stream.getvalue()
    for leaked in ("jane@", "bob@", "example.test", "3025550142", "3025550199", "555-0142", "Elm St"):
        assert leaked not in out, leaked
    assert "Key (email)=(:redacted)" in out and "Failing row contains (:redacted)" in out
    assert "insert failed: duplicate key value violates unique constraint" in out
    assert "Traceback" in out and "CheckViolationError" in out


def test_scrub_keeps_ids_times_and_amounts():
    from log_redaction import scrub_text

    line = ("job 3fa85f64-5717-4562-b3fc-2c963f66afa6 at 2026-10-14T12:00:00+00:00 "
            "took 1234.5 ms; 10.0.0.1:5432 status=503 count=4155551234x")
    assert scrub_text(line) == line


def test_server_installs_the_scrub_on_its_handlers():
    source = (BACKEND / "server.py").read_text()
    body = source.split("def _configure_logging", 1)[1].split("\ndef ", 1)[0]
    assert "install_pii_scrub()" in body and "install_access_log_redaction()" in body


def test_websocket_handshake_lines_drop_the_query_string():
    """uvicorn logs WebSocket handshakes on uvicorn.error with the path SECOND
    ('%s - "WebSocket %s" [accepted]'); the first filter only rewrote the
    third argument on uvicorn.access, so ?bridge_token= reached the log."""
    from log_redaction import AccessLogRedactionFilter, install_access_log_redaction

    install_access_log_redaction()
    assert any(isinstance(f, AccessLogRedactionFilter) for f in logging.getLogger("uvicorn.error").filters)
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, '%s - "WebSocket %s" [accepted]',
                               ("203.0.113.9:5000", "/api/commands/media/plivo?bridge_token=SECRET"), None)
    AccessLogRedactionFilter().filter(record)
    assert "SECRET" not in record.getMessage()
    access = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("203.0.113.9:5000", "POST", "/api/commands/webhooks/acs?token=S", "1.1", 200),
                               None)
    AccessLogRedactionFilter().filter(access)
    assert "token=S" not in access.getMessage()
