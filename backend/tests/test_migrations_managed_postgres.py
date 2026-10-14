"""Migrations must run as the managed-PostgreSQL admin, which is not a superuser.

Staging's PRE_DEPLOY job (DigitalOcean managed PostgreSQL, role doadmin) failed
on 0134 with 42501 "permission denied to set parameter app.record_scope": a
non-superuser cannot create a function with a SET clause for a custom
(unregistered) parameter. Local and CI databases migrate as a superuser, so
nothing caught it. This scans every migration for that construct; set
app.* values with set_config() inside the function body instead.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"

# `SET some.param = …` / `SET some.param TO …` as a statement or a function
# option. Plain `SET search_path` (a real parameter) and `SET LOCAL
# statement_timeout` are fine; a dotted name is a custom placeholder.
_CUSTOM_SET = re.compile(r"^\s*SET\s+(?!LOCAL\b|SESSION\b)([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\s*(=|TO\b)",
                         re.IGNORECASE | re.MULTILINE)


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def test_no_function_sets_a_custom_parameter():
    offenders = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        for m in _CUSTOM_SET.finditer(_strip_comments(path.read_text())):
            offenders.append(f"{path.name}: SET {m.group(1)}")
    assert not offenders, (
        "custom-parameter SET clauses need superuser on managed PostgreSQL; "
        f"use set_config() in the body: {offenders}")


def test_the_guard_catches_the_staging_failure():
    bad = "CREATE FUNCTION f() RETURNS int LANGUAGE sql\n    SET app.record_scope = 'brokerage' AS $$ SELECT 1 $$;"
    ok = "CREATE FUNCTION f() RETURNS int LANGUAGE sql\n    SET search_path = pg_catalog, public AS $$ SELECT 1 $$;"
    assert _CUSTOM_SET.search(bad) and not _CUSTOM_SET.search(ok)
