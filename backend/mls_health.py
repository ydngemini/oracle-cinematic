"""MLS feed health, freshness, entitlement, and brokerage readiness.

The question an operator must be able to answer without SSH and SQL: is MLS
working, for which board, how fresh, and if not — why not. And the question a
brokerage's setup screen must answer honestly: can this customer actually use
MLS yet.

The hard rule running through all of it: **developer data can never make a
customer READY**. A frozen sample dataset is useful for staging and demos and
is not inventory. mls_licensing decides classification and fails closed; this
module decides health and readiness and refuses to promote anything that is
not licensed, no matter how green its sync looks.

Entitlement is at the FEED grain, not the row grain. oracle_mls_listings
carries regexp_replace() expression indexes for address and parcel matching,
and a non-leakproof expression index is silently unusable under FORCE RLS —
adding row security to that table would kill property matching with no error.
So every read narrows to the set of mls_id values the tenant is entitled to,
derived server-side from mls_feed_entitlements, which IS row-secured.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from mls_licensing import DEVELOPER, LICENSED

log = logging.getLogger("oracle.mls_health")

# §4 health vocabulary.
NOT_CONFIGURED = "NOT_CONFIGURED"
CONFIGURED = "CONFIGURED"
BACKFILLING = "BACKFILLING"
READY = "READY"
STALE = "STALE"
DEGRADED = "DEGRADED"
ERROR = "ERROR"
AUTH_ERROR = "AUTH_ERROR"
RATE_LIMITED = "RATE_LIMITED"

#: Consecutive failures before a feed that still has data is called DEGRADED
#: rather than merely late.
_DEGRADED_AFTER_FAILURES = 3


def _utc(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def feed_age_seconds(row: dict, *, now: Optional[datetime] = None) -> Optional[float]:
    """How long since this feed last succeeded. None if it never has."""
    last = _utc(row.get("last_success_at")) or _utc(row.get("last_sync_at"))
    if not last:
        return None
    return ((now or datetime.now(timezone.utc)) - last).total_seconds()


def is_stale(row: dict, *, now: Optional[datetime] = None) -> bool:
    """Staleness is per-feed, not one global constant.

    Boards publish at very different cadences; a threshold that suits a feed
    syncing every ten minutes would call a nightly feed broken every morning.
    `stale_after_minutes` is configured per feed and defaults to a day.
    """
    age = feed_age_seconds(row, now=now)
    if age is None:
        return False          # never synced is NOT stale — it is not started
    return age > int(row.get("stale_after_minutes") or 1440) * 60


def compute_health(row: Optional[dict], *, now: Optional[datetime] = None) -> str:
    """Derive a feed's health from what actually happened to it.

    Deliberately NOT keyed on "credentials are present". A feed is READY when
    a licensed source has completed at least one successful sync and that sync
    is recent — never because an environment variable is set.
    """
    if not row:
        return NOT_CONFIGURED

    # A terminal provider condition outranks everything: a feed that cannot
    # authenticate is not "stale", it is broken in a way retrying will not fix.
    err = (row.get("last_error_class") or "").lower()
    if err == "auth":
        return AUTH_ERROR
    if err == "rate_limit":
        return RATE_LIMITED

    if not row.get("last_success_at") and not row.get("last_sync_at"):
        # Configured but never completed a sync.
        return BACKFILLING if row.get("backfill_cursor_key") else CONFIGURED

    if not row.get("backfill_complete"):
        # A half-finished initial walk must never read as READY — that is how
        # a brokerage ends up searching 8% of a board and believing it is all.
        return BACKFILLING

    if int(row.get("consecutive_failures") or 0) >= _DEGRADED_AFTER_FAILURES:
        return DEGRADED

    if is_stale(row, now=now):
        return STALE

    if row.get("last_error") and int(row.get("consecutive_failures") or 0) > 0:
        return DEGRADED

    return READY


def feed_is_usable(row: Optional[dict]) -> bool:
    """Can a search serve from this feed at all?

    Stale and degraded still serve — §32: a provider outage must leave cached
    listings readable and marked, not make the product say "no listings".
    """
    return bool(row) and compute_health(row) in (READY, STALE, DEGRADED, BACKFILLING)


# ---------------------------------------------------------------------------
# Entitlement
# ---------------------------------------------------------------------------

async def entitled_feed_ids(conn, ctx) -> list[str]:
    """Which feeds this tenant may read. Server-derived, never from a request.

    A browser filter may narrow this list; nothing a browser sends can widen
    it, because the widening set is computed here from the tenant on the
    verified session.
    """
    rows = await conn.fetch(
        "SELECT mls_id FROM mls_feed_entitlements WHERE tenant_id = $1::uuid ORDER BY mls_id",
        ctx.tenant_id,
    )
    return [r["mls_id"] for r in rows]


async def narrow_to_entitled(conn, ctx, requested: Optional[list[str]] = None) -> list[str]:
    """Intersect a caller's requested feeds with what they are entitled to."""
    allowed = set(await entitled_feed_ids(conn, ctx))
    if not requested:
        return sorted(allowed)
    return sorted(allowed.intersection({str(r).strip() for r in requested if str(r).strip()}))


