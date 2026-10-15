"""Message bodies, call transcript excerpts and chat turns are sealed at rest,
and every reader opens both shapes (audit A01, ledger L17-21). No database
here; tests/test_sealed_text_live.py proves the same against real PostgreSQL.
"""

from __future__ import annotations

import ast
import asyncio
import json
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


_NOT_ALIAS = r"(?!(?:WHERE|ON|JOIN|LEFT|RIGHT|INNER|ORDER|GROUP|LIMIT|SET|USING|VALUES|SELECT|RETURNING)\b)"


def _names_text_column(text: str, field) -> bool:
    """The statement names the field's column on ITS table: unqualified, or
    qualified by the table or one of its aliases. `ld.payload` in a statement
    that also reads interaction_logs is a leads column, not the sealed one."""
    aliases = {a.lower() for a in re.findall(
        rf"\b{field.table}\s+(?:AS\s+)?{_NOT_ALIAS}(\w+)", text, re.I)}
    aliases.add(field.table)
    for m in re.finditer(rf"(?:\b(\w+)\.)?\b{field.plain}\b", text):
        if m.group(1) is None or m.group(1).lower() in aliases:
            return True
    return False


def test_the_guard_tells_the_tables_columns_apart():
    log = sealed_text.LOG_CONTENT
    assert _names_text_column("SELECT il.payload FROM interaction_logs il", log)
    assert _names_text_column("INSERT INTO interaction_logs (tenant_id, payload) VALUES ($1,$2)", log)
    assert not _names_text_column(
        "SELECT l.payload, i.created_at FROM leads l JOIN interaction_logs i ON i.lead_id = l.id", log)
    assert _names_text_column("SELECT body FROM sms_messages WHERE id=$1", sealed_text.SMS_BODY)


@pytest.mark.parametrize("field", sealed_text.FIELDS, ids=lambda f: f"{f.table}.{f.plain}")
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
            if not _names_text_column(text, field):
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
    assert "NULL,pgp_sym_encrypt(NULLIF($9::text,''),$10::text,'s2k-mode=1')" in query
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
    assert "$7,$8,NULL,pgp_sym_encrypt(NULLIF($9::text,''),$12::text,'s2k-mode=1')" in query
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
    assert "VALUES ($1, $2, $3, '', pgp_sym_encrypt(NULLIF($4::text, ''), $6::text, 's2k-mode=1'), $5)" in query
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


def test_every_app_pgp_sym_encrypt_uses_single_pass_s2k():
    # Keys are 32-byte HKDF outputs, not passwords: the iterated default S2K
    # cost ~0.5 ms per row each way for no strength (0153). A new encrypt
    # site must pass 's2k-mode=1' too. Docstrings/comments are ignored.
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(("tests/", "venv/", "ml_forge/", "scripts/")):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"pgp_sym_encrypt\(\$|pgp_sym_encrypt\((NULLIF|body|content|transcript)", code) \
                    and "s2k-mode=1" not in code:
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, offenders


# ── slice 2 (0154, L17-22) ──────────────────────────────────────────────────

def test_split_json_moves_only_the_content_keys():
    stored, sealed = sealed_text.split_json(
        {"body": "Gate code 4411", "transcript": "roof", "delivery_status": "not_sent", "listing_id": "L1"},
        sealed_text.LOG_CONTENT)
    assert stored == {"delivery_status": "not_sent", "listing_id": "L1"}
    assert json.loads(sealed) == {"body": "Gate code 4411", "transcript": "roof"}
    assert sealed_text.split_json({"asset": "a1"}, sealed_text.LOG_CONTENT) == ({"asset": "a1"}, None)


@pytest.mark.parametrize("stored", ['{"delivery_status": "not_sent"}', {"delivery_status": "not_sent"}])
def test_opening_a_sealed_payload_merges_it_back_in_the_same_shape(monkeypatch, stored):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-master")
    field = sealed_text.LOG_CONTENT

    class _Conn:
        async def fetchval(self, _query, ciphertexts, _key):
            return ['{"body": "hello"}' for _ in ciphertexts]

    rows = [{"id": 1, "payload": stored, field.cipher: b"x", field.tenant_alias: TENANT},
            {"id": 2, "payload": '{"body": "old"}', field.cipher: None, field.tenant_alias: TENANT}]
    out = asyncio.run(sealed_text.open_rows(_Conn(), rows, field))
    merged = out[0]["payload"]
    assert type(merged) is type(stored)
    assert (json.loads(merged) if isinstance(merged, str) else merged) == {
        "delivery_status": "not_sent", "body": "hello"}
    assert out[1]["payload"] == '{"body": "old"}'


