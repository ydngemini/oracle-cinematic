"""One sync per feed at a time, enforced by Postgres.

The durable-job layer already protects against the obvious duplication:
interval-bucketed idempotency keys with a UNIQUE index mean two schedulers
ticking the same minute enqueue one job; `FOR UPDATE SKIP LOCKED` plus a lease
token means one worker claims it; and the periodic handler heartbeats the lease
every `lease/3` seconds so a six-minute backfill cannot have its lease expire
underneath it and be re-claimed. The DigitalOcean app spec also pins the worker
to `instance_count: 1`.

So why this too? Because that last protection is a comment in a YAML file. The
day someone scales the worker to two for throughput, every guarantee above
still holds EXCEPT that two schedulers now tick — and they are deduplicated
only while the interval bucket is shared. A manual sync triggered from an
operator route, or a restart landing mid-bucket, does not go through that path
at all.

A feed sync writes to a shared cursor. Two concurrent walks of the same feed
would interleave their cursor advances, and the loser's cursor could move the
feed PAST records the winner never wrote — records that a delta sync will then
never fetch, because the cursor says they are done. Silent, permanent data
loss that no error surfaces.

This costs one cheap lock acquisition per sync and makes that impossible
regardless of how the process is deployed or invoked.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

log = logging.getLogger("oracle.mls_feed_lock")

#: Namespace for feed locks, so a hash collision with another advisory-lock
#: user in this codebase cannot silently serialise unrelated work.
_NAMESPACE = 0x4D4C5300  # "MLS\0"


class FeedBusy(RuntimeError):
    """Another worker is already syncing this feed."""


def _require_transaction(conn) -> None:
    """A transaction-scoped lock taken outside a transaction locks nothing.

    `pg_try_advisory_xact_lock` releases at the end of the enclosing
    transaction. On an asyncpg connection in autocommit, that is the end of
    the SELECT that took it — so two workers would both be told they acquired
    the feed, and both would walk the same cursor. Silent, and precisely the
    race this module exists to prevent, so it fails loudly instead.
    """
    in_tx = getattr(conn, "is_in_transaction", None)
    if callable(in_tx) and not in_tx():
        raise RuntimeError(
            "feed_sync_lock requires an open transaction — a transaction-scoped "
            "advisory lock taken in autocommit is released immediately and "
            "excludes nothing."
        )


@asynccontextmanager
async def feed_sync_lock(conn, mls_id: str, *, wait: bool = False):
    """Hold an advisory lock for one feed for the life of the transaction.

    `pg_try_advisory_xact_lock` rather than the blocking form by default: if
    another worker holds this feed, the right answer is to skip this tick and
    come back on the next one, not to pile up workers waiting on a backfill
    that may run for minutes.

    The lock is transaction-scoped, so it is released on commit OR rollback —
    a worker that crashes mid-sync cannot leave the feed locked forever, which
    a session-scoped lock would.
    """
    _require_transaction(conn)
    key = f"{_NAMESPACE}:{mls_id}"
    if wait:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", key)
        acquired = True
    else:
        acquired = await conn.fetchval(
            "SELECT pg_try_advisory_xact_lock(hashtextextended($1, 0))", key
        )
    if not acquired:
        log.warning("Feed %s is already being synced by another worker; skipping this tick.", mls_id)
        raise FeedBusy(mls_id)
    try:
        yield
    finally:
        # Transaction-scoped: Postgres releases it. Nothing to undo here, and
        # an explicit unlock would be wrong — it would release a lock this
        # transaction may still need for the rest of its work.
        pass


@asynccontextmanager
async def feed_sync_session_lock(mls_id: str, *, on_busy: str = "skip"):
    """Hold a feed lock across a whole sync, on a connection of its own.

    The transaction-scoped form above cannot be used for a sync. A feed sync
    deliberately does its provider I/O OUTSIDE a transaction — a slow board must
    not hold an app connection or row locks for minutes — and a backfill commits
    its cursor at every page so an interrupted walk resumes. Wrapping either in
    one long transaction to hold an xact lock would undo both of those on
    purpose-built behaviour.

    So this takes a SESSION-scoped lock on a dedicated pooled connection and
    holds it for the duration. Session locks are released when the connection
    closes, so a crashed or restarted worker cannot strand a feed — the failure
    mode a hand-rolled `locked_until` column would have.

    Yields True when the lock was taken, False when the feed is already being
    synced. Callers skip rather than wait: another worker is mid-walk, and the
    right answer is to come back next tick, not to queue up behind a backfill.
    """
    from db.connection import get_pool

    key = f"{_NAMESPACE}:{mls_id}"
    pool = get_pool()
    async with pool.acquire() as conn:
        acquired = bool(await conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", key
        ))
        if not acquired:
            log.warning(
                "Feed %s is already being synced on another connection; skipping this run.",
                mls_id,
            )
            if on_busy == "raise":
                raise FeedBusy(mls_id)
            yield False
            return
        try:
            yield True
        finally:
            # Session-scoped, so it must be released explicitly. Releasing on
            # the same connection that took it is required — a session lock is
            # owned by its session, not by the pool.
            await conn.execute(
                "SELECT pg_advisory_unlock(hashtextextended($1, 0))", key
            )


async def try_lock(conn, mls_id: str) -> bool:
    """Non-raising form, for callers that want to branch rather than catch."""
    _require_transaction(conn)
    key = f"{_NAMESPACE}:{mls_id}"
    return bool(await conn.fetchval(
        "SELECT pg_try_advisory_xact_lock(hashtextextended($1, 0))", key
    ))
