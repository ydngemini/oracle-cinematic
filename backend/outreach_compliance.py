"""
Outreach compliance gate — TCPA / mini-TCPA / BIPA enforcement for every
outbound voice, SMS, and message path.

Why this exists
---------------
The Jun-2026 legal audit found Oracle's outreach engine had *zero* telephony /
SMS compliance: an AI voice "Closer" was prompted to hide that it was artificial
(a per-se TCPA violation post-FCC 24-17), with no consent capture, no opt-out
honouring, no calling-hours window, and no BIPA voiceprint consent. This module
is the single chokepoint that closes those gaps. Every send path calls
``guard_outreach`` (or the channel helpers) BEFORE contacting a homeowner.

Layering
--------
- Pure decision logic (``evaluate``, the window/frequency/disclosure helpers) has
  no DB dependency and is unit-tested directly — see
  compliance_engine/tests/test_outreach_compliance.py.
- ``ConsentLedger`` wraps the 0015 tables (outreach_consent / _suppression /
  _attempt_log) through the tenant-scoped ``tenant_tx`` so RLS stays live.
- The FastAPI router exposes consent capture, opt-out (STOP) handling, and a
  dry-run check used by the dashboard before launching a campaign.

Legal basis (audit observation IDs in parens):
- FCC 24-17 (2787, 2764): AI/cloned voices are "artificial" under TCPA →
  prior express WRITTEN consent + an artificial-voice disclosure are mandatory.
- FL FTSA (2788) / OK OTSA (2786): mini-TCPA, 8am-8pm window, OK ≤3 calls/24h,
  STOP handling with a 15-day cure (FL).
- TCPA revocation (2764): honour opt-out via any reasonable method within
  10 business days (eff. Apr 11 2025).
- IL BIPA (2775): voiceprint = biometric identifier → written release before
  recording an IL recipient.
- A2P 10DLC (2790): SMS requires a registered brand+campaign before delivery.
"""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from db.connection import tenant_tx
from tenancy import TenantContext, require_context

logger = logging.getLogger("oracle.outreach_compliance")


# ── Channels ────────────────────────────────────────────────────────────────
class Channel(str, Enum):
    VOICE = "voice"   # AI / prerecorded voice call
    SMS = "sms"
    EMAIL = "email"


class VoiceMode(str, Enum):
    """Who is speaking on an outbound voice call.

    AI remains the default so every existing call path keeps the stricter
    written-consent and artificial-voice disclosure requirements. Browser
    calls explicitly opt into AGENT mode and default to no recording.
    """

    AI = "ai"
    AGENT = "agent"


# ── Audit-derived rule data ──────────────────────────────────────────────────

# States with a mini-TCPA private right of action and a tightened calling-hours
# window. FL (FTSA) and OK (OTSA) are the highest-litigation-volume; the rest
# have analogous telephone-solicitation acts. Membership = treat as strict.
MINI_TCPA_STATES: frozenset[str] = frozenset(
    {"FL", "OK", "WA", "MD", "NJ", "CT"}
)

# OK OTSA: no more than three calls to a number within 24h. Applied to every
# mini-TCPA state as the conservative floor.
VOICE_CALLS_PER_24H_CAP = 3

# IL BIPA gives a private right of action over voiceprints; recording an IL
# recipient without a written biometric release is the exposure the audit
# flagged. TX (CUBI) and WA have biometric statutes but no private PoA — we
# gate IL and surface the others as a warning, not a hard block.
VOICEPRINT_CONSENT_STATES: frozenset[str] = frozenset({"IL"})
VOICEPRINT_WARN_STATES: frozenset[str] = frozenset({"TX", "WA"})

# Calling-hours window. Federal TCPA is 8am-9pm recipient-local; FL/OK mini-TCPA
# is 8am-8pm. We use the stricter 8pm close everywhere — never narrower than the
# law, never wider than the strictest state we operate in.
CALL_WINDOW_OPEN = time(8, 0)
CALL_WINDOW_CLOSE = time(20, 0)

