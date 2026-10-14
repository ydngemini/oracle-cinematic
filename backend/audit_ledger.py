"""
Cryptographic audit ledger — tamper-proof chain of custody for compliance-critical actions.

Primary storage: Aurora PostgreSQL via asyncpg (table: audit_ledger, schema defined in
db/migrations/0005_audit_ledger.sql). Falls back to local SQLite when the pool is
unavailable (same degraded-mode pattern used by the rest of the backend).

Hash chain: every entry commits to its predecessor via SHA-256 over the canonical
block string, making silent row-deletion or reordering immediately detectable by
verify_chain().
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from tenancy import Role, require_context

import db.connection as _dbc  # pool via _dbc.get_pool() — read live (it is created in the lifespan, AFTER this import)

logger = logging.getLogger("oracle.audit")

# ---------------------------------------------------------------------------
# SQLite fallback — used only when the PG pool is absent / insert fails.
# ---------------------------------------------------------------------------
# Default lives beside the code, but in containers the source tree is a
# read-only-for-this-uid bind mount — point ORACLE_AUDIT_SQLITE somewhere
# writable (compose sets /tmp/audit_logs.db). PG is the primary store; this
# is the degraded-mode fallback only.
#
# Lifecycle (L17-12, 2026-10-14). Until then nothing ever read this file back:
# rows written while Postgres was down stayed on the container disk, with the
# account emails and client IPs they carry, and never reached the real chain.
# Now:
#   * drain: once Postgres answers again, the rows are appended to the PG
#     chain in order (original time and content, re-linked to the PG head,
#     provenance in metadata._fallback) and deleted from SQLite. It runs at
#     boot and after the first successful PG write that finds rows pending.
#   * cap: at most ORACLE_AUDIT_SQLITE_MAX_ROWS rows; a write beyond it is
#     refused, logged and alerted (ops_alerts), never silently dropped.
#   * age: rows older than ORACLE_AUDIT_SQLITE_MAX_AGE_DAYS that could not be
#     drained are purged, and the purge is alerted with its count, so personal
#     data does not stay on a disk indefinitely.
# A drained or purged prefix leaves its last hash in audit_fallback_anchor, so
# the rows that remain still verify as a chain.
_SQLITE_PATH = Path(
    os.environ.get("ORACLE_AUDIT_SQLITE", str(Path(__file__).parent / "audit_logs.db"))
)
_GENESIS = "0" * 64


def _sqlite_max_rows() -> int:
    try:
        return max(100, int(os.environ.get("ORACLE_AUDIT_SQLITE_MAX_ROWS", "20000")))
    except ValueError:
        return 20000


def _sqlite_max_age_days() -> int:
    try:
        return max(1, int(os.environ.get("ORACLE_AUDIT_SQLITE_MAX_AGE_DAYS", "30")))
    except ValueError:
        return 30


def _sqlite_init(path: Path | str | None = None) -> None:
    conn = sqlite3.connect(str(path or _SQLITE_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at   TEXT NOT NULL,
            tenant_id    TEXT,
            user_id      TEXT,
            category     TEXT NOT NULL,
            action       TEXT NOT NULL,
            target_id    TEXT,
            metadata     TEXT NOT NULL,
            prev_hash    TEXT NOT NULL,
            entry_hash   TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_al_created  ON audit_logs(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_al_category ON audit_logs(category)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_fallback_anchor (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            anchor_hash   TEXT NOT NULL,
            drained_rows  INTEGER NOT NULL DEFAULT 0,
            purged_rows   INTEGER NOT NULL DEFAULT 0,
            updated_at    TEXT
        )
    """)
    conn.commit()
    conn.close()


def _sqlite_anchor(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT anchor_hash FROM audit_fallback_anchor WHERE id = 1").fetchone()
    return row[0] if row else _GENESIS


def _sqlite_advance_anchor(conn: sqlite3.Connection, last_id: int, last_hash: str, *,
                           drained: int = 0, purged: int = 0) -> None:
    """Delete rows up to `last_id` and remember `last_hash` as the anchor the
    next remaining row links to. Caller commits."""
    conn.execute("DELETE FROM audit_logs WHERE id <= ?", (last_id,))
    conn.execute(
        "INSERT INTO audit_fallback_anchor (id, anchor_hash, drained_rows, purged_rows, updated_at) "
        "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET anchor_hash = excluded.anchor_hash, "
        "drained_rows = drained_rows + excluded.drained_rows, "
        "purged_rows = purged_rows + excluded.purged_rows, updated_at = excluded.updated_at",
        (last_hash, drained, purged, datetime.now(tz=timezone.utc).isoformat()))


def _sqlite_purge_expired(path: Path | str | None = None) -> int:
    """Purge the prefix of rows older than the age limit. Returns the count."""
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=_sqlite_max_age_days())).isoformat()
    conn = sqlite3.connect(str(path or _SQLITE_PATH))
    try:
        row = conn.execute(
            "SELECT id, entry_hash FROM audit_logs WHERE id = "
            "(SELECT max(id) FROM audit_logs WHERE created_at < ?)", (cutoff,)).fetchone()
        if not row:
            return 0
        count = conn.execute("SELECT count(*) FROM audit_logs WHERE id <= ?", (row[0],)).fetchone()[0]
        _sqlite_advance_anchor(conn, row[0], row[1], purged=count)
        conn.commit()
        return int(count)
    finally:
        conn.close()


