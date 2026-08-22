"""Tenant-safe inbound voice routing and deterministic CRM handoff.

This module owns no Twilio or Qwen credentials.  Signed HTTP/WebSocket entry
points live in ``telephony_api`` and the existing realtime bridge; this service
resolves one agent endpoint, encrypts caller/transcript PII, matches canonical
contacts by tenant-keyed lookup hash, and creates an idempotent callback task.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from crypto import CryptoError, decrypt_pii, derive_tenant_key, encrypt_pii
from contact_truth import (
    BUYER_INTAKE_QUESTIONS,
    INTAKE_QUESTION_SET_VERSION,
    SELLER_INTAKE_QUESTIONS,
    normalize_intake_answers,
    questions_for,
    seal_json,
)
from db.connection import tenant_tx
from outreach_compliance import AI_VOICE_DISCLOSURE, STOP_KEYWORDS
from tenancy import Role, TenantContext

INTAKE_ROUTING_QUESTION = "Are you calling about buying a home or selling a property?"

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_ACCOUNT_SID_RE = re.compile(r"^AC[0-9a-fA-F]{32}$")
_CALL_SID_RE = re.compile(r"^CA[0-9a-fA-F]{32}$")
_TERMINAL_CALL_STATUSES = {
    "completed",
    "busy",
    "failed",
    "no-answer",
    "canceled",
    "declined",
}
_DISCLOSURE_VERSION = "sha256:" + hashlib.sha256(
    AI_VOICE_DISCLOSURE.encode("utf-8")
).hexdigest()[:20]
_MAX_TRANSCRIPT_ITEMS = 80
_MAX_TRANSCRIPT_ITEM_CHARS = 4_000
_MAX_TRANSCRIPT_TOTAL_CHARS = 40_000

# One definition of the route projection — it is read back in four places and
# silently drifted apart before.
_ROUTE_COLUMNS = (
    "id,tenant_id,agent_id,endpoint_key,inbound_did,"
    "twilio_account_sid,intake_mode,forwarding_mode,"
    "forwarding_source_e164,sip_domain,voice_caller_id_e164,"
    "voice_caller_id_verified,sms_sender_e164,sms_sender_type,"
    "agent_forward_e164,forward_on_request,forward_when_ai_unavailable,"
    "forward_timeout_seconds,active"
)


class InboundVoiceError(RuntimeError):
    """Safe inbound-call failure; callers should not expose internal details."""


@dataclass(frozen=True)
class InboundCallBinding:
    call_id: str
    tenant_id: str
    agent_id: str
    route_id: str
    contact_id: Optional[str]
    client_id: Optional[str]
    intake_mode: str


def _platform_context() -> TenantContext:
    return TenantContext(
        agent_id="twilio-inbound-webhook",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )


def _tenant_context(tenant_id: str, agent_id: str) -> TenantContext:
    return TenantContext(
        agent_id=agent_id,
        tenant_id=tenant_id,
        role=Role.AGENT,
    )


def _tenant_key(tenant_id: str) -> str:
    master_key = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not master_key:
        raise InboundVoiceError("Inbound voice encryption is not configured")
    try:
        return derive_tenant_key(tenant_id, master_key)
    except CryptoError as exc:
        raise InboundVoiceError("Inbound voice encryption is unavailable") from exc


def normalize_e164(value: Any) -> str:
    """Use the canonical contact normalizer and enforce a real E.164 result."""
    from contact_truth import normalize_phone

    normalized = normalize_phone(value)
    if not normalized or not _E164_RE.fullmatch(normalized):
        raise ValueError("phone number must be E.164")
    return normalized


def phone_lookup_hash(tenant_id: str, phone_e164: str) -> str:
    """Use the exact keyed contact lookup hash; never persist plaintext search keys."""
    from contact_truth import lookup_hash

    digest = lookup_hash(tenant_id, "phone", phone_e164)
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise InboundVoiceError("Canonical phone lookup is unavailable")
    return digest


def build_inbound_intake_instructions(state: Mapping[str, Any]) -> str:
    """Return the bounded no-tools persona for this resolved inbound route."""
    mode = str(state.get("intake_mode") or "auto")
    if mode not in {"buyer", "seller", "auto"}:
        mode = "auto"

    common = (
        "You are NEOH, an automated real-estate intake assistant on a recorded "
        "line. The disclosure was spoken before the media stream connected. Never "
        "hide that you are AI or that the line is recorded. This persona has no MLS, "
        "public-record, valuation, web-search, messaging, contract, or publishing "
        "tools. Do not search for properties, estimate value, invent facts, promise "
        "results, negotiate, or provide legal or financial advice. Ask one question "
        "at a time and accept the caller's answer without adding qualification "
        "questions. If the caller says stop, remove me, do not contact me, or similar, "
        "acknowledge immediately, say the request will be honored, and end the call. "
    )
    buyer = " Then ask exactly these three buyer questions, verbatim and in order: " + " | ".join(
        BUYER_INTAKE_QUESTIONS
    )
    seller = " Then ask exactly these three seller questions, verbatim and in order: " + " | ".join(
        SELLER_INTAKE_QUESTIONS
    )
    close = (
        " After the third answer, thank the caller and say the agent will follow up. "
        "Do not ask any other intake question."
    )
    if state.get("forward_available"):
        # The transfer itself is performed by the application, not by the model —
        # this only keeps the spoken line truthful when it happens.
        close += (
            " If the caller asks to speak to a person, do not refuse and do not "
            "try to keep handling it yourself: say 'Of course — connecting you to "
            "your agent now, one moment.' and then stop talking. The application "
            "performs the transfer."
        )
    if mode == "buyer":
        return common + buyer + close
    if mode == "seller":
        return common + seller + close
    return (
        common
        + f"First ask this routing question verbatim: {INTAKE_ROUTING_QUESTION}"
        + " If the answer is buyer, use the buyer flow. If the answer is seller, use "
        "the seller flow. Do not combine the flows."
        + buyer
        + seller
        + close
    )


async def resolve_inbound_route(
    endpoint_key: str,
    inbound_did: str,
    account_sid: str,
) -> Optional[dict[str, Any]]:
    """Resolve the signed webhook to one route before any tenant work occurs."""
    try:
        endpoint_uuid = str(uuid.UUID(endpoint_key))
        normalized_did = normalize_e164(inbound_did)
    except (TypeError, ValueError, AttributeError):
        return None
    if not _ACCOUNT_SID_RE.fullmatch(account_sid or ""):
        return None

    async with tenant_tx(_platform_context()) as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_ROUTE_COLUMNS}
              FROM telephony_routes
             WHERE endpoint_key=$1::uuid
               AND inbound_did=$2
               AND twilio_account_sid=$3
               AND active
             LIMIT 1
            """,
            endpoint_uuid,
            normalized_did,
            account_sid,
        )
    return dict(row) if row is not None else None


