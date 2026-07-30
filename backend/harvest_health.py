"""Source-health classification shared by the scheduler and operator APIs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


VALID_HEALTH_STATES = {"fresh", "stale", "degraded", "failed", "unknown"}


def classify_health(
    *,
    last_succeeded_at: datetime | None,
    schedule_seconds: int | None,
    circuit_state: Any,
    failure_count: Any,
    now: datetime | None = None,
) -> str:
    """Return an honest source status without probing or inferring source data."""
    circuit = str(circuit_state or "closed").lower()
    failures = max(0, int(failure_count or 0))
    if circuit == "open" or failures >= 5:
        return "failed"
    if last_succeeded_at is None:
        return "degraded" if failures else "unknown"
    if last_succeeded_at.tzinfo is None:
        last_succeeded_at = last_succeeded_at.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    stale_after = max(int(schedule_seconds or 0) * 2, 6 * 3600)
    if reference - last_succeeded_at.astimezone(timezone.utc) > timedelta(seconds=stale_after):
        return "stale"
    return "degraded" if failures else "fresh"


def safe_health_detail(error: Any) -> str | None:
    """Bound diagnostics for operators; never preserve response bodies or URLs."""
    text = " ".join(str(error or "").split())
    if not text:
        return None
    return text[:320]
