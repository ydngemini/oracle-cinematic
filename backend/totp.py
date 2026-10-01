"""Time-based one-time passwords (RFC 6238) for the platform operator login.

The operator is the one identity with cross-tenant power, and it signed in with
a single static passphrase (security review AUTH-12). This adds the second
factor any authenticator app produces: HMAC-SHA1, 30-second steps, 6 digits —
the defaults every app implements. Standard library only.

Replay: a code proves possession only once. `verify()` returns the accepted
time step; the caller records it (auth._consume_totp_step) so the same code
cannot be used twice within its validity window, on any replica.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Optional
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
# Accept the previous and next step too: phone clocks drift, and a code typed
# at second 29 arrives in the next step.
WINDOW = 1


def new_secret() -> str:
    """A 160-bit base32 secret (RFC 4226's recommended length)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    cleaned = "".join(secret.split()).upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** DIGITS)).zfill(DIGITS)


def verify(secret: str, code: str, *, now: Optional[float] = None) -> Optional[int]:
    """Return the matching time step, or None. Constant-time per candidate."""
    candidate = "".join((code or "").split())
    if len(candidate) != DIGITS or not candidate.isdigit():
        return None
    current = int((time.time() if now is None else now) // STEP_SECONDS)
    matched: Optional[int] = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        if hmac.compare_digest(code_at(secret, step), candidate) and matched is None:
            matched = step
    return matched


def provisioning_uri(secret: str, account: str, issuer: str = "Neoh") -> str:
    """otpauth:// URI an authenticator app imports (scan it as a QR code)."""
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}"
            f"?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP_SECONDS}")
