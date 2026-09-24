"""Record what a sync run actually did — including when it fails.

`record_sync_status` has accepted `error` and `error_class` since the health
model landed, and nothing ever passed them. A real failure — a revoked token, a
provider timeout, a 429 — propagated out of `sync_once`, past the periodic
task, and set an IN-MEMORY status on the scheduler object. `mls_sync_status`
was never touched.

The consequence was a feed that lies by omission. Revoke the Bridge token on
Monday morning and the feed reports READY until Tuesday morning, then STALE —
never AUTH_ERROR, with `last_error_class` NULL the whole time. Three of
`compute_health`'s branches (AUTH_ERROR, RATE_LIMITED, ERROR) were unreachable,
and the runbook's alerting recipe ("alert on last_error_class = 'auth'") could
never fire.

This wraps a feed's sync so a raised exception is written down before it is
re-raised. The re-raise matters: the scheduler still sees the failure, still
retries on its own cadence, still surfaces it in the job record. Swallowing it
here would trade one silence for another.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("oracle.mls_sync_guard")


def classify_error(exc: BaseException) -> str:
    """Map a provider failure onto the class an operator should act on.

    The three that matter behave differently: `auth` will never succeed on
    retry and needs a human, `rate_limit` needs backoff and patience, and
    everything else is worth retrying. An unclassified error string cannot be
    alerted on without regex-matching provider prose.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(t in text for t in ("401", "403", "unauthorized", "forbidden",
                               "invalid token", "authentication")):
        return "auth"
    if any(t in text for t in ("429", "rate limit", "too many requests", "quota")):
        return "rate_limit"
    if any(t in text for t in ("timeout", "timed out", "connection", "dns",
                               "unreachable", "ssl")):
        return "transport"
    if any(t in text for t in ("500", "502", "503", "504", "bad gateway")):
        return "provider"
    return "data"


async def guarded_sync(
    run: Callable[[], Awaitable[dict]],
    *,
    mls_id: str,
    mls_name: str,
    feed_type: str,
    provider: str,
    dataset: str,
) -> dict:
    """Run one feed sync, recording a failure before letting it propagate."""
    try:
        return await run()
    except Exception as exc:  # noqa: BLE001 — every failure is recorded the same way
        error_class = classify_error(exc)
        # Truncated: a provider can return a page of HTML in an error body, and
        # this column is read by operators, not parsers.
        message = f"{type(exc).__name__}: {exc}"[:500]
        try:
            from datetime import datetime, timezone
            from db.connection import tenant_tx
            from tenancy import Role, TenantContext
            import os

            ctx = TenantContext(
                agent_id="mls-sync-guard",
                tenant_id=os.getenv("ORACLE_INGEST_TENANT_ID")
                or "00000000-0000-0000-0000-000000000000",
                role=Role.PLATFORM_ADMIN,
            )
            from data_integrations.mls_sink import record_sync_status
            async with tenant_tx(ctx) as conn:
                await record_sync_status(
                    conn,
                    mls_id=mls_id, mls_name=mls_name, feed_type=feed_type,
                    last_sync_at=datetime.now(timezone.utc), listings_synced=0,
                    provider=provider, dataset=dataset,
                    succeeded=False, error=message, error_class=error_class,
                )
        except Exception as record_error:  # noqa: BLE001
            # Recording the failure must never replace the failure.
            log.error("Could not record sync failure for %s: %s", mls_id, record_error)
        log.error(
            "mls_sync feed=%s provider=%s state=failed error_class=%s",
            mls_id, provider, error_class,
        )
        raise
