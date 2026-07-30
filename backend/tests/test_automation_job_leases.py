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
