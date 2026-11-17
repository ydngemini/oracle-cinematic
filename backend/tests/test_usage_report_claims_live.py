"""The Stripe usage drain, run for real against PostgreSQL (audits 24.5, 24.6).

24.5: a send whose outcome is unknown (Stripe accepted it, then our
acknowledgement failed; or the connection dropped) is recorded before the
send, resent with the same identifier only inside Stripe's de-duplication
window, and quarantined for a person after it — never resent days later. Two
drain replicas never send the same row.
24.6: rows older than Stripe accepts are quarantined, not retried ahead of
current usage; rows nearing that age are counted for an alert.

Stripe is a fake that de-duplicates identifiers the way Stripe does inside its
window. Skipped unless ORACLE_LIVE_DB_ADMIN_DSN is set (see
test_privacy_lifecycle_live).
"""
from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from collections import Counter

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


@pytest.mark.parametrize("part", ["unknown_outcome", "resend_window", "expired", "replicas"])
def test_usage_reaches_stripe_at_most_once(monkeypatch, part):
    asyncio.run(_scenario(monkeypatch, part))


async def _scenario(monkeypatch, part):
    import asyncpg
    import stripe

    import billing_usage as bu
    from db import connection
    from tenancy import Role, TenantContext

    sent: list[str] = []            # every identifier handed to Stripe, in order
    on_meter: Counter = Counter()   # what Stripe actually billed
    behaviour: dict[str, str] = {}  # identifier -> how the next send goes

    def create(*, event_name, payload, identifier, timestamp):
        sent.append(identifier)
        mode = behaviour.pop(identifier, "ok")
        if mode == "connection":
            raise stripe.APIConnectionError("connection reset")
        if mode == "refused":
            raise stripe.InvalidRequestError("timestamp too far in the past", "timestamp")
        if mode == "duplicate":
            raise stripe.InvalidRequestError("already submitted", "identifier",
                                             code="duplicate_meter_event")
        if on_meter[identifier] == 0:  # inside the window Stripe keeps the first only
            on_meter[identifier] += 1
        return type("Event", (), {"identifier": identifier})()

    async def customer(_ctx):
        return "cus_live"

    monkeypatch.setattr(stripe.billing.MeterEvent, "create", create)
    monkeypatch.setattr(bu, "STRIPE_METERED_PRICE_ID", "price_metered_live")
    monkeypatch.setattr(bu, "_stripe_customer_for", customer)

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    ctx = TenantContext(agent_id="usage-drain", tenant_id=T, role=Role.PLATFORM_ADMIN)
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)

    async def usage(key, age="0 seconds", **cols):
        extra = "".join(f", {k}" for k in cols)
        vals = "".join(f", ${i + 4}" for i in range(len(cols)))
        await admin.execute(
            "INSERT INTO billing_usage_events (tenant_id, metric, quantity, occurred_at, idempotency_key,"
            " lifecycle_stage, source_table, source_id" + extra + ") VALUES ($1, 'lead_engaged', 1,"
            " now() - $3::text::interval, $2, 'delivered', 'command_executions', gen_random_uuid()" + vals + ")",
            T, f"{s}-{key}", age, *cols.values())
        return f"{s}-{key}"

    async def row(key):
        return await admin.fetchrow(
            "SELECT reported_at IS NOT NULL AS reported, report_state, report_attempts,"
            " report_uncertain_since IS NOT NULL AS uncertain, report_claimed_until, report_error"
            " FROM billing_usage_events WHERE tenant_id=$1 AND idempotency_key=$2", T, key)

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"u-{s}", f"U {s}")

        if part == "unknown_outcome":
            # 1. Stripe accepts, then our acknowledgement fails: the attempt was
            # recorded first, so the row stays uncertain, and the resend inside the
            # window is de-duplicated — billed once.
            a = await usage("acked-late")
            real_tx = bu.tenant_tx
            failed = {"n": 0}

            def flaky_tx(c):
                tx = real_tx(c)

                class _Tx:
                    async def __aenter__(self):
                        conn = await tx.__aenter__()

                        class _Conn:
                            def __getattr__(self, name):
                                return getattr(conn, name)

                            async def execute(self, sql, *args):
                                if "SET reported_at=now()" in sql and not failed["n"]:
                                    failed["n"] += 1
                                    raise ConnectionResetError("database went away")
                                return await conn.execute(sql, *args)
                        return _Conn()

                    async def __aexit__(self, *exc):
                        return await tx.__aexit__(*exc)
                return _Tx()

            monkeypatch.setattr(bu, "tenant_tx", flaky_tx)
            first = await bu.drain_usage_to_stripe(ctx)
            monkeypatch.setattr(bu, "tenant_tx", real_tx)
            assert first["reported"] == 0 and on_meter[a] == 1
            r = await row(a)
            assert not r["reported"] and r["uncertain"] and r["report_attempts"] == 1
            # the lease is still held: a drain right now must not send it
            await bu.drain_usage_to_stripe(ctx)
            assert sent.count(a) == 1
            await admin.execute("UPDATE billing_usage_events SET report_claimed_until = now() - interval '1 second'"
                                " WHERE tenant_id=$1 AND idempotency_key=$2", T, a)
            await bu.drain_usage_to_stripe(ctx)
            r = await row(a)
            assert r["reported"] and sent.count(a) == 2 and on_meter[a] == 1 and r["report_claimed_until"] is None

        if part == "resend_window":
            # 2. An unknown outcome older than the window is never resent: it is
            # quarantined as 'uncertain' for a person.
            b = await usage("stale-uncertain", report_uncertain_since=None, report_attempts=1)
            await admin.execute("UPDATE billing_usage_events SET report_uncertain_since = now() - interval '21 hours'"
                                " WHERE tenant_id=$1 AND idempotency_key=$2", T, b)
            result = await bu.drain_usage_to_stripe(ctx)
            r = await row(b)
            assert r["report_state"] == "uncertain" and not r["reported"] and b not in sent
            assert result["quarantined"]["uncertain"] == 1

            # A connection failure is an unknown outcome too: the uncertainty stays.
            c = await usage("conn-drop")
            behaviour[c] = "connection"
            await bu.drain_usage_to_stripe(ctx)
            r = await row(c)
            assert r["uncertain"] and not r["reported"] and r["report_claimed_until"] is None
            assert "connection reset" in r["report_error"]
            await bu.drain_usage_to_stripe(ctx)              # resent inside the window
            assert (await row(c))["reported"] and on_meter[c] == 1

            # A refusal of a never-sent row adds no uncertainty; a refusal of a
            # resend does not erase the first attempt's.
            d = await usage("refused")
            behaviour[d] = "refused"
            await bu.drain_usage_to_stripe(ctx)
            r = await row(d)
            assert not r["uncertain"] and not r["reported"] and r["report_attempts"] == 1
            await bu.drain_usage_to_stripe(ctx)
            assert (await row(d))["reported"]
            e = await usage("refused-after-drop")
            behaviour[e] = "connection"
            await bu.drain_usage_to_stripe(ctx)
            behaviour[e] = "refused"
            await bu.drain_usage_to_stripe(ctx)
            assert (await row(e))["uncertain"]
            # Stripe naming the identifier a duplicate proves the first send landed.
            behaviour[e] = "duplicate"
            await bu.drain_usage_to_stripe(ctx)
            assert (await row(e))["reported"]

        if part == "expired":
            # 3. (24.6) 36-day-old rows ahead of current usage: quarantined as
            # expired, never sent, and the recent rows drain within the limit.
            stale = [await usage(f"old-{i}", "36 days") for i in range(3)]
            recent = [await usage(f"new-{i}") for i in range(2)]
            aging = await usage("aging", "29 days")
            sent.clear()
            result = await bu.drain_usage_to_stripe(ctx, limit=3)
            assert result["quarantined"]["expired"] == 3
            assert not set(stale) & set(sent)
            assert set(recent) <= set(sent) and aging in sent
            for k in stale:
                assert (await row(k))["report_state"] == "expired"
            assert all([(await row(k))["reported"] for k in recent])
            late = await usage("aging-2", "29 days")
            behaviour[late] = "refused"
            assert (await bu.drain_usage_to_stripe(ctx))["aging"] == 1

        if part == "replicas":
            # 4. Two drain replicas at once: every row is sent exactly once.
            many = [await usage(f"race-{i}") for i in range(12)]
            sent.clear()
            both = await asyncio.gather(bu.drain_usage_to_stripe(ctx), bu.drain_usage_to_stripe(ctx))
            assert sorted(k for k in sent if k in many) == sorted(many)
            assert sum(x["reported"] for x in both) >= len(many)
            assert all([(await row(k))["reported"] for k in many])

        if part == "expired":
            # The sweep chooses tenants by rows still in play: this tenant now has
            # only quarantined rows, so it is not chosen (and its slot not wasted).
            # The per-tenant drain is replaced so other tenants' rows in this shared
            # database are never touched.
            await admin.execute("UPDATE billing_usage_events SET reported_at = now()"
                                " WHERE tenant_id=$1 AND report_state IS NULL", T)
            chosen = []

            async def record(tctx, **_k):
                chosen.append(str(tctx.tenant_id))
                return {"state": "ok", "reported": 0}

            monkeypatch.setattr(bu, "drain_usage_to_stripe", record)
            await bu.drain_all_tenants(tenant_limit=5000)
            assert T not in chosen
            await admin.execute("UPDATE billing_usage_events SET reported_at = NULL, report_state = NULL,"
                                " report_quarantined_at = NULL WHERE tenant_id=$1 AND idempotency_key=$2",
                                T, stale[0])
            await bu.drain_all_tenants(tenant_limit=5000)
            assert T in chosen
    finally:
        await admin.execute("DELETE FROM billing_usage_events WHERE tenant_id=$1", T)
        await admin.execute("DELETE FROM tenants WHERE id=$1", T)
        await admin.close()
        await connection.close_pool()
