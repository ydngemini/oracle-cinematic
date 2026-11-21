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

Consent evidence and message purpose (external audit 26.5 / 26.6). The rules
were chosen conservatively by the Claude session under the owner's instruction
of 2026-11-20 ("continue all" on the open decisions); the owner may revise them
and counsel has not reviewed them:
- A consent row is 'documented' only when the server saw a recognised capture
  method, the disclosure text agreed to, an evidence reference and a capture
  time not in the future (``consent_evidence_status``); anything else is stored
  'attested_unverified' (migration 0183). Only documented written consent
  authorises an AI call or a marketing text, and only a documented voice-channel
  release counts as a BIPA voiceprint release.
- Every send path says why it sends (``Purpose``). A marketing text needs
  documented written consent; an informational text needs prior express consent
  (written or oral). A prior business relationship never counts for a text; it
  remains a basis only for a live AGENT call. A call site that cannot tell
  passes MARKETING.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, time, timezone
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


class Purpose(str, Enum):
    """Why a message is sent (external audit 26.5).

    Only SMS reads it: a MARKETING text needs documented prior express WRITTEN
    consent; an INFORMATIONAL text needs prior express consent (written in any
    evidence state, or oral). Voice and email ignore it: an AI call already
    needs documented written consent whatever it says, a live agent call keeps
    its consent-or-prior-business basis, and email is opt-out (CAN-SPAM; the
    commercial footer goes on every send). A call site that cannot tell what
    the message is passes MARKETING (strict)."""

    INFORMATIONAL = "informational"
    MARKETING = "marketing"


class VoiceMode(str, Enum):
    """Who is speaking on an outbound voice call.

    AI remains the default so every existing call path keeps the stricter
    written-consent and artificial-voice disclosure requirements. Browser
    calls explicitly opt into AGENT mode and default to no recording.
    """

    AI = "ai"
    AGENT = "agent"


# ── Consent evidence (external audit 26.6, migration 0183) ───────────────────
EVIDENCE_DOCUMENTED = "documented"
EVIDENCE_ATTESTED = "attested_unverified"

# Capture methods whose evidence can be retrieved later by its reference: the
# signed web form event, the e-signature envelope, the signed document, the
# call recording, the inbound keyword reply, the IVR keypress log. Mirrored by
# 0183's chk_consent_documented_evidence.
DOCUMENTED_PROOF_SOURCES: frozenset[str] = frozenset({
    "web_form", "e_signature", "signed_document", "recorded_call",
    "sms_keyword_reply", "ivr_keypress",
})

# Consent types the gate reads as written consent or a BIPA release, so an
# undocumented one is worth telling the person about when it is recorded.
EVIDENCE_GATED_TYPES: frozenset[str] = frozenset({"express_written", "biometric_voiceprint"})

UNVERIFIED_CONSENT_NOTICE = (
    "Recorded as attested, not documented. It will not authorise AI calls, voiceprint "
    "recording or marketing texts until it is documented: record it again with the capture "
    "method, the disclosure text the person agreed to and an evidence reference."
)


def _blank(value: Optional[str]) -> bool:
    return not (value or "").strip()


def consent_evidence_status(
    *, proof_source: Optional[str], proof_text: Optional[str], evidence_ref: Optional[str],
    captured_at: Optional[datetime], now_utc: datetime,
) -> str:
    """The evidence status the SERVER assigns a consent row (26.6; rule chosen
    conservatively under the owner's 2026-11-20 instruction, revisable by the
    owner). 'documented' needs all four: a recognised capture
    method, the disclosure text the person agreed to, a reference the evidence
    can be retrieved by, and a capture time that is not in the future (None =
    recorded now). Anything less is 'attested_unverified'."""
    if (proof_source or "").strip().lower() not in DOCUMENTED_PROOF_SOURCES:
        return EVIDENCE_ATTESTED
    if _blank(proof_text) or _blank(evidence_ref):
        return EVIDENCE_ATTESTED
    if captured_at is not None:
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
        if captured_at > now_utc:
            return EVIDENCE_ATTESTED
    return EVIDENCE_DOCUMENTED


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
# inbound_voice applies this set to SPEECH with its own narrower clause rule (a
# voice opt-out is permanent and discards the caller's answers): unchanged here.
STOP_KEYWORDS: frozenset[str] = frozenset(
    {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "opt-out", "remove"}
)