# Disclosure the AI voice agent MUST speak at the top of every call. Satisfies
# the FCC 24-17 "artificial or prerecorded voice" identification requirement.
AI_VOICE_DISCLOSURE = (
    "Hi, before we go further I want to let you know that you're speaking with "
    "an automated AI assistant on a recorded line. If you'd prefer not to "
    "continue, just say 'stop' and I'll end the call and won't contact you again."
)

# Spoken biometric-consent ask appended for BIPA states before any recording is
# retained / used to build a voiceprint.
BIOMETRIC_VOICE_DISCLOSURE = (
    "Because you're in {state}, I also need your permission to record this call. "
    "Your recording is kept only to service your request and is not used to train "
    "any voice model. Is it okay to record? Please say yes or no."
)

# Keywords that, inbound, constitute a TCPA opt-out via "any reasonable method".
STOP_KEYWORDS: frozenset[str] = frozenset(
    {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "opt-out", "remove"}
)

# Representative IANA tz per state for the calling-hours check. Multi-zone states
# map to their most-populous zone; this is an approximation — when a contact's
# own tz/coords are known, pass them to evaluate() and they win over this map.
_STATE_TZ: dict[str, str] = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix",
    "AR": "America/Chicago", "CA": "America/Los_Angeles", "CO": "America/Denver",
    "CT": "America/New_York", "DE": "America/New_York", "DC": "America/New_York",
    "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago", "KS": "America/Chicago", "KY": "America/New_York",
    "LA": "America/Chicago", "ME": "America/New_York", "MD": "America/New_York",
    "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver",
    "NE": "America/Chicago", "NV": "America/Los_Angeles", "NH": "America/New_York",
    "NJ": "America/New_York", "NM": "America/Denver", "NY": "America/New_York",
    "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York",
    "RI": "America/New_York", "SC": "America/New_York", "SD": "America/Chicago",
    "TN": "America/Chicago", "TX": "America/Chicago", "UT": "America/Denver",
    "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver",
}


# ── Decision types ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OutreachDecision:
    """Result of evaluating one outbound attempt. ``allowed`` is the only thing
    a caller must check; ``blockers`` explains a denial, ``warnings`` and
    ``required_disclosures`` carry obligations the caller must still satisfy."""
    allowed: bool
    channel: str
    contact: str
    state_code: Optional[str]
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    required_disclosures: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "channel": self.channel,
            "contact": self.contact,
            "state_code": self.state_code,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "required_disclosures": list(self.required_disclosures),
        }


# ── Normalisation ────────────────────────────────────────────────────────────
_DIGITS = re.compile(r"\D+")


def normalize_contact(raw: str, channel: Channel | str) -> str:
    """Canonicalise a contact so consent/suppression match regardless of
    formatting. Phones → E.164 (+1 assumed for 10-digit NANP); emails → lower."""
    channel = Channel(channel)
    raw = (raw or "").strip()
    if channel is Channel.EMAIL:
        return raw.lower()
    digits = _DIGITS.sub("", raw)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if raw.startswith("+"):
        return "+" + digits
    return digits or raw.lower()


def _infer_channel(contact: str) -> Channel:
    """Pick a normalization shape from the contact's *content*: an '@' means an
    email (lowercased), anything else is treated as a phone (E.164). Used for
    opt-outs targeting all channels ('*'), where the channel string can't say
    whether the contact is a phone or an email."""
    return Channel.EMAIL if "@" in (contact or "") else Channel.SMS


def is_stop_keyword(body: str) -> bool:
    """True when an inbound message body is a TCPA opt-out request."""
    token = re.sub(r"[^a-z]", "", (body or "").strip().lower())
    return token in STOP_KEYWORDS


# ── Pure evaluation (no DB) ──────────────────────────────────────────────────

