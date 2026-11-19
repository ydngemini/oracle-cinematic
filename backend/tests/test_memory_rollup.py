"""AI32-02 / AI32-07: the memory roll-up deletes history only for a real
summary, summarizes outside any transaction, and commits only if nothing
moved underneath it."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import memory_core.session_manager as sm

CTX = SimpleNamespace(tenant_id="00000000-0000-0000-0000-0000000000aa", agent_id="agent-1")


@pytest.fixture
def world(monkeypatch):
    long = "fact " * 400  # 2,000 characters a turn: facts sit far past any 1,500-character cut
    state = {"rows": [{"id": i, "role": "user", "content": f"{i}:{long}", "token_estimate": 500,
                       "created_at": i} for i in range(1, 7)],
             "prior": "Prefers duplexes.", "executed": [], "stored": [], "depth": 0}

    class Conn:
        async def fetch(self, sql, *args):
            if "FROM user_interactions" in sql:
                return [dict(r) for r in state["rows"]]
            return [{"profile_summary": state["prior"]}] if state["prior"] is not None else []

        async def fetchval(self, sql, *args):
            return sum(1 for r in state["rows"] if r["id"] in args[0])

        async def execute(self, sql, *args):
            state["executed"].append(sql)
            if sql.startswith("DELETE"):
                state["rows"] = [r for r in state["rows"] if r["id"] not in args[0]]

    @asynccontextmanager
    async def tenant_tx(ctx):
        state["depth"] += 1
        try:
            yield Conn()
        finally:
            state["depth"] -= 1

    async def open_rows(conn, rows, spec):
        return rows

    async def store(self, conn, user_id, summary):
        state["stored"].append(summary)

    import sealed_text
    from db import connection

    monkeypatch.setattr(connection, "tenant_tx", tenant_tx)
    monkeypatch.setattr(sealed_text, "open_rows", open_rows)
    monkeypatch.setattr(sm.SessionManager, "store_profile_summary", store)
    return state


def _rollup(summarizer):
    return asyncio.run(sm.SessionManager(CTX, summarizer=summarizer, token_threshold=10,
                                         keep_recent=2).summarize_rolling_memory("agent-1"))


def test_an_unavailable_summarizer_deletes_nothing(world):
    async def down(transcript, prior):
        assert len(transcript) > 5000
        raise sm.SummaryUnavailable("URLError")

    assert _rollup(down) is None
    assert len(world["rows"]) == 6 and world["stored"] == []
    assert not any(q.startswith("DELETE") for q in world["executed"])


def test_the_default_summarizer_reports_an_outage_instead_of_truncating(monkeypatch):
    import urllib.request

    def refuse(*_a, **_kw):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(sm.SummaryUnavailable):
        asyncio.run(sm._local_llm_summarize("[user] " + "x" * 5000, "prior"))


def test_a_summary_is_made_outside_any_transaction_and_replaces_only_the_older_rows(world):
    async def summarize(transcript, prior):
        assert world["depth"] == 0, "the network call must not hold a transaction"
        assert prior == "Prefers duplexes."
        return "  Prefers duplexes; closes fast.  "

    assert _rollup(summarize) == "Prefers duplexes; closes fast."
    assert [r["id"] for r in world["rows"]] == [5, 6] and world["stored"] == ["Prefers duplexes; closes fast."]


def test_history_or_summary_that_moved_meanwhile_is_left_alone(world):
    async def concurrent_rollup(transcript, prior):
        world["rows"] = world["rows"][1:]  # another roll-up already removed a row
        return "summary"

    assert _rollup(concurrent_rollup) is None
    assert len(world["rows"]) == 5 and world["stored"] == []

    async def summary_changed(transcript, prior):
        world["prior"] = "Edited meanwhile."
        return "summary"

    assert _rollup(summary_changed) is None
    assert len(world["rows"]) == 5 and world["stored"] == []


def test_an_empty_summary_deletes_nothing(world):
    async def blank(transcript, prior):
        return "   "

    assert _rollup(blank) is None and len(world["rows"]) == 6
