"""Speed-to-lead's first-response ledger on real PostgreSQL, as the job's own
worker context under FORCE RLS (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Review of PR #176 (L09-11): a deferral is now retried, so a retry first asks
the ledger whether the lead is already answered for, and the last undecided
attempt records the lead 'failed'. Proves:

- that 'failed' row lands as written: its channel, no reason, no command;
- 0067 refuses a reason on any row but 'blocked', which is why the cause
  stays on the dead-lettered job (OUTREACH_UNDECIDED), not in blocked_reason;
- a later write for the same intake event adds nothing (ON CONFLICT DO
  NOTHING against 0067's unique index), so the 'failed' row stands;
- _first_response_on_record finds the row a retry would collide with: by
  intake event, by lead when there is no intake event, and by contact when
  there is neither; and nothing for another lead.

The brokerage is a fresh throwaway tenant, removed afterwards.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_the_ledger_a_retry_reads_and_the_failed_row_it_leaves():
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import speed_to_lead
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant = str(uuid.uuid4())
    worker = TenantContext(agent_id="speed-to-lead-worker", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
    created = datetime.now(timezone.utc) - timedelta(seconds=30)
    intake, other_intake, lead, contact, lone_contact = (str(uuid.uuid4()) for _ in range(5))
    on_record = speed_to_lead._first_response_on_record

    async def record(**kwargs) -> None:
        anchors = {"intake_event_id": None, "lead_id": None, "contact_id": None, "client_id": None}
        await speed_to_lead._record_response_event(worker, lead_created_at=created, **{**anchors, **kwargs})

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Speed-to-lead ledger live test')",
                            uuid.UUID(tenant), f"s2l-ledger-{tenant[:8]}")
        assert await on_record(worker, intake_event_id=intake, lead_id=None,
                               contact_id=contact, client_id=None) is None

        # The last undecided attempt's write (speed_to_lead, except OutreachUndecided).
        await record(intake_event_id=intake, contact_id=contact, channel="sms", disposition="failed")
        rows = await admin.fetch(
            "SELECT disposition, channel, blocked_reason, command_id FROM lead_response_events "
            "WHERE tenant_id=$1 AND intake_event_id=$2", uuid.UUID(tenant), uuid.UUID(intake))
        assert [dict(r) for r in rows] == [
            {"disposition": "failed", "channel": "sms", "blocked_reason": None, "command_id": None}]
        assert await on_record(worker, intake_event_id=intake, lead_id=None,
                               contact_id=contact, client_id=None) == "failed"
        assert await on_record(worker, intake_event_id=other_intake, lead_id=None,
                               contact_id=contact, client_id=None) is None

        # A reason belongs on a 'blocked' row only (0067 lead_response_blocked_reason_chk).
        with pytest.raises(asyncpg.CheckViolationError, match="lead_response_blocked_reason_chk"):
            await admin.execute(
                "INSERT INTO lead_response_events (tenant_id, intake_event_id, channel, disposition, "
                "blocked_reason, lead_created_at) VALUES ($1,$2,'sms','failed','outreach_undecided',$3)",
                uuid.UUID(tenant), uuid.UUID(other_intake), created)

        # A later write for the same lead adds nothing: the first row stands.
        await record(intake_event_id=intake, contact_id=contact, channel="sms", disposition="blocked",
                     blocked_reason="outside_calling_window")
        assert await admin.fetchval(
            "SELECT array_agg(disposition) FROM lead_response_events WHERE tenant_id=$1 AND intake_event_id=$2",
            uuid.UUID(tenant), uuid.UUID(intake)) == ["failed"]

        # By lead when there is no intake event; by contact when there is neither.
        await record(lead_id=lead, channel=None, disposition="skipped")
        assert await on_record(worker, intake_event_id=None, lead_id=lead,
                               contact_id=None, client_id=None) == "skipped"
        await record(contact_id=lone_contact, channel="email", disposition="skipped")
        assert await on_record(worker, intake_event_id=None, lead_id=None,
                               contact_id=lone_contact, client_id=None) == "skipped"
        # The intake-anchored row names `contact` too, but answers only for its intake event.
        assert await on_record(worker, intake_event_id=None, lead_id=None,
                               contact_id=contact, client_id=None) is None
    finally:
        await admin.execute("SET session_replication_role = replica")
        await admin.execute("DELETE FROM lead_response_events WHERE tenant_id=$1", uuid.UUID(tenant))
        await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(tenant))
        await admin.execute("SET session_replication_role = origin")
        await admin.close()
        await connection.close_pool()