def within_calling_window(
    state_code: Optional[str],
    now_utc: datetime,
    *,
    tz_name: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Is `now` inside the 8am-8pm recipient-local calling window?

    Returns (ok, local_clock_str). Unknown state + no tz → cannot prove it's
    inside the window, so we fail closed for voice/SMS callers.
    """
    tz_name = tz_name or (_STATE_TZ.get((state_code or "").upper()) if state_code else None)
    if not tz_name:
        return False, None
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local = now_utc.astimezone(ZoneInfo(tz_name))
    ok = CALL_WINDOW_OPEN <= local.time() < CALL_WINDOW_CLOSE
    return ok, local.strftime("%H:%M %Z")


def required_disclosures(
    channel: Channel,
    state_code: Optional[str],
    *,
    voice_mode: VoiceMode | str = VoiceMode.AI,
    recording_enabled: bool = True,
) -> tuple[str, ...]:
    """Disclosures the caller must deliver for a permitted attempt."""
    out: list[str] = []
    sc = (state_code or "").upper()
    voice_mode = VoiceMode(voice_mode)
    if channel is Channel.VOICE:
        # Artificial-voice identification is an AI-only obligation.
        if voice_mode is VoiceMode.AI:
            out.append(AI_VOICE_DISCLOSURE)
        # Recording / voiceprint consent is owed by whoever records — human
        # agents included. Mirrors the BIPA gate in evaluate(), which keys off
        # recording_enabled alone.
        if recording_enabled and sc in VOICEPRINT_CONSENT_STATES:
            out.append(BIOMETRIC_VOICE_DISCLOSURE.format(state=sc))
    return tuple(out)


def evaluate(
    *,
    channel: Channel | str,
    contact: str,
    state_code: Optional[str],
    now_utc: datetime,
    suppressed: bool,
    has_consent: bool,
    has_written_consent: bool = False,
    has_voiceprint_consent: bool = False,
    recent_voice_attempts: int = 0,
    tz_name: Optional[str] = None,
    voice_mode: VoiceMode | str = VoiceMode.AI,
    recording_enabled: bool = True,
) -> OutreachDecision:
    """Core gate. Combines persisted facts (suppression / consent / frequency,
    supplied by the caller via ConsentLedger) with the pure rule data above into
    a single allow/deny decision. Keeping the DB lookups out of here makes every
    branch unit-testable.
    """
    channel = Channel(channel)
    voice_mode = VoiceMode(voice_mode)
    contact = normalize_contact(contact, channel)
    sc = (state_code or "").upper() or None
    blockers: list[str] = []
    warnings: list[str] = []

    # 1. Suppression / opt-out is absolute — it overrides any consent on file.
    if suppressed:
        blockers.append("contact is on the do-not-contact / opt-out list")

    # 2. Consent. AI artificial-voice calls require prior express WRITTEN consent
    #    (FCC 24-17): oral consent and an established business relationship do NOT
    #    qualify, so the VOICE gate keys off has_written_consent specifically.
    #    Marketing SMS requires prior express consent (has_consent).
    if channel is Channel.VOICE:
        if voice_mode is VoiceMode.AI and not has_written_consent:
            blockers.append(
                "no prior express WRITTEN consent on file for voice "
                "(FCC 24-17 — oral / prior-business relationship does not qualify for AI voice)"
            )
        elif voice_mode is VoiceMode.AGENT and not has_consent:
            blockers.append("no consent or prior-business basis on file for agent voice")
    elif channel is Channel.SMS and not has_consent:
        blockers.append("no prior express consent on file for sms")

    # 3. Calling-hours window (voice + SMS).
    if channel in (Channel.VOICE, Channel.SMS):
        ok, clock = within_calling_window(sc, now_utc, tz_name=tz_name)
        if not ok:
            where = clock or f"unknown timezone for state {sc or '??'}"
            blockers.append(f"outside the 8am-8pm calling window (recipient local time {where})")

    # 4. Frequency cap (voice, mini-TCPA states — applied everywhere as a floor).
    if channel is Channel.VOICE and recent_voice_attempts >= VOICE_CALLS_PER_24H_CAP:
        blockers.append(
            f"frequency cap reached ({recent_voice_attempts}/{VOICE_CALLS_PER_24H_CAP} calls in 24h)"
        )

    # 5. BIPA voiceprint — hard gate in IL, warning elsewhere.
    if channel is Channel.VOICE and recording_enabled:
        if sc in VOICEPRINT_CONSENT_STATES and not has_voiceprint_consent:
            blockers.append(f"no BIPA voiceprint consent on file for {sc} recipient")
        elif sc in VOICEPRINT_WARN_STATES:
            warnings.append(f"{sc} has a biometric-privacy statute; capture recording consent")

    # 6. Mini-TCPA awareness flag (not a blocker on its own).
    if sc in MINI_TCPA_STATES:
        warnings.append(f"{sc} is a mini-TCPA state — strict liability, $500-$1,500/violation")

    return OutreachDecision(
        allowed=not blockers,
        channel=channel.value,
        contact=contact,
        state_code=sc,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        required_disclosures=required_disclosures(
            channel,
            sc,
            voice_mode=voice_mode,
            recording_enabled=recording_enabled,
        ),
    )


# ── Persistence layer ────────────────────────────────────────────────────────
class ConsentLedger:
    """Async accessor for the 0015 consent / suppression / attempt tables. All
    queries run through tenant_tx so RLS scopes rows to the caller's tenant."""

    @staticmethod
    async def gather_state(conn, contact: str, channel: Channel):
        """One round-trip on a caller-supplied connection returning every fact
        guard_outreach needs: suppression, channel consent, BIPA voiceprint
        consent, and the 24h voice-attempt count. Replaces what used to be four
        separate tenant_tx acquisitions. RLS scopes rows to the conn's tenant."""
        return await conn.fetchrow(
            """
            SELECT
                EXISTS(SELECT 1 FROM outreach_suppression
                        WHERE contact = $1 AND channel IN ($2, '*')
                          AND lifted_at IS NULL)                        AS suppressed,
                EXISTS(SELECT 1 FROM outreach_consent
                        WHERE contact = $1 AND channel = $2
                          AND consent_type IN ('express_written', 'express_oral', 'prior_business')
                          AND revoked_at IS NULL
                          AND (expires_at IS NULL OR expires_at > now()))  AS has_consent,
                EXISTS(SELECT 1 FROM outreach_consent
                        WHERE contact = $1 AND channel = $2
                          AND consent_type = 'express_written'
                          AND revoked_at IS NULL
                          AND (expires_at IS NULL OR expires_at > now()))  AS has_written_consent,
                EXISTS(SELECT 1 FROM outreach_consent
                        WHERE contact = $1 AND consent_type = 'biometric_voiceprint'
                          AND revoked_at IS NULL)                       AS vp_consent,
                (SELECT count(*) FROM outreach_attempt_log
                        WHERE contact = $1 AND channel = 'voice' AND allowed = true
                          AND attempted_at > now() - interval '24 hours') AS recent_voice
            """,
            contact, channel.value,
        )

    @staticmethod
    async def record_consent(
        ctx: TenantContext, *, contact: str, channel: Channel, consent_type: str,
        state_code: Optional[str], proof_source: Optional[str], proof_text: Optional[str],
        lead_id: Optional[str] = None, client_id: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> str:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                """INSERT INTO outreach_consent
                       (tenant_id, lead_id, client_id, contact, channel, consent_type,
                        state_code, proof_source, proof_text, expires_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                   RETURNING id""",
                ctx.tenant_id, lead_id, client_id, contact, channel.value, consent_type,
                state_code, proof_source, proof_text, expires_at,
            )
        return str(row["id"])

    @staticmethod
    async def suppress(
        ctx: TenantContext, *, contact: str, channel: str, reason: str,
        source_text: Optional[str] = None,
    ) -> None:
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """INSERT INTO outreach_suppression (tenant_id, contact, channel, reason, source_text)
                   VALUES ($1,$2,$3,$4,$5)
                   ON CONFLICT (tenant_id, contact, channel) WHERE lifted_at IS NULL
                   DO NOTHING""",
                ctx.tenant_id, contact, channel, reason, source_text,
            )

    @staticmethod
    async def log_attempt(
        ctx: TenantContext, *, contact: str, channel: Channel, state_code: Optional[str],
        allowed: bool, block_reason: Optional[str],
    ) -> None:
        """Record one outreach attempt on its OWN committed transaction. This is
        deliberately independent of any caller transaction: the attempt log is
        compliance evidence (esp. the allowed=false / block_reason denial rows)
        and must survive even when the caller's surrounding tx rolls back — e.g.
        enforce_outreach raises HTTP 451 inside crm.send_message's tenant_tx."""
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """INSERT INTO outreach_attempt_log
                       (tenant_id, contact, channel, state_code, allowed, block_reason)
                   VALUES ($1,$2,$3,$4,$5,$6)""",
                ctx.tenant_id, contact, channel.value, state_code, allowed, block_reason,
            )