async def _match_contact(
    conn: Any,
    *,
    tenant_id: str,
    agent_id: str,
    lookup_digest: str,
    normalized_phone: str,
) -> tuple[Optional[str], Optional[str]]:
    row = await conn.fetchrow(
        """
        SELECT ac.id AS contact_id,
               COALESCE(ac.legacy_client_id, matched_client.id) AS client_id
          FROM agent_contacts ac
          LEFT JOIN LATERAL (
              SELECT c.id
                FROM clients c
               WHERE c.tenant_id=ac.tenant_id
                 AND c.contact_id=ac.id
                 AND c.archived_at IS NULL
               ORDER BY c.created_at ASC
               LIMIT 1
          ) matched_client ON true
         WHERE ac.tenant_id=$1::uuid
           AND ac.phone_lookup_hash=$2
           AND ac.deleted_at IS NULL
         ORDER BY (ac.assigned_agent_id=$3) DESC, ac.created_at ASC
         LIMIT 1
        """,
        tenant_id,
        lookup_digest,
        agent_id,
    )
    if row is not None:
        return str(row["contact_id"]), (
            str(row["client_id"]) if row["client_id"] else None
        )

    # Dual-read bridge while 0054 encrypts/backfills legacy client phones.  The
    # comparison is parameterized and the phone is never selected or logged.
    digits = re.sub(r"\D", "", normalized_phone)
    legacy = await conn.fetchrow(
        """
        SELECT id,contact_id
          FROM clients
         WHERE tenant_id=$1::uuid
           AND archived_at IS NULL
           AND regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g')=$2
         ORDER BY created_at ASC
         LIMIT 1
        """,
        tenant_id,
        digits,
    )
    if legacy is None:
        return None, None
    return (
        str(legacy["contact_id"]) if legacy["contact_id"] else None,
        str(legacy["id"]),
    )


