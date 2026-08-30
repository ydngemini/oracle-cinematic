"""Usage metering — the guards that keep a billing ledger trustworthy.

Everything here protects one of two properties: usage is never invented
(unknown metrics, negative quantities, double counting), and usage is never
lost because Stripe was unreachable.
"""

from __future__ import annotations

import asyncio
import functools

import pytest

import billing_usage
from billing_usage import METRICS, metering_configured, record_usage
from tenancy import Role, TenantContext

TENANT_ID = "11111111-1111-4111-8111-111111111111"


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def _ctx():
    return TenantContext(agent_id="test", tenant_id=TENANT_ID, role=Role.BROKER_OWNER)


class _Savepoint:
    """asyncpg's nested transaction() — a SAVEPOINT, released or rolled back."""

    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.savepoints.append("enter")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.savepoints.append("rollback" if exc_type else "release")
        return False  # never swallow — record_usage's own except decides


class _Conn:
    def __init__(self, sink):
        self.sink = sink
        self.savepoints: list[str] = []

    def transaction(self):
        return _Savepoint(self)

    async def fetchrow(self, sql, *args):
        self.sink.append(args)
        return {"id": "usage-1"}

    async def execute(self, *a, **k):
        return None


@sync
async def test_unknown_metrics_are_refused_not_recorded():
    """The Python set mirrors the CHECK in 0067 so a typo fails at the call site
    rather than as a constraint-violation 500 several frames away."""
    sink: list = []
    assert await record_usage(
        _ctx(), metric="leads_engaged",  # note the plural — not a real metric
        idempotency_key="k1", conn=_Conn(sink),
    ) is False
    assert sink == [], "an unknown metric must not reach the database"


@sync
async def test_negative_quantities_are_refused():
    sink: list = []
    assert await record_usage(
        _ctx(), metric="lead_engaged", quantity=-1,
        idempotency_key="k1", conn=_Conn(sink),
    ) is False
    assert sink == []


@sync
async def test_a_valid_metric_is_written_with_its_idempotency_key():
    sink: list = []
    assert await record_usage(
        _ctx(), metric="lead_engaged", quantity=1,
        idempotency_key="lead-engaged:evt-1", conn=_Conn(sink),
    ) is True
    args = sink[0]
    assert args[0] == TENANT_ID
    assert args[1] == "lead_engaged"
    assert args[4] == "lead-engaged:evt-1"


@sync
async def test_over_long_idempotency_keys_are_truncated_to_the_column_width():
    """0067 caps the key at 240 chars. Truncating here beats a 500 on a value
    the caller derived from user-controlled data."""
    sink: list = []
    await record_usage(
        _ctx(), metric="media_capture", idempotency_key="x" * 400, conn=_Conn(sink),
    )
    assert len(sink[0][4]) == 240


@sync
async def test_a_database_failure_never_propagates_to_the_caller():
    """Every call site is a CRM action whose success is independent of whether
    we managed to record that it happened."""

    class _Broken(_Conn):
        async def fetchrow(self, *a, **k):
            raise RuntimeError("connection reset")

    assert await record_usage(
        _ctx(), metric="lead_engaged", idempotency_key="k1", conn=_Broken([]),
    ) is False


@sync
async def test_a_failed_insert_is_contained_in_a_savepoint():
    """Swallowing the exception is not enough when the caller handed us its own
    in-flight transaction: in Postgres a failed statement aborts the WHOLE
    transaction, so the caller's next statement and its COMMIT would fail too —
    a metering hiccup would sink the media upload that triggered it. The insert
    therefore runs inside a SAVEPOINT that rolls back on its own.
    """

    class _Broken(_Conn):
        async def fetchrow(self, *a, **k):
            raise RuntimeError("relation \"billing_usage_events\" does not exist")

    conn = _Broken([])
    assert await record_usage(
        _ctx(), metric="lead_engaged", idempotency_key="k1", conn=conn,
    ) is False
    assert conn.savepoints == ["enter", "rollback"]


@sync
async def test_a_successful_insert_releases_its_savepoint():
    conn = _Conn([])
    assert await record_usage(
        _ctx(), metric="lead_engaged", idempotency_key="k1", conn=conn,
    ) is True
    assert conn.savepoints == ["enter", "release"]


@sync
async def test_drain_is_a_no_op_when_no_metered_price_is_configured(monkeypatch):
    """Unset STRIPE_METERED_PRICE_ID is a supported steady state: meter locally,
    bill flat. It must not be an error path."""
    monkeypatch.setattr(billing_usage, "STRIPE_METERED_PRICE_ID", "")
    result = await billing_usage.drain_usage_to_stripe(_ctx())
    assert result == {"state": "unconfigured", "reported": 0}


@sync
async def test_drain_stops_cleanly_when_the_tenant_has_no_stripe_customer(monkeypatch):
    monkeypatch.setattr(billing_usage, "STRIPE_METERED_PRICE_ID", "price_metered_123")

    async def _no_customer(ctx):
        return None

    monkeypatch.setattr(billing_usage, "_stripe_customer_for", _no_customer)
    result = await billing_usage.drain_usage_to_stripe(_ctx())
    # Rows stay unreported rather than being dropped or marked reported.
    assert result == {"state": "no_customer", "reported": 0}


def test_metering_configured_reflects_the_env(monkeypatch):
    monkeypatch.setattr(billing_usage, "STRIPE_METERED_PRICE_ID", "")
    assert metering_configured() is False
    monkeypatch.setattr(billing_usage, "STRIPE_METERED_PRICE_ID", "price_x")
    assert metering_configured() is True