# ── Guard entrypoint used by send paths ──────────────────────────────────────
@asynccontextmanager
async def _conn_or_tx(ctx: TenantContext, conn):
    """Yield the caller's connection if one was supplied, else acquire a fresh
    tenant_tx. Lets a send path that already holds an open tenant_tx reuse it for
    the read instead of nesting a second pool acquisition (which can deadlock the
    pool under load). Future voice/SMS send paths should route through this."""
    if conn is not None:
        yield conn
    else:
        async with tenant_tx(ctx) as c:
            yield c


async def guard_outreach(
    ctx: TenantContext, *, channel: Channel | str, contact: str,
    state_code: Optional[str], now_utc: Optional[datetime] = None,
    tz_name: Optional[str] = None, log: bool = True, conn=None,
    voice_mode: VoiceMode | str = VoiceMode.AI,
    recording_enabled: bool = True,
) -> OutreachDecision:
    """Resolve persisted consent/suppression/frequency, evaluate, optionally log
    the attempt, and return the decision. Callers MUST check `.allowed` and, when
    permitted, deliver `.required_disclosures`. Raises nothing — see
    ``enforce_outreach`` for the raising variant.

    The state read uses ``conn`` when supplied (reusing an already-open
    ``tenant_tx`` — the email send path in crm.py does this) to avoid a nested
    pool acquisition; otherwise one is acquired. The attempt log is written on a
    SEPARATE, independently-committed transaction so the denial record survives
    even when the caller's surrounding tx rolls back (e.g. enforce_outreach
    raises 451 inside that tx)."""
    channel = Channel(channel)
    now_utc = now_utc or datetime.now(timezone.utc)
    norm = normalize_contact(contact, channel)
    is_voice = channel is Channel.VOICE

    async with _conn_or_tx(ctx, conn) as c:
        row = await ConsentLedger.gather_state(c, norm, channel)

    decision = evaluate(
        channel=channel, contact=norm, state_code=state_code, now_utc=now_utc,
        suppressed=bool(row["suppressed"]), has_consent=bool(row["has_consent"]),
        has_written_consent=bool(row["has_written_consent"]),
        has_voiceprint_consent=bool(row["vp_consent"]) if is_voice else False,
        recent_voice_attempts=int(row["recent_voice"]) if is_voice else 0,
        tz_name=tz_name,
        voice_mode=voice_mode,
        recording_enabled=recording_enabled,
    )

    if log:
        await ConsentLedger.log_attempt(
            ctx, contact=norm, channel=channel, state_code=decision.state_code,
            allowed=decision.allowed,
            block_reason=decision.blockers[0] if decision.blockers else None,
        )
    return decision


