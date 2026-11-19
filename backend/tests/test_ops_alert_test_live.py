"""Audit 30.3: a test alert proves delivery only when a person types its
one-time code back. Real PostgreSQL, as the app role (oracle_app), inside one
transaction that is rolled back. Skipped without ORACLE_LIVE_DB_ADMIN_DSN."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")
MIGRATION = Path(__file__).resolve().parents[1] / "db" / "migrations" / "0180_ops_alert_tests.sql"
TO = "Ops@Example.test"


def test_a_test_alert_is_proof_only_once_its_code_is_typed_back():
    asyncio.run(_scenario())


async def _age(conn, minutes=2):
    await conn.execute("UPDATE ops_alert_tests SET requested_at = requested_at - make_interval(mins => $1)", minutes)


async def _scenario():
    import asyncpg

    import ops_alert_test as t

    conn = await asyncpg.connect(DSN)
    tx = conn.transaction()
    await tx.start()
    try:
        if not await conn.fetchval("SELECT to_regclass('public.ops_alert_tests')"):
            await conn.execute(MIGRATION.read_text())
        await conn.execute("DELETE FROM ops_alert_tests")
        await conn.execute("SET LOCAL ROLE oracle_app")

        # No recipient: recorded, nothing to confirm, nothing proven.
        missing = await t.begin(conn, "op-1", "  ")
        assert missing.code is None and missing.retry_after is None
        st = await t.status(conn, "")
        assert st["recipient_configured"] is False and st["proven"] is False
        assert st["last_attempt"]["outcome"] == "recipient_missing"

        # One test a minute.
        refused = await t.begin(conn, "op-2", TO)
        assert refused.code is None and 1 <= refused.retry_after <= 60
        await _age(conn)

        # Sent, then confirmed with the code as typed (any case, with the dash).
        started = await t.begin(conn, "op-1", TO)
        assert started.code and started.recipient == TO
        stored = await conn.fetch("SELECT * FROM ops_alert_tests")
        assert all(started.code not in str(dict(r)) for r in stored), "the code itself is never stored"
        assert all("example.test" not in str(dict(r)).lower() for r in stored), "nor the address"
        await t.finish(conn, started.id, None)
        st = await t.status(conn, TO)
        assert st["awaiting_code"] is True and st["proven"] is False
        wrong = await t.confirm(conn, "ZZZZ-ZZZZ", "op-1")
        assert wrong == {"result": "wrong_code", "attempts_left": t.MAX_ATTEMPTS - 1}
        assert (await t.confirm(conn, t.display_code(started.code).lower(), "op-3"))["result"] == "confirmed"
        assert (await t.confirm(conn, started.code, "op-3"))["result"] == "no_pending_test", "a code works once"
        st = await t.status(conn, " ops@example.TEST ")
        assert st["proven"] is True and st["confirmed_at"] and st["awaiting_code"] is False
        assert (await t.status(conn, "someone-else@example.test"))["proven"] is False, "proof is per recipient"

        # A confirmation older than the window is stale, not proof.
        await conn.execute("UPDATE ops_alert_tests SET confirmed_at = confirmed_at - make_interval(days => $1)",
                           t.PROOF_MAX_AGE_DAYS + 1)
        st = await t.status(conn, TO)
        assert st["proven"] is False and st["stale"] is True

        # A send the relay refused can never be confirmed.
        await _age(conn)
        failed = await t.begin(conn, "op-1", TO)
        await t.finish(conn, failed.id, "SMTPAuthenticationError")
        assert (await t.confirm(conn, failed.code, "op-1"))["result"] == "no_pending_test"
        assert (await t.status(conn, TO))["last_attempt"]["outcome"] == "SMTPAuthenticationError"

        # Too many wrong codes end that test, even for the right code.
        await _age(conn)
        guessed = await t.begin(conn, "op-1", TO)
        await t.finish(conn, guessed.id, None)
        for _ in range(t.MAX_ATTEMPTS):
            assert (await t.confirm(conn, "AAAA-AAAA", "op-9"))["result"] == "wrong_code"
        assert (await t.confirm(conn, guessed.code, "op-9"))["result"] == "too_many_attempts"
        assert (await t.status(conn, TO))["awaiting_code"] is False

        # An unconfirmed code expires.
        await _age(conn, minutes=t.CODE_TTL_HOURS * 60 + 5)
        late = await t.begin(conn, "op-1", TO)
        await t.finish(conn, late.id, None)
        await conn.execute("UPDATE ops_alert_tests SET requested_at = requested_at - make_interval(hours => $1) "
                           "WHERE id = $2", t.CODE_TTL_HOURS + 1, late.id)
        assert (await t.confirm(conn, late.code, "op-1"))["result"] == "no_pending_test"
    finally:
        await tx.rollback()
        await conn.close()
