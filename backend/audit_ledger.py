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
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import db.connection as _dbc  # pool lives at _dbc._pool — read live (it is created in the lifespan, AFTER this import)

logger = logging.getLogger("oracle.audit")

# ---------------------------------------------------------------------------
# SQLite fallback — used only when the PG pool is absent / insert fails.
# ---------------------------------------------------------------------------
# Default lives beside the code, but in containers the source tree is a
# read-only-for-this-uid bind mount — point ORACLE_AUDIT_SQLITE somewhere
# writable (compose sets /tmp/audit_logs.db). PG is the primary store; this
# is the degraded-mode fallback only.
_SQLITE_PATH = Path(
    os.environ.get("ORACLE_AUDIT_SQLITE", str(Path(__file__).parent / "audit_logs.db"))
)


def _sqlite_init() -> None:
    conn = sqlite3.connect(str(_SQLITE_PATH))
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
    conn.commit()
    conn.close()


_sqlite_init()


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

    # Legal / contracts
    LEGAL_CONTRACT    = "LEGAL_CONTRACT"     # contract creation / signature

    # Session lifecycle
    LOGIN             = "LOGIN"
    LOGOUT            = "LOGOUT"

    # Identity / role changes
    USER_STATE_CHANGE = "USER_STATE_CHANGE"  # activation, suspension, role assignment

    # Platform administration
    ADMIN_ACTION      = "ADMIN_ACTION"       # any platform_admin operation


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
    async with _dbc._pool.acquire() as conn:  # type: ignore[union-attr]
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


def _sqlite_last_hash() -> str:
    conn = sqlite3.connect(str(_SQLITE_PATH))
    row = conn.execute(
        "SELECT entry_hash FROM audit_logs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row[0] if row else "0" * 64


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

    def __init__(self) -> None:
        self._sqlite_path = str(_SQLITE_PATH)
        self._subscribers: list[WebSocket] = []

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
        if _dbc._pool is not None:
            try:
                async with _global_chain_conn() as conn:
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
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "audit: PG append failed (%s); persisting to SQLite fallback", exc
                )

        if not pg_ok:
            prev_hash = _sqlite_last_hash()
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
        if use_sqlite or _dbc._pool is None:
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("audit: verify_chain PG query failed (%s); falling back to SQLite", exc)
            return self._sqlite_verify_chain()

        expected_prev = "0" * 64
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
        if use_sqlite or _dbc._pool is None:
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
            logger.debug("audit: SQLite fallback write ok — %s / %s", category, action)
        except Exception as exc:  # noqa: BLE001
            logger.error("audit: BOTH PG and SQLite writes failed — entry LOST: %s", exc)

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
        conn.close()

        expected_prev = "0" * 64
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

    On connect: sends the 50 most recent entries as AUDIT_HISTORY.
    Thereafter: each new ledger.record() call broadcasts AUDIT_ENTRY frames.
    """
    await websocket.accept()
    ledger.subscribe(websocket)

    history = await ledger.get_entries(limit=50)
    await websocket.send_text(
        json.dumps({"type": "AUDIT_HISTORY", "data": list(reversed(history))})
    )

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ledger.unsubscribe(websocket)