async def enforce_outreach(ctx: TenantContext, **kwargs) -> OutreachDecision:
    """guard_outreach + raise HTTP 451 (Unavailable For Legal Reasons) when the
    attempt is blocked. Use at the top of any send handler. Accepts the same
    kwargs as ``guard_outreach`` (including ``conn`` to reuse an open tx)."""
    decision = await guard_outreach(ctx, **kwargs)
    if not decision.allowed:
        # Do NOT echo the normalized contact back in the error body — it's a
        # preprocessing artifact the client never submitted in that form, and
        # the dry-run check endpoint would otherwise leak normalization results.
        detail = decision.as_dict()
        detail.pop("contact", None)
        detail["message"] = "outreach blocked by compliance gate"
        raise HTTPException(
            status_code=status.HTTP_451_UNAVAILABLE_FOR_LEGAL_REASONS,
            detail=detail,
        )
    return decision


# ── API surface ──────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/compliance/outreach", tags=["Outreach Compliance"])


class ConsentRecord(BaseModel):
    contact: str = Field(..., min_length=3, max_length=320)
    channel: Channel
    consent_type: str = Field("express_written")
    state_code: Optional[str] = Field(None, max_length=2)
    proof_source: Optional[str] = None
    proof_text: Optional[str] = None
    lead_id: Optional[str] = None
    client_id: Optional[str] = None

    @field_validator("lead_id", "client_id")
    @classmethod
    def validate_optional_uuid(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        uuid.UUID(value)
        return value


class OptOutRecord(BaseModel):
    contact: str = Field(..., min_length=3, max_length=320)
    channel: str = Field("*")
    reason: str = Field("manual_dnc")
    source_text: Optional[str] = None


class CheckRequest(BaseModel):
    contact: str
    channel: Channel
    state_code: Optional[str] = None
    voice_mode: VoiceMode = VoiceMode.AI
    recording_enabled: bool = True


class InboundMessage(BaseModel):
    """Inbound SMS/keyword webhook payload — STOP handling lives here."""
    contact: str
    body: str
    channel: Channel = Channel.SMS


@router.post("/consent", status_code=status.HTTP_201_CREATED)
async def record_consent(body: ConsentRecord, ctx: TenantContext = Depends(require_context)):
    if body.consent_type not in (
        "express_written", "express_oral", "prior_business", "biometric_voiceprint"
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid consent_type")
    norm = normalize_contact(body.contact, body.channel)
    consent_id = await ConsentLedger.record_consent(
        ctx, contact=norm, channel=body.channel, consent_type=body.consent_type,
        state_code=(body.state_code or None), proof_source=body.proof_source,
        proof_text=body.proof_text, lead_id=body.lead_id, client_id=body.client_id,
    )
    logger.info("Consent recorded: %s %s (%s) tenant=%s", norm, body.channel.value,
                body.consent_type, ctx.tenant_id)
    return {"consent_id": consent_id, "contact": norm}


@router.post("/opt-out", status_code=status.HTTP_201_CREATED)
async def record_opt_out(body: OptOutRecord, ctx: TenantContext = Depends(require_context)):
    if body.reason not in ("stop_keyword", "manual_dnc", "litigator", "regulatory"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid reason")
    ch = "*" if body.channel == "*" else Channel(body.channel).value
    # Normalize by the contact's content, not the (possibly wildcard) channel —
    # '*' suppresses every channel and can carry either a phone or an email.
    shape = _infer_channel(body.contact)  # EMAIL for '@'-contacts, else phone (SMS/voice)
    # Reject channel/contact-shape mismatches that could never match a send and
    # would store a dead suppression row (e.g. an email contact under 'voice').
    if ch != "*":
        email_channel = ch == Channel.EMAIL.value
        if (shape is Channel.EMAIL) != email_channel:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "channel does not match contact: use 'email' (or '*') for an email "
                "address, and 'voice'/'sms' (or '*') for a phone number",
            )
    norm = normalize_contact(body.contact, shape)
    await ConsentLedger.suppress(ctx, contact=norm, channel=ch, reason=body.reason,
                                 source_text=body.source_text)
    logger.info("Opt-out recorded: %s ch=%s reason=%s tenant=%s", norm, ch, body.reason, ctx.tenant_id)
    return {"contact": norm, "channel": ch, "suppressed": True}


@router.post("/inbound")
async def handle_inbound(body: InboundMessage, ctx: TenantContext = Depends(require_context)):
    """Inbound message webhook. A STOP/UNSUBSCRIBE/etc body suppresses the
    contact across all channels (TCPA: honour within 10 business days)."""
    if is_stop_keyword(body.body):
        norm = normalize_contact(body.contact, body.channel)
        await ConsentLedger.suppress(ctx, contact=norm, channel="*", reason="stop_keyword",
                                     source_text=body.body[:200])
        logger.info("STOP honoured: %s suppressed (tenant=%s)", norm, ctx.tenant_id)
        return {"opted_out": True, "contact": norm}
    return {"opted_out": False}


@router.post("/check")
async def check_outreach(body: CheckRequest, ctx: TenantContext = Depends(require_context)):
    """Dry-run the gate without recording an attempt — used by the dashboard to
    show whether a campaign target is contactable before launch."""
    decision = await guard_outreach(
        ctx, channel=body.channel, contact=body.contact,
        state_code=(body.state_code or None), log=False,
        voice_mode=body.voice_mode,
        recording_enabled=body.recording_enabled,
    )
    return decision.as_dict()