# SMS keywords: the above plus REVOKE (named per-se valid by the FCC's 2024
# revocation rule), UNSUB, and the Spanish ALTO/BAJA/PARAR/CANCELAR that CTIA
# asks Spanish-language programs to honour (audit 25.3).
SMS_STOP_KEYWORDS: frozenset[str] = STOP_KEYWORDS | {"revoke", "unsub", "alto", "baja", "parar", "cancelar"}

# SMS only. Plain-language opt-outs, matched on WORD BOUNDARIES anywhere in a
# reply. Known safe-direction over-matches (accepted, audit 25.3 review):
# "unsubscribe from Zillow?", "remove me from the deed", "don't call before 9".
SMS_OPT_OUT_PHRASES: tuple[str, ...] = (
    "do not contact", "don't contact", "dont contact", "do not call", "don't call", "dont call",
    "never call", "never contact", "never text", "never message", "stop calling", "stop contacting",
    "stop texting", "stop txting", "stop texing", "stop messaging", "stop msging", "stop sending",
    "stop receiving", "quit calling", "quit contacting", "quit texting", "do not text", "don't text",
    "dont text", "don't ever text", "don't ever call", "don't ever contact", "no more texts",
    "no more messages", "no more calls", "remove me", "take me off", "take my name off",
    "take my number off", "delete my number", "lose my number", "lose this number",
    "remove my number", "remove this number", "leave me alone", "wrong number", "stop txt",
    "opt-out",
    "unsubscribe", "opt out", "opt me out", "revoke consent", "revoke my consent",
    "withdraw my consent", "withdraw consent",
)
_SMS_PHRASE_RE = re.compile(
    r"(?<![a-z'])(?:" + "|".join(re.escape(p) for p in SMS_OPT_OUT_PHRASES) + r")(?![a-z])")

# Words that can sit alongside a bare stop keyword in a text without changing
# that it is the whole instruction: "STOP please", "stop it now", "lol stop".
_SMS_FILLER: frozenset[str] = frozenset(
    {
        "i", "im", "id", "ive", "me", "my", "you", "your", "it", "all", "this", "lol",
        "please", "pls", "plz", "just", "now", "really", "actually", "seriously", "already",
        "ok", "okay", "no", "nope", "yes", "yeah", "yep", "well", "so", "and",
        "but", "um", "uh", "look", "listen", "hey", "sir", "maam",
        "said", "say", "saying", "sorry", "thanks", "thank", "thx",
    }
)

# Clause boundaries for texts: punctuation, line breaks, a spaced dash ("Stop - not
# selling") and conjunctions. A hyphen INSIDE a word is not a boundary
# ("opt-out" stays one token).
_SMS_CLAUSE_RE = re.compile(r"[.,;:!?—–\n]+|\s-+\s|\b(?:and|but|so|then|because)\b")