async def prepare_inbound_call(
    route: Mapping[str, Any],
    *,
    call_sid: str,
    caller_phone: str,
) -> InboundCallBinding:
    """Create or recover the encrypted call row for a signed Twilio retry."""
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        raise ValueError("Twilio call SID is invalid")
    normalized_phone = normalize_e164(caller_phone)
    tenant_id = str(route.get("tenant_id") or "")
    agent_id = str(route.get("agent_id") or "")
    route_id = str(route.get("id") or "")
    intake_mode = str(route.get("intake_mode") or "auto")
    try:
        uuid.UUID(tenant_id)
        uuid.UUID(route_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InboundVoiceError("Inbound route is invalid") from exc
    if not agent_id or intake_mode not in {"buyer", "seller", "auto"}:
        raise InboundVoiceError("Inbound route is invalid")

    lookup_digest = phone_lookup_hash(tenant_id, normalized_phone)
    ctx = _tenant_context(tenant_id, agent_id)
    async with tenant_tx(ctx) as conn:
        contact_id, client_id = await _match_contact(
            conn,
            tenant_id=tenant_id,
            agent_id=agent_id,
            lookup_digest=lookup_digest,
            normalized_phone=normalized_phone,
        )
        encrypted_phone = await encrypt_pii(
            conn,
            normalized_phone,
            _tenant_key(tenant_id),
        )
        row = await conn.fetchrow(
            """
            INSERT INTO inbound_voice_calls (
                tenant_id,route_id,contact_id,client_id,provider_call_sid,
                intake_mode,caller_phone_lookup_hash,caller_phone_ciphertext,
                handoff_status,disclosure_version,disclosed_at
            ) VALUES (
                $1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,$7,$8,
                $9,$10,now()
            )
            ON CONFLICT (provider_call_sid) DO NOTHING
            RETURNING id,tenant_id,route_id,contact_id,client_id,intake_mode
            """,
            tenant_id,
            route_id,
            contact_id,
            client_id,
            call_sid,
            intake_mode,
            lookup_digest,
            encrypted_phone,
            "matched" if contact_id or client_id else "unqualified",
            _DISCLOSURE_VERSION,
        )
        if row is None:
            row = await conn.fetchrow(
                """
                SELECT id,tenant_id,route_id,contact_id,client_id,intake_mode
                  FROM inbound_voice_calls
                 WHERE provider_call_sid=$1
                   AND tenant_id=$2::uuid
                   AND route_id=$3::uuid
                """,
                call_sid,
                tenant_id,
                route_id,
            )
        if row is None:
            raise InboundVoiceError("Inbound call binding conflict")

    return InboundCallBinding(
        call_id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        agent_id=agent_id,
        route_id=str(row["route_id"]),
        contact_id=str(row["contact_id"]) if row["contact_id"] else None,
        client_id=str(row["client_id"]) if row["client_id"] else None,
        intake_mode=str(row["intake_mode"]),
    )


def _clean_transcript(
    transcript: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    total = 0
    for item in transcript[:_MAX_TRANSCRIPT_ITEMS]:
        role = str(item.get("role") or "").lower()
        if role not in {"caller", "assistant"}:
            continue
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        if not text:
            continue
        text = text[:_MAX_TRANSCRIPT_ITEM_CHARS]
        remaining = _MAX_TRANSCRIPT_TOTAL_CHARS - total
        if remaining <= 0:
            break
        text = text[:remaining]
        cleaned.append({"role": role, "text": text})
        total += len(text)
    return cleaned


_OPT_OUT_PHRASES = (
    "do not contact",
    "don't contact",
    "do not call",
    "don't call",
    "never call",
    "never contact",
    "stop calling",
    "stop contacting",
    "stop texting",
    "stop messaging",
    "quit calling",
    "quit contacting",
    "remove me",
    "take me off",
    "take my name off",
    "lose my number",
    "unsubscribe",
    "opt out",
    "opt me out",
)

# Words that can sit alongside a bare stop keyword without changing that it is
# the whole instruction. "I said stop", "please, stop", "no — stop" are all the
# caller saying stop; "stop by the house on Tuesday" is not.
_OPT_OUT_FILLER = frozenset(
    {
        "i", "im", "id", "ive", "me", "my", "you", "your",
        "please", "just", "now", "really", "actually", "seriously",
        "ok", "okay", "no", "nope", "yes", "yeah", "yep", "well", "so", "and",
        "but", "um", "uh", "look", "listen", "hey", "sir", "maam",
        "said", "say", "saying", "sorry", "thanks", "thank",
    }
)

# Clause boundaries. A stop keyword is an opt-out when it is the entirety of
# some clause the caller uttered, not merely present somewhere in the turn.
# Note the plain hyphen is absent on purpose: splitting on it would break
# "opt-out" into two clauses and lose the keyword entirely.
_CLAUSE_SPLIT_RE = re.compile(r"[.,;:!?—–]+|\b(?:and|but|so|then|because)\b")


def _requested_opt_out(transcript: Sequence[Mapping[str, str]]) -> bool:
    """True when the caller asked not to be contacted again.

    STOP_KEYWORDS is an SMS convention: the whole message body is one word. In a
    free-form voice transcript those same words turn up inside ordinary
    sentences — "I need to sell before the END of the summer", "I want to CANCEL
    my listing with my current agent" — and scanning the joined transcript for
    any of them permanently marked genuine inbound sellers do-not-contact,
    discarded the answers they had just given, and left no path in this module
    that ever clears the flag.

    So: an explicit opt-out phrase anywhere, or a stop keyword that constitutes
    a whole *clause* rather than the whole *utterance*. Requiring the utterance
    to be nothing but the keyword is too strict in the other direction — real
    callers say "I'm not interested, stop." and "I said stop" far more often
    than they say a bare "stop", and under TCPA an opt-out is valid by "any
    reasonable method". Clause scope is what separates those from "stop by the
    house on Tuesday", where the keyword is a verb with an object.
    """
    utterances = [
        item["text"].lower() for item in transcript if item.get("role") == "caller"
    ]
    caller_text = " ".join(utterances)
    if any(phrase in caller_text for phrase in _OPT_OUT_PHRASES):
        return True

    normalized_stop = {keyword.replace(" ", "") for keyword in STOP_KEYWORDS}
    for utterance in utterances:
        for clause in _CLAUSE_SPLIT_RE.split(utterance):
            if not clause:
                continue
            tokens = [
                token
                for token in re.findall(r"[a-z]+(?:-[a-z]+)?", clause)
                if token not in _OPT_OUT_FILLER
            ]
            if len(tokens) == 1 and tokens[0] in normalized_stop:
                return True
    return False


def requested_human_handoff(transcript: Sequence[Mapping[str, str]]) -> bool:
    """True when the caller has asked to reach a person rather than the AI.

    Deliberately conservative and phrase-based, mirroring _requested_opt_out: a
    false positive hands a live caller to the agent's cell, which is annoying but
    safe, while matching on a bare word like "agent" would fire on "my agent
    said..." in ordinary real-estate conversation.
    """
    caller_text = " ".join(
        item["text"].lower() for item in transcript if item.get("role") == "caller"
    )
    if not caller_text:
        return False
    # An opt-out is a stronger, opposite instruction — never transfer on it.
    if _requested_opt_out(transcript):
        return False
    patterns = (
        # Present tense only. "spoke" was here, but past tense cannot express a
        # request — "I already spoke to an agent last week" is narration, and it
        # matched, ending the AI intake mid-question and ringing the agent's cell.
        r"\b(?:talk|speak|connect me|put me through)\b[^.?!]{0,40}"
        r"\b(?:a |an |my |the )?(?:real |actual |live |human )?"
        r"(?:person|human|agent|realtor|broker|someone|somebody)\b",
        r"\b(?:can|could|may)\s+i\s+(?:please\s+)?(?:talk|speak)\b",
        r"\bget me (?:a|an|my|the)\b[^.?!]{0,20}\b(?:person|human|agent|realtor)\b",
        r"\btransfer me\b",
        r"\bis (?:there|anyone|somebody|someone)\b[^.?!]{0,30}\bthere\b",
        r"\bi (?:want|need|would like)\b[^.?!]{0,30}\b(?:a |an )?"
        r"(?:real person|human|live agent)\b",
        r"\breal person\b",
        r"\bstop talking to (?:a |the )?(?:robot|bot|machine|computer)\b",
    )
    return any(re.search(pattern, caller_text) for pattern in patterns)


async def resolve_forward_target(
    call_sid: str,
    *,
    reason: str,
) -> Optional[dict[str, Any]]:
    """The agent phone this live call may be handed to, or None.

    Resolved from the DB rather than the Redis call state so the agent's personal
    number is never held in the cache alongside the call, and so toggling the
    route off takes effect on the very next call.
    """
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        return None
    gate = {
        "caller_request": "r.forward_on_request",
        "ai_unavailable": "r.forward_when_ai_unavailable",
        "turn_limit": "r.forward_on_request",
    }.get(reason)
    if gate is None:
        return None
    async with tenant_tx(_platform_context()) as conn:
        row = await conn.fetchrow(
            f"""
            SELECT r.agent_forward_e164,r.forward_timeout_seconds,
                   r.voice_caller_id_e164,r.inbound_did,r.endpoint_key,c.tenant_id
              FROM inbound_voice_calls c
              JOIN telephony_routes r
                ON r.tenant_id=c.tenant_id AND r.id=c.route_id
             WHERE c.provider_call_sid=$1
               AND r.active
               AND r.agent_forward_e164 IS NOT NULL
               AND {gate}
             LIMIT 1
            """,
            call_sid,
        )
    if row is None:
        return None
    return {
        "forward_e164": row["agent_forward_e164"],
        "timeout_seconds": int(row["forward_timeout_seconds"] or 25),
        # Present the caller's own DID as caller ID so the agent's phone shows
        # the Neoh line, not the caller's number.
        "caller_id": row["voice_caller_id_e164"] or row["inbound_did"],
        "tenant_id": str(row["tenant_id"]),
        "endpoint_key": str(row["endpoint_key"]),
    }


async def record_forward_attempt(
    call_sid: str,
    *,
    reason: str,
    outcome: str = "requested",
) -> None:
    """Stamp the hand-off on the call record. Never raises into the call path."""
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        return
    if reason not in {"caller_request", "ai_unavailable", "turn_limit"}:
        return
    if outcome not in {"requested", "connected", "no_answer", "busy", "failed"}:
        return
    async with tenant_tx(_platform_context()) as conn:
        await conn.execute(
            """
            UPDATE inbound_voice_calls
               SET forwarded_at=COALESCE(forwarded_at,now()),
                   forward_reason=COALESCE(forward_reason,$2),
                   forward_outcome=$3
             WHERE provider_call_sid=$1
            """,
            call_sid,
            reason,
            outcome,
        )


def _resolve_mode_and_answers(
    intake_mode: str,
    transcript: Sequence[Mapping[str, str]],
) -> tuple[str, dict[str, str]]:
    caller_turns = [
        item["text"] for item in transcript if item.get("role") == "caller"
    ]
    resolved_mode = intake_mode
    if intake_mode == "auto" and caller_turns:
        routing_answer = caller_turns[0].lower()
        if re.search(r"\b(sell|seller|selling|list|listing)\b", routing_answer):
            resolved_mode = "seller"
            caller_turns = caller_turns[1:]
        elif re.search(r"\b(buy|buyer|buying|purchase|purchasing)\b", routing_answer):
            resolved_mode = "buyer"
            caller_turns = caller_turns[1:]

    if resolved_mode == "buyer":
        fields = ("target_budget", "bedrooms_and_bathrooms", "area_or_zip")
    elif resolved_mode == "seller":
        fields = ("property_address", "desired_timeline", "desired_outcome")
    else:
        return "auto", {}
    return resolved_mode, {
        field: caller_turns[index]
        for index, field in enumerate(fields)
        if index < len(caller_turns)
    }


async def mark_inbound_streaming(call_sid: str) -> None:
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        return
    async with tenant_tx(_platform_context()) as conn:
        await conn.execute(
            """
            UPDATE inbound_voice_calls
               SET provider_status='in-progress',transcript_status='active',
                   started_at=COALESCE(started_at,now())
             WHERE provider_call_sid=$1
            """,
            call_sid,
        )


async def finalize_inbound_voice_call(
    call_sid: str,
    transcript: Sequence[Mapping[str, Any]],
    state: Optional[Mapping[str, Any]] = None,
) -> None:
    """Encrypt transcript/answers and create one callback or compliance task."""
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        return
    state = state or {}
    if state.get("direction") != "inbound":
        return
    tenant_id = str(state.get("tenant_id") or "")
    agent_id = str(state.get("agent_id") or "")
    try:
        uuid.UUID(tenant_id)
    except (TypeError, ValueError, AttributeError):
        return
    if not agent_id:
        return

    cleaned = _clean_transcript(transcript)
    opted_out = _requested_opt_out(cleaned)
    ctx = _tenant_context(tenant_id, agent_id)
    key = _tenant_key(tenant_id)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT id,client_id,contact_id,intake_mode,callback_task_id,
                   contact_intake_session_id,intake_handoff_task_id
              FROM inbound_voice_calls
             WHERE tenant_id=$1::uuid AND provider_call_sid=$2
             FOR UPDATE
            """,
            tenant_id,
            call_sid,
        )
        if row is None:
            return

        resolved_mode, answers = _resolve_mode_and_answers(
            str(row["intake_mode"]), cleaned
        )
        summary = (
            f"{resolved_mode.title()} intake captured {len(answers)}/3 answers."
            if resolved_mode in {"buyer", "seller"}
            else "Caller intent requires agent qualification."
        )
        if opted_out:
            summary = "Caller requested no further contact; compliance review required."

        transcript_ciphertext = (
            await encrypt_pii(
                conn,
                json.dumps(cleaned, separators=(",", ":")),
                key,
            )
            if cleaned
            else None
        )
        summary_ciphertext = await encrypt_pii(conn, summary, key)
        answers_ciphertext = (
            await encrypt_pii(
                conn,
                json.dumps(
                    {"mode": resolved_mode, "answers": answers},
                    separators=(",", ":"),
                ),
                key,
            )
            if answers
            else None
        )

        intake_session_id = row["contact_intake_session_id"]
        intake_handoff_task_id = row["intake_handoff_task_id"]
        if (
            intake_session_id is None
            and row["contact_id"]
            and not opted_out
            and resolved_mode in {"buyer", "seller"}
            and len(answers) == 3
        ):
            ordered_fields = (
                ("target_budget", "bedrooms_and_bathrooms", "area_or_zip")
                if resolved_mode == "buyer"
                else ("property_address", "desired_timeline", "desired_outcome")
            )
            raw_answer_values = [answers[field] for field in ordered_fields]
            normalized_answers = normalize_intake_answers(
                resolved_mode,
                raw_answer_values,
            )
            raw_payload = {
                "version": INTAKE_QUESTION_SET_VERSION,
                "persona": resolved_mode,
                "questions": list(questions_for(resolved_mode)),
                "answers": raw_answer_values,
                "source": "inbound_voice",
            }
            transcript_payload = {
                "transcript": "\n".join(
                    f"{item['role']}: {item['text']}" for item in cleaned
                )
            }
            intake_session = await conn.fetchrow(
                """
                INSERT INTO contact_intake_sessions (
                    tenant_id,contact_id,client_id,persona,question_set_version,
                    question_count,raw_answers_ciphertext,
                    normalized_fields_ciphertext,transcript_ciphertext,
                    tool_access,status,created_by
                ) VALUES (
                    $1::uuid,$2::uuid,$3::uuid,$4,$5,3,$6::bytea,$7::bytea,
                    $8::bytea,ARRAY[]::text[],'handoff_pending',$9
                ) RETURNING id
                """,
                tenant_id,
                row["contact_id"],
                row["client_id"],
                resolved_mode,
                INTAKE_QUESTION_SET_VERSION,
                await seal_json(conn, tenant_id, raw_payload),
                await seal_json(conn, tenant_id, normalized_answers),
                await seal_json(conn, tenant_id, transcript_payload),
                "neoh-inbound-voice",
            )
            intake_session_id = intake_session["id"]
            intake_task = await conn.fetchrow(
                """
                INSERT INTO intake_handoff_tasks (
                    tenant_id,intake_session_id,contact_id,client_id,title,
                    assigned_agent_id,due_at
                ) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,now())
                RETURNING id
                """,
                tenant_id,
                intake_session_id,
                row["contact_id"],
                row["client_id"],
                f"Review {resolved_mode} voice intake",
                agent_id,
            )
            intake_handoff_task_id = intake_task["id"]

        task_id = row["callback_task_id"]
        if task_id is None:
            if opted_out:
                title = "Process inbound do-not-contact request"
                priority = "urgent"
            elif row["contact_id"] or row["client_id"]:
                title = "Follow up on inbound AI intake"
                priority = "high"
            else:
                title = "Qualify inbound AI caller"
                priority = "high"
            task = await conn.fetchrow(
                """
                INSERT INTO client_tasks (
                    tenant_id,client_id,title,details,status,priority,
                    assignee_id,created_by
                ) VALUES (
                    $1::uuid,$2::uuid,$3,$4,'open',$5,$6,$7
                )
                RETURNING id
                """,
                tenant_id,
                row["client_id"],
                title,
                f"Review encrypted inbound voice call {row['id']}.",
                priority,
                agent_id,
                "neoh-inbound-voice",
            )
            task_id = task["id"]

        if opted_out and row["contact_id"]:
            await conn.execute(
                """
                UPDATE agent_contacts
                   SET suppression = COALESCE(suppression,'{}'::jsonb)
                       || jsonb_build_object(
                           'voice',true,
                           'dnc',true
                       )
                 WHERE tenant_id=$1::uuid AND id=$2::uuid
                """,
                tenant_id,
                row["contact_id"],
            )

        await conn.execute(
            """
            UPDATE inbound_voice_calls
               SET transcript_ciphertext=$3,
                   summary_ciphertext=$4,
                   intake_answers_ciphertext=$5,
                   transcript_status=$6,
                   handoff_status=$7,
                   callback_task_id=$8::uuid,
                   opt_out_requested=$9,
                   contact_intake_session_id=$10::uuid,
                   intake_handoff_task_id=$11::uuid,
                   ended_at=COALESCE(ended_at,now())
             WHERE tenant_id=$1::uuid AND id=$2::uuid
            """,
            tenant_id,
            row["id"],
            transcript_ciphertext,
            summary_ciphertext,
            answers_ciphertext,
            "complete" if cleaned else "failed",
            "do_not_contact" if opted_out else "callback_ready",
            task_id,
            opted_out,
            intake_session_id,
            intake_handoff_task_id,
        )


async def update_inbound_call_status(call_sid: str, call_status: str) -> None:
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        return
    normalized = str(call_status or "").strip().lower()
    if normalized not in _TERMINAL_CALL_STATUSES | {"ringing", "in-progress"}:
        return
    async with tenant_tx(_platform_context()) as conn:
        await conn.execute(
            """
            UPDATE inbound_voice_calls
               SET provider_status=$2,
                   started_at=CASE WHEN $2='in-progress'
                              THEN COALESCE(started_at,now()) ELSE started_at END,
                   ended_at=CASE WHEN $3 THEN COALESCE(ended_at,now()) ELSE ended_at END
             WHERE provider_call_sid=$1
            """,
            call_sid,
            normalized,
            normalized in _TERMINAL_CALL_STATUSES,
        )


async def inbound_call_matches_endpoint(
    endpoint_key: str,
    call_sid: str,
    account_sid: str,
) -> bool:
    try:
        endpoint_uuid = str(uuid.UUID(endpoint_key))
    except (TypeError, ValueError, AttributeError):
        return False
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        return False
    if not _ACCOUNT_SID_RE.fullmatch(account_sid or ""):
        return False
    async with tenant_tx(_platform_context()) as conn:
        return bool(
            await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                      FROM inbound_voice_calls c
                      JOIN telephony_routes r ON r.id=c.route_id
                     WHERE c.provider_call_sid=$1
                       AND r.endpoint_key=$2::uuid
                       AND r.twilio_account_sid=$3
                )
                """,
                call_sid,
                endpoint_uuid,
                account_sid,
            )
        )


