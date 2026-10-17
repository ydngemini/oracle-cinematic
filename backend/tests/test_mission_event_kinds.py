"""Every mission_events kind the code writes is one the database accepts
(L08-02). executor._journal swallows a refused insert by design, so a kind
missing from mission_events_kind_check fails silently at run time: 0100
never listed 'action_deferred' (written since L05-03) and every deferral
journal row was lost. This compares the code with the newest CHECK."""

from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
WRITERS = [BACKEND / "missions_api.py", *sorted((BACKEND / "missions").glob("*.py"))]


def _kinds_in_code() -> set[str]:
    kinds: set[str] = set()
    for path in WRITERS:
        src = path.read_text(encoding="utf-8")
        # _journal(ctx, mission_id, "kind", ...) and missions_api's journal helper
        kinds |= set(re.findall(r"_?journal\(\s*[^,()]+,\s*[^,()]+,\s*\"([a-z_]+)\"", src))
        # literal kinds in INSERT ... VALUES (..., 'kind', ...) / SELECT ..., 'kind'
        for stmt in re.findall(r"INSERT INTO mission_events.*?\"\"\"", src, re.S):
            kinds |= set(re.findall(r"'([a-z_]+)'", stmt))
    return kinds


def _kinds_in_newest_check() -> set[str]:
    newest = None
    for path in sorted((BACKEND / "db" / "migrations").glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        m = re.search(r"ADD CONSTRAINT mission_events_kind_check CHECK \(kind IN \((.*?)\)\)", sql, re.S)
        if m:
            newest = m.group(1)
    assert newest, "no migration defines mission_events_kind_check"
    return set(re.findall(r"'([a-z_]+)'", newest))


def test_every_journalled_kind_is_allowed_by_the_database():
    code = _kinds_in_code()
    assert {"action_deferred", "action_withheld", "strategy_changed", "digest_sent"} <= code, code
    missing = code - _kinds_in_newest_check()
    assert not missing, f"journalled but refused by mission_events_kind_check: {sorted(missing)}"