def requests_opt_out(text: str) -> bool:
    """SMS: True when a reply asks not to be contacted — an opt-out phrase
    (word-bounded) anywhere, or a clause that is only stop keywords plus
    filler ("STOP", "STOP STOP", "stop it now", "REVOKE.")."""
    lowered = (text or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    if _SMS_PHRASE_RE.search(lowered):
        return True
    normalized_stop = {keyword.replace(" ", "") for keyword in SMS_STOP_KEYWORDS}
    for clause in _SMS_CLAUSE_RE.split(lowered):
        tokens = [t for t in re.findall(r"[a-z]+(?:-[a-z]+)?", clause or "") if t not in _SMS_FILLER]
        if tokens and set(tokens) <= normalized_stop:
            return True
    return False

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
    """True when an inbound message body is a TCPA opt-out request.

    Was: the WHOLE body, letters only, had to be one keyword, so "REVOKE",
    "STOP please", "Stop texting me" and "please take me off your list"
    recorded no suppression and later messages could still be approved
    (audit 25.3). Now: SMS keywords, word-bounded SMS phrases, or a clause of
    only stop keywords plus filler (requests_opt_out). Spoken opt-out keeps its
    own, narrower rule in inbound_voice; the two deliberately differ."""
    token = re.sub(r"[^a-z]", "", (body or "").strip().lower())
    return token in SMS_STOP_KEYWORDS or requests_opt_out(body)


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
    has_prior_business: bool = False,
    purpose: Purpose | str = Purpose.MARKETING,
) -> OutreachDecision:
    """Core gate. Combines persisted facts (suppression / consent / frequency,
    supplied by the caller via ConsentLedger) with the pure rule data above into
    a single allow/deny decision. Keeping the DB lookups out of here makes every
    branch unit-testable.

    Consent facts (ConsentLedger.gather_state): ``has_consent`` = prior express
    consent, written (any evidence status) or oral; ``has_prior_business`` = an
    established business relationship; ``has_written_consent`` = DOCUMENTED
    written consent; ``has_voiceprint_consent`` = a DOCUMENTED voice-channel
    BIPA release.
    """
    channel = Channel(channel)
    voice_mode = VoiceMode(voice_mode)
    purpose = Purpose(purpose)
    contact = normalize_contact(contact, channel)
    sc = (state_code or "").upper() or None
    blockers: list[str] = []
    warnings: list[str] = []

    # 1. Suppression / opt-out is absolute — it overrides any consent on file.
    if suppressed:
        blockers.append("contact is on the do-not-contact / opt-out list")

    # 2. Consent. AI artificial-voice calls require prior express WRITTEN consent
    #    (FCC 24-17): oral consent, an established business relationship and a
    #    written claim recorded without evidence do NOT qualify, so the VOICE
    #    gate keys off has_written_consent (documented) specifically. A live
    #    agent call keeps consent or a prior business relationship as its basis.
    #    SMS (26.5): a marketing text needs documented written consent; an
    #    informational text prior express consent. A prior business
    #    relationship is never consent for a text.
    if channel is Channel.VOICE:
        if voice_mode is VoiceMode.AI and not has_written_consent:
            blockers.append(
                "no documented prior express WRITTEN consent on file for voice "
                "(FCC 24-17 — oral / prior-business relationship / consent recorded without "
                "evidence does not qualify for AI voice)"
            )
        elif voice_mode is VoiceMode.AGENT and not (has_consent or has_prior_business):
            blockers.append("no consent or prior-business basis on file for agent voice")
    elif channel is Channel.SMS:
        if purpose is Purpose.MARKETING and not has_written_consent:
            blockers.append(
                "no documented prior express WRITTEN consent on file for a marketing text (sms) "
                "— oral consent, a prior business relationship and consent recorded without "
                "evidence do not qualify"
            )
        elif purpose is Purpose.INFORMATIONAL and not has_consent:
            blockers.append(
                "no prior express consent on file for sms "
                "(a prior business relationship does not count for texts)"
            )

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
            blockers.append(f"no documented BIPA voiceprint consent on file for {sc} recipient")
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


def _tombstone_hash(tenant_id: str, normalized: str) -> Optional[str]:
    """The keyed hash erasure stores for an opt-out it had to delete. None
    when no master key is configured (then no tombstone can exist either)."""
    if not os.getenv("ORACLE_ENCRYPTION_MASTER_KEY"):
        return None
    try:
        from privacy_lifecycle import contact_hmac

        return contact_hmac(tenant_id, normalized)
    except Exception:  # noqa: BLE001 - never let the hash block the check itself
        return None


# ── The CRM's flags and the outreach ledger (external audit 26.1) ───────────
# The outreach ledger (outreach_suppression) is the one suppression authority:
# every send gate reads it. A contact's CRM "do not contact" flags are written
# into it in the CRM edit's own transaction, and nurture reads it. Insert-only:
# turning a CRM flag off never lifts an opt-out (the stricter decision wins;
# lifting one is its own reviewed path).
_CRM_SUPPRESS = """
    INSERT INTO outreach_suppression (tenant_id, contact, channel, reason, source_text)
    VALUES ($1::uuid, $2, $3, 'manual_dnc', $4)
    ON CONFLICT (tenant_id, contact, channel) WHERE lifted_at IS NULL DO NOTHING
"""
_LEDGER_SUPPRESSED = """
    SELECT EXISTS(SELECT 1 FROM outreach_suppression
                   WHERE contact = $1 AND channel IN ($2, '*') AND lifted_at IS NULL)
        OR ($3::text IS NOT NULL AND EXISTS(SELECT 1 FROM suppression_tombstones
                   WHERE contact_hmac = $3 AND channel IN ($2, '*')))
"""


