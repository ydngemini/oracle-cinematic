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
}
# Wrapping a value in one of these logs a fact about it, not the value.
_SAFE_WRAPPERS = {"len", "bool", "type", "id"}


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
