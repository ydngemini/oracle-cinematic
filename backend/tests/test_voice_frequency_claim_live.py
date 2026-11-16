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


def test_another_brokerages_release_does_not_authorise_the_worker():
    """The command worker gates AI calls as PLATFORM_ADMIN, which RLS lets read
    every brokerage; a release held by brokerage B must not clear brokerage A's
    call to the same number (review of the 26.7/26.8 fix)."""
    assert asyncio.run(_cross_brokerage()) == {
        "B's own read": {"has_consent": True, "has_written_consent": True, "vp_consent": True},
        "worker for A": {"has_consent": False, "has_written_consent": False, "vp_consent": False},
        "worker for A, AI call allowed": False,
        "worker for B, AI call allowed": True,
    }


async def _cross_brokerage() -> dict:
    import asyncpg

    from db import connection
    from outreach_compliance import Channel, ConsentLedger, VoiceMode, guard_outreach
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    a, b = await _tenant(admin, "xa"), await _tenant(admin, "xb")
    contact = "+13125550142"
    keys = ("has_consent", "has_written_consent", "vp_consent")
    out: dict = {}
    try:
        for consent_type in ("express_written", "biometric_voiceprint"):
            await admin.execute(
                "INSERT INTO outreach_consent (tenant_id, contact, channel, consent_type) "
                "VALUES ($1,$2,'voice',$3)", b, contact, consent_type)
        agent_b = TenantContext(agent_id="agent@xb.test", tenant_id=b, role=Role.AGENT)
        async with connection.tenant_tx(agent_b) as conn:
            row = await ConsentLedger.gather_state(conn, contact, Channel.VOICE, tenant_id=b)
        out["B's own read"] = {k: bool(row[k]) for k in keys}
        worker_a = TenantContext(agent_id="command-worker", tenant_id=a, role=Role.PLATFORM_ADMIN)
        async with connection.tenant_tx(worker_a) as conn:
            row = await ConsentLedger.gather_state(conn, contact, Channel.VOICE, tenant_id=a)
        out["worker for A"] = {k: bool(row[k]) for k in keys}
        for label, tenant in (("A", a), ("B", b)):
            worker = TenantContext(agent_id="command-worker", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
            decision = await guard_outreach(worker, channel=Channel.VOICE, contact=contact,
                                            state_code="IL", now_utc=IN_WINDOW, log=False,
                                            voice_mode=VoiceMode.AI)
            out[f"worker for {label}, AI call allowed"] = decision.allowed
        return out
    finally:
        for t in (a, b):
            await _cleanup(admin, t)
        await admin.close()
        await connection.close_pool()


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

        # Two spellings of one number must contend for the same claim.
        spellings = (callee, contact)
        decisions = await asyncio.gather(*(
            guard_outreach(ctx, channel=Channel.VOICE, contact=spellings[i % 2], state_code="DE",
                           now_utc=IN_WINDOW, voice_mode=VoiceMode.AGENT, recording_enabled=False)
            for i in range(RACERS)))
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
                row = await ConsentLedger.gather_state(conn, contact, Channel.VOICE, tenant_id=tenant)
            out[label] = bool(row["vp_consent"])
        return out
    finally:
        await _cleanup(admin, tenant)
        await admin.close()
        await connection.close_pool()
