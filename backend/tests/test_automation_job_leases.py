from __future__ import annotations

import asyncio

from automation_jobs import _reap_exhausted_leases


def test_exhausted_expired_job_leases_are_dead_lettered():
    class FakeConnection:
        def __init__(self):
            self.query = ""

        async def execute(self, query):
            self.query = query
            return "UPDATE 2"

    conn = FakeConnection()
    reaped = asyncio.run(_reap_exhausted_leases(conn))

    assert reaped == 2
    assert "state='dead_letter'" in conn.query
    assert "lease_expires_at < now()" in conn.query
    assert "attempt_count >= max_attempts" in conn.query
    assert "LEASE_ATTEMPTS_EXHAUSTED" in conn.query


def test_a_result_that_json_cannot_encode_never_fails_a_succeeded_job():
    """Regression: `periodic:mission_digest` returned a bare datetime in its
    result dict. The handler had already done its work and succeeded, but
    `complete_job`'s serialization raised, so the lease was never released, the
    row stayed `running`, and the job was re-leased and re-run until it
    exhausted all five attempts and dead-lettered — every cycle, indefinitely.
    That is this file's subject seen from the other side: the reaper above was
    doing its job correctly on work that had in fact succeeded.

    The encoder must therefore be total. A receipt that cannot be typed is not
    a reason to redo work that already happened.
    """
    import json
    from datetime import datetime, timezone

    from automation_jobs import canonical_json

    moment = datetime(2026, 9, 23, 18, 30, tzinfo=timezone.utc)
    encoded = canonical_json({"skipped": "not due yet", "last_sent_at": moment})
    assert json.loads(encoded)["last_sent_at"] == "2026-09-23T18:30:00+00:00"

    # Total even for a type with no isoformat at all.
    assert json.loads(canonical_json({"v": object()}))["v"].startswith("<object")


def test_mission_digest_not_due_result_carries_no_raw_datetime():
    """Checked at the source too, so the fix survives either side being
    rewritten. `not due yet` is the path taken on almost every run."""
    import inspect

    import missions.digest as digest

    source = inspect.getsource(digest.send_digest)
    assert '"last_sent_at": last_sent_at.isoformat() if last_sent_at else None' in source
