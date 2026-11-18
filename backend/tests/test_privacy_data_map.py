"""The data map is complete, the erasure order is sound, and the receipt is honest.

These run without a database. tests/test_privacy_lifecycle_live.py proves the
same flows against real PostgreSQL; tests/privacy_lifecycle.sql proves the
database guards.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import privacy_data_map as dm
import privacy_export
import privacy_lifecycle as pl
from retention_policy import RetentionCategory

MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"


def _migration_tables() -> set[str]:
    created, dropped = set(), set()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = re.sub(r"--[^\n]*", "", path.read_text())
        created |= {m.lower() for m in re.findall(
            r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?([a-z_0-9]+)", text, re.I)}
        dropped |= {m.lower() for m in re.findall(
            r"drop\s+table\s+(?:if\s+exists\s+)?(?:public\.)?([a-z_0-9]+)", text, re.I)}
    return created - {"if"}


def test_every_table_a_migration_creates_is_classified():
    """A new table must say what erasure does to it — or this fails."""
    missing = sorted(_migration_tables() - set(dm.TABLES))
    assert not missing, (
        "Classify these tables in privacy_data_map.TABLES (category + disposition):\n  "
        + "\n  ".join(missing))


def test_data_map_names_no_table_that_does_not_exist():
    # schema_migrations is created by run_migrations.py itself, not a migration.
    stale = sorted(set(dm.TABLES) - _migration_tables() - {"schema_migrations"})
    assert not stale, f"privacy_data_map names tables no migration creates: {stale}"


def test_sql_retained_list_matches_the_map():
    """privacy_retained_tables() in 0122 and NEVER_ERASED here must agree on
    every tenant-scoped retained table — a drift would let erasure delete
    evidence the policy says to keep."""
    sql = (MIGRATIONS / "0122_customer_data_lifecycle.sql").read_text()
    body = re.search(r"privacy_retained_tables\(\).*?ARRAY\[(.*?)\]", sql, re.S).group(1)
    in_sql = set(re.findall(r"'([a-z_]+)'", body))
    retained = {n for n, e in dm.TABLES.items()
                if e.disposition in (dm.Disposition.RETAIN, dm.Disposition.TENANT_ROW)}
    assert retained <= in_sql, f"retained in the map but erasable in SQL: {sorted(retained - in_sql)}"
    erasable = set(dm.erasable())
    assert not (in_sql & erasable), f"marked erasable but protected in SQL: {sorted(in_sql & erasable)}"


def test_evidence_the_policy_keeps_is_never_erased():
    for table in ("subscriptions", "billing_usage_events", "audit_ledger", "privacy_operations",
                  "erasure_ledger", "legal_holds", "suppression_tombstones", "tenants"):
        assert table in dm.NEVER_ERASED, table


def test_customer_content_is_erased():
    for table in ("clients", "agent_contacts", "leads", "sms_messages", "inbound_voice_calls",
                  "ai_chat_messages", "user_interactions", "client_notes", "users",
                  "provider_credentials", "property_media"):
        assert dm.entry(table).disposition is dm.Disposition.ERASE, table


def test_opt_outs_become_tombstones_not_deletions():
    assert dm.entry("outreach_suppression").disposition is dm.Disposition.TOMBSTONE
    assert dm.entry("suppression_tombstones").category is RetentionCategory.CONSENT_SUPPRESSION


# ── erasure_order ─────────────────────────────────────────────────────────

def test_children_are_deleted_before_their_parents():
    _, order = pl.erasure_order(
        ["transactions", "clients", "transaction_offers"],
        [("transactions", "clients", "client_id", True),
         ("transaction_offers", "transactions", "transaction_id", False)])
    assert order.index("transaction_offers") < order.index("transactions") < order.index("clients")


def test_a_reference_cycle_is_broken_through_a_nullable_column():
    to_null, order = pl.erasure_order(
        ["clients", "agent_contacts"],
        [("clients", "agent_contacts", "contact_id", True),
         ("agent_contacts", "clients", "legacy_client_id", True)])
    assert len(to_null) == 1 and to_null[0][1] in ("contact_id", "legacy_client_id")
    assert set(order) == {"clients", "agent_contacts"}


def test_the_real_smart_plan_cycle_resolves():
    to_null, order = pl.erasure_order(
        ["smart_plans", "smart_plan_revisions", "smart_plan_enrollments"],
        [("smart_plans", "smart_plan_revisions", "current_revision_id", True),
         ("smart_plan_revisions", "smart_plans", "plan_id", False),
         ("smart_plan_enrollments", "smart_plans", "plan_id", False),
         ("smart_plan_enrollments", "smart_plan_revisions", "revision_id", False)])
    assert ("smart_plans", "current_revision_id") in to_null
    assert order.index("smart_plan_enrollments") < order.index("smart_plan_revisions") < order.index("smart_plans")


def test_an_unbreakable_cycle_stops_rather_than_guesses():
    with pytest.raises(RuntimeError, match="unbreakable"):
        pl.erasure_order(["a", "b"], [("a", "b", "b_id", False), ("b", "a", "a_id", False)])


def test_self_references_do_not_block():
    _, order = pl.erasure_order(["beliefs"], [("beliefs", "beliefs", "superseded_by", True)])
    assert order == ["beliefs"]


# ── receipts & exports ────────────────────────────────────────────────────

def test_receipt_reports_backups_and_limits_honestly():
    progress = {
        "rows": {"deleted": {"clients": 3}}, "objects": {"deleted": 2, "vault_deleted": 1},
        "tombstones": {"tombstoned": 1}, "providers": {"steps": [{"provider": "twilio", "status": "released"}]},
        "verify": {"clean": True, "rows_left": {}}, "tenant_row": {"at": "2026-10-01T00:00:00+00:00"},
    }
    receipt = pl.build_receipt("op", "t", progress)
    assert receipt["rows_deleted"] == {"clients": 3}
    assert receipt["objects_deleted"] == 3
    assert "2026-10-08" in receipt["backups"]          # 7-day backup window stated, with a date
    assert "re-applied" in receipt["backups"]
    assert any("Google Calendar" in item for item in receipt["not_reachable_by_neoh"])
    assert len(receipt["sha256"]) == 64
    assert "billing_records" in receipt["retained"]


def test_receipt_never_contains_customer_content():
    progress = {"rows": {"deleted": {"clients": 1}}, "verify": {"clean": True}}
    text = repr(pl.build_receipt("op", "t", progress))
    assert "@" not in text.replace("noreply", "")


def test_export_excludes_secrets_and_tokens():
    tables = set(privacy_export.exported_tables())
    for secret in ("provider_credentials", "lead_source_connectors", "oauth_authorization_states",
                   "password_reset_tokens", "client_portals", "brokerage_invitations",
                   "property_view_upload_links"):
        assert secret not in tables, secret
    for kept in ("clients", "agent_contacts", "sms_messages", "ai_chat_messages", "subscriptions"):
        assert kept in tables, kept


@pytest.mark.parametrize("column,dtype,omitted", [
    ("password_hash", "text", True), ("token_hash", "text", True), ("email_lookup_hash", "text", True),
    ("name_search_tokens", "text[]", True), ("audio", "bytea", True), ("payload_digest", "text", True),
    ("full_name", "text", False), ("body", "text", False), ("created_at", "timestamptz", False),
])
def test_columns_never_exported_as_stored(column, dtype, omitted):
    """_omit: not exported as the stored value. A binary column is exported
    only through DECRYPT / BINARY (base64), or refuses the export (29.5)."""
    assert privacy_export._omit(column, dtype) is omitted


def test_every_decrypted_export_column_belongs_to_an_exported_table():
    assert set(privacy_export.DECRYPT) <= set(privacy_export.exported_tables())


def test_pending_side_effects_never_touch_privacy_jobs():
    jobs = next(p for p in pl._PENDING if p.table == "automation_jobs")
    assert "privacy:%" in jobs.where


def test_offboarding_never_rewrites_authorship():
    for _, sql in pl._REASSIGN:
        assert "created_by" not in sql.split("WHERE")[0], sql
        assert "lower(" in sql, f"agent ids must be compared case-insensitively: {sql}"


def test_contact_hmac_is_keyed_per_tenant(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "a" * 64)
    one = pl.contact_hmac("11111111-1111-1111-1111-111111111111", "+13025550100")
    two = pl.contact_hmac("22222222-2222-2222-2222-222222222222", "+13025550100")
    assert one != two and len(one) == 64
    assert pl.contact_hmac("11111111-1111-1111-1111-111111111111", " +13025550100 ") == one


def test_an_export_without_the_master_key_refuses_before_writing(monkeypatch):
    """Audit 29.5: no key used to mean a 'succeeded' archive missing every
    sealed column. Now nothing is written and the job is not retried."""
    import asyncio
    import contextlib
    import tempfile

    import object_storage

    class Conn:
        async def fetchrow(self, sql, *a):
            return {"id": "op", "kind": "export", "state": "running", "tenant_id": "t", "params": "{}"}

    @contextlib.asynccontextmanager
    async def tx(_ctx):
        yield Conn()

    def no_write(*a, **k):
        raise AssertionError("nothing may be written")

    monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY", raising=False)
    monkeypatch.setattr(privacy_export, "tenant_tx", tx)
    monkeypatch.setattr(tempfile, "mkstemp", no_write)
    monkeypatch.setattr(object_storage, "put_file", no_write)
    with pytest.raises(privacy_export.ExportIncomplete) as exc:
        asyncio.run(privacy_export.build_export("op"))
    assert exc.value.job_error_code == "master_key_missing" and exc.value.job_terminal is True




def _fake_table(monkeypatch, table, cols):
    """Run _export_table against a fake connection with these columns; return
    the SQL it would stream and its prefetch, or raise what it raises."""
    import asyncio
    import contextlib

    seen = {}

    class Cursor:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class Conn:
        async def fetchval(self, sql, *a):
            return "11111111-1111-1111-1111-111111111111"

        async def fetch(self, sql, *a):
            return [{"attname": n, "t": t} for n, t in cols]

        def cursor(self, sql, *a, prefetch=None):
            seen["sql"], seen["prefetch"] = sql, prefetch
            return Cursor()

    @contextlib.asynccontextmanager
    async def tx(_ctx):
        yield Conn()

    class Archive:
        @contextlib.contextmanager
        def open(self, *a, **k):
            yield type("Member", (), {"write": lambda self, b: None})()

    monkeypatch.setattr(privacy_export, "tenant_tx", tx)
    asyncio.run(privacy_export._export_table(None, "op", table, "k", Archive()))
    return seen


def test_an_unmapped_binary_column_refuses_the_export(monkeypatch):
    """29.5: a sealed column nobody mapped is not silently left out."""
    with pytest.raises(privacy_export.ExportIncomplete) as exc:
        _fake_table(monkeypatch, "clients", [("id", "uuid"), ("tenant_id", "uuid"), ("new_secret_blob", "bytea")])
    assert exc.value.job_error_code == "unexported_column" and "clients.new_secret_blob" in str(exc.value)


def test_recorded_audio_is_exported_base64_a_few_rows_at_a_time(monkeypatch):
    seen = _fake_table(monkeypatch, "voice_walkthrough_jobs", [("id", "uuid"), ("tenant_id", "uuid"), ("audio", "bytea")])
    assert "encode(\"audio\", 'base64') AS \"audio_base64\"" in seen["sql"]
    assert seen["prefetch"] == 4
