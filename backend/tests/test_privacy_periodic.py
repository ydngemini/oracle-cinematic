"""The hourly privacy task passes the published retention periods to the
database sweeps — the schedule in retention_policy.py is the one enforced."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


def test_privacy_task_enforces_the_published_schedule(monkeypatch):
    import privacy_export
    import privacy_lifecycle
    from data_integrations import periodic
    from db import connection

    calls = []

    class Conn:
        async def fetchval(self, sql, *args):
            calls.append((sql, args))
            return '{"client_portals": 2}' if "sweep" in sql else 5

    @asynccontextmanager
    async def fake_tx(ctx):
        assert ctx.is_platform_admin
        yield Conn()

    async def no_erasures():
        return ["op1"]

    async def no_exports():
        return 1

    monkeypatch.setattr(connection, "tenant_tx", fake_tx)
    monkeypatch.setattr(privacy_lifecycle, "start_due_erasures", no_erasures)
    monkeypatch.setattr(privacy_export, "expire_exports", no_exports)

    async def orphans():
        return {"orphans_deleted": 2}

    monkeypatch.setattr(privacy_export, "sweep_orphan_exports", orphans)
    monkeypatch.setenv("ORACLE_AUDIT_RETENTION_DAYS", "800")
    monkeypatch.setenv("ORACLE_EXPIRED_TOKEN_RETENTION_DAYS", "30")

    out = asyncio.run(periodic._privacy_lifecycle_task())
    assert out == {"erasures_started": 1, "exports_expired": 1, "export_orphans": {"orphans_deleted": 2},
                   "swept": {"client_portals": 2}, "audit_rows_expired": 5}
    sweep = next(a for s, a in calls if "privacy_retention_sweep" in s)
    assert sweep == (30, 90, 1826, 2557)
    audit = next(a for s, a in calls if "privacy_expire_audit" in s)
    assert audit == (800,)


def test_audit_retention_is_never_below_a_year(monkeypatch):
    from retention_policy import RetentionCategory, policy_for

    monkeypatch.setenv("ORACLE_AUDIT_RETENTION_DAYS", "30")
    # the task clamps to 365; the database refuses less as well (0122)
    assert max(365, policy_for(RetentionCategory.AUDIT_SECURITY).retention_days) == 365


def test_one_refused_directive_delete_does_not_stop_the_rest(monkeypatch):
    """29.6 review: a denied delete now raises; the run continues per key."""
    import asyncio
    import json as _json

    import object_storage
    import privacy_requests

    keys = ["d/a.json", "d/b.json", "d/c.json"]
    deleted = []

    def delete(key):
        if key == "d/a.json":
            raise object_storage.StorageError("denied")
        deleted.append(key)
        return True

    monkeypatch.setattr(object_storage, "is_configured", lambda: True)
    monkeypatch.setattr(object_storage, "list_prefix", lambda prefix: keys)
    monkeypatch.setattr(object_storage, "get_bytes", lambda key: _json.dumps({}).encode())
    monkeypatch.setattr(object_storage, "delete_object", delete)
    monkeypatch.setattr(privacy_requests, "_directive_expired", lambda d: True)
    assert asyncio.run(privacy_requests.expire_subject_directives()) == 2
    assert deleted == ["d/b.json", "d/c.json"]
