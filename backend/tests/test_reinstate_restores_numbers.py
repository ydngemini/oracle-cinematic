"""Reinstating a member switches back on the numbers a closure withdrawal left
off because of them (L17-59, owner instruction 2026-11-27, 'phone numbers back
on').

privacy_lifecycle.withdraw_closure leaves off the numbers held by members the
owner suspended or offboarded since the closure and records them on the
closure operation's result (skipped_routes). _reinstate used to touch only
users and team_memberships, so those numbers stayed off for good. The SQL that
picks the candidates is proved against real PostgreSQL in
test_reinstate_restores_numbers_live; here the fake returns the candidates and
the Python decisions around them are pinned.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import asyncpg
import pytest
from fastapi import HTTPException

import brokerage_onboarding as bo
from tenancy import Role, TenantContext

T = "00000000-0000-0000-0000-0000000000cc"
OWNER = TenantContext(agent_id="owner@x.test", tenant_id=T, role=Role.BROKER_OWNER)
K_ID = "11111111-1111-1111-1111-111111111111"
K_AGENT = "Kim@X.test"
WHY = bo.MemberStatusChange(reason="back from leave")


def _cand(table, rid, *, offboarded=False, disconnected=False):
    return {"route_table": table, "id": rid, "offboarded": offboarded, "disconnected": disconnected}


class _Conn:
    """The reinstated member's users row, the candidates the left-off query
    returns, the brokerage's lifecycle state, and per route id what switching
    it on does: 'on' (default), 'conflict' (a unique index refuses it) or
    'gone' (the guarded UPDATE matches nothing)."""

    def __init__(self, candidates=(), lifecycle="active", outcome=None, found=True):
        self.candidates, self.lifecycle = list(candidates), lifecycle
        self.outcome, self.found = dict(outcome or {}), found
        self.switch_calls, self.left_off_args, self.lifecycle_reads = [], None, 0
        self.savepoints = {"opened": 0, "rolled_back": 0}
        self.in_savepoint = False
        self.order, self.consumed = [], None

    async def fetchrow(self, sql, *args):
        assert sql.lstrip().startswith("UPDATE users SET is_active = true"), sql
        self.order.append("users")
        return {"id": K_ID, "agent_id": K_AGENT, "role": "agent"} if self.found else None

    async def execute(self, sql, *args):
        if sql is bo._CONSUME_SKIPS_SQL:
            assert self.consumed is None, "consumed once per reinstatement"
            assert args[0] == T
            self.consumed = list(args[1])
            return "UPDATE 1"
        assert sql.lstrip().startswith("UPDATE team_memberships SET status = 'active'"), sql
        return "UPDATE 1"

    async def fetch(self, sql, *args):
        assert sql is bo._LEFT_OFF_ROUTES_SQL, sql
        self.left_off_args = args
        self.order.append("candidates")
        return self.candidates

    async def fetchval(self, sql, *args):
        if sql.startswith("SELECT lifecycle_state FROM tenants"):
            # FOR UPDATE: users_seat_limit takes this row FOR UPDATE on the
            # activation below; a share lock here deadlocked two concurrent
            # reinstatements (re-review of PR #190)
            assert sql.rstrip().endswith("FOR UPDATE"), sql
            self.order.append("tenants")
            self.lifecycle_reads += 1
            return self.lifecycle
        table = next(t for t, q in bo._SWITCH_ROUTE_ON_SQL.items() if q == sql)
        assert self.in_savepoint, "each route is switched on in its own savepoint"
        assert args == (T, args[1], K_AGENT), args
        self.switch_calls.append((table, args[1]))
        what = self.outcome.get(args[1], "on")
        if what == "conflict":
            raise asyncpg.UniqueViolationError(
                'duplicate key value violates unique constraint "uq_telephony_routes_active_did"')
        return None if what == "gone" else args[1]

    @asynccontextmanager
    async def _savepoint(self):
        self.savepoints["opened"] += 1
        self.in_savepoint = True
        try:
            yield
        except BaseException:
            self.savepoints["rolled_back"] += 1
            raise
        finally:
            self.in_savepoint = False

    def transaction(self):
        return self._savepoint()


@pytest.fixture
def run(monkeypatch):
    audits = []

    async def record(ctx, action, row, reason, extra=None):
        audits.append({"action": action, "target": str(row["id"]), "reason": reason, "extra": extra})

    monkeypatch.setattr(bo, "_record_member_change", record)

    def go(conn):
        @asynccontextmanager
        async def tx(_ctx):
            yield conn

        monkeypatch.setattr(bo, "tenant_tx", tx)
        return asyncio.run(bo._reinstate(OWNER, K_ID, WHY))

    go.audits = audits
    return go


def test_left_off_numbers_come_back_on_with_the_member(run):
    conn = _Conn([_cand("telephony_routes", "tr-k"), _cand("messaging_routes", "mr-k")])
    out = run(conn)
    assert out == {"id": K_ID, "agent_id": K_AGENT, "status": "active",
                   "restored_routes": ["tr-k", "mr-k"], "routes_not_restored": []}
    assert conn.switch_calls == [("telephony_routes", "tr-k"), ("messaging_routes", "mr-k")]
    # the candidates are this brokerage's, this member's (agent id and user id)
    assert conn.left_off_args == (T, K_AGENT, K_ID)
    # counts only on the audit event, never the route ids
    [audit] = run.audits
    assert audit["action"] == "team.member.reinstated"
    assert audit["extra"] == {"restored_routes": 2, "routes_not_restored": 0}


def test_a_plain_suspension_has_nothing_to_restore(run):
    # suspend_member never switches a route off (it clears forwarding), so no
    # closure lists the member's numbers: nothing is read or switched.
    conn = _Conn([])
    out = run(conn)
    assert out["restored_routes"] == [] and out["routes_not_restored"] == []
    assert conn.switch_calls == [] and conn.savepoints["opened"] == 0 and conn.consumed is None
    assert run.audits[0]["extra"] == {"restored_routes": 0, "routes_not_restored": 0}


def test_a_parked_or_disconnected_number_stays_off_and_is_reported(run):
    conn = _Conn([_cand("telephony_routes", "tr-parked", offboarded=True),
                  _cand("messaging_routes", "mr-parked", offboarded=True),
                  _cand("messaging_routes", "mr-cut", disconnected=True),
                  _cand("telephony_routes", "tr-k")])
    out = run(conn)
    assert out["restored_routes"] == ["tr-k"]
    assert out["routes_not_restored"] == [
        {"id": "tr-parked", "kind": "call", "reason": "offboarded"},
        {"id": "mr-parked", "kind": "text", "reason": "offboarded"},
        {"id": "mr-cut", "kind": "text", "reason": "disconnected"}]
    assert conn.switch_calls == [("telephony_routes", "tr-k")]
    assert run.audits[0]["extra"] == {"restored_routes": 1, "routes_not_restored": 3}


def test_a_uniqueness_refusal_skips_that_number_never_the_reinstatement(run):
    conn = _Conn([_cand("telephony_routes", "tr-taken"), _cand("messaging_routes", "mr-k")],
                 outcome={"tr-taken": "conflict"})
    out = run(conn)
    assert out["status"] == "active"
    assert out["restored_routes"] == ["mr-k"]
    assert out["routes_not_restored"] == [{"id": "tr-taken", "kind": "call", "reason": "number_in_use"}]
    # each switch in its own savepoint; only the refused one rolled back, so
    # the reinstatement's transaction carries on
    assert conn.savepoints == {"opened": 2, "rolled_back": 1}


def test_a_route_changed_under_the_reinstatement_is_reported_not_claimed(run):
    conn = _Conn([_cand("telephony_routes", "tr-k")], outcome={"tr-k": "gone"})
    out = run(conn)
    assert out["restored_routes"] == []
    assert out["routes_not_restored"] == [{"id": "tr-k", "kind": "call", "reason": "changed"}]


@pytest.mark.parametrize("state", ["closing", "erasing"])
def test_a_closing_brokerage_keeps_its_numbers_off(run, state):
    # a new closure froze the brokerage: reinstating must not undo its freeze
    conn = _Conn([_cand("telephony_routes", "tr-k"), _cand("messaging_routes", "mr-k")], lifecycle=state)
    out = run(conn)
    assert out["restored_routes"] == [] and conn.switch_calls == []
    assert [r["reason"] for r in out["routes_not_restored"]] == ["brokerage_not_active"] * 2


def test_no_member_to_reinstate_still_404s(run):
    with pytest.raises(HTTPException) as err:
        run(_Conn([_cand("telephony_routes", "tr-k")], found=False))
    assert err.value.status_code == 404 and run.audits == []


def test_the_statements_select_and_switch_only_left_off_numbers():
    q = " ".join(bo._LEFT_OFF_ROUTES_SQL.split())
    for part in ("o.tenant_id = $1::uuid AND o.kind = 'closure'", "o.result->'skipped_routes'",
                 "WHERE NOT r.active AND lower(r.agent_id) = lower($2)",
                 "p.kind = 'offboard' AND p.subject_ref = $3", "p.requested_at > l.since",
                 "su->>'reason' = 'offboarded'", "FROM telephony_routes WHERE tenant_id = $1::uuid",
                 "FROM messaging_routes WHERE tenant_id = $1::uuid"):
        assert part in q, part
    for table, sql in bo._SWITCH_ROUTE_ON_SQL.items():
        s = " ".join(sql.split())
        assert s.startswith(f"UPDATE {table} SET active = true"), s
        for part in ("tenant_id = $1::uuid", "id = $2::uuid", "NOT active", "lower(agent_id) = lower($3)"):
            assert part in s, (table, part)
    assert "disconnected_at IS NULL" in bo._SWITCH_ROUTE_ON_SQL["messaging_routes"]


def test_the_tenants_row_is_locked_before_the_member_and_the_candidates(run):
    # review of PR #190: the tenants row locked first (FOR UPDATE), so a concurrent
    # withdrawal's skips are committed before the candidates are read (or
    # after this transaction), in withdraw_closure's lock order; also when
    # there turn out to be no candidates
    for cands in ([], [_cand("telephony_routes", "tr-k")]):
        conn = _Conn(cands)
        run(conn)
        assert conn.order[:3] == ["tenants", "users", "candidates"], conn.order
        assert conn.lifecycle_reads == 1


def test_every_decided_route_is_consumed_once_and_an_undecided_one_is_not(run):
    # one shot: what this reinstatement restored or refused for good is
    # consumed on the closures that listed it
    conn = _Conn([_cand("telephony_routes", "tr-k"), _cand("messaging_routes", "mr-parked", offboarded=True),
                  _cand("telephony_routes", "tr-taken"), _cand("messaging_routes", "mr-gone")],
                 outcome={"tr-taken": "conflict", "mr-gone": "gone"})
    run(conn)
    assert sorted(conn.consumed) == ["mr-gone", "mr-parked", "tr-k", "tr-taken"]
    # while the brokerage is closing nothing is decided, so nothing consumed
    closing = _Conn([_cand("telephony_routes", "tr-k")], lifecycle="closing")
    run(closing)
    assert closing.consumed is None


def test_a_consumed_route_is_never_a_candidate_again():
    q = " ".join(bo._LEFT_OFF_ROUTES_SQL.split())
    assert "o.result->'reinstate_consumed'" in q and "END) ? r.route_id" in q
    c = " ".join(bo._CONSUME_SKIPS_SQL.split())
    for part in ("o.tenant_id = $1::uuid AND o.kind = 'closure'", "'{reinstate_consumed}'",
                 "o.result->'skipped_routes' ?| $2::text[]"):
        assert part in c, part