def crm_suppression_rows(phone: Optional[str], email: Optional[str],
                         flags: dict) -> list[tuple[str, str]]:
    """(contact, channel) ledger rows for a contact's CRM suppression flags:
    global -> every channel of the phone and the email; dnc (do not contact
    by phone) -> every channel of the phone; sms / voice -> that channel of
    the phone; email -> the email."""
    p = normalize_contact(phone, Channel.SMS) if phone else None
    e = normalize_contact(email, Channel.EMAIL) if email else None
    rows: set[tuple[Optional[str], str]] = set()
    if flags.get("global"):
        rows |= {(p, "*"), (e, "*")}
    if flags.get("dnc"):
        rows.add((p, "*"))
    if flags.get("sms"):
        rows.add((p, "sms"))
    if flags.get("voice"):
        rows.add((p, "voice"))
    if flags.get("email"):
        rows.add((e, "email"))
    return sorted((c, ch) for c, ch in rows if c)


async def record_crm_suppression(conn, tenant_id: str, *, phone: Optional[str], email: Optional[str],
                                 flags: dict, source: str) -> int:
    """Write a contact's CRM suppression flags into the outreach ledger on the
    caller's connection (same transaction as the CRM edit). Returns rows added."""
    added = 0
    for contact, channel in crm_suppression_rows(phone, email, flags):
        status = await conn.execute(_CRM_SUPPRESS, tenant_id, contact, channel, source[:500])
        added += int(str(status).split()[-1] or 0)
    return added


async def ledger_suppressed(conn, tenant_id: str, contact: Optional[str], channel: Channel | str) -> bool:
    """Is this destination suppressed for this channel in the outreach ledger
    (an opt-out, a CRM do-not-contact, an erased person's tombstone)? The same
    rule the send gate applies (ConsentLedger.gather_state)."""
    if not contact:
        return False
    channel = Channel(channel)
    norm = normalize_contact(contact, channel)
    if channel is not Channel.EMAIL:
        # A spoken STOP still spooled (not yet written) blocks, as at the gate.
        import voice_opt_out

        if voice_opt_out.pending_opt_out(tenant_id, normalize_contact(contact, Channel.VOICE)):
            return True
    return bool(await conn.fetchval(_LEDGER_SUPPRESSED, norm, channel.value, _tombstone_hash(tenant_id, norm)))


