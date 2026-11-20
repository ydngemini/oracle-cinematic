"""A consent record authorises only what its evidence supports (external audit
26.5 / 26.6), against real PostgreSQL with FORCE
RLS as the application role (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Before 0183 any active 'express_written' row cleared an AI call and a text, any
'biometric_voiceprint' voice row was a BIPA release, and a prior business
relationship counted as consent for a text. Now the server stores each row as
'documented' or 'attested_unverified', the gate reads written consent and
voiceprint releases only when documented, and a text's purpose decides which
consent it needs.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

# A Monday, 12:00 in Delaware / 11:00 in Illinois: inside the calling window.
IN_WINDOW = datetime(2026, 11, 16, 17, 0, tzinfo=timezone.utc)
EVIDENCE = dict(proof_source="e_signature", proof_text="I agree to marketing texts, AI calls and recording.",
                evidence_ref="envelope-live-1")


def test_evidence_and_purpose_decide_what_consent_authorises():
    out = asyncio.run(_matrix())
    assert out == {
        "statuses": {"attested": "attested_unverified", "documented": "documented",
                     "future capture": "attested_unverified", "voiceprint attested": "attested_unverified",
                     "voiceprint documented": "documented"},
        "recorded_by": "agent@evidence.test",
        "written, attested: marketing text": False,
        "written, attested: informational text": True,
        "written, attested: AI call": False,
        "written, documented: marketing text": True,
        "written, documented: AI call": True,
        "written, future capture: marketing text": False,
        "prior business: informational text": False,
        "prior business: marketing text": False,
        "prior business: live agent call": True,
        "prior business: AI call": False,
        "IL, voiceprint attested: recorded AI call": False,
        "IL, voiceprint documented: recorded AI call": True,
        "IL, documented voiceprint stored under sms: recorded AI call": False,
        "pre-0183 row (no evidence columns): status": "attested_unverified",
        "pre-0183 row: AI call": False,
        "pre-0183 row: marketing text": False,
        "pre-0183 row: live agent call": True,
    }


def test_the_database_refuses_documented_without_evidence():
    assert asyncio.run(_constraints()) == {
        "documented, no evidence_ref": "CheckViolationError",
        "documented, unrecognised method": "CheckViolationError",
        "documented, blank disclosure": "CheckViolationError",
        "unknown status": "CheckViolationError",
        "documented with evidence": "ok",
        "expires_at comment": "Consent validity",
    }


async def _tenant(admin) -> str:
    tenant = str(uuid.uuid4())
    await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Consent evidence')",
                        tenant, f"cev-{tenant[:8]}")
    return tenant


async def _cleanup(admin, tenant: str) -> None:
    for table in ("outreach_attempt_log", "outreach_consent"):
        await admin.execute(f"DELETE FROM {table} WHERE tenant_id=$1", tenant)
    await admin.execute("DELETE FROM tenants WHERE id=$1", tenant)


async def _matrix() -> dict:
    import asyncpg

    from db import connection
    from outreach_compliance import Channel, ConsentLedger, Purpose, VoiceMode, guard_outreach
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant = await _tenant(admin)
    ctx = TenantContext(agent_id="agent@evidence.test", tenant_id=tenant, role=Role.AGENT)
    phones = {k: f"+1302555{n:04d}" for n, k in enumerate(
        ("attested", "documented", "future", "ebr", "vp_att", "vp_doc", "vp_sms", "legacy"), start=700)}
    out: dict = {"statuses": {}}

    async def record(key, label, channel, consent_type, **evidence):
        _id, status = await ConsentLedger.record_consent(
            ctx, contact=phones[key], channel=channel, consent_type=consent_type, state_code=None,
            proof_source=evidence.get("proof_source"), proof_text=evidence.get("proof_text"),
            evidence_ref=evidence.get("evidence_ref"), captured_at=evidence.get("captured_at"))
        if label:
            out["statuses"][label] = status

    async def gate(key, channel, *, state="DE", purpose=Purpose.MARKETING, mode=VoiceMode.AI,
                   recording=False) -> bool:
        decision = await guard_outreach(ctx, channel=channel, contact=phones[key], state_code=state,
                                        now_utc=IN_WINDOW, log=False, purpose=purpose,
                                        voice_mode=mode, recording_enabled=recording)
        return decision.allowed

    try:
        # Written consent with and without evidence, on both channels.
        for channel in (Channel.SMS, Channel.VOICE):
            sms = channel is Channel.SMS
            await record("attested", "attested" if sms else None, channel, "express_written",
                         proof_text="said yes on the phone")
            await record("documented", "documented" if sms else None, channel, "express_written", **EVIDENCE)
        await record("future", "future capture", Channel.SMS, "express_written", **EVIDENCE,
                     captured_at=datetime.now(timezone.utc) + timedelta(days=3))
        out["recorded_by"] = await admin.fetchval(
            "SELECT recorded_by FROM outreach_consent WHERE tenant_id=$1 AND contact=$2 LIMIT 1",
            tenant, phones["documented"])

        out["written, attested: marketing text"] = await gate("attested", Channel.SMS)
        out["written, attested: informational text"] = await gate(
            "attested", Channel.SMS, purpose=Purpose.INFORMATIONAL)
        out["written, attested: AI call"] = await gate("attested", Channel.VOICE)
        out["written, documented: marketing text"] = await gate("documented", Channel.SMS)
        out["written, documented: AI call"] = await gate("documented", Channel.VOICE)
        out["written, future capture: marketing text"] = await gate("future", Channel.SMS)

        # A prior business relationship: a basis for a live agent call only.
        for channel in (Channel.SMS, Channel.VOICE):
            await record("ebr", None, channel, "prior_business", **EVIDENCE)
        out["prior business: informational text"] = await gate("ebr", Channel.SMS, purpose=Purpose.INFORMATIONAL)
        out["prior business: marketing text"] = await gate("ebr", Channel.SMS)
        out["prior business: live agent call"] = await gate("ebr", Channel.VOICE, mode=VoiceMode.AGENT)
        out["prior business: AI call"] = await gate("ebr", Channel.VOICE)

        # Illinois recorded AI calls need a DOCUMENTED voice-channel release.
        for key in ("vp_att", "vp_doc", "vp_sms"):
            await record(key, None, Channel.VOICE, "express_written", **EVIDENCE)
        await record("vp_att", "voiceprint attested", Channel.VOICE, "biometric_voiceprint")
        await record("vp_doc", "voiceprint documented", Channel.VOICE, "biometric_voiceprint", **EVIDENCE)
        # The API refuses a release off the voice channel (26.8); older rows may
        # still sit under 'sms' (L09-13) — they stay blocked, not re-labelled.
        await admin.execute(
            "INSERT INTO outreach_consent (tenant_id, contact, channel, consent_type, evidence_status, "
            "proof_source, proof_text, evidence_ref) VALUES ($1,$2,'sms','biometric_voiceprint',"
            "'documented','e_signature','I agree to recording.','env-sms')", tenant, phones["vp_sms"])
        for key, label in (("vp_att", "voiceprint attested"), ("vp_doc", "voiceprint documented"),
                           ("vp_sms", "documented voiceprint stored under sms")):
            out[f"IL, {label}: recorded AI call"] = await gate(key, Channel.VOICE, state="IL", recording=True)

        # A row written before 0183 (the old INSERT, no evidence columns).
        for channel in ("sms", "voice"):
            await admin.execute(
                "INSERT INTO outreach_consent (tenant_id, contact, channel, consent_type, proof_source, proof_text) "
                "VALUES ($1,$2,$3,'express_written','web_form','old form')", tenant, phones["legacy"], channel)
        out["pre-0183 row (no evidence columns): status"] = await admin.fetchval(
            "SELECT DISTINCT evidence_status FROM outreach_consent WHERE tenant_id=$1 AND contact=$2",
            tenant, phones["legacy"])
        out["pre-0183 row: AI call"] = await gate("legacy", Channel.VOICE)
        out["pre-0183 row: marketing text"] = await gate("legacy", Channel.SMS)
        out["pre-0183 row: live agent call"] = await gate("legacy", Channel.VOICE, mode=VoiceMode.AGENT)
        return out
    finally:
        await _cleanup(admin, tenant)
        await admin.close()
        await connection.close_pool()


async def _constraints() -> dict:
    import asyncpg

    admin = await asyncpg.connect(DSN)
    tenant = await _tenant(admin)
    out: dict = {}
    cases = {
        "documented, no evidence_ref": ("documented", "web_form", "I agree.", None),
        "documented, unrecognised method": ("documented", "signed_doc", "I agree.", "doc-1"),
        "documented, blank disclosure": ("documented", "web_form", "   ", "form-1"),
        "unknown status": ("verified", "web_form", "I agree.", "form-1"),
        "documented with evidence": ("documented", "ivr_keypress", "Press 1 to agree.", "ivr-log-9"),
    }
    try:
        for label, (status, source, text, ref) in cases.items():
            try:
                await admin.execute(
                    "INSERT INTO outreach_consent (tenant_id, contact, channel, consent_type, evidence_status, "
                    "proof_source, proof_text, evidence_ref) VALUES ($1,'+13025550790','sms','express_written',"
                    "$2,$3,$4,$5)", tenant, status, source, text, ref)
                out[label] = "ok"
            except asyncpg.PostgresError as exc:
                out[label] = type(exc).__name__
        comment = await admin.fetchval(
            "SELECT col_description('outreach_consent'::regclass, attnum) FROM pg_attribute "
            "WHERE attrelid = 'outreach_consent'::regclass AND attname = 'expires_at'")
        out["expires_at comment"] = (comment or "")[:len("Consent validity")]
        return out
    finally:
        await _cleanup(admin, tenant)
        await admin.close()