async def resolve_inbound_call_route(
    endpoint_key: str,
    call_sid: str,
    account_sid: str,
) -> Optional[dict[str, Any]]:
    """Resolve a status callback to its tenant route before signature validation.

    Twilio signs callbacks with the auth token belonging to the account that
    owns the call.  Looking up the already-bound call lets the public webhook
    select that tenant's encrypted credential without trusting caller supplied
    tenant or agent identifiers.
    """
    try:
        endpoint_uuid = str(uuid.UUID(endpoint_key))
    except (TypeError, ValueError, AttributeError):
        return None
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        return None
    if not _ACCOUNT_SID_RE.fullmatch(account_sid or ""):
        return None
    async with tenant_tx(_platform_context()) as conn:
        row = await conn.fetchrow(
            """
            SELECT r.id,r.tenant_id,r.agent_id,r.endpoint_key,r.inbound_did,
                   r.twilio_account_sid,r.intake_mode,r.active
              FROM inbound_voice_calls c
              JOIN telephony_routes r ON r.id=c.route_id
             WHERE c.provider_call_sid=$1
               AND r.endpoint_key=$2::uuid
               AND r.twilio_account_sid=$3
               AND r.active
             LIMIT 1
            """,
            call_sid,
            endpoint_uuid,
            account_sid,
        )
    return dict(row) if row is not None else None


