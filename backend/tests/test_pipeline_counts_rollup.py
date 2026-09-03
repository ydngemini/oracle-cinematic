"""`get_team_pipeline` must not count `leads`.

Migration 0038 exists because counting that table directly is a multi-second
scan on any tenant holding the harvested corpus. This tool did it anyway, and
the cost was not a slow answer but no answer at all: 15.3s of an 8.6M-row
aggregate, then asyncpg's 30s command_timeout raising a bare TimeoutError that
killed the whole chat turn with no reason attached.

Measured on the local corpus, before and after: 15,254ms -> 0.371ms.
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
import re

import ai_chat_store
from tenancy import Role, TenantContext

MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations"
CTX = TenantContext(
    agent_id="a@t.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)


class _Conn:
    """Answers each query by shape, and records what it was asked."""

    def __init__(self):
        self.queries: list[str] = []
        self.args: list[tuple] = []

    async def fetch(self, query, *args):
        self.queries.append(" ".join(query.split()))
        self.args.append(args)
        if "lead_pipeline_counts" in query:
            return [
                {"stage": "draft", "deal_count": 8_636_058},
                {"stage": "under_contract", "deal_count": 2},
            ]
        return [{"stage": "under_contract", "expiring": 1}]


def _run_tool():
    conn = _Conn()
    result = asyncio.run(ai_chat_store._read_team_or_providers(conn, CTX, "get_team_pipeline"))
    return conn, result


def test_stage_counts_come_from_the_rollup_not_a_scan():
    conn, result = _run_tool()
    assert result["ok"] is True
    stage_query = next(q for q in conn.queries if "lead_pipeline_counts" in q)
    assert "FROM leads" not in stage_query, (
        "stage counts must come from the rollup; counting leads is the 15s scan"
    )
    assert "sum(row_count)" in stage_query, (
        "the rollup's grain is finer than the question, so it must be summed"
    )


def test_the_time_dependent_half_stays_live_and_indexable():
    """A deadline crossing "within 14 days" changes no row, so no trigger can
    maintain it. It has to be a live query — and it has to match the index."""
    conn, _ = _run_tool()
    expiring = next(q for q in conn.queries if "FROM leads" in q)
    assert "contract_expires_at >= now()" in expiring
    assert "contract_expires_at <= now() + interval '14 days'" in expiring
    assert "tenant_id=$1::uuid" in expiring, (
        "idx_leads_tenant_contract_window leads on tenant_id; without it the "
        "0.7ms range scan is a sequential one"
    )


def test_both_halves_are_scoped_to_one_tenant():
    """This tool answers "MY team's pipeline". A platform admin must see one
    tenant, not the sum of every tenant — deliberate business scope."""
    conn, _ = _run_tool()
    assert len(conn.queries) == 2, f"expected exactly two queries, got {conn.queries}"
    for query, args in zip(conn.queries, conn.args):
        assert "tenant_id=$1::uuid" in query, query
        assert args == (CTX.tenant_id,)
    source = inspect.getsource(ai_chat_store._read_team_or_providers)
    assert "business scope" in source, "the exception must say why it exists"


def test_the_two_halves_are_joined_without_losing_a_stage():
    _, result = _run_tool()
    stages = {row["stage"]: row for row in result["stages"]}
    assert stages["draft"]["deal_count"] == 8_636_058
    assert stages["draft"]["expiring_within_14_days"] == 0, (
        "a stage with no expiring deals must report zero, not be dropped"
    )
    assert stages["under_contract"]["expiring_within_14_days"] == 1


def test_the_rollup_grain_includes_dossier_status():
    migration = (MIGRATIONS / "0099_pipeline_counts_by_stage.sql").read_text()
    assert "PRIMARY KEY (tenant_id, state, dossier_status)" in migration

    # The trigger must fire on the new column too, or the rollup drifts the
    # first time a lead changes stage.
    trigger = migration.split("CREATE TRIGGER trg_leads_pipeline_counts")[1]
    assert "UPDATE OF tenant_id, state, dossier_status" in trigger

    # And it must be installed before the rebuild, so concurrent writers queue
    # rather than land in the gap — the ordering 0038 documented.
    # Anchor on the REBUILD specifically: the trigger function body contains
    # the same INSERT, and matching that one would compare the wrong pair.
    rebuild = "SELECT tenant_id, state, dossier_status, COUNT(*), now()"
    assert rebuild in migration
    assert migration.index("CREATE TRIGGER trg_leads_pipeline_counts") < migration.index(rebuild), \
        "the trigger must exist before the snapshot is taken"


def test_refining_the_grain_keeps_every_existing_reader_correct():
    """(tenant_id, state) became (tenant_id, state, dossier_status). That is
    only safe because every existing consumer aggregates with sum()."""
    backend = pathlib.Path(__file__).resolve().parents[1]
    readers = []
    for path in backend.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "lead_pipeline_counts" not in text:
            continue
        for match in re.finditer(r"[^\n]*lead_pipeline_counts[^\n]*", text):
            readers.append((path.name, match.group(0)))
    assert readers, "no readers found — this test would pass vacuously"
    for name, line in readers:
        if "FROM lead_pipeline_counts" not in line:
            continue
        # The SELECT list is on this line or just above it; both forms in the
        # tree today put sum()/count() with the FROM. A bare column read at the
        # old grain would double-count under the new one.
        assert "sum(" in line.lower() or "select" not in line.lower(), (
            f"{name} reads the rollup without summing: {line.strip()}"
        )
