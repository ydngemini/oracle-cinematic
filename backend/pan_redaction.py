"""Keep payment card numbers out of everything Neoh stores or logs (PCI DSS SAQ A).

Neoh takes payment only through Stripe-hosted Checkout and the Stripe customer
portal (billing.py, BillingOverlay.jsx): no card number ever reaches a Neoh
server through the payment flow, which is what keeps the platform in the SAQ A
scope (docs/compliance/pci-saq-a.md). The way that scope breaks is a card number
arriving through another channel — a caller reading it to the AI on a call, a
client texting it, an agent pasting it into a note, an email, a chat message —
and being stored. This module is the one rule every such write path applies
before it stores, sends to a model, or logs the text.

What it finds
  * 13–19 digit numbers, written solid (4111111111111111) or in card-shaped
    groups separated by one consistent space, hyphen, dot or comma
    (4111 1111 1111 1111, 3782-822463-10005), that pass the Luhn check AND
    start with a payment-network prefix valid for that length (Visa, Mastercard,
    Amex, Discover, Diners, JCB, UnionPay, Maestro, Mir, RuPay).
  * A 15- or 16-digit card said as digit words or lone numerals, the way a
    speech transcript writes a number read aloud ("four one one one ..."; "oh"
    counts as zero), alone or followed straight away by an expiry and/or code.
    Dictated 13/14/19-digit numbers are not matched: phone numbers are
    dictated the same way and share those prefixes (Diners vs area code 302).
  * A card security code (CVV/CVC) after an unambiguous keyword ("CVV 123"),
    and after a looser phrase ("security code is 123", "the code on the back")
    only when a card number was found in the same text — "your security code is
    482913" in a login text stays as it is.

What it deliberately leaves alone (tests pin each): phone numbers and pairs of
them, parcel / APN / MLS / folio ids and anything introduced by those words,
prices, ZIP+4, dates, UUIDs, millisecond timestamps, long reference numbers
(a solid run longer than 19 digits is never searched for a card inside it).
One known false positive: two phone numbers (or a phone number and a ZIP)
dictated digit by digit with nothing between them are redacted about 1-2% of
the time (a seeded sample in tests/test_pan_redaction.py bounds it). A
removed phone number is the safe way to be wrong.

Known limits (documented in docs/compliance/pci-saq-a.md): spoken forms other
than single digit words ("forty-one eleven", "double four"), a number split
across two messages or two transcript turns, card numbers in images,
attachments or audio, and expiry dates on their own.

The redactor never returns, logs or records the number it removed: callers get
the cleaned text and a count, and `report()` records only surface + counts.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import uuid as _uuid
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("oracle.pci")

PAN_PLACEHOLDER = "[card number removed]"
CVV_PLACEHOLDER = "[security code removed]"

#: The persona rule every AI voice and chat prompt carries (pinned by
#: tests/test_pan_redaction.py). Neoh never takes payment details itself; any
#: payment goes through a secure payment link.
PAYMENT_CARD_RULE = (
    "Never ask for, repeat, confirm or read back a payment card number, expiry date or "
    "security code (CVV), and never write one into a note, message or tool call. If someone "
    "starts to give card details, stop them politely, say you cannot take payment details "
    "here, and that any payment is made only through a secure payment link, never by phone, "
    "text, email or chat."
)

_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_WORD_ALT = "|".join(sorted(_DIGIT_WORDS, key=len, reverse=True))
# A token is a run of digits or one digit word; neither may touch a letter,
# digit or underscore on its outer side (so "someone", "phone", "a1b2" and the
# digits inside a UUID's hex never start a run).
_TOKEN = rf"(?:(?<![A-Za-z0-9_])\d+(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])(?:{_WORD_ALT})(?![A-Za-z0-9_]))"
_SEP = r"(?:[ \t]*[,.\-][ \t]*|[ \t]+)"
_RUN_RE = re.compile(rf"{_TOKEN}(?:{_SEP}{_TOKEN})*", re.IGNORECASE)
_TOKEN_RE = re.compile(rf"\d+|(?:{_WORD_ALT})", re.IGNORECASE)

#: Card-shaped groupings for a number written in groups.
_LAYOUTS = {
    (4, 4, 4, 1), (4, 4, 4, 2), (4, 4, 4, 3), (4, 4, 4, 4),
    (4, 4, 4, 4, 1), (4, 4, 4, 4, 2), (4, 4, 4, 4, 3),
    (4, 6, 5), (4, 6, 4), (4, 4, 5),
}

#: Words that introduce a non-card identifier; a candidate right after one is
#: left alone (an agent writing "parcel 4012…" means a parcel).
_NOT_A_CARD_CONTEXT = re.compile(
    r"(?:parcel|apn|mls|folio|tax\s*(?:id|map|parcel)|permit|tracking)"
    r"\s*(?:id|no\.?|number|#)?\s*[:#]?\s*$",
    re.IGNORECASE,
)

_CVV_STRICT = r"(?:cvv2?|cvc2?|csc|card\s+verification(?:\s+(?:value|code|number))?)"
_CVV_LOOSE = (
    r"(?:security\s+code|card\s+code|code\s+on\s+the\s+back|(?:three|four|3|4)[\s-]+digit\s+code|"
    r"the\s+(?:three|four|3|4)\s+digits(?:\s+on\s+the\s+back)?)"
)
_CVV_VALUE = rf"(?:(?<![A-Za-z0-9_])\d{{3,4}}(?![A-Za-z0-9_])|(?:{_WORD_ALT})(?:{_SEP}(?:{_WORD_ALT})){{2,3}}(?![A-Za-z0-9_]))"
_CVV_FILLER = r"[\s:=#\-,]*(?:(?:is|it's|its|are|was|of|number|no\.?|code)[\s:=#\-,]*){0,3}"
_CVV_STRICT_RE = re.compile(rf"\b{_CVV_STRICT}\b{_CVV_FILLER}(?P<v>{_CVV_VALUE})", re.IGNORECASE)
_CVV_LOOSE_RE = re.compile(rf"\b{_CVV_LOOSE}\b{_CVV_FILLER}(?P<v>{_CVV_VALUE})", re.IGNORECASE)


@dataclass(frozen=True)
class Redaction:
    text: str
    cards: int = 0
    codes: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.cards or self.codes)


def luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _network_accepts(digits: str) -> bool:
    """Is there a payment network whose numbers start like this AND are this long?"""
    n = len(digits)
    if not 13 <= n <= 19:
        return False
    p1, p2, p3, p4, p6 = digits[0], int(digits[:2]), int(digits[:3]), int(digits[:4]), int(digits[:6])
    if p1 == "4":                                   # Visa
        return n in (13, 16, 19)
    if 51 <= p2 <= 55 or 2221 <= p4 <= 2720:        # Mastercard
        return n == 16
    if p2 in (34, 37):                              # American Express
        return n == 15
    if 2200 <= p4 <= 2204:                          # Mir
        return 16 <= n <= 19
    if p4 == 6011 or 644 <= p3 <= 649 or p2 == 65 or 622126 <= p6 <= 622925:  # Discover
        return 16 <= n <= 19
    if p2 == 62:                                    # UnionPay
        return 16 <= n <= 19
    if 3528 <= p4 <= 3589:                          # JCB
        return 16 <= n <= 19
    if 300 <= p3 <= 305 or p2 in (36, 38, 39):      # Diners Club
        return 14 <= n <= 19
    if p4 in (5018, 5020, 5038, 5893, 6304, 6759, 6761, 6762, 6763):  # Maestro
        return 13 <= n <= 19
    if p2 == 60 or p3 == 508:                       # RuPay
        return n == 16
    return False


def is_card_number(digits: str) -> bool:
    return digits.isdigit() and _network_accepts(digits) and luhn_valid(digits)


@dataclass(frozen=True)
class _Token:
    start: int
    end: int
    digits: str
    word: bool  # a digit word ("four", "oh")

    @property
    def spoken(self) -> bool:
        """A digit word or a lone numeral, as dictation produces."""
        return self.word or len(self.digits) == 1


def _tokens(run: str, offset: int) -> tuple[list[_Token], list[str]]:
    tokens: list[_Token] = []
    seps: list[str] = []
    last_end = None
    for match in _TOKEN_RE.finditer(run):
        raw = match.group(0)
        if last_end is not None:
            seps.append(run[last_end:match.start()].strip() or " ")
        if raw.isdigit():
            tokens.append(_Token(offset + match.start(), offset + match.end(), raw, False))
        else:
            tokens.append(_Token(offset + match.start(), offset + match.end(), _DIGIT_WORDS[raw.lower()], True))
        last_end = match.end()
    return tokens, seps


def _window_is_card(tokens: list[_Token], seps: list[str], i: int, j: int) -> bool:
    window = tokens[i:j]
    digits = "".join(t.digits for t in window)
    if not 13 <= len(digits) <= 19 or not is_card_number(digits):
        return False
    if len(window) == 1:
        return True
    if all(t.spoken for t in window):
        # Dictated digit by digit. Phone numbers are dictated the same way and
        # area codes 300-305/36x/38x/39x share Diners Club's prefixes, so a
        # dictated number counts only in the shapes nearly every US card has:
        # 16 digits (Visa, Mastercard, Discover, JCB) or 15 (Amex).
        return (len(digits) in (15, 16)
                and (digits[0] in "456" or digits[:2] in ("34", "35", "37")
                     or 2221 <= int(digits[:4]) <= 2720))
    if any(t.word for t in window):
        return False                                  # words mixed with numerals: not a card layout
    if len(set(seps[i:j - 1])) != 1:
        return False                                  # one separator, used consistently
    return tuple(len(t.digits) for t in window) in _LAYOUTS


def _card_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for run in _RUN_RE.finditer(text):
        tokens, seps = _tokens(run.group(0), run.start())
        if sum(len(t.digits) for t in tokens) < 13:
            continue
        i = 0
        while i < len(tokens):
            token = tokens[i]
            # A solid run of more than 19 digits is a reference number; never
            # look for a card inside it.
            if not token.spoken and len(token.digits) > 19:
                i += 1
                continue
            # Dictated digits start a card only where the dictation starts (a
            # number read aloud is said first, then the expiry or the code).
            if token.spoken and i > 0 and tokens[i - 1].spoken:
                i += 1
                continue
            # A dictated card is the whole dictation, or the dictation less a
            # trailing expiry (4) and/or security code (3-4) said straight after.
            dictation_end = i
            while dictation_end < len(tokens) and tokens[dictation_end].spoken:
                dictation_end += 1
            dictated = sum(len(t.digits) for t in tokens[i:dictation_end])
            found = None
            total = 0
            for j in range(i + 1, len(tokens) + 1):
                total += len(tokens[j - 1].digits)
                if total > 19:
                    break
                if token.spoken and j <= dictation_end and (dictated - total) not in (0, 3, 4, 7, 8):
                    continue
                if total >= 13 and _window_is_card(tokens, seps, i, j):
                    found = j
            if found is not None and not _NOT_A_CARD_CONTEXT.search(text[max(0, token.start - 40):token.start]):
                spans.append((token.start, tokens[found - 1].end))
                i = found
                continue
            i += 1
    return spans


def _replace(text: str, spans: list[tuple[int, int]], placeholder: str) -> str:
    out, cursor = [], 0
    for start, end in sorted(spans):
        if start < cursor:
            continue
        out.append(text[cursor:start])
        out.append(placeholder)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _code_spans(text: str, loose: bool) -> list[tuple[int, int]]:
    spans = [(m.start("v"), m.end("v")) for m in _CVV_STRICT_RE.finditer(text)]
    if loose:
        spans += [(m.start("v"), m.end("v")) for m in _CVV_LOOSE_RE.finditer(text)]
    return spans


def _may_hold_card(text: str) -> bool:
    digits = sum(c.isdigit() for c in text)
    if digits >= 13:
        return True
    lowered = text.lower()
    if "cv" in lowered or "csc" in lowered or "verification" in lowered:
        return True
    if digits + len(re.findall(rf"\b(?:{_WORD_ALT})\b", lowered)) >= 13:
        return True
    return False


def redact(text: Any) -> Redaction:
    """The text with every card number and card security code replaced."""
    if not isinstance(text, str) or not text or not _may_hold_card(text):
        return Redaction(text if isinstance(text, str) else text)
    spans = _card_spans(text)
    cleaned = _replace(text, spans, PAN_PLACEHOLDER) if spans else text
    codes = _code_spans(cleaned, loose=bool(spans))
    if codes:
        cleaned = _replace(cleaned, codes, CVV_PLACEHOLDER)
    return Redaction(cleaned, cards=len(spans), codes=len(codes))


def redact_obj(value: Any) -> tuple[Any, int, int]:
    """Every string inside a JSON-like value (dict keys are left as they are)."""
    if isinstance(value, str):
        result = redact(value)
        return result.text, result.cards, result.codes
    if isinstance(value, dict):
        cards = codes = 0
        out = {}
        for key, item in value.items():
            out[key], c, k = redact_obj(item)
            cards += c
            codes += k
        return out, cards, codes
    if isinstance(value, (list, tuple)):
        cards = codes = 0
        items = []
        for item in value:
            cleaned, c, k = redact_obj(item)
            items.append(cleaned)
            cards += c
            codes += k
        return (type(value)(items) if isinstance(value, tuple) else items), cards, codes
    return value, 0, 0


# ---------------------------------------------------------------------------
# Recording that something was removed — never what.
# ---------------------------------------------------------------------------

_counts_lock = threading.Lock()
#: Process-local count per surface, for health/diagnostics. Never holds text.
REDACTION_COUNTS: dict[str, int] = {}
_pending: set[asyncio.Task] = set()


async def _audit(surface: str, cards: int, codes: int, tenant_id: Optional[str],
                 target_id: Optional[str]) -> None:
    from audit_ledger import AuditCategory, ledger

    await ledger.record(
        category=AuditCategory.PCI_REDACTION,
        action="payment_card_data_removed",
        tenant_id=tenant_id,
        user_id=None,
        target_id=target_id,
        metadata={"surface": surface, "cards": cards, "codes": codes},
    )


def _uuid_or_none(value: Any) -> Optional[str]:
    try:
        return str(_uuid.UUID(str(value))) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


def report(surface: str, cards: int, codes: int, *, tenant_id: Any = None,
           target_id: Any = None) -> None:
    """A card number or code was removed before `surface` stored the text.

    Records the surface and the counts — a log line, a process counter and an
    audit-ledger event (best effort, off the request path). The number itself
    is gone by the time this runs and is never passed here.
    """
    if not (cards or codes):
        return
    with _counts_lock:
        REDACTION_COUNTS[surface] = REDACTION_COUNTS.get(surface, 0) + cards + codes
    logger.warning("payment card data removed before storage: surface=%s cards=%d codes=%d",
                   surface, cards, codes)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_audit(surface, cards, codes, _uuid_or_none(tenant_id),
                                   _uuid_or_none(target_id)))
    _pending.add(task)
    task.add_done_callback(_audit_done)


def _audit_done(task: asyncio.Task) -> None:
    _pending.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("payment card redaction audit event not recorded: %s",
                       type(task.exception()).__name__)


def scrub(text: Any, surface: str, *, tenant_id: Any = None, target_id: Any = None) -> Any:
    """`redact` + `report`: the call every write path makes before it stores."""
    result = redact(text)
    if result.changed:
        report(surface, result.cards, result.codes, tenant_id=tenant_id, target_id=target_id)
    return result.text


def scrub_obj(value: Any, surface: str, *, tenant_id: Any = None, target_id: Any = None) -> Any:
    cleaned, cards, codes = redact_obj(value)
    if cards or codes:
        report(surface, cards, codes, tenant_id=tenant_id, target_id=target_id)
    return cleaned


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

class PanLogRedactionFilter(logging.Filter):
    """Rewrites a log record whose message or traceback holds a card number.

    Installed on the handlers (a logger's own filters do not see records that
    propagate from its children). Only the written forms are checked here —
    the log stream is not a transcript — and a record without 13 digits or a
    CVV keyword costs one character count.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a malformed record is logging's problem
            return True
        result = redact(message)
        if result.changed:
            record.msg = result.text
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            cleaned = redact(record.exc_text)
            if cleaned.changed:
                record.exc_text = cleaned.text
        return True


def install_log_redaction(*loggers: logging.Logger) -> None:
    """Attach the filter to every handler of root and uvicorn's loggers."""
    targets = list(loggers) or [logging.getLogger(), logging.getLogger("uvicorn"),
                                logging.getLogger("uvicorn.error"),
                                logging.getLogger("uvicorn.access")]
    for target in targets:
        for handler in target.handlers:
            if not any(isinstance(f, PanLogRedactionFilter) for f in handler.filters):
                handler.addFilter(PanLogRedactionFilter())
