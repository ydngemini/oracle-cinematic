"""Static webhook secrets stay out of URLs (security review HOOK-3, L17-10).

The ACS and custom-call webhooks used to authenticate with `?token=<secret>`:
the same long-lived secret in every callback URL Neoh handed a provider. The
access log redacts query strings (log_redaction.py), but every proxy, edge and
provider console that records a URL still kept a reusable credential.

Accepted now, strongest first:

1. ``X-Neoh-Signature: t=<unix>,v1=<hex>``: HMAC-SHA256 over
   ``"<t>.<raw body>"`` with the shared secret, at most five minutes old. For a
   provider that can sign (a custom telephony integration).
2. ``X-Neoh-Webhook-Token: <secret>``: a header, which is not part of the URL.
   Azure Event Grid sets it as a custom delivery property on the subscription.
3. A signed callback URL, ``?exp=<unix>&sig=<hex>``, where sig is
   HMAC-SHA256(secret, "v1|<path>|<exp>"). Neoh builds these for providers
   that can only call back a URL (the ACS mid-call callback, the custom-call
   ``callback_url``). The URL never contains the secret, and it expires
   (ORACLE_WEBHOOK_CALLBACK_TTL_SECONDS, default 24 h).
4. Deprecated: ``?token=<secret>``. Still accepted for one release, because
   calls placed and subscriptions configured by the previous release carry it,
   and each use logs a deprecation warning that never contains the secret.
   ORACLE_WEBHOOK_QUERY_TOKEN_COMPAT=0 turns it off; the next release
   removes it.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from typing import Optional

from fastapi import HTTPException

log = logging.getLogger("oracle.webhook_auth")

SIGNATURE_HEADER = "x-neoh-signature"
TOKEN_HEADER = "x-neoh-webhook-token"
SIGNATURE_TOLERANCE_SECONDS = 300
_DEFAULT_CALLBACK_TTL = 24 * 3600


def _secret(env_name: str) -> str:
    return os.getenv(env_name, "").strip()


def _callback_ttl() -> int:
    try:
        return max(300, min(7 * 24 * 3600, int(os.getenv("ORACLE_WEBHOOK_CALLBACK_TTL_SECONDS",
                                                           str(_DEFAULT_CALLBACK_TTL)))))
    except ValueError:
        return _DEFAULT_CALLBACK_TTL


def _url_signature(secret: str, path: str, exp: int) -> str:
    return hmac.new(secret.encode(), f"v1|{path}|{exp}".encode(), hashlib.sha256).hexdigest()


def body_signature(secret: str, timestamp: int, body: bytes) -> str:
    """The v1 value of X-Neoh-Signature for `body` sent at `timestamp`."""
    return hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()


def signed_callback_url(base: str, path: str, secret: str, *, now: Optional[float] = None) -> str:
    """`base + path` with an expiring signature instead of the static secret."""
    exp = int(now if now is not None else time.time()) + _callback_ttl()
    query = urllib.parse.urlencode({"exp": exp, "sig": _url_signature(secret, path, exp)})
    return f"{base}{path}?{query}"


def _header(request, name: str) -> str:
    headers = getattr(request, "headers", None) or {}
    try:
        return str(headers.get(name) or "")
    except Exception:  # noqa: BLE001 - a malformed header set is just absent
        return ""


def _query(request) -> dict:
    return getattr(request, "query_params", None) or {}


def _signature_ok(header: str, secret: str, body: bytes, now: float) -> bool:
    parts = dict(p.strip().split("=", 1) for p in header.split(",") if "=" in p)
    try:
        ts = int(parts.get("t", ""))
    except ValueError:
        return False
    supplied = parts.get("v1", "")
    if not supplied or abs(now - ts) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    return hmac.compare_digest(body_signature(secret, ts, body), supplied)


def _signed_url_ok(request, secret: str, now: float) -> bool:
    params = _query(request)
    sig, exp_raw = str(params.get("sig", "")), str(params.get("exp", ""))
    if not sig or not exp_raw.isdigit():
        return False
    exp = int(exp_raw)
    if exp < now:
        return False
    url = getattr(request, "url", None)
    path = getattr(url, "path", None) or ""
    return bool(path) and hmac.compare_digest(_url_signature(secret, path, exp), sig)


async def verify_webhook(request, env_name: str, *, now: Optional[float] = None) -> str:
    """Authenticate a provider callback. Returns the method that passed
    ("signature" | "header" | "signed_url" | "query_token_deprecated");
    raises 503 when the secret is not configured and 403 otherwise."""
    secret = _secret(env_name)
    if not secret:
        raise HTTPException(status_code=503, detail="Webhook authentication is not configured.")
    now = time.time() if now is None else now

    signature = _header(request, SIGNATURE_HEADER)
    if signature:
        body = await request.body()
        if _signature_ok(signature, secret, body, now):
            return "signature"
        raise HTTPException(status_code=403, detail="Invalid webhook credential.")

    token = _header(request, TOKEN_HEADER)
    if token:
        if hmac.compare_digest(token.encode(), secret.encode()):
            return "header"
        raise HTTPException(status_code=403, detail="Invalid webhook credential.")

    if _signed_url_ok(request, secret, now):
        return "signed_url"

    legacy = str(_query(request).get("token", ""))
    if legacy and os.getenv("ORACLE_WEBHOOK_QUERY_TOKEN_COMPAT", "1") != "0" \
            and hmac.compare_digest(legacy.encode(), secret.encode()):
        # Never the secret, never the URL: only which webhook still uses it.
        log.warning("DEPRECATED webhook credential: %s accepted a ?token= query secret. "
                    "Move the provider to the %s header or a signed callback URL; "
                    "the query form is removed next release (HOOK-3).", env_name, TOKEN_HEADER)
        return "query_token_deprecated"
    raise HTTPException(status_code=403, detail="Invalid webhook credential.")