async def upsert_telephony_route(
    ctx: TenantContext,
    values: Mapping[str, Any],
) -> dict[str, Any]:
    inbound_did = normalize_e164(values.get("inbound_did"))
    account_sid = str(values.get("twilio_account_sid") or "")
    if not _ACCOUNT_SID_RE.fullmatch(account_sid):
        raise ValueError("twilio_account_sid is invalid")

    agent_forward = values.get("agent_forward_e164")
    agent_forward = normalize_e164(agent_forward) if agent_forward else None
    if agent_forward and agent_forward == inbound_did:
        raise ValueError(
            "agent_forward_e164 cannot be the inbound DID — the call would loop"
        )
    # A hand-off with no destination is a dropped caller, not a no-op.
    forward_on_request = bool(values.get("forward_on_request", True)) and bool(agent_forward)
    forward_when_unavailable = (
        bool(values.get("forward_when_ai_unavailable", True)) and bool(agent_forward)
    )

    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO telephony_routes (
                tenant_id,agent_id,inbound_did,twilio_account_sid,intake_mode,
                forwarding_mode,forwarding_source_e164,sip_domain,
                voice_caller_id_e164,voice_caller_id_verified,
                sms_sender_e164,sms_sender_type,active,
                agent_forward_e164,forward_on_request,
                forward_when_ai_unavailable,forward_timeout_seconds
            ) VALUES (
                $1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17
            )
            ON CONFLICT (tenant_id,agent_id) DO UPDATE SET
                inbound_did=EXCLUDED.inbound_did,
                twilio_account_sid=EXCLUDED.twilio_account_sid,
                intake_mode=EXCLUDED.intake_mode,
                forwarding_mode=EXCLUDED.forwarding_mode,
                forwarding_source_e164=EXCLUDED.forwarding_source_e164,
                sip_domain=EXCLUDED.sip_domain,
                voice_caller_id_e164=EXCLUDED.voice_caller_id_e164,
                voice_caller_id_verified=EXCLUDED.voice_caller_id_verified,
                sms_sender_e164=EXCLUDED.sms_sender_e164,
                sms_sender_type=EXCLUDED.sms_sender_type,
                active=EXCLUDED.active,
                agent_forward_e164=EXCLUDED.agent_forward_e164,
                forward_on_request=EXCLUDED.forward_on_request,
                forward_when_ai_unavailable=EXCLUDED.forward_when_ai_unavailable,
                forward_timeout_seconds=EXCLUDED.forward_timeout_seconds
            RETURNING {_ROUTE_COLUMNS},created_at,updated_at
            """,
            ctx.tenant_id,
            ctx.agent_id,
            inbound_did,
            account_sid,
            values.get("intake_mode", "auto"),
            values.get("forwarding_mode", "none"),
            values.get("forwarding_source_e164"),
            values.get("sip_domain"),
            values.get("voice_caller_id_e164"),
            bool(values.get("voice_caller_id_verified", False)),
            values.get("sms_sender_e164"),
            values.get("sms_sender_type"),
            bool(values.get("active", True)),
            agent_forward,
            forward_on_request,
            forward_when_unavailable,
            int(values.get("forward_timeout_seconds", 25) or 25),
        )
    return dict(row)


async def list_telephony_routes(ctx: TenantContext) -> list[dict[str, Any]]:
    async with tenant_tx(ctx) as conn:
        if ctx.is_broker_owner or ctx.is_platform_admin:
            rows = await conn.fetch(
                f"""
                SELECT {_ROUTE_COLUMNS},created_at,updated_at
                  FROM telephony_routes
                 ORDER BY agent_id
                """
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT {_ROUTE_COLUMNS},created_at,updated_at
                  FROM telephony_routes
                 WHERE agent_id=$1
                 ORDER BY agent_id
                """,
                ctx.agent_id,
            )
    return [dict(row) for row in rows]


