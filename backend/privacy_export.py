"""Brokerage data export: everything the brokerage owns, in one archive.

What goes in: every table privacy_data_map classifies as the brokerage's
(disposition ERASE), one JSON-lines file per table, encrypted PII decrypted
(an export of ciphertext is no export), plus the media and documents those
rows point at, plus a manifest with row counts and a SHA-256 per file.

What never goes in: secrets (provider credentials, webhook secrets, OAuth
verifiers), capability-token hashes, password hashes, HMAC lookup indexes,
raw binary columns. The manifest lists every omission and why.

Where it goes: private object storage under privacy/exports/{tenant}/, never
a public URL; downloaded only by a brokerage owner who re-enters their
password; deleted after EXPORT_ARTIFACT retention (7 days by default).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import tempfile
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from db.connection import tenant_tx
from privacy_data_map import TABLES, Disposition
from retention_policy import POLICY_VERSION, RetentionCategory, policy_for
from tenancy import TenantContext

log = logging.getLogger("oracle.privacy_export")

JOB_EXPORT = "privacy:export"
_EXCLUDED_CATEGORIES = {RetentionCategory.SECRET, RetentionCategory.CAPABILITY_TOKEN}
# Also the brokerage's own billing history (Neoh retains it; they may have a copy).
_EXTRA_TABLES = ("subscriptions", "billing_usage_events")
# Column-name patterns that are never exported.
_OMIT_SUFFIXES = ("_hash", "_hmac", "_digest", "password_hash", "_secret_ciphertext", "_verifier_ciphertext")
_OMIT_COLUMNS = {"name_search_tokens", "lease_token", "token_hash", "code_hmac"}
# Encrypted columns decrypted into the export: table -> {cipher column: output name}.
DECRYPT: dict[str, dict[str, str]] = {
    "agent_contacts": {"pii_ciphertext": "pii"},
    "ai_chat_messages": {"content_ciphertext": "content"},
    "ai_chat_actions": {"before_ciphertext": "before", "after_ciphertext": "after"},
    "inbound_voice_calls": {"caller_phone_ciphertext": "caller_phone", "transcript_ciphertext": "transcript",
                            "summary_ciphertext": "summary", "intake_answers_ciphertext": "intake_answers"},
    "contact_intake_sessions": {"raw_answers_ciphertext": "raw_answers",
                                "normalized_fields_ciphertext": "normalized_fields",
                                "transcript_ciphertext": "transcript"},
    "transaction_parties": {"contact_ciphertext": "contact"},
    "contact_property_relationships": {"property_label_ciphertext": "property_label"},
    "lead_intake_events": {"payload_ciphertext": "payload"},
    "contract_draft_workspaces": {"payload_ciphertext": "payload"},
    "contract_documents": {"content_ciphertext": "content"},
}
_MAX_MEDIA_BYTES = int(os.getenv("ORACLE_EXPORT_MAX_MEDIA_BYTES", str(2 * 1024 ** 3)))


def exported_tables() -> list[str]:
    names = [n for n, e in TABLES.items()
             if e.disposition is Disposition.ERASE and e.category not in _EXCLUDED_CATEGORIES]
    return sorted(set(names) | set(_EXTRA_TABLES))


def _omit(column: str, dtype: str) -> bool:
    if column in _OMIT_COLUMNS or column.endswith(_OMIT_SUFFIXES):
        return True
    return dtype == "bytea"


def _default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return None
    return str(value)


def _platform_ctx() -> TenantContext:
    from privacy_lifecycle import _platform_ctx as ctx

    return ctx("privacy-export")


async def request_export(ctx: TenantContext, *, include_media: bool = True) -> dict:
    from automation_jobs import enqueue_job
    from privacy_lifecycle import LifecycleError, _create_operation

    if not ctx.is_broker_owner:
        raise LifecycleError("Only a brokerage owner can export the brokerage's data.", status_code=403)
    async with tenant_tx(ctx) as conn:
        open_count = await conn.fetchval(
            "SELECT count(*) FROM privacy_operations WHERE tenant_id=$1 AND kind='export' "
            "AND state IN ('requested','running')", ctx.tenant_id)
        if open_count:
            raise LifecycleError("An export is already being prepared.")
        op = await _create_operation(conn, tenant_id=ctx.tenant_id, kind="export",
                                     requested_by=ctx.agent_id, subject_kind="tenant",
                                     subject_ref=ctx.tenant_id, state="running",
                                     params={"include_media": include_media})
    await enqueue_job(_platform_ctx(), job_type=JOB_EXPORT, payload={"operation_id": str(op["id"])},
                      idempotency_key=f"privacy-export:{op['id']}", created_by=ctx.agent_id,
                      max_attempts=3, priority=60)
    from privacy_lifecycle import emit_event

    emit_event("export.requested", operation_id=str(op["id"]), tenant_id=ctx.tenant_id)
    await _audit(ctx, "privacy.export.requested", str(op["id"]))
    return {"operation_id": str(op["id"]), "state": "running"}


async def build_export(operation_id: str) -> dict:
    """Write the archive. Runs in the worker as a platform session pinned to
    the one tenant by privacy_begin_read()."""
    import object_storage
    from privacy_lifecycle import _finish_operation

    pctx = _platform_ctx()
    async with tenant_tx(pctx) as conn:
        op = await conn.fetchrow("SELECT * FROM privacy_operations WHERE id=$1", operation_id)
    if op is None or op["kind"] != "export" or op["state"] != "running":
        return {"state": op["state"] if op else "missing"}
    tenant_id = str(op["tenant_id"])
    params = json.loads(op["params"]) if isinstance(op["params"], str) else (op["params"] or {})
    master = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    from crypto import derive_tenant_key

    key = derive_tenant_key(tenant_id, master) if master else None

    manifest: dict[str, Any] = {
        "format": "neoh-brokerage-export/1", "operation_id": operation_id, "tenant_id": tenant_id,
        "generated_at": datetime.now(timezone.utc).isoformat(), "policy_version": POLICY_VERSION,
        "files": {}, "omitted_columns": {}, "excluded_tables": {}, "media": {},
    }
    for name, entry in TABLES.items():
        if entry.disposition is Disposition.ERASE and entry.category in _EXCLUDED_CATEGORIES:
            manifest["excluded_tables"][name] = f"{entry.category.value}: never exported"

    fd, path = tempfile.mkstemp(prefix="neoh-export-", suffix=".zip")
    os.close(fd)
    media_keys: list[str] = []
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for table in exported_tables():
                info = await _export_table(pctx, operation_id, table, key, archive)
                if info is None:
                    continue
                manifest["files"][f"tables/{table}.jsonl"] = {"rows": info["rows"], "sha256": info["sha256"]}
                if info["omitted"]:
                    manifest["omitted_columns"][table] = info["omitted"]
                media_keys.extend(info.get("media_keys", []))
            if params.get("include_media", True) and object_storage.is_configured():
                manifest["media"] = await asyncio.to_thread(_export_media, archive, sorted(set(media_keys)))
            elif media_keys:
                manifest["media"] = {"included": 0, "not_included": len(media_keys),
                                     "reason": "media export disabled or storage unavailable"}
            manifest_bytes = json.dumps(manifest, indent=2, default=_default).encode()
            archive.writestr("manifest.json", manifest_bytes)
            archive.writestr("README.txt", _README)
        digest = _sha256_file(path)
        artifact_key = f"privacy/exports/{tenant_id}/{operation_id}.zip"
        await asyncio.to_thread(object_storage.put_file, artifact_key, path, "application/zip")
        days = policy_for(RetentionCategory.EXPORT_ARTIFACT).retention_days or 7
        expires = datetime.now(timezone.utc) + timedelta(days=days)
        size = os.path.getsize(path)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    rows = sum(f["rows"] for f in manifest["files"].values())
    async with tenant_tx(pctx) as conn:
        await conn.execute(
            "UPDATE privacy_operations SET artifact_key=$2, artifact_expires_at=$3 WHERE id=$1",
            operation_id, artifact_key, expires)
        await _finish_operation(conn, operation_id, state="succeeded",
                                result={"rows": rows, "tables": len(manifest["files"]), "bytes": size,
                                        "sha256": digest, "media": manifest["media"]},
                                receipt={"sha256": digest, "expires_at": expires.isoformat()})
    from privacy_lifecycle import emit_event

    emit_event("export.completed", operation_id=operation_id, tenant_id=tenant_id, rows=rows, bytes=size)
    return {"state": "succeeded", "rows": rows, "sha256": digest}


async def _export_table(pctx, operation_id: str, table: str, key: Optional[str], archive) -> Optional[dict]:
    async with tenant_tx(pctx) as conn:
        await conn.fetchval("SELECT privacy_begin_read($1)", operation_id)
        cols = await conn.fetch(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a "
            "WHERE a.attrelid = to_regclass('public.' || quote_ident($1)) AND a.attnum > 0 "
            "AND NOT a.attisdropped ORDER BY a.attnum", table)
        if not cols or not any(c["attname"] == "tenant_id" for c in cols):
            return None
        decrypt = DECRYPT.get(table, {}) if key else {}
        select, omitted = [], []
        for c in cols:
            name, dtype = c["attname"], c["t"]
            if name in decrypt:
                select.append(f"CASE WHEN {_q(name)} IS NULL THEN NULL ELSE "
                              f"pgp_sym_decrypt({_q(name)}, $2) END AS {_q(decrypt[name])}")
            elif _omit(name, dtype):
                omitted.append(name)
            else:
                select.append(_q(name))
        tid_type = next(c["t"] for c in cols if c["attname"] == "tenant_id")
        sql = f"SELECT {', '.join(select)} FROM public.{_q(table)} WHERE tenant_id = $1::{tid_type}"
        args: list[Any] = [str(await conn.fetchval("SELECT app_current_tenant()"))]
        if decrypt:
            args.append(key)
        else:
            sql = sql.replace("$2", "NULL")
        hasher = hashlib.sha256()
        rows = 0
        media_keys: list[str] = []
        with archive.open(f"tables/{table}.jsonl", "w", force_zip64=True) as member:
            async for record in conn.cursor(sql, *args, prefetch=1000):
                data = dict(record)
                if table == "property_media" and data.get("s3_key"):
                    media_keys.append(str(data["s3_key"]))
                line = (json.dumps(data, default=_default, ensure_ascii=False) + "\n").encode()
                member.write(line)
                hasher.update(line)
                rows += 1
    return {"rows": rows, "sha256": hasher.hexdigest(), "omitted": omitted, "media_keys": media_keys}


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _export_media(archive, keys: list[str]) -> dict:
    import object_storage

    included = skipped = failed = 0
    total = 0
    for key in keys:
        try:
            data = object_storage.get_bytes(key)
        except Exception:  # noqa: BLE001 - listed, never silently dropped
            failed += 1
            continue
        if total + len(data) > _MAX_MEDIA_BYTES:
            skipped += 1
            continue
        archive.writestr(f"media/{key}", data)
        total += len(data)
        included += 1
    return {"included": included, "bytes": total, "skipped_over_size_cap": skipped, "unreadable": failed}


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def export_download(ctx: TenantContext, operation_id: str) -> dict:
    """Owner-only, after a password check by the caller. Returns either a
    short-lived signed URL or a local path to stream."""
    import object_storage
    from privacy_lifecycle import LifecycleError

    if not ctx.is_broker_owner:
        raise LifecycleError("Only a brokerage owner can download an export.", status_code=403)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT artifact_key, artifact_expires_at, state FROM privacy_operations "
            "WHERE id=$1 AND tenant_id=$2 AND kind='export'", operation_id, ctx.tenant_id)
    if row is None:
        raise LifecycleError("Export not found.", status_code=404)
    if row["state"] != "succeeded" or not row["artifact_key"]:
        raise LifecycleError("That export is not ready, or has expired.", status_code=409)
    if row["artifact_expires_at"] and row["artifact_expires_at"] <= datetime.now(timezone.utc):
        raise LifecycleError("That export has expired. Request a new one.", status_code=410)
    await _audit(ctx, "privacy.export.downloaded", operation_id)
    url = object_storage.signed_url(row["artifact_key"], expires_seconds=300)
    if url:
        return {"url": url, "expires_in": 300}
    if object_storage.BACKEND in ("local", "azure-files"):
        return {"path": str(object_storage._safe_destination(row["artifact_key"]))}
    raise LifecycleError("Download is unavailable on this storage backend.", status_code=503)


async def expire_exports() -> int:
    """Delete archives past their expiry. Idempotent."""
    import object_storage

    async with tenant_tx(_platform_ctx()) as conn:
        rows = await conn.fetch(
            "SELECT id, artifact_key FROM privacy_operations WHERE kind='export' "
            "AND artifact_key IS NOT NULL AND artifact_expires_at <= now() LIMIT 200")
    expired = 0
    for row in rows:
        try:
            if object_storage.is_configured():
                await asyncio.to_thread(object_storage.delete_object, row["artifact_key"])
        except Exception:  # noqa: BLE001 - retried next hour
            log.warning("export %s could not be deleted yet", row["id"])
            continue
        async with tenant_tx(_platform_ctx()) as conn:
            await conn.execute(
                "UPDATE privacy_operations SET artifact_key=NULL, "
                "result = result || '{\"artifact\":\"expired_and_deleted\"}'::jsonb, updated_at=now() "
                "WHERE id=$1", row["id"])
        expired += 1
    return expired


async def _export_job(job: dict, reporter) -> dict:
    payload = job.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    op_id = str(payload["operation_id"])
    try:
        return await build_export(op_id)
    except Exception as exc:
        from privacy_lifecycle import _finish_operation

        from privacy_lifecycle import emit_event

        async with tenant_tx(_platform_ctx()) as conn:
            await _finish_operation(conn, op_id, state="failed", result={},
                                    error=f"export failed: {type(exc).__name__}")
        emit_event("export.failed", operation_id=op_id, reason_code=type(exc).__name__)
        raise


async def _audit(ctx: TenantContext, action: str, target: str) -> None:
    from audit_ledger import AuditCategory, ledger

    try:
        await ledger.record(AuditCategory.EXPORT_LEAD, action, tenant_id=ctx.tenant_id,
                            user_id=ctx.agent_id, target_id=target, metadata={})
    except Exception:  # noqa: BLE001
        log.exception("audit record for %s failed", action)


def register() -> None:
    from automation_jobs import register_handler

    register_handler(JOB_EXPORT, _export_job)


_README = """Neoh brokerage data export
==========================

tables/<name>.jsonl   One JSON object per line, one file per table. Encrypted
                      contact details, chat messages and call transcripts are
                      decrypted. Timestamps are ISO-8601 UTC.
media/<key>           Photos, video and documents the records point at.
manifest.json         Row counts, a SHA-256 per file, and every column or table
                      deliberately left out (passwords, provider credentials,
                      link tokens, lookup hashes) with the reason.

This archive contains personal information about your clients. Store it as
securely as you store your CRM. Neoh deletes its copy of this archive after 7
days.
"""
