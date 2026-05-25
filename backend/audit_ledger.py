"""
Cryptographic audit ledger — tamper-proof chain of custody for compliance-critical actions.
"""

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

DB_PATH = Path(__file__).parent / "audit_logs.db"

router = APIRouter()


class AuditCategory(str, Enum):
    AI_PHONE_CALL = "AI_PHONE_CALL"
    LEGAL_CONTRACT = "LEGAL_CONTRACT"
    USER_STATE_CHANGE = "USER_STATE_CHANGE"


def _init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            category TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            payload TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_logs_timestamp ON audit_logs(timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_logs_category ON audit_logs(category)
    """)
    conn.commit()
    conn.close()


_init_db()


def _get_last_hash() -> str:
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute(
        "SELECT entry_hash FROM audit_logs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row[0] if row else "0" * 64


def _compute_hash(timestamp: str, category: str, actor: str, action: str, payload: str, prev_hash: str) -> str:
    block = f"{timestamp}|{category}|{actor}|{action}|{payload}|{prev_hash}"
    return hashlib.sha256(block.encode("utf-8")).hexdigest()


class AuditLedger:
    def __init__(self):
        self._db_path = str(DB_PATH)
        self._subscribers: list[WebSocket] = []

    def record(self, category: AuditCategory, actor: str, action: str, payload: dict) -> dict:
        timestamp = datetime.now(tz=timezone.utc).isoformat()
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        prev_hash = _get_last_hash()
        entry_hash = _compute_hash(timestamp, category.value, actor, action, payload_json, prev_hash)

        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """INSERT INTO audit_logs (timestamp, category, actor, action, payload, prev_hash, entry_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, category.value, actor, action, payload_json, prev_hash, entry_hash),
        )
        conn.commit()
        conn.close()

        entry = {
            "timestamp": timestamp,
            "category": category.value,
            "actor": actor,
            "action": action,
            "payload": payload,
            "prev_hash": prev_hash,
            "entry_hash": entry_hash,
        }

        self._broadcast(entry)
        return entry

    def verify_chain(self) -> bool:
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute(
            "SELECT timestamp, category, actor, action, payload, prev_hash, entry_hash FROM audit_logs ORDER BY id"
        ).fetchall()
        conn.close()

        expected_prev = "0" * 64
        for row in rows:
            timestamp, category, actor, action, payload, prev_hash, entry_hash = row
            if prev_hash != expected_prev:
                return False
            computed = _compute_hash(timestamp, category, actor, action, payload, prev_hash)
            if computed != entry_hash:
                return False
            expected_prev = entry_hash
        return True

    def get_entries(self, limit: int = 100, category: AuditCategory | None = None) -> list[dict]:
        conn = sqlite3.connect(self._db_path)
        if category:
            rows = conn.execute(
                "SELECT timestamp, category, actor, action, payload, prev_hash, entry_hash "
                "FROM audit_logs WHERE category = ? ORDER BY id DESC LIMIT ?",
                (category.value, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT timestamp, category, actor, action, payload, prev_hash, entry_hash "
                "FROM audit_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        conn.close()

        return [
            {
                "timestamp": r[0],
                "category": r[1],
                "actor": r[2],
                "action": r[3],
                "payload": json.loads(r[4]),
                "prev_hash": r[5],
                "entry_hash": r[6],
            }
            for r in rows
        ]

    def subscribe(self, ws: WebSocket):
        self._subscribers.append(ws)

    def unsubscribe(self, ws: WebSocket):
        self._subscribers = [s for s in self._subscribers if s is not ws]

    def _broadcast(self, entry: dict):
        import asyncio

        for ws in list(self._subscribers):
            try:
                asyncio.get_event_loop().create_task(
                    ws.send_text(json.dumps({"type": "AUDIT_ENTRY", "data": entry}))
                )
            except Exception:
                self.unsubscribe(ws)


ledger = AuditLedger()


@router.websocket("/admin/audit-trail")
async def audit_trail_ws(websocket: WebSocket):
    await websocket.accept()
    ledger.subscribe(websocket)

    history = ledger.get_entries(limit=50)
    await websocket.send_text(json.dumps({"type": "AUDIT_HISTORY", "data": list(reversed(history))}))

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ledger.unsubscribe(websocket)
