"""Per-source lookup status: a failed source is never a verified empty result.

CONTRACT C6 vocabulary — one status per source a response depends on:

    checked         the source answered; an empty answer is a verified miss
    timed_out       the source did not answer in time
    unavailable     the source errored, refused the credential, was rate
                    limited, or is not configured on this deployment
    not_applicable  the source was not asked, because it cannot answer this
                    request (no coordinates, unknown FIPS state, ...)

A required source that is not `checked` makes the response `degraded`, and an
empty field next to it means "not checked", never "none" (audit A02; ledger
L11-02 / L11-03). The `reason` is a stable code from REASONS — never exception
text, a URL or the request (which can carry an address or a credential).

This module is the shared form of the pattern first shipped for the client
steward's property lookup (`client_ai_automation.property_lookup_status`).
"""

from __future__ import annotations

import asyncio
import urllib.error
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

CHECKED = "checked"
TIMED_OUT = "timed_out"
UNAVAILABLE = "unavailable"
NOT_APPLICABLE = "not_applicable"
STATUSES = frozenset({CHECKED, TIMED_OUT, UNAVAILABLE, NOT_APPLICABLE})
FAILED = frozenset({TIMED_OUT, UNAVAILABLE})

# Stable reason codes. Add here rather than inventing one at a call site.
NOT_CONFIGURED = "not_configured"
CREDENTIAL_REJECTED = "credential_rejected"
CREDENTIAL_EXPIRED = "credential_expired"
RATE_LIMITED = "rate_limited"
UPSTREAM_TIMEOUT = "upstream_timeout"
UPSTREAM_ERROR = "upstream_error"
UPSTREAM_HTTP_ERROR = "upstream_http_error"
BAD_RESPONSE = "bad_response"
CACHE_UNAVAILABLE = "cache_unavailable"
NO_COORDINATES = "no_coordinates"
NO_ADDRESS = "no_address"
UNKNOWN_AREA = "unknown_area"
SCORE_PENDING = "score_pending"
REASONS = frozenset({
    NOT_CONFIGURED, CREDENTIAL_REJECTED, CREDENTIAL_EXPIRED, RATE_LIMITED,
    UPSTREAM_TIMEOUT, UPSTREAM_ERROR, UPSTREAM_HTTP_ERROR, BAD_RESPONSE,
    CACHE_UNAVAILABLE, NO_COORDINATES, NO_ADDRESS, UNKNOWN_AREA, SCORE_PENDING,
})

# 57014 query_canceled is what a `SET LOCAL statement_timeout` expiry raises.
_QUERY_CANCELED_SQLSTATE = "57014"


class SourceFailure(RuntimeError):
    """Raised inside a fetcher so a failure is reported, and never cached.

    IntegrationCache stores whatever a fetcher returns — including an empty
    "no match" — so a fetcher that swallowed an outage and returned nothing
    replayed that outage as a verified miss for the empty-result TTL. Raising
    this instead keeps the cache out of it and carries a stable reason up.
    """

    def __init__(self, status: str = UNAVAILABLE, reason: str = UPSTREAM_ERROR):
        if status not in FAILED:
            raise ValueError(f"SourceFailure status must be one of {sorted(FAILED)}")
        super().__init__(f"{status}: {reason}")
        self.status = status
        self.reason = reason


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def entry(
    status: str,
    *,
    reason: Optional[str] = None,
    required: bool = True,
    matched: Optional[int] = None,
    checked_at: Optional[str] = None,
) -> dict[str, Any]:
    """One source's status entry. `checked_at` defaults to now for `checked`."""
    if status not in STATUSES:
        raise ValueError(f"unknown source status {status!r}")
    if status == CHECKED and checked_at is None:
        checked_at = now_iso()
    return {
        "status": status,
        "reason": reason,
        "required": required,
        "matched": matched,
        "checked_at": checked_at if status == CHECKED else None,
    }


def checked(*, matched: Optional[int] = None, required: bool = True) -> dict[str, Any]:
    return entry(CHECKED, matched=matched, required=required)


def not_applicable(reason: str, *, required: bool = True) -> dict[str, Any]:
    return entry(NOT_APPLICABLE, reason=reason, required=required)


def unavailable(reason: str = UPSTREAM_ERROR, *, required: bool = True) -> dict[str, Any]:
    return entry(UNAVAILABLE, reason=reason, required=required)


def is_timeout(exc: BaseException) -> bool:
    """Socket / asyncio / aiohttp timeouts, a urllib timeout, a statement timeout."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    if getattr(exc, "sqlstate", None) == _QUERY_CANCELED_SQLSTATE:
        return True
    if isinstance(exc, urllib.error.URLError) and isinstance(
        getattr(exc, "reason", None), (TimeoutError, asyncio.TimeoutError)
    ):
        return True
    return False


def _http_code(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "code", None)
    if code is None:
        code = getattr(exc, "status", None)
    return code if isinstance(code, int) else None


def classify(exc: BaseException) -> tuple[str, str]:
    """(status, reason) for an exception raised while asking a source."""
    if isinstance(exc, SourceFailure):
        return exc.status, exc.reason
    if is_timeout(exc):
        return TIMED_OUT, UPSTREAM_TIMEOUT
    # Imported lazily: data_integrations imports this module's callers.
    try:
        from data_integrations.cache import IntegrationCacheUnavailable

        if isinstance(exc, IntegrationCacheUnavailable):
            return UNAVAILABLE, CACHE_UNAVAILABLE
    except Exception:  # noqa: BLE001 — classification must never raise
        pass
    if type(exc).__name__ == "RetryableError" and "HTTP 429" in str(exc):
        return UNAVAILABLE, RATE_LIMITED
    code = _http_code(exc)
    if code in (401, 402, 403):
        return UNAVAILABLE, CREDENTIAL_REJECTED
    if code == 429:
        return UNAVAILABLE, RATE_LIMITED
    if code is not None and code >= 400:
        return UNAVAILABLE, UPSTREAM_HTTP_ERROR
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return UNAVAILABLE, BAD_RESPONSE
    return UNAVAILABLE, UPSTREAM_ERROR


def failure(exc: BaseException, *, required: bool = True) -> dict[str, Any]:
    status, reason = classify(exc)
    return entry(status, reason=reason, required=required)


def is_failed(item: Optional[Mapping[str, Any]]) -> bool:
    return bool(item) and item.get("status") in FAILED


def degraded(statuses: Mapping[str, Mapping[str, Any]]) -> bool:
    """True when a required source is not `checked` (not_applicable excepted)."""
    return any(
        is_failed(item) and item.get("required", True)
        for item in (statuses or {}).values()
    )


def worst(*items: Mapping[str, Any]) -> dict[str, Any]:
    """The least trustworthy of several entries for one logical source."""
    rank = {TIMED_OUT: 0, UNAVAILABLE: 1, NOT_APPLICABLE: 2, CHECKED: 3}
    present = [dict(i) for i in items if i]
    if not present:
        return not_applicable(NOT_CONFIGURED)
    return min(present, key=lambda i: rank.get(i.get("status"), 0))