# ── Persistence layer ────────────────────────────────────────────────────────
class ConsentLedger:
    """Async accessor for the 0015 consent / suppression / attempt tables. All
    queries run through tenant_tx so RLS scopes rows to the caller's tenant."""

    @staticmethod
    async def gather_state(conn, contact: str, channel: Channel, tombstone: Optional[str] = None,
                           *, tenant_id: str):
        """One round-trip on a caller-supplied connection returning every fact
        guard_outreach needs: suppression, channel consent, BIPA voiceprint
        consent, and the 24h voice-attempt count. Replaces what used to be four
        separate tenant_tx acquisitions.

        Consent and the attempt count are filtered to ``tenant_id`` explicitly:
        a PLATFORM_ADMIN context (the command worker that places AI calls) reads
        EVERY brokerage's rows under RLS, so another brokerage's release must not
        authorise this one's call (review of the 26.7/26.8 fix). Suppression is
        deliberately not narrowed: there a wider read only blocks more.

        26.5 / 26.6 (0183): ``has_consent`` is prior express consent (written in
        any evidence status, or oral); a prior business relationship is its own
        fact (a basis for a live agent call only); written consent and the
        voiceprint release count only when ``evidence_status = 'documented'``."""
        return await conn.fetchrow(
            """
            SELECT
                (EXISTS(SELECT 1 FROM outreach_suppression
                        WHERE contact = $1 AND channel IN ($2, '*')
                          AND lifted_at IS NULL)
                 -- An erased person's opt-out survives only as a keyed hash
                 -- (0122 suppression_tombstones); it still blocks.
                 OR ($3::text IS NOT NULL AND EXISTS(SELECT 1 FROM suppression_tombstones
                        WHERE contact_hmac = $3 AND channel IN ($2, '*')))) AS suppressed,
                -- expires_at is consent validity (0183 column comment).
                EXISTS(SELECT 1 FROM outreach_consent
                        WHERE tenant_id = $4::uuid AND contact = $1 AND channel = $2
                          AND consent_type IN ('express_written', 'express_oral')
                          AND revoked_at IS NULL
                          AND (expires_at IS NULL OR expires_at > now()))  AS has_consent,
                EXISTS(SELECT 1 FROM outreach_consent
                        WHERE tenant_id = $4::uuid AND contact = $1 AND channel = $2
                          AND consent_type = 'prior_business'
                          AND revoked_at IS NULL
                          AND (expires_at IS NULL OR expires_at > now()))  AS has_prior_business,
                EXISTS(SELECT 1 FROM outreach_consent
                        WHERE tenant_id = $4::uuid AND contact = $1 AND channel = $2
                          AND consent_type = 'express_written'
                          AND evidence_status = 'documented'
                          AND revoked_at IS NULL
                          AND (expires_at IS NULL OR expires_at > now()))  AS has_written_consent,
                -- A BIPA release counts only on the voice channel, only until
                -- it expires (external audit 26.8) and only when documented
                -- (26.6). One recorded under 'sms' never counts (L09-13).
                EXISTS(SELECT 1 FROM outreach_consent
                        WHERE tenant_id = $4::uuid AND contact = $1 AND channel = 'voice'
                          AND consent_type = 'biometric_voiceprint'
                          AND evidence_status = 'documented'
                          AND revoked_at IS NULL
                          AND (expires_at IS NULL OR expires_at > now())) AS vp_consent,
                (SELECT count(*) FROM outreach_attempt_log
                        WHERE tenant_id = $4::uuid AND contact = $1 AND channel = 'voice'
                          AND allowed = true
                          AND attempted_at > now() - interval '24 hours') AS recent_voice
            """,
            contact, channel.value, tombstone, tenant_id,
        )

    @staticmethod
    async def record_consent(
        ctx: TenantContext, *, contact: str, channel: Channel, consent_type: str,
        state_code: Optional[str], proof_source: Optional[str], proof_text: Optional[str],
        lead_id: Optional[str] = None, client_id: Optional[str] = None,
        expires_at: Optional[datetime] = None, evidence_ref: Optional[str] = None,
        captured_at: Optional[datetime] = None,
    ) -> tuple[str, str]:
        """Insert one consent row; returns (id, evidence_status). The evidence
        status is computed HERE from what was supplied (never taken from the
        caller), and recorded_by is the signed-in user (26.6, 0183)."""
        proof_source = (proof_source or "").strip().lower() or None
        proof_text = (proof_text or "").strip() or None
        evidence_ref = (evidence_ref or "").strip() or None
        if captured_at is not None and captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
        evidence_status = consent_evidence_status(
            proof_source=proof_source, proof_text=proof_text, evidence_ref=evidence_ref,
            captured_at=captured_at, now_utc=datetime.now(timezone.utc))
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                """INSERT INTO outreach_consent
                       (tenant_id, lead_id, client_id, contact, channel, consent_type,
                        state_code, proof_source, proof_text, expires_at,
                        evidence_status, evidence_ref, recorded_by, captured_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,COALESCE($14, now()))
                   RETURNING id, evidence_status""",
                ctx.tenant_id, lead_id, client_id, contact, channel.value, consent_type,
                state_code, proof_source, proof_text, expires_at,
                evidence_status, evidence_ref, ctx.agent_id, captured_at,
            )
        return str(row["id"]), str(row["evidence_status"])

    @staticmethod
    async def suppress(
        ctx: TenantContext, *, contact: str, channel: str, reason: str,
        source_text: Optional[str] = None,
    ) -> None:
        if source_text:
            # The opt-out message is kept as proof; a card number in it is not.
            import pan_redaction

            source_text = pan_redaction.scrub(source_text, "opt_out_source_text",
                                              tenant_id=ctx.tenant_id)
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
            await ConsentLedger.insert_attempt(
                conn, ctx, contact=contact, channel=channel, state_code=state_code,
                allowed=allowed, block_reason=block_reason)

    @staticmethod
    async def insert_attempt(
        conn, ctx: TenantContext, *, contact: str, channel: Channel,
        state_code: Optional[str], allowed: bool, block_reason: Optional[str],
    ) -> None:
        """The attempt-log INSERT on a caller-supplied connection."""
        await conn.execute(
            """INSERT INTO outreach_attempt_log
                   (tenant_id, contact, channel, state_code, allowed, block_reason)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            ctx.tenant_id, contact, channel.value, state_code, allowed, block_reason,
        )

    @staticmethod
    async def lock_voice_attempts(conn, tenant_id: str, contact: str) -> None:
        """Serialise voice attempts to one contact within one brokerage until the
        surrounding transaction ends (external audit 26.7)."""
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                           f"outreach-voice:{tenant_id}:{contact}")


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
    purpose: Purpose | str = Purpose.MARKETING,
) -> OutreachDecision:
    """Resolve persisted consent/suppression/frequency, evaluate, optionally log
    the attempt, and return the decision. Callers MUST check `.allowed` and, when
    permitted, deliver `.required_disclosures`. Raises nothing — see
    ``enforce_outreach`` for the raising variant.

    ``purpose`` (26.5): every production call site passes it explicitly with a
    one-line reason (tests/test_outreach_purpose.py checks); the default is the
    strict MARKETING so a forgotten one fails closed.

    The state read uses ``conn`` when supplied (reusing an already-open
    ``tenant_tx`` — the email send path in crm.py does this) to avoid a nested
    pool acquisition; otherwise one is acquired. The attempt log is written on a
    SEPARATE, independently-committed transaction so the denial record survives
    even when the caller's surrounding tx rolls back (e.g. enforce_outreach
    raises 451 inside that tx).

    A LOGGED voice attempt is the exception (external audit 26.7): the 24-hour
    count, the decision and the attempt row are one transaction of its own,
    under an advisory lock on (brokerage, contact). Otherwise two workers
    calling the same person at once both read the count below the cap and both
    dial. That path never reuses ``conn``: the claim must commit on its own."""
    channel = Channel(channel)
    now_utc = now_utc or datetime.now(timezone.utc)
    norm = normalize_contact(contact, channel)
    is_voice = channel is Channel.VOICE
    tombstone = _tombstone_hash(ctx.tenant_id, norm)

    import voice_opt_out  # a spoken STOP still being written counts as suppressed (23.5)

    def decide(row) -> OutreachDecision:
        suppressed = bool(row["suppressed"]) or (
            channel in (Channel.SMS, Channel.VOICE)
            and voice_opt_out.pending_opt_out(ctx.tenant_id, normalize_contact(contact, Channel.VOICE)))
        return evaluate(
            channel=channel, contact=norm, state_code=state_code, now_utc=now_utc,
            suppressed=suppressed, has_consent=bool(row["has_consent"]),
            has_prior_business=bool(row["has_prior_business"]),
            has_written_consent=bool(row["has_written_consent"]),
            has_voiceprint_consent=bool(row["vp_consent"]) if is_voice else False,
            recent_voice_attempts=int(row["recent_voice"]) if is_voice else 0,
            tz_name=tz_name,
            voice_mode=voice_mode,
            recording_enabled=recording_enabled,
            purpose=purpose,
        )

    if is_voice and log:
        async with tenant_tx(ctx) as c:
            await ConsentLedger.lock_voice_attempts(c, ctx.tenant_id, norm)
            decision = decide(await ConsentLedger.gather_state(
                c, norm, channel, tombstone, tenant_id=ctx.tenant_id))
            await ConsentLedger.insert_attempt(
                c, ctx, contact=norm, channel=channel, state_code=decision.state_code,
                allowed=decision.allowed,
                block_reason=decision.blockers[0] if decision.blockers else None,
            )
        return decision

    async with _conn_or_tx(ctx, conn) as c:
        row = await ConsentLedger.gather_state(c, norm, channel, tombstone, tenant_id=ctx.tenant_id)
    decision = decide(row)

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
    kwargs as ``guard_outreach`` (including ``conn`` to reuse an open tx).
    Purpose: forwarded as given — each caller classifies its own send (26.5);
    absent, guard_outreach's strict MARKETING default applies."""
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
    """What a person says they obtained. There is no evidence_status or
    recorded_by field: the server sets both (26.6); extra keys are ignored."""
    contact: str = Field(..., min_length=3, max_length=320)
    channel: Channel
    consent_type: str = Field("express_written")
    state_code: Optional[str] = Field(None, max_length=2)
    # Capture method: one of DOCUMENTED_PROOF_SOURCES makes documented evidence
    # possible; any other value is kept but stays attested_unverified.
    proof_source: Optional[str] = Field(None, max_length=64)
    # The disclosure text the person agreed to.
    proof_text: Optional[str] = Field(None, max_length=20_000)
    # Signed-event / envelope / document / message / recording id.
    evidence_ref: Optional[str] = Field(None, max_length=500)
    # When the person gave consent; omitted = now. A future time is not evidence.
    captured_at: Optional[datetime] = None
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
    # The dry run answers for the message the caller intends; unsaid = strict.
    purpose: Purpose = Purpose.MARKETING


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
    if body.consent_type == "biometric_voiceprint" and (
            body.channel is not Channel.VOICE or _infer_channel(body.contact) is Channel.EMAIL):
        # The gate reads a voiceprint release on the voice channel only, for a
        # phone number (26.8).
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "a biometric voiceprint release is recorded on the voice channel "
                            "for a phone number")
    norm = normalize_contact(body.contact, body.channel)
    # A claim without documented evidence is still kept (it is what the agent
    # says happened) but stored attested_unverified, and the answer says what
    # that means (26.6).
    consent_id, evidence_status = await ConsentLedger.record_consent(
        ctx, contact=norm, channel=body.channel, consent_type=body.consent_type,
        state_code=(body.state_code or None), proof_source=body.proof_source,
        proof_text=body.proof_text, lead_id=body.lead_id, client_id=body.client_id,
        evidence_ref=body.evidence_ref, captured_at=body.captured_at,
    )
    # The id, never the number or address (CONTRACT C5.4, audit A06).
    logger.info("Consent recorded: id=%s %s (%s, %s) tenant=%s", consent_id, body.channel.value,
                body.consent_type, evidence_status, ctx.tenant_id)
    notice = (UNVERIFIED_CONSENT_NOTICE
              if evidence_status != EVIDENCE_DOCUMENTED and body.consent_type in EVIDENCE_GATED_TYPES
              else None)
    return {"consent_id": consent_id, "contact": norm, "evidence_status": evidence_status,
            "evidence_notice": notice}


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
    logger.info("Opt-out recorded: ch=%s reason=%s tenant=%s", ch, body.reason, ctx.tenant_id)
    return {"contact": norm, "channel": ch, "suppressed": True}


@router.post("/inbound")
async def handle_inbound(body: InboundMessage, ctx: TenantContext = Depends(require_context)):
    """Inbound message webhook. A STOP/UNSUBSCRIBE/etc body suppresses the
    contact across all channels (TCPA: honour within 10 business days)."""
    if is_stop_keyword(body.body):
        norm = normalize_contact(body.contact, body.channel)
        await ConsentLedger.suppress(ctx, contact=norm, channel="*", reason="stop_keyword",
                                     source_text=body.body[:200])
        logger.info("STOP honoured: %s contact suppressed (tenant=%s)", body.channel.value, ctx.tenant_id)
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
        # Purpose: what the caller says it intends to send; absent = MARKETING (strict).
        purpose=body.purpose,
    )
    return decision.as_dict()
