"""Message bodies, call transcript excerpts and chat turns are sealed at rest,
and every reader opens both shapes (audit A01, ledger L17-21). No database
here; tests/test_sealed_text_live.py proves the same against real PostgreSQL.
"""

from __future__ import annotations

import ast
import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import sealed_text
from crypto import derive_tenant_key

BACKEND = Path(__file__).resolve().parents[1]
TENANT = "00000000-0000-0000-0000-0000000000a1"


class _NoDb:
    async def fetchval(self, *_a):
        raise AssertionError("plaintext rows need no database round trip")


@pytest.mark.parametrize("field", sealed_text.FIELDS, ids=lambda f: f.table)
def test_plaintext_rows_read_without_a_key(monkeypatch, field):
    monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY", raising=False)
    rows = [{"id": 1, field.plain: "old text", field.cipher: None, field.tenant_alias: TENANT}]
    assert asyncio.run(sealed_text.open_rows(_NoDb(), rows, field)) == [{"id": 1, field.plain: "old text"}]


def test_sealed_rows_open_with_their_own_tenants_key(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-master")
    other = "00000000-0000-0000-0000-0000000000b2"
    field = sealed_text.SMS_BODY
    calls = []

    class _Conn:
        async def fetchval(self, query, ciphertexts, key):
            calls.append((ciphertexts, key))
            assert "pgp_sym_decrypt" in query
            return [c.decode().upper() for c in ciphertexts]

    rows = [
        {"id": 1, "body": None, "body_ciphertext": b"a", "body_ciphertext_tenant_id": TENANT},
        {"id": 2, "body": "plain", "body_ciphertext": None, "body_ciphertext_tenant_id": TENANT},
        {"id": 3, "body": None, "body_ciphertext": b"b", "body_ciphertext_tenant_id": other},
        {"id": 4, "body": None, "body_ciphertext": b"c", "body_ciphertext_tenant_id": TENANT},
    ]
    out = asyncio.run(sealed_text.open_rows(_Conn(), rows, field))
    assert [r["body"] for r in out] == ["A", "plain", "B", "C"]
    assert all(set(r) == {"id", "body"} for r in out)
    assert sorted(calls) == sorted([
        ([b"a", b"c"], derive_tenant_key(TENANT, "unit-master")),
        ([b"b"], derive_tenant_key(other, "unit-master")),
    ]), "one round trip per tenant, each under that tenant's key"


def test_the_key_derivation_is_crypto_py_unchanged_and_case_stable(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-master")
    expected = derive_tenant_key(TENANT, "unit-master")
    assert sealed_text.key_for(TENANT) == expected
    assert sealed_text.key_for(TENANT.upper()) == expected, "user_interactions.tenant_id is text"
    import uuid

    assert sealed_text.key_for(uuid.UUID(TENANT)) == expected


def test_a_sealed_row_without_the_key_fails_closed(monkeypatch):
    monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY", raising=False)
    field = sealed_text.TRANSCRIPT
    rows = [{"id": 1, field.plain: None, field.cipher: b"x", field.tenant_alias: TENANT}]
    with pytest.raises(sealed_text.SealError):
        asyncio.run(sealed_text.open_rows(_NoDb(), rows, field))
    with pytest.raises(sealed_text.SealError):
        sealed_text.key_for(TENANT)


def test_a_decrypt_failure_names_no_content(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-master")

    class _Conn:
        async def fetchval(self, *_a):
            raise ValueError("Wrong key or corrupt data: secret fragment")

    field = sealed_text.INTERACTION
    rows = [{"id": 1, "content": "", "content_ciphertext": b"x", "content_ciphertext_tenant_id": TENANT}]
    with pytest.raises(sealed_text.SealError) as err:
        asyncio.run(sealed_text.open_rows(_Conn(), rows, field))
    assert "secret" not in str(err.value)


def _sql_literals(tree: ast.AST):
    """Every string literal or f-string, with {name} for each interpolation."""
    parts = {id(p) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for p in n.values}
    for node in ast.walk(tree):
        if id(node) in parts:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            yield node.lineno, "".join(
                part.value if isinstance(part, ast.Constant) else "{" + ast.unparse(part.value) + "}"
                for part in node.values)


@pytest.mark.parametrize("field", sealed_text.FIELDS, ids=lambda f: f.table)
def test_every_app_statement_on_the_text_uses_the_sealed_shape(field):
    """A reader that forgets the cipher column shows a sealed message as empty;
    a writer that forgets it writes plaintext. Every SQL literal in the app that
    names the table and its text column must name the cipher column too."""
    offenders, seen = [], 0
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] in {"tests", "venv", ".venv", "scripts"} or rel.name in {
                "sealed_text.py", "backfill_sealed_text.py"}:
            continue
        for lineno, text in _sql_literals(ast.parse(path.read_text())):
            if not re.search(rf"\b(FROM|INTO|UPDATE|JOIN)\s+{field.table}\b", text, re.I):
                continue
            if not re.search(rf"\b{field.plain}\b", text):
                continue  # ids, status, counts, deletes
            seen += 1
            if field.cipher not in text:
                offenders.append(f"{rel}:{lineno}")
    assert seen, f"the guard matched no statement on {field.table}; the pattern is stale"
    assert not offenders, f"{field.table}.{field.plain} without {field.cipher}: " + ", ".join(offenders)


def test_the_brokerage_export_decrypts_every_sealed_column():
    import privacy_export

    for field in sealed_text.FIELDS:
        assert field.cipher in privacy_export.DECRYPT.get(field.table, {}), field.table


def _capture_conn(seen):
    class _Conn:
        async def fetchrow(self, query, *args):
            seen.append((query, args))
            return None

        async def fetchval(self, query, *args):
            seen.append((query, args))
            return None

        async def execute(self, query, *args):
            seen.append((query, args))
            return "INSERT 0 1"

        async def fetch(self, query, *args):
            seen.append((query, args))
            return []

    return _Conn()


def test_outbound_sms_stores_no_plaintext(monkeypatch):
    import messaging_data

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-master")
    seen = []

    @asynccontextmanager
    async def tx(_ctx):
        yield _capture_conn(seen)

    monkeypatch.setattr(messaging_data, "tenant_tx", tx)
    asyncio.run(messaging_data.record_outbound_message(
        tenant_id=TENANT, agent_id="agent-a", contact_id=None, client_id=None, provider="telnyx",
        provider_message_id="m-1", from_e164="+15550000001", to_e164="+15550000002",
        body="Gate code is 4411"))
    query, args = next((q, a) for q, a in seen if "INSERT INTO sms_messages" in q)
    assert "NULL,pgp_sym_encrypt(NULLIF($9::text,''),$10::text)" in query
    assert args[-1] == derive_tenant_key(TENANT, "unit-master")


def test_inbound_sms_stores_no_plaintext(monkeypatch):
    import messaging_data

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-master")
    seen = []

    @asynccontextmanager
    async def tx(_ctx):
        yield _capture_conn(seen)

    async def no_match(*_a, **_kw):
        return None, None

    monkeypatch.setattr(messaging_data, "tenant_tx", tx)
    monkeypatch.setattr(messaging_data, "_match_contact", no_match)
    asyncio.run(messaging_data.record_inbound_message(
        tenant_id=TENANT, agent_id="agent-a", provider="telnyx", provider_message_id="m-2",
        from_e164="+15550000002", to_e164="+15550000001", text="Call me after five",
        media=[], opted_out=False))
    query, args = next((q, a) for q, a in seen if "INSERT INTO sms_messages" in q)
    assert "$7,$8,NULL,pgp_sym_encrypt(NULLIF($9::text,''),$12::text)" in query
    assert args[-1] == derive_tenant_key(TENANT, "unit-master")


def test_a_chat_turn_stores_no_plaintext(monkeypatch):
    from db import connection
    from memory_core.session_manager import SessionManager
    from tenancy import Role, TenantContext

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-master")
    seen = []

    @asynccontextmanager
    async def tx(_ctx):
        yield _capture_conn(seen)

    monkeypatch.setattr(connection, "tenant_tx", tx)
    ctx = TenantContext(agent_id="agent-a", tenant_id=TENANT, role=Role.AGENT)
    asyncio.run(SessionManager(ctx).record_interaction("agent-a", "user", "Seller is going through probate"))
    query, args = seen[-1]
    assert "VALUES ($1, $2, $3, '', pgp_sym_encrypt(NULLIF($4::text, ''), $6::text), $5)" in query
    assert args[-1] == derive_tenant_key(TENANT, "unit-master")


def test_a_write_without_the_master_key_fails_closed(monkeypatch):
    import messaging_data

    monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY", raising=False)
    seen = []

    @asynccontextmanager
    async def tx(_ctx):
        yield _capture_conn(seen)

    monkeypatch.setattr(messaging_data, "tenant_tx", tx)
    with pytest.raises(sealed_text.SealError):
        asyncio.run(messaging_data.record_outbound_message(
            tenant_id=TENANT, agent_id="agent-a", contact_id=None, client_id=None, provider="telnyx",
            provider_message_id="m-3", from_e164="+15550000001", to_e164="+15550000002", body="x"))
    assert not any("INSERT INTO sms_messages" in q for q, _ in seen), "plaintext written without a key"


def test_the_migration_is_expand_only():
    """0152 must leave the previous release working: no constraint, trigger or
    NOT NULL that refuses the plaintext shape it still writes (CONTRACT step
    is a later migration, L17-21)."""
    sql = re.sub(r"--[^\n]*", "", (BACKEND / "db/migrations/0152_sealed_communication_text.sql").read_text())
    assert "CREATE TRIGGER" not in sql.upper()
    assert "SET NOT NULL" not in sql.upper()
    assert "DROP COLUMN" not in sql.upper()
    for check in re.findall(r"CHECK\s*\((.*?)\);", sql, re.S):
        assert re.match(r"\s*\w+_ciphertext IS NULL OR", check), check
