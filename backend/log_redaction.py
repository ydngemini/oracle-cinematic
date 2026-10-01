"""Keep capability tokens and query-string secrets out of logs and the audit trail.

Several routes carry a credential in the URL itself: public property-upload
links and client-portal sessions put a bearer capability in the path, and the
ACS / custom-call webhooks and the Plivo media bridge take a shared secret or
bridge token in the query string. uvicorn's access log and the audit middleware
both recorded the full URL, so anyone who could read either could replay the
link or forge the webhook (security review HOOK-3 / TEN-4, 2026-10-01).

`redact_path` is the one rule both use: drop the query string entirely and mask
the path segment that follows a known capability prefix.
"""
from __future__ import annotations

import logging

# Path prefixes whose NEXT segment is a bearer capability.
_CAPABILITY_PREFIXES: tuple[str, ...] = (
    "/api/public/property-upload/",
    "/portal/session/",
    "/vault/secure-access/",
    "/api/commands/webhooks/twilio/tts/",
)

REDACTED = ":redacted"


def redact_path(path: str) -> str:
    """Return `path` with any query string removed and capability segments masked."""
    if not isinstance(path, str):
        return path
    base = path.split("?", 1)[0]
    for prefix in _CAPABILITY_PREFIXES:
        if base.startswith(prefix):
            rest = base[len(prefix):]
            tail = rest.split("/", 1)
            masked = REDACTED + ("/" + tail[1] if len(tail) > 1 else "")
            return prefix + masked
    return base


class AccessLogRedactionFilter(logging.Filter):
    """uvicorn.access formats `'%s - "%s %s HTTP/%s" %d'` with the full path
    (including the query string) as the third argument; rewrite it in place."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = args[:2] + (redact_path(args[2]),) + args[3:]
        return True


def install_access_log_redaction() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogRedactionFilter) for f in access.filters):
        access.addFilter(AccessLogRedactionFilter())