# ---------------------------------------------------------------------------
# Brokerage readiness (§14)
# ---------------------------------------------------------------------------

async def mls_capability(conn, ctx) -> dict[str, Any]:
    """The `mls` capability for a brokerage's setup screen.

    Never READY on developer data. That is the whole point of the licensing
    split: a demo feed should let a brokerage explore the product and must not
    let the product tell them their MLS is live.
    """
    feed_ids = await entitled_feed_ids(conn, ctx)
    if not feed_ids:
        return {
            "status": "NOT_STARTED",
            "detail": "No licensed MLS feed is connected for this brokerage.",
            "feeds": [],
        }

    rows = await conn.fetch(
        "SELECT mls_id, mls_name, provider, dataset, license_classification, "
        "       license_reason, last_success_at, last_attempt_at, "
        "       last_error, consecutive_failures, backfill_complete, "
        "       backfill_records, listings_synced, stale_after_minutes "
        "  FROM mls_sync_status WHERE mls_id = ANY($1::text[])",
        feed_ids,
    )

    feeds = []
    for row in rows:
        row = dict(row)
        health = compute_health(row)
        feeds.append({
            "mls_id": row["mls_id"],
            "mls_name": row.get("mls_name") or row["mls_id"],
            "provider": row.get("provider"),
            "licensed": row.get("license_classification") == LICENSED,
            "health": health,
            "last_success_at": row.get("last_success_at"),
            "records": int(row.get("listings_synced") or 0),
            "age_seconds": feed_age_seconds(row),
        })

    licensed = [f for f in feeds if f["licensed"]]
    if not licensed:
        # This is the honest answer for the current state of the world: data
        # is present and searchable, and none of it is licensed inventory.
        return {
            "status": "BLOCKED",
            "detail": (
                "Connected feeds are developer/reference datasets, which cannot be "
                "reported as live MLS coverage. A licensed board feed requires "
                "provider approval."
            ),
            "feeds": feeds,
        }

    if any(f["health"] in (AUTH_ERROR, ERROR) for f in licensed):
        return {"status": "ERROR", "detail": "A licensed feed is failing to sync.", "feeds": feeds}
    if any(f["health"] == BACKFILLING for f in licensed):
        return {"status": "IN_PROGRESS", "detail": "Initial MLS backfill is running.", "feeds": feeds}
    if all(f["health"] in (STALE, DEGRADED) for f in licensed):
        return {"status": "ERROR", "detail": "Licensed MLS data is stale.", "feeds": feeds}
    if any(f["health"] == READY for f in licensed):
        return {"status": "READY", "detail": "Licensed MLS feed synced and fresh.", "feeds": feeds}

    return {"status": "IN_PROGRESS", "detail": "MLS feed configured.", "feeds": feeds}


# ---------------------------------------------------------------------------
# Search-time data state (§18)
# ---------------------------------------------------------------------------

def coverage_note(feeds: list[dict]) -> dict[str, Any]:
    """Tell a caller WHY a result set is empty.

    Zero matching listings and no usable MLS data are different answers, and
    returning `[]` for both is how a brokerage concludes there is nothing for
    sale in their market when the feed actually failed this morning.
    """
    if not feeds:
        return {"state": "no_coverage",
                "message": "No MLS feed is connected for this brokerage."}
    licensed = [f for f in feeds if f.get("licensed")]
    if not licensed:
        return {"state": "developer_data_only",
                "message": "Showing developer/reference data, not live MLS inventory."}
    if all(f.get("health") == BACKFILLING for f in licensed):
        return {"state": "backfilling",
                "message": "Initial MLS sync is still running; results are incomplete."}
    stale = [f for f in licensed if f.get("health") in (STALE, DEGRADED)]
    if stale and len(stale) == len(licensed):
        oldest = max((f.get("age_seconds") or 0) for f in stale)
        return {"state": "stale",
                "message": f"MLS data last refreshed {int(oldest // 3600)}h ago."}
    return {"state": "fresh", "message": ""}