async def list_inbound_calls(
    ctx: TenantContext,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    key = _tenant_key(ctx.tenant_id)
    async with tenant_tx(ctx) as conn:
        agent_clause = "" if (ctx.is_broker_owner or ctx.is_platform_admin) else "AND r.agent_id=$2"
        args: list[Any] = [ctx.tenant_id]
        if agent_clause:
            args.append(ctx.agent_id)
        args.append(limit)
        limit_position = len(args)
        rows = await conn.fetch(
            f"""
            SELECT c.id,c.contact_id,c.client_id,c.provider_status,c.intake_mode,
                   c.caller_phone_ciphertext,c.summary_ciphertext,
                   c.transcript_status,c.handoff_status,c.opt_out_requested,
                   c.started_at,c.ended_at,c.created_at,r.agent_id
              FROM inbound_voice_calls c
              JOIN telephony_routes r ON r.id=c.route_id
             WHERE c.tenant_id=$1::uuid {agent_clause}
             ORDER BY c.created_at DESC
             LIMIT ${limit_position}
            """,
            *args,
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            caller = await decrypt_pii(conn, row["caller_phone_ciphertext"], key)
            summary = (
                await decrypt_pii(conn, row["summary_ciphertext"], key)
                if row["summary_ciphertext"]
                else None
            )
            result.append(
                {
                    "id": str(row["id"]),
                    "contact_id": str(row["contact_id"]) if row["contact_id"] else None,
                    "client_id": str(row["client_id"]) if row["client_id"] else None,
                    "agent_id": row["agent_id"],
                    "caller_phone": caller,
                    "provider_status": row["provider_status"],
                    "intake_mode": row["intake_mode"],
                    "transcript_status": row["transcript_status"],
                    "handoff_status": row["handoff_status"],
                    "opt_out_requested": bool(row["opt_out_requested"]),
                    "summary": summary,
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "created_at": row["created_at"],
                }
            )
    return result


async def get_inbound_call(ctx: TenantContext, call_id: str) -> Optional[dict[str, Any]]:
    try:
        safe_call_id = str(uuid.UUID(call_id))
    except (TypeError, ValueError, AttributeError):
        return None
    key = _tenant_key(ctx.tenant_id)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT c.id,c.contact_id,c.client_id,c.provider_status,c.intake_mode,
                   c.caller_phone_ciphertext,c.summary_ciphertext,
                   c.transcript_ciphertext,c.intake_answers_ciphertext,
                   c.transcript_status,c.handoff_status,c.opt_out_requested,
                   c.started_at,c.ended_at,c.created_at,r.agent_id
              FROM inbound_voice_calls c
              JOIN telephony_routes r ON r.id=c.route_id
             WHERE c.tenant_id=$1::uuid AND c.id=$2::uuid
               AND ($3 OR r.agent_id=$4)
            """,
            ctx.tenant_id,
            safe_call_id,
            ctx.is_broker_owner or ctx.is_platform_admin,
            ctx.agent_id,
        )
        if row is None:
            return None
        payload = {
            "id": str(row["id"]),
            "contact_id": str(row["contact_id"]) if row["contact_id"] else None,
            "client_id": str(row["client_id"]) if row["client_id"] else None,
            "agent_id": row["agent_id"],
            "caller_phone": await decrypt_pii(conn, row["caller_phone_ciphertext"], key),
            "provider_status": row["provider_status"],
            "intake_mode": row["intake_mode"],
            "transcript_status": row["transcript_status"],
            "handoff_status": row["handoff_status"],
            "opt_out_requested": bool(row["opt_out_requested"]),
            "summary": None,
            "transcript": [],
            "intake": None,
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "created_at": row["created_at"],
        }
        if row["summary_ciphertext"]:
            payload["summary"] = await decrypt_pii(conn, row["summary_ciphertext"], key)
        if row["transcript_ciphertext"]:
            payload["transcript"] = json.loads(
                await decrypt_pii(conn, row["transcript_ciphertext"], key)
            )
        if row["intake_answers_ciphertext"]:
            payload["intake"] = json.loads(
                await decrypt_pii(conn, row["intake_answers_ciphertext"], key)
            )
    return payload
