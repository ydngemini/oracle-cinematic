"""The voice frequency cap holds under concurrent callers (external audit 26.7),
and a voiceprint release counts only on the voice channel and until it expires
(26.8), against real PostgreSQL with FORCE RLS as the application role
(skipped without ORACLE_LIVE_DB_ADMIN_DSN).

guard_outreach used to read the 24-hour count on one connection and write the
attempt row on another, so callers racing for the last slot under the cap all
read the same count and all dialled.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

RACERS = 6
# A Monday, 12:00 in Delaware: inside the calling window.
IN_WINDOW = datetime(2026, 11, 16, 17, 0, tzinfo=timezone.utc)


def test_concurrent_calls_cannot_exceed_the_voice_cap():
    allowed, logged = asyncio.run(_race())
    assert allowed == 1
    assert logged == {"allowed": 3, "denied": RACERS - 1}


def test_a_voiceprint_release_counts_only_on_voice_and_until_it_expires():
    assert asyncio.run(_voiceprint()) == {
        "voice, no expiry": True,
        "voice, expires tomorrow": True,
        "voice, expired": False,
        "recorded on sms": False,
        "voice, revoked": False,
    }


async def _tenant(admin, tag: str) -> str:
    tenant = str(uuid.uuid4())
    await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Voice cap')",
                        tenant, f"{tag}-{tenant[:8]}")
    return tenant


async def _cleanup(admin, tenant: str) -> None:
    for table in ("outreach_attempt_log", "outreach_consent"):
        await admin.execute(f"DELETE FROM {table} WHERE tenant_id=$1", tenant)
    await admin.execute("DELETE FROM tenants WHERE id=$1", tenant)


async def _race() -> tuple[int, dict[str, int]]:
    import asyncpg

    from db import connection
    from outreach_compliance import (
        VOICE_CALLS_PER_24H_CAP, Channel, VoiceMode, guard_outreach, normalize_contact,
    )
    from tenancy import Role, TenantContext

    assert VOICE_CALLS_PER_24H_CAP == 3
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=RACERS + 1)
    tenant = await _tenant(admin, "vcap")
    callee = "(302) 555-0142"
    contact = normalize_contact(callee, Channel.VOICE)
    ctx = TenantContext(agent_id="agent@vcap.test", tenant_id=tenant, role=Role.AGENT)
    try:
        await admin.execute(
            "INSERT INTO outreach_consent (tenant_id, contact, channel, consent_type) "
            "VALUES ($1,$2,'voice','express_written')", tenant, contact)
        for _ in range(VOICE_CALLS_PER_24H_CAP - 1):  # one slot left under the cap
            await admin.execute(
                "INSERT INTO outreach_attempt_log (tenant_id, contact, channel, state_code, allowed) "
                "VALUES ($1,$2,'voice','DE',true)", tenant, contact)

        decisions = await asyncio.gather(*(
            guard_outreach(ctx, channel=Channel.VOICE, contact=callee, state_code="DE",
                           now_utc=IN_WINDOW, voice_mode=VoiceMode.AGENT, recording_enabled=False)
            for _ in range(RACERS)))
        rows = await admin.fetch(
            "SELECT allowed, count(*) AS n FROM outreach_attempt_log WHERE tenant_id=$1 GROUP BY allowed",
            tenant)
        counts = {("allowed" if r["allowed"] else "denied"): r["n"] for r in rows}
        return sum(d.allowed for d in decisions), counts
    finally:
        await _cleanup(admin, tenant)
        await admin.close()
        await connection.close_pool()


async def _voiceprint() -> dict[str, bool]:
    import asyncpg

    from db import connection
    from outreach_compliance import Channel, ConsentLedger
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant = await _tenant(admin, "vprint")
    ctx = TenantContext(agent_id="agent@vprint.test", tenant_id=tenant, role=Role.AGENT)
    cases = {
        "voice, no expiry": ("voice", None, False),
        "voice, expires tomorrow": ("voice", timedelta(days=1), False),
        "voice, expired": ("voice", timedelta(days=-1), False),
        "recorded on sms": ("sms", None, False),
        "voice, revoked": ("voice", None, True),
    }
    out: dict[str, bool] = {}
    try:
        for n, (label, (channel, expires, revoked)) in enumerate(cases.items()):
            contact = f"+1302555{n:04d}"
            await admin.execute(
                "INSERT INTO outreach_consent (tenant_id, contact, channel, consent_type, expires_at, revoked_at) "
                "VALUES ($1,$2,$3,'biometric_voiceprint', now() + $4::interval, "
                "CASE WHEN $5 THEN now() END)",
                tenant, contact, channel, expires, revoked)
            async with connection.tenant_tx(ctx) as conn:
                row = await ConsentLedger.gather_state(conn, contact, Channel.VOICE)
            out[label] = bool(row["vp_consent"])
        return out
    finally:
        await _cleanup(admin, tenant)
        await admin.close()
        await connection.close_pool()