def test_the_metric_vocabulary_matches_the_migration_check():
    # Keep this list in step with the CHECK constraint — 0067 created it, 0084
    # widened it for the token metrics. If they drift, the first symptom is a
    # production 500 on an INSERT that passed local validation.
    assert METRICS == {
        "lead_engaged", "ai_voice_minute", "transaction_closed", "media_capture",
        "ai_prompt_tokens", "ai_completion_tokens",
    }


def test_only_metrics_with_a_stripe_meter_are_drained():
    """The other three accrue locally until a pricing decision is made. Draining
    a metric with no configured meter would push events Stripe cannot price."""
    assert billing_usage._STRIPE_REPORTED <= METRICS
    assert billing_usage._STRIPE_REPORTED == {"lead_engaged"}


# ---------------------------------------------------------------------------
# The sweep — what actually gets usage to Stripe on a timer.
# ---------------------------------------------------------------------------

OTHER_TENANT = "22222222-2222-4222-8222-222222222222"


class _FetchConn:
    """Records every statement it is handed and replays canned rows."""

    def __init__(self, rows, calls):
        self._rows = rows
        self.calls = calls

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._rows

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self._rows[0] if self._rows else None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def _fake_tx(rows, calls):
    class _Tx:
        def __init__(self, ctx):
            self.ctx = ctx

        async def __aenter__(self):
            return _FetchConn(rows, calls)

        async def __aexit__(self, *exc):
            return False

    return _Tx


@sync
async def test_the_sweep_is_a_no_op_when_metering_is_unconfigured(monkeypatch):
    """Registered unconditionally in the scheduler, so the unconfigured case must
    cost nothing — not even the cross-tenant scan."""
    monkeypatch.setattr(billing_usage, "STRIPE_METERED_PRICE_ID", "")

    def _boom(ctx):
        raise AssertionError("unconfigured sweep must not touch the database")

    monkeypatch.setattr(billing_usage, "tenant_tx", _boom)
    assert await billing_usage.drain_all_tenants() == {
        "state": "unconfigured", "tenants": 0, "reported": 0,
    }


@sync
async def test_the_sweep_drains_each_pending_tenant_under_its_own_context(monkeypatch):
    """The cross-tenant scan runs as platform admin; the per-tenant drain must be
    re-scoped to that tenant, or usage is metered against the wrong customer."""
    monkeypatch.setattr(billing_usage, "STRIPE_METERED_PRICE_ID", "price_metered_123")
    calls: list = []
    monkeypatch.setattr(
        billing_usage,
        "tenant_tx",
        _fake_tx([{"tenant_id": TENANT_ID, "pending": 3},
                  {"tenant_id": OTHER_TENANT, "pending": 1}], calls),
    )

    seen: list[str] = []

    async def _drain(ctx, *, limit=200):
        seen.append(ctx.tenant_id)
        return {"state": "ok", "reported": 2, "considered": 2}

    monkeypatch.setattr(billing_usage, "drain_usage_to_stripe", _drain)

    result = await billing_usage.drain_all_tenants()
    assert seen == [TENANT_ID, OTHER_TENANT]
    assert result["tenants"] == 2
    assert result["reported"] == 4
    assert result["by_state"] == {"ok": 2}


@sync
async def test_one_failing_tenant_does_not_abort_the_sweep(monkeypatch):
    """A single tenant with a broken Stripe customer must not strand every other
    tenant's usage behind it until the next tick."""
    monkeypatch.setattr(billing_usage, "STRIPE_METERED_PRICE_ID", "price_metered_123")
    calls: list = []
    monkeypatch.setattr(
        billing_usage,
        "tenant_tx",
        _fake_tx([{"tenant_id": TENANT_ID, "pending": 1},
                  {"tenant_id": OTHER_TENANT, "pending": 1}], calls),
    )

    async def _drain(ctx, *, limit=200):
        if ctx.tenant_id == TENANT_ID:
            raise RuntimeError("stripe exploded")
        return {"state": "ok", "reported": 5, "considered": 5}

    monkeypatch.setattr(billing_usage, "drain_usage_to_stripe", _drain)

    result = await billing_usage.drain_all_tenants()
    assert result["reported"] == 5
    assert result["tenants"] == 1
    assert result["by_state"] == {"error": 1, "ok": 1}


@sync
async def test_the_drain_scopes_every_statement_to_the_tenant_explicitly(monkeypatch):
    """Regression guard for a cross-tenant billing leak.

    The sweep calls this under a PLATFORM_ADMIN context, and the 0067 RLS policy
    is ``app_is_platform_admin() OR tenant_id=app_current_tenant()`` — wide open
    under that role. If the drain leans on RLS alone it will pull every tenant's
    rows and meter them against whichever customer it looked up first.
    """
    monkeypatch.setattr(billing_usage, "STRIPE_METERED_PRICE_ID", "price_metered_123")
    calls: list = []
    monkeypatch.setattr(billing_usage, "tenant_tx", _fake_tx([], calls))

    async def _customer(ctx):
        return "cus_123"

    monkeypatch.setattr(billing_usage, "_stripe_customer_for", _customer)

    await billing_usage.drain_usage_to_stripe(_ctx())

    select_sql, select_args = calls[0]
    assert "tenant_id=$3::uuid" in select_sql
    assert TENANT_ID in select_args