def _sqlite_pending(path: Path | str | None = None) -> int:
    conn = sqlite3.connect(str(path or _SQLITE_PATH))
    try:
        return int(conn.execute("SELECT count(*) FROM audit_logs").fetchone()[0])
    finally:
        conn.close()


_sqlite_init()


_LOSS_ALERTED_AT: dict[str, float] = {}


def _alert_audit_loss(kind: str, count: int) -> None:
    """An audit record could not be kept: alert (15-minute quiet period per
    kind). Counts only — never the record's content."""
    import time as _time

    logger.error("audit: %d record(s) %s", count, kind)
    if _time.time() - _LOSS_ALERTED_AT.get(kind, 0) < 900:
        return
    _LOSS_ALERTED_AT[kind] = _time.time()
    try:
        import asyncio

        import ops_alerts

        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(ops_alerts._notify(
        f"[Neoh] audit records {kind}",
        f"{count} audit record(s) {kind} on this container's SQLite fallback "
        f"({_SQLITE_PATH}). Postgres was unreachable for audit writes. "
        "See backend/audit_ledger.py (fallback lifecycle) and docs/privacy-data-map.md §7."))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


_BACKGROUND: set = set()


# ---------------------------------------------------------------------------
# AuditCategory — all compliance-relevant action classes.
# ---------------------------------------------------------------------------
class AuditCategory(str, Enum):
    # Data access
    ACCESS_PII        = "ACCESS_PII"         # any read of encrypted/sensitive fields
    EXPORT_LEAD       = "EXPORT_LEAD"        # CSV/bulk export of lead data
    DATA_DELETE       = "DATA_DELETE"        # any deletion of records

    # AI / cost-incurring
    AI_PHONE_CALL     = "AI_PHONE_CALL"      # outbound AI voice call
    GENERATE_TOUR     = "GENERATE_TOUR"      # AI tour generation (money + property data)
    VIDEO_STUDIO      = "VIDEO_STUDIO"       # Sora video generation (money + property data)

    # Legal / contracts
    LEGAL_CONTRACT    = "LEGAL_CONTRACT"     # contract creation / signature

    # Session lifecycle
    LOGIN             = "LOGIN"
    LOGOUT            = "LOGOUT"

    # Identity / role changes
    USER_STATE_CHANGE = "USER_STATE_CHANGE"  # activation, suspension, role assignment

    # Platform administration
    ADMIN_ACTION      = "ADMIN_ACTION"       # any platform_admin operation

    # Record access
    ACCESS_GRANT      = "ACCESS_GRANT"       # a client shared, re-permissioned or unshared (0131)


# ---------------------------------------------------------------------------
# Hash-chain helpers — deterministic over the canonical block string.
# ---------------------------------------------------------------------------
def _compute_hash(
    created_at: str,
    category: str,
    action: str,
    tenant_id: str,
    user_id: str,
    target_id: str,
    metadata: str,
    prev_hash: str,
) -> str:
    block = f"{created_at}|{category}|{action}|{tenant_id}|{user_id}|{target_id}|{metadata}|{prev_hash}"
    return hashlib.sha256(block.encode("utf-8")).hexdigest()


def _canonical_meta(raw) -> str:
    value = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _pg_row_links(row, expected_prev: str) -> bool:
    """True when `row` links to `expected_prev` and its own hash recomputes."""
    if row["prev_hash"] != expected_prev:
        return False
    return _compute_hash(
        row["created_at"].isoformat(), row["category"], row["action"],
        str(row["tenant_id"]) if row["tenant_id"] else "",
        str(row["user_id"]) if row["user_id"] else "",
        str(row["target_id"]) if row["target_id"] else "",
        _canonical_meta(row["metadata"]), row["prev_hash"],
    ) == row["entry_hash"]


