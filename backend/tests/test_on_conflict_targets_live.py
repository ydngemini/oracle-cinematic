"""Every `ON CONFLICT (...)` in the code must have an arbiter PostgreSQL can infer.

Skipped without ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL built from every
migration — the CI "database security" job).

Found on staging 2026-10-06: approved calls wrote their acknowledgement with
`ON CONFLICT (command_id) DO NOTHING`, but the only unique index on
live_call_sessions.command_id is PARTIAL (`WHERE command_id IS NOT NULL`). A
partial index is inferred only when the statement repeats its predicate, so
every approved call raised "there is no unique or exclusion constraint matching
the ON CONFLICT specification" after the phone had already rung — the command
fell into reconciliation, and live_call_sessions had 0 rows on staging, ever.
The unit suite runs on fakes, which accept any SQL. This test reads the real
catalog and every ON CONFLICT target in the source.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Iterable

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
BACKEND = Path(__file__).resolve().parents[1]

_STATEMENT = re.compile(
    r"INSERT\s+INTO\s+(?:public\.)?([a-z_][a-z0-9_]*)\b"
    r"(?:(?!INSERT\s+INTO).){0,6000}?"
    r"ON\s+CONFLICT\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*(WHERE\b(?:(?!\bDO\b).)*)?\s*DO\b",
    re.IGNORECASE | re.DOTALL,
)

CATALOG_SQL = """
SELECT t.relname AS tbl,
       pg_get_expr(ix.indpred, ix.indrelid) AS pred,
       ix.indexprs IS NOT NULL AS has_expr,
       ARRAY(SELECT a.attname FROM unnest(ix.indkey) WITH ORDINALITY k(attnum, n)
             JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
             ORDER BY k.n) AS cols
  FROM pg_index ix
  JOIN pg_class t ON t.oid = ix.indrelid
  JOIN pg_namespace ns ON ns.oid = t.relnamespace
 WHERE ix.indisunique AND ns.nspname = 'public'
"""


def source_files() -> Iterable[Path]:
    for path in BACKEND.rglob("*.py"):
        parts = set(path.relative_to(BACKEND).parts)
        if parts & {"tests", "venv", "ml_forge", "node_modules", "__pycache__"}:
            continue
        yield path


FUNCTIONS_SQL = """
SELECT p.proname, p.prosrc FROM pg_proc p JOIN pg_namespace ns ON ns.oid = p.pronamespace
 WHERE ns.nspname = 'public' AND p.prosrc ILIKE '%on conflict%'
"""


def statements(functions: Iterable[tuple[str, str]] = ()) -> list[tuple[str, str, frozenset, bool]]:
    """(location, table, conflict columns, has WHERE) for every targeted ON CONFLICT
    in the Python source and in the database's LIVE function bodies (`functions`
    = (name, source) from pg_proc). Migration files are not scanned: a later
    migration redefines a function, so an old file's body is not what runs."""
    found = []
    texts = [(str(p.relative_to(BACKEND)), p.read_text(errors="replace")) for p in source_files()]
    texts += [(f"function {name}()", body) for name, body in functions]
    for where, text in texts:
        for match in _STATEMENT.finditer(text):
            table, target = match.group(1).lower(), match.group(2)
            cols = frozenset(c.strip().strip('"').lower() for c in target.split(",") if c.strip())
            line = text.count("\n", 0, match.start()) + 1
            found.append((f"{where}:{line}", table, cols, bool(match.group(3))))
    return found


def audit(indexes: list[dict], stmts: list[tuple[str, str, frozenset, bool]]) -> tuple[list[str], list[str]]:
    """(failures, skipped). A target needs a unique index on exactly those
    columns that is either total, or partial with the predicate repeated."""
    by_table: dict[str, list[dict]] = {}
    for ix in indexes:
        by_table.setdefault(ix["tbl"], []).append(ix)
    failures, skipped = [], []
    for where_at, table, cols, has_where in stmts:
        if any("(" in c for c in cols):
            skipped.append(f"{where_at}: expression target on {table}")
            continue
        candidates = [ix for ix in by_table.get(table, [])
                      if not ix["has_expr"] and frozenset(ix["cols"]) == cols]
        if table not in by_table:
            skipped.append(f"{where_at}: {table} has no unique index in this database")
            continue
        if any(ix["pred"] is None for ix in candidates):
            continue
        if candidates and has_where:
            continue
        if candidates:
            failures.append(f"{where_at}: ON CONFLICT ({', '.join(sorted(cols))}) on {table} "
                            f"must repeat the partial index predicate: WHERE {candidates[0]['pred']}")
        else:
            failures.append(f"{where_at}: no unique index on {table}({', '.join(sorted(cols))})")
    return failures, skipped


def test_the_scanner_finds_the_statements_it_must_check():
    found = statements()
    assert len(found) >= 40, len(found)
    assert any(t == "live_call_sessions" and c == frozenset({"command_id"}) for _, t, c, _ in found)


def test_audit_rules():
    partial = {"tbl": "s", "pred": "(command_id IS NOT NULL)", "has_expr": False, "cols": ["command_id"]}
    total = {"tbl": "t", "pred": None, "has_expr": False, "cols": ["tenant_id", "parcel_id"]}
    stmts = [("a:1", "s", frozenset({"command_id"}), False),
             ("a:2", "s", frozenset({"command_id"}), True),
             ("a:3", "t", frozenset({"parcel_id", "tenant_id"}), False),
             ("a:4", "t", frozenset({"parcel_id"}), False)]
    failures, _ = audit([partial, total], stmts)
    assert [f.split(":")[0] + ":" + f.split(":")[1] for f in failures] == ["a:1", "a:4"]


@pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")
def test_every_on_conflict_target_has_an_inferable_arbiter():
    async def catalog():
        import asyncpg

        conn = await asyncpg.connect(DSN)
        try:
            return ([dict(r) for r in await conn.fetch(CATALOG_SQL)],
                    [(r["proname"], r["prosrc"]) for r in await conn.fetch(FUNCTIONS_SQL)])
        finally:
            await conn.close()

    indexes, functions = asyncio.run(catalog())
    failures, _skipped = audit(indexes, statements(functions))
    assert not failures, "\n".join(failures)