async def visible_feeds_with(fetch, ctx) -> tuple[list[str], list[dict]]:
    """`visible_feeds` over any async fetcher.

    Exists because the state_compliance routers query through their own
    `_fetch(ctx, sql, *args)` helper rather than holding a connection, and the
    alternative — re-deriving "which feeds may this tenant see" inside that
    module — is precisely the duplication that let the licence rule drift in
    the first place. One implementation, two ways to call it.
    """
    rows = await fetch(_VISIBLE_FEEDS_SQL)
    entitled = {r["mls_id"] for r in await fetch(_ENTITLED_SQL, ctx.tenant_id)}
    return _partition(rows, entitled)


def _partition(rows, entitled: set) -> tuple[list[str], list[dict]]:
    allowed: list[str] = []
    described: list[dict] = []
    for raw in rows:
        row = dict(raw)
        licensed = row.get("license_classification") == LICENSED
        if licensed and row["mls_id"] not in entitled:
            continue
        allowed.append(row["mls_id"])
        described.append({
            "mls_id": row["mls_id"],
            "mls_name": row.get("mls_name") or row["mls_id"],
            "licensed": licensed,
            "health": compute_health(row),
            "age_seconds": feed_age_seconds(row),
        })
    return allowed, described


_VISIBLE_FEEDS_SQL = """
    SELECT s.mls_id,
           COALESCE(s.mls_name, s.mls_id)          AS mls_name,
           s.provider,
           COALESCE(s.license_classification,
                    'developer_listing_dataset')   AS license_classification,
           s.last_success_at,
           COALESCE(s.backfill_complete, false)    AS backfill_complete,
           COALESCE(s.consecutive_failures, 0)     AS consecutive_failures,
           s.last_error, s.last_error_class,
           COALESCE(s.listings_synced, 0)          AS listings_synced,
           COALESCE(s.stale_after_minutes, 1440)   AS stale_after_minutes
      FROM mls_sync_status AS s
"""
# mls_sync_status is the feed registry: a row appears the moment a feed is
# configured, and the unlicensed default above applies until a sync classifies
# it. A feed with listings but no status row is therefore not a real state —
# and if one ever appears (a hand-loaded table), staying invisible is the safe
# direction, because an unregistered feed has no licence classification and so
# nothing can prove it is allowed to be served.
# This used to union `SELECT DISTINCT mls_id FROM oracle_mls_listings`, to
# cover feeds that had rows but no status row yet — which happened because the
# Bridge backfill only wrote its status row after the whole walk finished, so
# an entire first import was invisible.
#
# The backfill now upserts that row at its FIRST checkpoint, so the gap is
# closed at the source. Removing the union also removes a DISTINCT over the
# whole listings table from the hot path of every search, every detail read and
# every health check — fine at 52k rows, a full index scan per request on a
# real board.

_ENTITLED_SQL = (
    "SELECT mls_id FROM mls_feed_entitlements WHERE tenant_id = $1::uuid ORDER BY mls_id"
)


async def visible_feeds(conn, ctx) -> tuple[list[str], list[dict]]:
    """Which feeds this tenant may see, and their health.

    The rule is licence-aware, because the two kinds of data carry different
    obligations:

      * A LICENSED feed belongs to whoever holds the agreement. It is visible
        only to tenants explicitly entitled to it. This is the boundary that
        stops one brokerage's paid board data becoming every tenant's data
        just because the listings table is shared.

      * DEVELOPER/reference data is licensed to nobody and is inventory for
        no one. It stays visible so staging, demos and local development keep
        working — and every response says plainly that is what it is.

    Enforcing entitlement on developer data as well would have been simpler and
    would have emptied every existing environment on deploy, to protect data
    that needs no protection.
    """
    # LEFT JOIN from the listings themselves, not from mls_sync_status.
    #
    # Selecting only feeds with a status row hid every listing whose feed had
    # not written one yet — and Bridge writes that row only AFTER a backfill
    # finishes, so an entire first import was invisible while it ran. Worse,
    # the documented "delete the status row to resume a backfill" step made a
    # feed's listings vanish from search for every tenant.
    #
    # A feed with no status row is unknown, and unknown is developer data
    # until something says otherwise — which is the same fail-closed rule the
    # licence check uses.
    rows = await conn.fetch(_VISIBLE_FEEDS_SQL)
    entitled = set(await entitled_feed_ids(conn, ctx))
    return _partition(rows, entitled)
