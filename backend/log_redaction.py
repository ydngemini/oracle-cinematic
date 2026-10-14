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

import hashlib
import hmac
import logging
import os
import re

# Path prefixes whose NEXT segment is a bearer capability.
_CAPABILITY_PREFIXES: tuple[str, ...] = (
    "/api/public/property-upload/",
    # The unsubscribe token: a capability (anyone holding it can opt that
    # address out) and an encrypted address. Masked like the others.
    "/api/public/email/unsubscribe/",
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
    """Rewrite every request path uvicorn logs, query string included.

    uvicorn.access formats `'%s - "%s %s HTTP/%s" %d'` with the full path as
    the third argument. The WebSocket handshake lines go to *uvicorn.error*
    instead, as `'%s - "WebSocket %s" [accepted]'` with the path second. The
    first version of this filter rewrote only the third argument on the access
    logger, so a WebSocket `?bridge_token=` still reached the log (HOOK-3
    follow-up, 2026-10-14). Any string argument that starts with "/" is treated
    as a path here."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            record.args = tuple(
                redact_path(a) if isinstance(a, str) and a.startswith("/") else a for a in args)
        return True


def install_access_log_redaction() -> None:
    for name in ("uvicorn.access", "uvicorn.error"):
        target = logging.getLogger(name)
        if not any(isinstance(f, AccessLogRedactionFilter) for f in target.filters):
            target.addFilter(AccessLogRedactionFilter())


# ---------------------------------------------------------------------------
# Actor references. agent_id is the user's email (docs/privacy-data-map.md §2),
# so a log line names the actor by a keyed pseudonym instead. One account always
# gets the same reference, so an operator can still follow one actor through
# the logs and can recompute the reference for a known account.
# ---------------------------------------------------------------------------

def actor_ref(agent_id: object) -> str:
    """A stable, non-reversible log reference for an account id."""
    if agent_id is None or agent_id == "":
        return "actor:-"
    key = (os.environ.get("ORACLE_SECRET_KEY", "") or "neoh-log-actor").encode()
    digest = hmac.new(key, b"log-actor:v1:" + str(agent_id).strip().lower().encode(
        "utf-8", errors="replace"), hashlib.sha256).hexdigest()
    return "actor:" + digest[:12]


# ---------------------------------------------------------------------------
# Exception text and messages: a last-line scrub (audit A06, L17-11).
#
# About two hundred logging calls interpolate an exception (`%s`, exc), and
# every `log.exception` prints a traceback. Most carry no personal data, but a
# database error can quote the row it refused (asyncpg's
# `Key (email)=(jane@example.test) already exists`, or `Failing row contains
# (...)`), and an HTTP client error can echo a request body. Rewriting each
# call would not stop the next one, so the root handlers scrub what is
# recognisably personal from the rendered message AND the traceback text before
# any handler formats it. The source guard (tests/test_log_pii_guard.py) still
# forbids passing a named PII value; this catches what no variable name reveals.
# ---------------------------------------------------------------------------

_SCRUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PostgreSQL DETAIL quoting key values: Key (a, b)=(v1, v2)
    (re.compile(r"(Key \([^)]*\))=\((?:[^()]|\([^()]*\))*\)"), r"\1=(:redacted)"),
    # PostgreSQL DETAIL quoting the whole refused row.
    (re.compile(r"(Failing row contains )\((?:[^()]|\([^()]*\))*\)"), r"\1(:redacted)"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"(?<![\w+])\+[1-9]\d{7,14}\b"), "<phone>"),
    (re.compile(r"(?<![\w-])\(?\d{3}\)?[-. ]\d{3}[-.]\d{4}\b"), "<phone>"),
)


def scrub_text(text: str) -> str:
    """Remove emails, phone numbers and quoted row values from `text`."""
    if not text:
        return text
    for pattern, replacement in _SCRUBS:
        text = pattern.sub(replacement, text)
    return text


class PIIScrubFilter(logging.Filter):
    """Handler filter: render the message and scrub it, and pre-render the
    traceback scrubbed (logging.Formatter reuses record.exc_text when set)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must still log
            message = str(record.msg)
        record.msg, record.args = scrub_text(message), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = scrub_text(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub_text(record.stack_info)
        return True


def install_pii_scrub(logger: logging.Logger | None = None) -> None:
    """Attach the scrub to every handler of `logger` (default: the root logger
    and uvicorn's error logger, which does not propagate to root). Not
    uvicorn.access: its formatter unpacks record.args, and its only variable
    text is the client address and the path redact_path already rewrote."""
    targets = [logger] if logger is not None else [
        logging.getLogger(), *(logging.getLogger(n) for n in ("uvicorn", "uvicorn.error"))]
    for target in targets:
        for handler in target.handlers:
            if not any(isinstance(f, PIIScrubFilter) for f in handler.filters):
                handler.addFilter(PIIScrubFilter())