def test_nothing_to_seal_needs_no_key_and_text_without_a_key_fails_closed(monkeypatch):
    monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY", raising=False)
    assert sealed_text.key_if_needed(TENANT, None, "") is None
    with pytest.raises(sealed_text.SealError):
        sealed_text.key_if_needed(TENANT, None, "Offer follow-up")
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-master")
    assert sealed_text.key_if_needed(TENANT, "x") == derive_tenant_key(TENANT, "unit-master")


def test_the_content_keys_agree_everywhere():
    """The JSON keys sealed out of interaction_logs.payload are written in
    four places: sealed_text, the 0154 CHECK and storage move, and the
    backfill's counts. A key added to one and not the others would leave text
    behind (or fail the CHECK)."""
    import backfill_sealed_text as backfill

    keys = "ARRAY[" + ",".join(f"'{k}'" for k in sealed_text.LOG_CONTENT.json_keys) + "]"
    sql = (BACKEND / "db/migrations/0154_sealed_text_slice_two.sql").read_text()
    assert sql.count(keys) == 2, "CHECK + seal_interaction_logs"
    for table in (backfill._SEAL_TENANTS, backfill._REMAINING):
        assert keys in table["interaction_log"]


def test_slice_two_is_expand_only():
    sql = re.sub(r"--[^\n]*", "", (BACKEND / "db/migrations/0154_sealed_text_slice_two.sql").read_text())
    assert "CREATE TRIGGER" not in sql.upper()
    assert "SET NOT NULL" not in sql.upper()
    assert "DROP COLUMN" not in sql.upper()
    checks = re.findall(r"CHECK\s*\((.*?)\);", sql, re.S)
    assert len(checks) == 7
    for check in checks:
        assert re.match(r"\s*\w+_ciphertext IS NULL\s+OR", check), check
    # The storage move cannot widen access: own tenant's platform context only.
    assert "app_is_platform_admin() OR p_tenant IS DISTINCT FROM app_current_tenant()" in sql
    assert "GRANT EXECUTE ON FUNCTION seal_interaction_logs(uuid, text, integer, boolean) TO oracle_app" in sql


def test_every_slice_two_field_is_backfilled():
    import backfill_sealed_text as backfill

    tables = {f.table for f in sealed_text.FIELDS}
    covered = " ".join(list(backfill._REMAINING.values()))
    assert all(t in covered for t in tables), tables
    assert set(backfill.FIELDS) == set(backfill._SEAL_TENANTS) == set(backfill._UNSEAL_TENANTS)


def test_a_belief_read_without_opening_its_quote_fails_loudly():
    import belief_store
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    row = {"id": "b1", "object_value": '"x"', "pinned_at": None, "confidence": 0.5, "predicate": "p",
           "learned_at": now, "valid_until": None, "subject_type": "client", "subject_id": "c",
           "status": "reported", "source_kind": "sms", "source_ref": None, "source_quote": None,
           "recorded_at": now, "revision_state": "active", "source_quote_ciphertext": b"sealed"}
    with pytest.raises(sealed_text.SealError):
        belief_store._row_to_belief(row, now)
    row.pop("source_quote_ciphertext")
    assert belief_store._row_to_belief(row, now)["source"]["quote"] is None


def test_the_tool_receipt_is_attached_only_sealed(monkeypatch):
    import ai_chat_store
    from tenancy import Role, TenantContext

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-master")
    seen = []

    @asynccontextmanager
    async def tx(_ctx):
        yield _capture_conn(seen)

    monkeypatch.setattr(ai_chat_store, "tenant_tx", tx)
    ctx = TenantContext(agent_id="agent-a", tenant_id=TENANT, role=Role.AGENT)
    asyncio.run(ai_chat_store._ledger_attach_result(ctx, "00000000-0000-0000-0000-0000000000c3", 0,
                                                    {"ok": True, "phone": "+15550001234"}))
    query, args = seen[-1]
    assert "result_ciphertext=pgp_sym_encrypt($1::text, $5::text, 's2k-mode=1')" in query
    assert "result=NULL" in query and args[-1] == derive_tenant_key(TENANT, "unit-master")