def _fallback_rows_intact(rows: list[tuple], anchor: str) -> bool:
    """Verify a batch of SQLite rows (id, created_at, tenant, user, category,
    action, target, metadata, prev_hash, entry_hash) from `anchor`."""
    expected = anchor
    for (_id, created_at, tenant, user, category, action, target, meta_raw, prev, entry) in rows:
        if prev != expected or _compute_hash(created_at, category, action, tenant or "", user or "",
                                             target or "", _canonical_meta(meta_raw), prev) != entry:
            return False
        expected = entry
    return True


def _uuid_or_none(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return str(UUID(str(value)))
    except ValueError:
        return None


# Fixed advisory-lock key shared by every audit-chain writer in every process.
# pg_advisory_xact_lock serialises the read-head + insert pair so two concurrent
# record() calls can never observe the same prev_hash and fork the chain. The
# value is arbitrary but must be identical across all writers.
_AUDIT_CHAIN_LOCK_KEY = 0x4155_4449_5403  # mnemonic: "AUDI" + chain v3


async def _pg_last_hash(conn) -> str:
    """Fetch the most recent entry_hash on the given connection. Returns the
    genesis sentinel on an empty table. The caller must run this inside the
    advisory-locked transaction (and under a context that can SELECT the whole
    chain) so the value is still current at INSERT time."""
    row = await conn.fetchrow(
        "SELECT entry_hash FROM audit_ledger ORDER BY seq DESC LIMIT 1"
    )
    return row["entry_hash"] if row else "0" * 64


@asynccontextmanager
async def _global_chain_conn():
    """Acquire a pooled connection in a transaction with platform_admin RLS
    context applied, so audit reads and writes see the whole cross-tenant chain.

    The audit_ledger is a single global hash chain, but audit_ledger_select
    scopes non-admins to their own tenant — without this, a bare-pool read sees
    no rows (no GUC set) and the head read / verify / admin feed all come back
    empty. SET LOCAL resets at transaction end, so no identity leaks onto the
    pooled socket."""
    # The platform pool: since 0120 the platform_admin GUC grants admin only
    # on a login in platform_admin_role, which request connections are not.
    pool = _dbc.get_platform_pool()
    if pool is None:
        raise RuntimeError("DB pool not initialized — audit chain requires an active pool")
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Match the apply_rls_context() GUC contract: set BOTH role and
            # tenant (to the all-zero platform sentinel) so any policy that
            # references app_current_tenant() can't silently break on this
            # global-chain connection. SET LOCAL resets at transaction end.
            await conn.execute(
                "SELECT set_config('app.current_role',   'platform_admin', true),"
                "       set_config('app.current_tenant', '00000000-0000-0000-0000-000000000000', true)"
            )
            yield conn


def _sqlite_last_hash(path: Path | str | None = None) -> str:
    conn = sqlite3.connect(str(path or _SQLITE_PATH))
    try:
        row = conn.execute(
            "SELECT entry_hash FROM audit_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        # Empty after a drain or purge: the chain continues from the anchor.
        return row[0] if row else _sqlite_anchor(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AuditLedger
# ---------------------------------------------------------------------------
class AuditLedger:
    """
    Async-first audit ledger with PostgreSQL primary storage and SQLite fallback.

    Usage::

        entry = await ledger.record(
            category=AuditCategory.EXPORT_LEAD,
            action="export_leads_csv",
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            target_id="batch-42",
            metadata={"row_count": 150, "filter": "status=qualified"},
        )
    """

    def __init__(self, sqlite_path: Optional[str] = None) -> None:
        self._sqlite_path = str(sqlite_path or _SQLITE_PATH)
        if sqlite_path:
            _sqlite_init(self._sqlite_path)
        self._subscribers: list[WebSocket] = []
        self._drain_task = None
        try:
            self._fallback_pending = _sqlite_pending(self._sqlite_path) > 0
        except sqlite3.Error:
            self._fallback_pending = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def record(
        self,
        category: AuditCategory,
        action: str,
        tenant_id: Optional[str | UUID] = None,
        user_id: Optional[str | UUID] = None,
        target_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        """
        Append a new entry to the audit chain.

        Tries PostgreSQL first; silently falls back to SQLite if the pool is
        absent or the INSERT fails (same degraded-mode pattern as the rest of
        the backend — the server boots and operates even without DB).

        Returns the serialisable entry dict (broadcast to WS subscribers too).
        """
        created_dt   = datetime.now(tz=timezone.utc)
        created_at   = created_dt.isoformat()  # string form: hashing, SQLite, JSON
        tenant_str   = str(tenant_id) if tenant_id is not None else ""
        user_str     = str(user_id)   if user_id   is not None else ""
        target_str   = str(target_id) if target_id is not None else ""
        meta_payload = metadata or {}
        meta_json    = json.dumps(meta_payload, separators=(",", ":"), sort_keys=True)

        # ---- persist atomically -------------------------------------------
        # PG path: read the chain head and append inside ONE advisory-locked
        # transaction, so prev_hash cannot go stale between the read and the
        # insert (closes the TOCTOU that let two concurrent record() calls share
        # a prev_hash and fork the chain). SET LOCAL platform_admin so the
        # global-chain SELECT sees every tenant's rows — audit_ledger_select
        # scopes non-admins to their own tenant, which would otherwise make the
        # head read return only the current tenant's last row (or nothing). The
        # GUC is SET LOCAL, so it resets when the transaction ends: no identity
        # leaks onto the pooled socket. Falls back to the SQLite chain if the
        # pool is down or the transaction fails.
        prev_hash: str
        entry_hash: str
        pg_ok = False
        if _dbc.get_pool() is not None:
            try:
                async with _global_chain_conn() as conn:
                    # Every audit write queues on this one lock; it must outlast
                    # the request pool's short lock_timeout (db/connection.py).
                    await conn.execute("SET LOCAL lock_timeout = '30s'")
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock($1)", _AUDIT_CHAIN_LOCK_KEY
                    )
                    prev_hash = await _pg_last_hash(conn)
                    entry_hash = _compute_hash(
                        created_at, category.value, action,
                        tenant_str, user_str, target_str, meta_json, prev_hash,
                    )
                    await conn.execute(
                        """
                        INSERT INTO audit_ledger
                            (tenant_id, user_id, category, action, target_id,
                             metadata, prev_hash, entry_hash, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
                        """,
                        tenant_str or None,
                        user_str   or None,
                        category.value,
                        action,
                        target_str or None,
                        meta_json,
                        prev_hash,
                        entry_hash,
                        created_dt,  # asyncpg needs the datetime, not the isoformat string
                    )
                pg_ok = True
                logger.debug("audit: PG insert ok — %s / %s", category.value, action)
                if self._fallback_pending:
                    self.start_fallback_drain()
            except Exception as exc:  # noqa: BLE001
                # The type only: a refused row's DETAIL quotes its values.
                logger.warning(
                    "audit: PG append failed (%s); persisting to SQLite fallback", type(exc).__name__
                )

        if not pg_ok:
            prev_hash = _sqlite_last_hash(self._sqlite_path)
            entry_hash = _compute_hash(
                created_at, category.value, action,
                tenant_str, user_str, target_str, meta_json, prev_hash,
            )
            self._sqlite_insert(
                created_at, tenant_str, user_str, category.value,
                action, target_str, meta_json, prev_hash, entry_hash,
            )

        entry = {
            "created_at": created_at,
            "tenant_id":  tenant_str or None,
            "user_id":    user_str   or None,
            "category":   category.value,
            "action":     action,
            "target_id":  target_str or None,
            "metadata":   meta_payload,
            "prev_hash":  prev_hash,
            "entry_hash": entry_hash,
        }
        await self._broadcast(entry)
        return entry

    async def verify_chain(self, use_sqlite: bool = False) -> bool:
        """
        Walk the full chain in insertion order and verify every link.

        Returns True when the chain is intact, False on any broken link or
        tampered hash. Pass use_sqlite=True to verify the local fallback chain
        instead of PostgreSQL.
        """
        if use_sqlite or _dbc.get_pool() is None:
            return self._sqlite_verify_chain()

        try:
            async with _global_chain_conn() as conn:
                rows = await conn.fetch(
                    """
                    SELECT created_at, category, action, tenant_id, user_id,
                           target_id, metadata, prev_hash, entry_hash
                    FROM   audit_ledger
                    ORDER  BY seq ASC
                    """
                )
                # Rows past their retention are expired as a prefix of the
                # chain (0122 privacy_expire_audit); the hash of the last one
                # removed is the anchor the first remaining row links to.
                anchor = await conn.fetchval(
                    "SELECT anchor_hash FROM audit_chain_checkpoints ORDER BY id DESC LIMIT 1"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("audit: verify_chain PG query failed (%s); falling back to SQLite", exc)
            return self._sqlite_verify_chain()

        expected_prev = anchor or "0" * 64
        for row in rows:
            meta_json = json.dumps(
                json.loads(row["metadata"]),
                separators=(",", ":"),
                sort_keys=True,
            )
            if row["prev_hash"] != expected_prev:
                return False
            computed = _compute_hash(
                # The hash was computed over the isoformat STRING at record
                # time — re-deriving from the PG datetime must match exactly.
                row["created_at"].isoformat(),
                row["category"],
                row["action"],
                str(row["tenant_id"]) if row["tenant_id"] else "",
                str(row["user_id"]) if row["user_id"] else "",
                row["target_id"] or "",
                meta_json,
                row["prev_hash"],
            )
            if computed != row["entry_hash"]:
                return False
            expected_prev = row["entry_hash"]
        return True

    async def get_entries(
        self,
        limit: int = 100,
        category: Optional[AuditCategory] = None,
        tenant_id: Optional[str | UUID] = None,
        use_sqlite: bool = False,
    ) -> list[dict]:
        """
        Retrieve recent audit entries, newest first.

        Falls back to SQLite automatically when the pool is absent.
        Pass use_sqlite=True to explicitly query the local fallback store.
        """
        if use_sqlite or _dbc.get_pool() is None:
            return self._sqlite_get_entries(limit=limit, category=category)

        try:
            conditions: list[str] = []
            params: list = []
            idx = 1
            if category:
                conditions.append(f"category = ${idx}")
                params.append(category.value)
                idx += 1
            if tenant_id:
                conditions.append(f"tenant_id = ${idx}")
                params.append(str(tenant_id))
                idx += 1

            where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            async with _global_chain_conn() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT created_at, tenant_id, user_id, category, action,
                           target_id, metadata, prev_hash, entry_hash
                    FROM   audit_ledger
                    {where_clause}
                    ORDER  BY seq DESC
                    LIMIT  ${idx}
                    """,
                    *params,
                    limit,
                )
            return [
                {
                    # JSON-safe scalars — these dicts go straight through
                    # json.dumps on the audit WS path (no FastAPI encoder).
                    "created_at": row["created_at"].isoformat(),
                    "tenant_id":  str(row["tenant_id"]) if row["tenant_id"] else None,
                    "user_id":    str(row["user_id"]) if row["user_id"] else None,
                    "category":   row["category"],
                    "action":     row["action"],
                    "target_id":  row["target_id"],
                    "metadata":   json.loads(row["metadata"]),
                    "prev_hash":  row["prev_hash"],
                    "entry_hash": row["entry_hash"],
                }
                for row in rows
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("audit: get_entries PG query failed (%s); reading from SQLite", exc)
            return self._sqlite_get_entries(limit=limit, category=category)

    # ------------------------------------------------------------------
    # WebSocket subscription helpers
    # ------------------------------------------------------------------

    def subscribe(self, ws: WebSocket) -> None:
        self._subscribers.append(ws)

    def unsubscribe(self, ws: WebSocket) -> None:
        self._subscribers = [s for s in self._subscribers if s is not ws]

    async def _broadcast(self, entry: dict) -> None:
        payload = json.dumps({"type": "AUDIT_ENTRY", "data": entry})
        dead: list[WebSocket] = []
        for ws in list(self._subscribers):
            try:
                await ws.send_text(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.unsubscribe(ws)

    # ------------------------------------------------------------------
    # SQLite fallback lifecycle: drain to Postgres, purge by age (L17-12)
    # ------------------------------------------------------------------

    def start_fallback_drain(self) -> None:
        """Schedule a drain unless one is already running in this process."""
        import asyncio

        if self._drain_task is not None and not self._drain_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._drain_task = loop.create_task(self._drain_quietly(), name="audit-fallback-drain")

    async def _drain_quietly(self) -> None:
        try:
            await self.drain_fallback()
        except Exception as exc:  # noqa: BLE001 - rows stay in SQLite; the next attempt retries
            logger.warning("audit: fallback drain did not finish (%s)", type(exc).__name__)

    def _sqlite_batch(self, limit: int) -> tuple[list[tuple], str]:
        conn = sqlite3.connect(self._sqlite_path)
        try:
            rows = conn.execute(
                "SELECT id, created_at, tenant_id, user_id, category, action, target_id, "
                "metadata, prev_hash, entry_hash FROM audit_logs ORDER BY id LIMIT ?", (limit,)).fetchall()
            return rows, _sqlite_anchor(conn)
        finally:
            conn.close()

    def _sqlite_release(self, last_id: int, last_hash: str, count: int) -> int:
        conn = sqlite3.connect(self._sqlite_path)
        try:
            _sqlite_advance_anchor(conn, last_id, last_hash, drained=count)
            conn.commit()
            remaining = conn.execute("SELECT count(*) FROM audit_logs").fetchone()[0]
        finally:
            conn.close()
        if not remaining:
            # Deleted pages keep their bytes until VACUUM; give them back.
            conn = sqlite3.connect(self._sqlite_path)
            try:
                conn.execute("VACUUM")
            finally:
                conn.close()
        return int(remaining)

    async def drain_fallback(self, *, batch: int = 500, max_batches: int = 20) -> dict:
        """Append this container's SQLite rows to the Postgres chain, in order,
        then delete them. Idempotent: a row already appended (a crash between
        the PG commit and the SQLite delete) is recognised by its fallback hash
        and not appended twice. Purges rows past the age limit first."""
        import asyncio
        import fcntl

        purged = await asyncio.to_thread(_sqlite_purge_expired, self._sqlite_path)
        if purged:
            _alert_audit_loss(f"purged after {_sqlite_max_age_days()} days undrained (LOST)", purged)
        if _dbc.get_pool() is None:
            return {"skipped": "no database pool", "purged": purged}
        lock_file = open(self._sqlite_path + ".drain.lock", "w")  # noqa: SIM115 - held for the drain
        try:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return {"skipped": "another process is draining", "purged": purged}
            drained, intact, remaining = 0, True, None
            for _ in range(max_batches):
                rows, anchor = await asyncio.to_thread(self._sqlite_batch, batch)
                if not rows:
                    remaining = 0
                    break
                intact = _fallback_rows_intact(rows, anchor) and intact
                await self._append_fallback_rows(rows, intact)
                remaining = await asyncio.to_thread(
                    self._sqlite_release, rows[-1][0], rows[-1][9], len(rows))
                drained += len(rows)
                if not remaining:
                    break
            self._fallback_pending = bool(remaining)
        finally:
            lock_file.close()
        if drained:
            logger.info("audit: drained %d fallback row(s) to Postgres; chain intact=%s; %s left",
                        drained, intact, remaining)
        if not intact:
            _alert_audit_loss("drained from a SQLite fallback chain that does NOT verify (tampered?)", drained)
        return {"drained": drained, "remaining": remaining, "chain_intact": intact, "purged": purged}

    async def _append_fallback_rows(self, rows: list[tuple], intact: bool) -> None:
        async with _global_chain_conn() as conn:
            await conn.execute("SET LOCAL lock_timeout = '30s'")
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _AUDIT_CHAIN_LOCK_KEY)
            prev_hash = await _pg_last_hash(conn)
            for (_id, created_at, tenant, user, category, action, target, meta_raw,
                 _prev, entry_hash) in rows:
                created_dt = datetime.fromisoformat(created_at)
                if await conn.fetchval(
                        "SELECT 1 FROM audit_ledger WHERE category = $1 AND created_at = $2 "
                        "AND metadata->'_fallback'->>'entry_hash' = $3 LIMIT 1",
                        category, created_dt, entry_hash):
                    continue
                meta = json.loads(meta_raw)
                provenance = {"store": "sqlite", "entry_hash": entry_hash, "chain_intact": intact}
                # The PG columns are uuids; keep a value that is not one in the
                # provenance instead of refusing the whole batch forever.
                tenant_col, target_col = _uuid_or_none(tenant), _uuid_or_none(target)
                if tenant and tenant_col is None:
                    provenance["tenant_id"] = tenant
                if target and target_col is None:
                    provenance["target_id"] = target
                meta["_fallback"] = provenance
                meta_json = json.dumps(meta, separators=(",", ":"), sort_keys=True)
                new_hash = _compute_hash(created_at, category, action, tenant_col or "", user or "",
                                         target_col or "", meta_json, prev_hash)
                await conn.execute(
                    "INSERT INTO audit_ledger (tenant_id, user_id, category, action, target_id, "
                    "metadata, prev_hash, entry_hash, created_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)",
                    tenant_col, user or None, category, action, target_col, meta_json,
                    prev_hash, new_hash, created_dt)
                prev_hash = new_hash

    # ------------------------------------------------------------------
    # Scheduled verification, bounded per run (L17-12, migration 0148)
    # ------------------------------------------------------------------

    async def verify_chain_segment(self, *, max_rows: int = 50_000, page: int = 2_000) -> dict:
        """Verify up to `max_rows` links from the stored cursor. At the head the
        pass completes and the next run starts again from the expiry anchor,
        so every row is re-checked once per pass. The first broken link is
        recorded and alerted once; verification continues past it."""
        async with _global_chain_conn() as conn:
            result, alert = await self._verify_segment_on(conn, max_rows=max_rows, page=page)
        if alert:
            import ops_alerts

            await ops_alerts._notify(
                "[Neoh] audit chain BROKEN",
                f"The audit hash chain does not verify at seq {result['broken_seq']}: a row was altered, "
                "removed or inserted outside audit_ledger.record(). Preserve the database as evidence; "
                "see docs/security-launch-review.md and backend/audit_ledger.py.")
        return result

    async def _verify_segment_on(self, conn, *, max_rows: int, page: int) -> tuple[dict, bool]:
        """One bounded verification step on `conn`, inside the caller's
        transaction (platform context). Returns (result, alert_now)."""
        state = await conn.fetchrow("SELECT * FROM audit_chain_verification WHERE id = 1 FOR UPDATE")
        if state is None:
            await conn.execute("INSERT INTO audit_chain_verification (id) VALUES (1) ON CONFLICT DO NOTHING")
            state = await conn.fetchrow("SELECT * FROM audit_chain_verification WHERE id = 1 FOR UPDATE")
        checkpoint = await conn.fetchrow(
            "SELECT last_expired_seq, anchor_hash FROM audit_chain_checkpoints ORDER BY id DESC LIMIT 1")
        anchor_seq = checkpoint["last_expired_seq"] if checkpoint else 0
        anchor_hash = checkpoint["anchor_hash"] if checkpoint else _GENESIS
        cursor_seq, cursor_hash = state["cursor_seq"], state["cursor_hash"]
        rows_in_pass = state["rows_in_pass"] or 0
        new_pass = cursor_seq is None or cursor_seq < anchor_seq
        if new_pass:
            # A fresh pass, or the expiry sweep removed rows past the cursor.
            cursor_seq, cursor_hash, rows_in_pass = anchor_seq, anchor_hash, 0
        checked, broken_seq, at_head = 0, None, False
        while checked < max_rows:
            rows = await conn.fetch(
                "SELECT seq, created_at, category, action, tenant_id, user_id, target_id, "
                "metadata, prev_hash, entry_hash FROM audit_ledger WHERE seq > $1 "
                "ORDER BY seq LIMIT $2", cursor_seq, min(page, max_rows - checked))
            if not rows:
                at_head = True
                break
            for row in rows:
                if not _pg_row_links(row, cursor_hash) and broken_seq is None:
                    broken_seq = row["seq"]
                cursor_seq, cursor_hash = row["seq"], row["entry_hash"]
            checked += len(rows)
        rows_in_pass += checked
        status = "broken" if broken_seq is not None else ("empty" if not rows_in_pass else "intact")
        first_break = broken_seq if broken_seq is not None else state["broken_seq"]
        await conn.execute(
            """
            UPDATE audit_chain_verification SET
                cursor_seq = CASE WHEN $1::boolean THEN NULL ELSE $2::bigint END,
                cursor_hash = CASE WHEN $1::boolean THEN NULL ELSE $3::text END,
                pass_started_at = CASE WHEN $4::boolean THEN now() ELSE pass_started_at END,
                rows_in_pass = CASE WHEN $1::boolean THEN 0 ELSE $5::bigint END,
                last_complete_pass_at = CASE WHEN $1::boolean THEN now() ELSE last_complete_pass_at END,
                last_pass_rows = CASE WHEN $1::boolean THEN $5::bigint ELSE last_pass_rows END,
                last_run_at = now(), last_status = $6::text,
                broken_seq = $7::bigint,
                broken_detected_at = CASE WHEN $8::bigint IS NOT NULL
                    AND $8::bigint IS DISTINCT FROM broken_seq THEN now() ELSE broken_detected_at END
             WHERE id = 1
            """, at_head, cursor_seq, cursor_hash, new_pass, rows_in_pass, status, first_break,
            broken_seq)
        alert = broken_seq is not None and broken_seq != state["alerted_broken_seq"]
        if alert:
            await conn.execute(
                "UPDATE audit_chain_verification SET alerted_broken_seq = $1 WHERE id = 1", broken_seq)
        return ({"checked": checked, "status": status, "broken_seq": broken_seq,
                 "pass_complete": at_head, "rows_in_pass": 0 if at_head else rows_in_pass}, alert)

    # ------------------------------------------------------------------
    # SQLite fallback internals
    # ------------------------------------------------------------------

    def _sqlite_insert(
        self,
        created_at: str,
        tenant_id: str,
        user_id: str,
        category: str,
        action: str,
        target_id: str,
        meta_json: str,
        prev_hash: str,
        entry_hash: str,
    ) -> None:
        try:
            conn = sqlite3.connect(self._sqlite_path)
            held = conn.execute("SELECT count(*) FROM audit_logs").fetchone()[0]
            if held >= _sqlite_max_rows():
                conn.close()
                _alert_audit_loss("LOST: SQLite fallback full", 1)
                return
            conn.execute(
                """
                INSERT INTO audit_logs
                    (created_at, tenant_id, user_id, category, action,
                     target_id, metadata, prev_hash, entry_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    tenant_id  or None,
                    user_id    or None,
                    category,
                    action,
                    target_id  or None,
                    meta_json,
                    prev_hash,
                    entry_hash,
                ),
            )
            conn.commit()
            conn.close()
            self._fallback_pending = True
            logger.debug("audit: SQLite fallback write ok — %s / %s", category, action)
        except Exception as exc:  # noqa: BLE001
            logger.error("audit: BOTH PG and SQLite writes failed (%s)", type(exc).__name__)
            _alert_audit_loss("LOST: Postgres and SQLite both refused the write", 1)

    def _sqlite_verify_chain(self) -> bool:
        conn = sqlite3.connect(self._sqlite_path)
        rows = conn.execute(
            """
            SELECT created_at, category, action, tenant_id, user_id,
                   target_id, metadata, prev_hash, entry_hash
            FROM   audit_logs
            ORDER  BY id ASC
            """
        ).fetchall()
        # A drained or purged prefix is replaced by its last hash.
        expected_prev = _sqlite_anchor(conn)
        conn.close()

        for row in rows:
            created_at, category, action, tenant_id, user_id, \
                target_id, meta_raw, prev_hash, entry_hash = row
            meta_json = json.dumps(
                json.loads(meta_raw), separators=(",", ":"), sort_keys=True
            )
            if prev_hash != expected_prev:
                return False
            computed = _compute_hash(
                created_at, category, action,
                tenant_id  or "",
                user_id    or "",
                target_id  or "",
                meta_json,
                prev_hash,
            )
            if computed != entry_hash:
                return False
            expected_prev = entry_hash
        return True

    def _sqlite_get_entries(
        self,
        limit: int = 100,
        category: Optional[AuditCategory] = None,
    ) -> list[dict]:
        conn = sqlite3.connect(self._sqlite_path)
        if category:
            rows = conn.execute(
                """
                SELECT created_at, tenant_id, user_id, category, action,
                       target_id, metadata, prev_hash, entry_hash
                FROM   audit_logs
                WHERE  category = ?
                ORDER  BY id DESC
                LIMIT  ?
                """,
                (category.value, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT created_at, tenant_id, user_id, category, action,
                       target_id, metadata, prev_hash, entry_hash
                FROM   audit_logs
                ORDER  BY id DESC
                LIMIT  ?
                """,
                (limit,),
            ).fetchall()
        conn.close()

        return [
            {
                "created_at": r[0],
                "tenant_id":  r[1],
                "user_id":    r[2],
                "category":   r[3],
                "action":     r[4],
                "target_id":  r[5],
                "metadata":   json.loads(r[6]),
                "prev_hash":  r[7],
                "entry_hash": r[8],
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
ledger = AuditLedger()


# ---------------------------------------------------------------------------
# FastAPI router — /admin/audit-trail WebSocket
# ---------------------------------------------------------------------------
router = APIRouter()


@router.websocket("/admin/audit-trail")
async def audit_trail_ws(websocket: WebSocket) -> None:
    """
    Real-time audit feed.

    Platform-admin JWT authentication is completed before the WebSocket
    upgrade. On connect: sends the 50 most recent entries as AUDIT_HISTORY.
    Thereafter: each new ledger.record() call broadcasts AUDIT_ENTRY frames.

    The browser supplies ``oracle.jwt`` and the JWT as WebSocket subprotocols,
    keeping credentials out of URLs and access logs. This feed is global by
    design, so ordinary tenant roles are rejected rather than tenant-filtered.
    """
    offered = [
        value.strip()
        for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if value.strip()
    ]
    raw_token = offered[1] if len(offered) == 2 and offered[0] == "oracle.jwt" else ""
    if not raw_token:
        logger.warning("Audit WebSocket rejected a missing credential.")
        await websocket.close(code=4401)
        return

    try:
        ctx = require_context(f"Bearer {raw_token}")
    except Exception:  # noqa: BLE001 — normalize all credential failures
        logger.warning("Audit WebSocket rejected an invalid or expired credential.")
        await websocket.close(code=4401)
        return
    if ctx.role is not Role.PLATFORM_ADMIN:
        logger.warning("Audit WebSocket rejected a non-admin role.")
        await websocket.close(code=4403)
        return

    await websocket.accept(subprotocol="oracle.jwt")
    ledger.subscribe(websocket)

    try:
        history = await ledger.get_entries(limit=50)
        await websocket.send_text(
            json.dumps({"type": "AUDIT_HISTORY", "data": list(reversed(history))})
        )
        while True:
            message = await websocket.receive_text()
            if len(message.encode("utf-8")) > 4096:
                await websocket.close(code=1009)
                return
    except WebSocketDisconnect:
        pass
    finally:
        ledger.unsubscribe(websocket)
