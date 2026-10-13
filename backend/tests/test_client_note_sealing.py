"""Client notes are sealed at rest and every reader opens both shapes (audit
A01, ledger L17-03). No database here; tests/test_client_note_sealing_live.py
and tests/test_call_notes_live.py prove the same against real PostgreSQL.
"""

from __future__ import annotations

import ast
import asyncio
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

import client_note_crypto
from crypto import derive_tenant_key

BACKEND = Path(__file__).resolve().parents[1]
TENANT = "00000000-0000-0000-0000-0000000000a1"


class _NoDb:
    async def fetchval(self, *_a):
        raise AssertionError("plaintext rows need no database round trip")


def test_plaintext_rows_read_without_a_key(monkeypatch):
    monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY", raising=False)
    rows = [{"id": 1, "body": "old note", "body_ciphertext": None, "body_tenant_id": TENANT}]
    assert asyncio.run(client_note_crypto.open_rows(_NoDb(), rows)) == [{"id": 1, "body": "old note"}]


def test_sealed_rows_open_with_their_own_tenants_key(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-master")
    other = "00000000-0000-0000-0000-0000000000b2"
    calls = []

    class _Conn:
        async def fetchval(self, query, ciphertexts, key):
            calls.append((ciphertexts, key))
            assert "pgp_sym_decrypt" in query
            return [c.decode().upper() for c in ciphertexts]

    rows = [
        {"id": 1, "body": "", "body_ciphertext": b"a", "body_tenant_id": TENANT},
        {"id": 2, "body": "plain", "body_ciphertext": None, "body_tenant_id": TENANT},
        {"id": 3, "body": "", "body_ciphertext": b"b", "body_tenant_id": other},
        {"id": 4, "body": "", "body_ciphertext": b"c", "body_tenant_id": TENANT},
    ]
    out = asyncio.run(client_note_crypto.open_rows(_Conn(), rows))
    assert [r["body"] for r in out] == ["A", "plain", "B", "C"]
    assert all(set(r) == {"id", "body"} for r in out)
    assert sorted(calls) == sorted([
        ([b"a", b"c"], derive_tenant_key(TENANT, "unit-master")),
        ([b"b"], derive_tenant_key(other, "unit-master")),
    ]), "one round trip per tenant, each under that tenant's key"


def test_a_sealed_row_without_the_key_fails_closed(monkeypatch):
    monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY", raising=False)
    rows = [{"id": 1, "body": "", "body_ciphertext": b"x", "body_tenant_id": TENANT}]
    with pytest.raises(RuntimeError):
        asyncio.run(client_note_crypto.open_rows(_NoDb(), rows))


def test_a_decrypt_failure_names_no_content(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-master")

    class _Conn:
        async def fetchval(self, *_a):
            raise ValueError("Wrong key or corrupt data: secret fragment")

    rows = [{"id": 1, "body": "", "body_ciphertext": b"x", "body_tenant_id": TENANT}]
    with pytest.raises(client_note_crypto.NoteSealError) as err:
        asyncio.run(client_note_crypto.open_rows(_Conn(), rows))
    assert "secret" not in str(err.value)


def _sql_literals(tree: ast.AST):
    """Every string literal or f-string, with {name} for each interpolation."""
    parts = {id(p) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for p in n.values}
    for node in ast.walk(tree):
        if id(node) in parts:
            continue  # judged as part of its f-string
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            yield node.lineno, "".join(
                part.value if isinstance(part, ast.Constant) else "{" + ast.unparse(part.value) + "}"
                for part in node.values)


def test_every_app_statement_on_note_bodies_uses_the_sealed_shape():
    """F17.03: a reader that forgets SEALED_COLUMNS shows a sealed note as
    empty; a writer that forgets body_ciphertext writes plaintext."""
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] in {"tests", "venv", ".venv"} or rel.name == "client_note_crypto.py":
            continue
        for lineno, text in _sql_literals(ast.parse(path.read_text())):
            if not re.search(r"\b(FROM|INTO|UPDATE)\s+client_notes\b", text, re.I):
                continue
            if not re.search(r"\bbody\b", text):
                continue  # ids, review state, deletes
            if "body_ciphertext" not in text and "SEALED_COLUMNS" not in text and "_NOTE_SEALED" not in text:
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, "client_notes body read/write without the sealed shape: " + ", ".join(offenders)


def test_the_api_note_write_stores_no_plaintext(monkeypatch):
    import client_enterprise
    from tenancy import Role, TenantContext

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-master")
    client_id, note_id = str(uuid.uuid4()), uuid.uuid4()
    now = datetime.now(timezone.utc)
    seen = []

    class _Conn:
        async def fetchrow(self, query, *args):
            seen.append((query, args))
            if "pgp_sym_encrypt" in query:
                return {"ct": b"sealed:" + args[0].encode()}
            assert "INSERT INTO client_notes" in query
            return {"id": note_id, "body": "", "body_ciphertext": args[2], "body_tenant_id": args[0],
                    "author_id": args[3], "pinned": args[4], "created_at": now, "updated_at": now,
                    "review_state": "approved", "reviewed_by": None, "reviewed_at": None}

        async def fetchval(self, query, ciphertexts, _key):
            return [c.decode().removeprefix("sealed:") for c in ciphertexts]

        async def execute(self, *_a):
            return "INSERT 0 1"

    @asynccontextmanager
    async def tx(_ctx):
        yield _Conn()

    async def allowed(*_a):
        return None

    async def no_activity(*_a, **_kw):
        return None

    monkeypatch.setattr(client_enterprise, "tenant_tx", tx)
    monkeypatch.setattr(client_enterprise, "require_client_edit", allowed)
    monkeypatch.setattr(client_enterprise, "_log_activity", no_activity)
    monkeypatch.setattr(client_enterprise, "_queue_client_ai", no_activity)
    ctx = TenantContext(agent_id="agent-a", tenant_id=TENANT, role=Role.AGENT)
    out = asyncio.run(client_enterprise.create_note(
        client_id, client_enterprise.NoteCreate(body="Wants a quiet sale"), ctx=ctx))
    assert out["body"] == "Wants a quiet sale"
    insert_query, insert_args = seen[-1]
    assert "VALUES ($1, $2, '', $3" in insert_query
    assert "Wants a quiet sale" not in [a for a in insert_args if isinstance(a, str)]
    assert insert_args[2] == b"sealed:Wants a quiet sale"
