"""Deterministic contact identity, minimal intake, and nurture policy helpers.

This module intentionally has no property-search, MLS, valuation, or public-
record client. Intake collects exactly three answers and hands them to a human
or a separately authorized workflow.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from crypto import CryptoError, decrypt_pii, derive_tenant_key, encrypt_pii


BUYER_INTAKE_QUESTIONS: tuple[str, str, str] = (
    "What is your target budget?",
    "How many bedrooms and bathrooms do you need?",
    "What area or ZIP code are you targeting?",
)
SELLER_INTAKE_QUESTIONS: tuple[str, str, str] = (
    "What is the property address?",
    "What is your desired timeline?",
    "What outcome are you hoping for?",
)
INTAKE_QUESTION_SET_VERSION = "neoh-intake-v1"
INTAKE_TOOL_ACCESS: tuple[()] = ()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
_ZIP_RE = re.compile(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_BED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bed(?:room)?s?|br)\b", re.IGNORECASE)
_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bath(?:room)?s?|ba)\b", re.IGNORECASE)
_MONEY_RE = re.compile(
    r"(?:\$|usd\s*)?\s*(\d+(?:\.\d+)?)\s*([km])?\b",
    re.IGNORECASE,
)

IntakePersona = Literal["buyer", "seller"]
NurtureEventType = Literal["birthday", "home_anniversary"]
NurtureChannel = Literal["email", "sms"]


class ContactTruthConfigError(RuntimeError):
    """Raised when contact encryption cannot be configured safely."""


def normalize_full_name(value: str) -> str:
    """Normalize Unicode and whitespace while preserving display casing."""
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = " ".join(normalized.split())
    if not normalized:
        raise ValueError("full_name must not be empty")
    return normalized


def normalize_email(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not _EMAIL_RE.fullmatch(normalized):
        raise ValueError("email is not valid")
    local, domain = normalized.rsplit("@", 1)
    try:
        ascii_domain = domain.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("email domain is not valid") from exc
    canonical = f"{local.casefold()}@{ascii_domain.casefold()}"
    if len(canonical) > 254:
        raise ValueError("email is too long")
    return canonical


def normalize_phone(value: str | None) -> str | None:
    """Return an E.164-shaped number without silently accepting extensions."""
    if value is None or not value.strip():
        return None
    raw = unicodedata.normalize("NFKC", value).strip()
    if re.search(r"(?:ext\.?|extension|x)\s*\d+\s*$", raw, re.IGNORECASE):
        raise ValueError("phone extensions must be stored separately")
    digits = "".join(character for character in raw if character.isdigit())
    if raw.startswith("+"):
        if not 8 <= len(digits) <= 15:
            raise ValueError("phone must contain 8 to 15 digits")
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    raise ValueError("phone must include a country code")


def _tenant_key(tenant_id: str) -> str:
    master_key = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not master_key:
        raise ContactTruthConfigError(
            "contact encryption is unavailable because the master key is not configured"
        )
    try:
        return derive_tenant_key(tenant_id, master_key)
    except CryptoError as exc:
        raise ContactTruthConfigError("contact encryption could not be initialized") from exc


def lookup_hash(tenant_id: str, field: Literal["email", "phone"], normalized: str | None) -> str | None:
    """Build a tenant-separated exact-match token; never persist raw lookup PII."""
    if normalized is None:
        return None
    if field not in ("email", "phone"):
        raise ValueError("field must be email or phone")
    key = bytes.fromhex(_tenant_key(tenant_id))
    message = f"contact-lookup-v1:{field}:{normalized}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _blind_index_hash(tenant_id: str, namespace: str, value: str) -> str:
    """Return a tenant-separated blind-index token for normalized search text."""
    key = bytes.fromhex(_tenant_key(tenant_id))
    message = f"contact-blind-index-v1:{namespace}:{value}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _name_words(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return [part for part in re.findall(r"[^\W_]+", normalized, re.UNICODE) if part]


def name_search_tokens(tenant_id: str, full_name: str) -> list[str]:
    """Build deterministic, non-reversible word-prefix tokens for a contact name.

    Prefixes make normal CRM searches such as ``sam joh`` useful without
    storing a plaintext or reversibly encrypted search column. The tenant key
    prevents correlation of the same name across brokerages.
    """
    words = _name_words(normalize_full_name(full_name))
    tokens = {
        _blind_index_hash(tenant_id, "name-prefix", word[:length])
        for word in words
        for length in range(1, len(word) + 1)
    }
    return sorted(tokens)


def name_query_tokens(tenant_id: str, query: str) -> list[str]:
    """Build the blind-index tokens required for every word in a name query."""
    words = _name_words(query)
    return sorted(
        {_blind_index_hash(tenant_id, "name-prefix", word) for word in words}
    )


async def seal_json(conn: Any, tenant_id: str, payload: Mapping[str, Any]) -> bytes:
    plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    try:
        return await encrypt_pii(conn, plaintext, _tenant_key(tenant_id))
    except CryptoError as exc:
        raise ContactTruthConfigError("contact data could not be encrypted") from exc


async def open_json(conn: Any, tenant_id: str, ciphertext: bytes) -> dict[str, Any]:
    try:
        plaintext = await decrypt_pii(conn, ciphertext, _tenant_key(tenant_id))
        payload = json.loads(plaintext)
    except (CryptoError, json.JSONDecodeError, TypeError) as exc:
        raise ContactTruthConfigError("contact data could not be decrypted") from exc
    if not isinstance(payload, dict):
        raise ContactTruthConfigError("contact data has an invalid encrypted shape")
    return payload


def questions_for(persona: IntakePersona) -> tuple[str, str, str]:
    if persona == "buyer":
        return BUYER_INTAKE_QUESTIONS
    if persona == "seller":
        return SELLER_INTAKE_QUESTIONS
    raise ValueError("persona must be buyer or seller")


def _parse_budget(answer: str) -> int | None:
    match = _MONEY_RE.search(answer.replace(",", ""))
    if match is None:
        return None
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation:
        return None
    suffix = (match.group(2) or "").casefold()
    if suffix == "k":
        amount *= 1_000
    elif suffix == "m":
        amount *= 1_000_000
    if amount < 0 or amount > 10_000_000_000:
        return None
    return int(amount)


def _parse_beds_baths(answer: str) -> tuple[int | None, float | None]:
    bed_match = _BED_RE.search(answer)
    bath_match = _BATH_RE.search(answer)
    bedrooms = int(Decimal(bed_match.group(1))) if bed_match else None
    bathrooms = float(Decimal(bath_match.group(1))) if bath_match else None
    if bedrooms is None or bathrooms is None:
        numbers = _NUMBER_RE.findall(answer)
        if bedrooms is None and numbers:
            bedrooms = int(Decimal(numbers[0]))
        if bathrooms is None and len(numbers) > 1:
            bathrooms = float(Decimal(numbers[1]))
    if bedrooms is not None and not 0 <= bedrooms <= 100:
        bedrooms = None
    if bathrooms is not None and not 0 <= bathrooms <= 100:
        bathrooms = None
    return bedrooms, bathrooms


def normalize_intake_answers(
    persona: IntakePersona,
    answers: Sequence[str],
) -> dict[str, Any]:
    """Normalize only recorded answers; never infer or search for property facts."""
    questions_for(persona)
    if len(answers) != 3:
        raise ValueError("exactly three answers are required")
    cleaned = [" ".join(str(answer).split()) for answer in answers]
    if any(not answer for answer in cleaned):
        raise ValueError("all three answers are required")

    if persona == "buyer":
        bedrooms, bathrooms = _parse_beds_baths(cleaned[1])
        zip_match = _ZIP_RE.search(cleaned[2])
        return {
            "target_budget": _parse_budget(cleaned[0]),
            "target_budget_raw": cleaned[0],
            "bedrooms": bedrooms,
            "bathrooms": bathrooms,
            "area_or_zip": cleaned[2],
            "zip_code": zip_match.group(1) if zip_match else None,
        }

    return {
        "property_address": cleaned[0],
        "desired_timeline": cleaned[1],
        "desired_outcome": cleaned[2],
    }


def consent_granted(consent: Mapping[str, Any], channel: NurtureChannel) -> bool:
    value = consent.get(channel, False)
    if isinstance(value, Mapping):
        return value.get("granted") is True
    return value is True


def channel_suppressed(suppression: Mapping[str, Any], channel: NurtureChannel) -> bool:
    if suppression.get("global") is True:
        return True
    if suppression.get(channel) is True:
        return True
    return channel == "sms" and suppression.get("dnc") is True


def _is_quiet_hour(hour: int, start_hour: int, end_hour: int) -> bool:
    if not 0 <= start_hour <= 23 or not 0 <= end_hour <= 23:
        raise ValueError("quiet-hour boundaries must be between 0 and 23")
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


@dataclass(frozen=True)
class NurtureDecision:
    eligible: bool
    reason: str
    local_date: date
    calendar_year: int
    timezone: str


def evaluate_nurture(
    *,
    event_type: NurtureEventType,
    channel: NurtureChannel,
    event_month: int | None,
    event_day: int | None,
    timezone_name: str,
    consent: Mapping[str, Any],
    suppression: Mapping[str, Any],
    nurture_enabled: bool,
    now: datetime | None = None,
    quiet_start_hour: int = 20,
    quiet_end_hour: int = 8,
) -> NurtureDecision:
    """Evaluate due date, consent, suppression, timezone, and quiet hours."""
    if event_type not in ("birthday", "home_anniversary"):
        raise ValueError("unsupported nurture event type")
    if channel not in ("email", "sms"):
        raise ValueError("unsupported nurture channel")
    try:
        contact_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone is not recognized") from exc
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local_now = current.astimezone(contact_timezone)
    base = {
        "local_date": local_now.date(),
        "calendar_year": local_now.year,
        "timezone": timezone_name,
    }
    if not nurture_enabled:
        return NurtureDecision(False, "nurture_disabled", **base)
    if event_month is None or event_day is None:
        return NurtureDecision(False, "event_date_missing", **base)
    if (local_now.month, local_now.day) != (event_month, event_day):
        return NurtureDecision(False, "not_due", **base)
    if channel_suppressed(suppression, channel):
        return NurtureDecision(False, "suppressed", **base)
    if not consent_granted(consent, channel):
        return NurtureDecision(False, "consent_missing", **base)
    if _is_quiet_hour(local_now.hour, quiet_start_hour, quiet_end_hour):
        return NurtureDecision(False, "quiet_hours", **base)
    return NurtureDecision(True, "eligible", **base)


def nurture_idempotency_key(
    tenant_id: str,
    contact_id: str,
    event_type: NurtureEventType,
    channel: NurtureChannel,
    calendar_year: int,
) -> str:
    if event_type not in ("birthday", "home_anniversary"):
        raise ValueError("unsupported nurture event type")
    if channel not in ("email", "sms"):
        raise ValueError("unsupported nurture channel")
    if not 2000 <= calendar_year <= 2200:
        raise ValueError("calendar_year is out of range")
    return (
        f"nurture:v1:{tenant_id}:{contact_id}:"
        f"{event_type}:{channel}:{calendar_year}"
    )
