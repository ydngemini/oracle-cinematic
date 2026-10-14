"""Commercial email basics: unsubscribe link, RFC 8058 one-click headers, and
the sender's postal address.

Every email Neoh sends to a client, lead or contact on a brokerage's behalf
goes through one of two senders — an approved command
(commands_api._execute_command_job, which carries AI drafts, speed-to-lead,
nurture/smart-plan steps and missions) or the composer outbox
(email_outbox.deliver). Until this module neither added an unsubscribe link,
a `List-Unsubscribe` header or the brokerage's postal address (gap T-CS-1 in
docs/compliance/framework-crosswalk.md). Both now call `prepare()`.

Classification (which mail gets this):

    COMMERCIAL — anything addressed to a client, lead or contact through the
        two senders above. Neoh cannot tell a sales pitch from a deal update,
        so every such email is treated as commercial; an unsubscribe link on a
        relationship email is harmless, a missing one on a promotion is not.
    TRANSACTIONAL / SYSTEM — exempt, never footered: password reset and
        operator MFA codes (auth.py), brokerage invitations to agents
        (brokerage_onboarding.py), operator alerts (ops_alerts.py), the AI
        mission digest to the operator (missions/digest.py) and the agent's
        own SMTP test (sales_api.send_test_email). They go straight to
        smtp_mailer.send and never reach prepare(). EXEMPT_SENDERS lists them
        and tests/test_commercial_email.py fails if a new direct
        smtp_mailer.send caller appears without being classified.

The unsubscribe token
    names the brokerage and the recipient without putting either in the URL:
    AES-256-GCM over (tenant uuid || normalized email), keyed by a key DERIVED
    from ORACLE_SECRET_KEY (the secret space_assets.py already derives its
    signed-link key from) — no new secret. The GCM tag is the signature: a
    token that was not minted here, or was altered, does not decrypt. It
    never expires, because an unsubscribe link must keep working long after
    the email was sent (CAN-SPAM: at least 30 days; Gmail/Yahoo: as long as
    the mail is in the inbox). Rotating ORACLE_SECRET_KEY invalidates every
    outstanding link (documented beside ORACLE_SECRET_KEY in .env.example);
    the page then says the link is not valid and asks the person to reply to
    the email instead.

The opt-out
    is written to outreach_suppression (channel 'email', reason
    'stop_keyword' = consumer-initiated, like a STOP text or a spoken
    opt-out), the brokerage-wide table outreach_compliance.guard_outreach
    reads before every send. It is not narrowed to an agent (0140), so every
    agent in the brokerage is blocked, whoever sent the email.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("oracle.commercial_email")

#: Public path the link and the List-Unsubscribe header point at.
UNSUBSCRIBE_PATH = "/api/public/email/unsubscribe/"
TOKEN_PREFIX = "u1"
_KEY_PURPOSE = b"neoh-email-unsubscribe-v1"
_AAD = b"neoh-unsubscribe-token-v1"
_NONCE = 12
_TAG = 16
# 16-byte tenant id + the shortest plausible address ("a@b.c").
_MIN_BLOB = _NONCE + _TAG + 16 + 5
# RFC 5321 caps an address at 254 octets; base64 of the whole blob stays well
# under this. Anything longer was not minted here.
MAX_TOKEN_LEN = 512
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")

ONE_CLICK_POST = "List-Unsubscribe=One-Click"

#: Direct smtp_mailer.send callers that are transactional/system mail, by
#: module: they never carry a commercial footer (see the module docstring).
EXEMPT_SENDERS = frozenset({
    "auth.py",                  # password reset, operator email OTP
    "brokerage_onboarding.py",  # invitation to join a brokerage (to an agent)
    "ops_alerts.py",            # operator alert
    "missions/digest.py",       # AI mission digest to the operator
    "sales_api.py",             # the agent's own SMTP test message
    "command_providers.py",     # the commercial sender itself: callers footer first
})

SETUP_REQUIRED_MESSAGE = (
    "Add your brokerage's postal mailing address. U.S. law (CAN-SPAM) requires "
    "it in commercial email. Until it is added, Neoh holds every email to "
    "clients, leads and contacts."
)

# Owner 2026-10-14: no postal address, no client email. Refused when queued
# (crm.send_message, stage_command) and again at send, so a brokerage that
# clears its address mid-queue sends nothing either. System mail is exempt.
HOLD_MESSAGE = (
    "Not sent: add your brokerage's postal mailing address in setup first. "
    "U.S. law (CAN-SPAM) requires it in every email to clients, leads and contacts."
)


class PostalAddressRequired(RuntimeError):
    """The brokerage has no postal address: commercial email is held."""

    def __init__(self) -> None:
        super().__init__(HOLD_MESSAGE)


class UnsubscribeTokenError(ValueError):
    """The recipient cannot be put into a token (not an email address)."""


def _key() -> bytes:
    from auth import SECRET_KEY

    # Derived, not reused (space_assets._key): an unsubscribe token must not
    # double as anything else signed with the app secret, and vice versa.
    return hmac.new(SECRET_KEY.encode("utf-8"), _KEY_PURPOSE, hashlib.sha256).digest()


def normalize_email(raw: str) -> str:
    from outreach_compliance import Channel, normalize_contact

    return normalize_contact(str(raw or ""), Channel.EMAIL)


def make_token(tenant_id: str, email: str) -> str:
    normalized = normalize_email(email)
    if "@" not in normalized or len(normalized) > 254:
        raise UnsubscribeTokenError("recipient is not an email address")
    nonce = os.urandom(_NONCE)
    sealed = AESGCM(_key()).encrypt(nonce, uuid.UUID(str(tenant_id)).bytes + normalized.encode("utf-8"), _AAD)
    return TOKEN_PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode("ascii").rstrip("=")


def read_token(token: str) -> Optional[tuple[str, str]]:
    """(tenant_id, normalized email) for a token minted here, else None.
    Never raises on hostile input."""
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or len(token) > MAX_TOKEN_LEN:
        return None
    body = token[len(TOKEN_PREFIX):]
    if not _TOKEN_RE.fullmatch(body):
        return None
    try:
        blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(blob) < _MIN_BLOB:
        return None
    try:
        plain = AESGCM(_key()).decrypt(blob[:_NONCE], blob[_NONCE:], _AAD)
        email = plain[16:].decode("utf-8")
    except (InvalidTag, UnicodeDecodeError):
        return None
    if "@" not in email:
        return None
    return str(uuid.UUID(bytes=plain[:16])), email


def unsubscribe_url(token: str) -> str:
    import config

    return config.public_base_url() + UNSUBSCRIBE_PATH + token


def list_unsubscribe_headers(url: str) -> dict[str, str]:
    """RFC 2369 + RFC 8058. Gmail and Yahoo require both for bulk senders,
    and the provider's DKIM signature must cover them (relay-side)."""
    return {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": ONE_CLICK_POST}


def clean_postal_address(raw: Optional[str]) -> Optional[str]:
    """Collapse the address to one tidy line set; None when empty."""
    if raw is None:
        return None
    lines = [" ".join(part.split()) for part in str(raw).replace("\r", "\n").split("\n")]
    text = "\n".join(line for line in lines if line)
    return text or None


def footer(*, brokerage_name: str, postal_address: Optional[str], url: str) -> str:
    name = " ".join(str(brokerage_name or "").split()) or "this brokerage"
    lines = ["", "", "--", name]
    if postal_address:
        lines.append(postal_address)
    lines += [
        f"You are receiving this email from {name} through Neoh.",
        f"Unsubscribe from {name} emails: {url}",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class PreparedEmail:
    body: str
    unsubscribe_url: str
    headers: dict[str, str]
    postal_address_configured: bool


def compose(*, tenant_id: str, recipient: str, body: str, brokerage_name: str,
            postal_address: Optional[str]) -> PreparedEmail:
    url = unsubscribe_url(make_token(tenant_id, recipient))
    address = clean_postal_address(postal_address)
    text = str(body or "").rstrip() + footer(brokerage_name=brokerage_name, postal_address=address, url=url)
    return PreparedEmail(body=text, unsubscribe_url=url, headers=list_unsubscribe_headers(url),
                         postal_address_configured=bool(address))


_SENDER_SQL = "SELECT name, postal_address FROM tenants WHERE id = $1::uuid"


async def _sender(tenant_id: str):
    """The brokerage's name and postal address, read as a worker (the tenants
    policy lets a platform-admin context read its own tenant row)."""
    from db.connection import tenant_tx
    from tenancy import Role, TenantContext

    ctx = TenantContext(agent_id="commercial-email", tenant_id=str(tenant_id), role=Role.PLATFORM_ADMIN)
    async with tenant_tx(ctx) as conn:
        return await conn.fetchrow(_SENDER_SQL, str(tenant_id))


async def require_postal_address(tenant_id: str) -> None:
    """Raise PostalAddressRequired unless the brokerage has a postal address."""
    row = await _sender(tenant_id)
    if not clean_postal_address(row["postal_address"] if row else None):
        # Ids only (log-PII guard). The setup screen shows the same state.
        log.warning("commercial email for tenant %s held: no postal address (setup required)", tenant_id)
        raise PostalAddressRequired()


async def prepare(tenant_id: str, *, recipient: str, body: str) -> PreparedEmail:
    """The footered body and list headers for one commercial email. Raises
    PostalAddressRequired when the brokerage has no postal address: nothing
    is sent (owner 2026-10-14)."""
    row = await _sender(tenant_id)
    name = row["name"] if row else ""
    address = row["postal_address"] if row else None
    prepared = compose(tenant_id=str(tenant_id), recipient=recipient, body=body,
                       brokerage_name=name, postal_address=address)
    if not prepared.postal_address_configured:
        log.warning("commercial email for tenant %s held: no postal address (setup required)", tenant_id)
        raise PostalAddressRequired()
    return prepared


async def record_unsubscribe(tenant_id: str, email: str) -> None:
    """Suppress `email` for the whole brokerage. Idempotent: an existing active
    suppression is left as it is (0015 partial unique index)."""
    from outreach_compliance import ConsentLedger
    from tenancy import Role, TenantContext

    ctx = TenantContext(agent_id="email-unsubscribe", tenant_id=str(tenant_id), role=Role.PLATFORM_ADMIN)
    await ConsentLedger.suppress(ctx, contact=normalize_email(email), channel="email",
                                 reason="stop_keyword", source_text="email unsubscribe link")
    log.info("email unsubscribe recorded for tenant %s", tenant_id)


async def tenant_name(tenant_id: str) -> str:
    from db.connection import tenant_tx
    from tenancy import Role, TenantContext

    ctx = TenantContext(agent_id="email-unsubscribe", tenant_id=str(tenant_id), role=Role.PLATFORM_ADMIN)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(_SENDER_SQL, str(tenant_id))
    return str(row["name"]) if row else ""
