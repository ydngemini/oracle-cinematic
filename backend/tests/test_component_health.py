"""Normalized component health and alerting (resilience 2026-10-02)."""

from __future__ import annotations

import asyncio

import component_health as ch
import ops_alerts


def test_no_database_is_unavailable_and_everything_else_unknown(monkeypatch):
    from db import connection

    monkeypatch.setattr(connection, "get_pool", lambda: None)
    snap = asyncio.run(ch.snapshot(fresh=True))
    assert snap["state"] == ch.UNAVAILABLE
    assert snap["components"]["database"]["state"] == ch.UNAVAILABLE
    for name in ("workers", "scheduler", "job_queue", "mls"):
        assert snap["components"][name]["state"] == ch.UNKNOWN, name   # never claimed healthy


def test_mls_auth_failure_is_reported_as_auth_not_down():
    row = {"mls_id": "actris", "license_classification": "licensed_property_listing",
           "last_error_class": "auth", "consecutive_failures": 3, "backfill_complete": True,
           "last_success_at": None, "last_attempt_at": None, "stale_after_minutes": 1440}
    comp = ch._mls([row])
    assert comp["state"] in (ch.AUTH_FAILED, ch.STALE, ch.DEGRADED)


def test_user_banner_uses_product_language_only():
    snap = {"components": {"ai": {"state": ch.UNAVAILABLE}, "valkey": {"state": ch.DEGRADED},
                           "database": {"state": ch.HEALTHY}}}
    messages = ch.user_banner(snap)
    assert "Neoh is having trouble answering right now. Your work is saved." in messages
    assert "Calling is temporarily unavailable." in messages
    assert not any(word in " ".join(messages).lower() for word in ("redis", "valkey", "qwen", "plivo", "http"))


def test_a_single_bad_probe_does_not_page(monkeypatch):
    calls = []

    async def snap(fresh=False):
        return {"state": ch.DEGRADED, "components": {
            "database": {"state": ch.HEALTHY, "summary": ""},
            "workers": {"state": ch.UNAVAILABLE, "summary": "no worker"}}}

    monkeypatch.setattr(ch, "snapshot", snap)

    class Conn:
        async def fetchval(self, sql, *a):
            return True

        async def fetchrow(self, sql, *a):
            calls.append(a[0])
            return {"id": 1, "notified_at": None, "inserted": True}

        async def fetch(self, sql, *a):
            return []

        async def execute(self, sql, *a):
            return None

    class Acq:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *a):
            return False

    class Pool:
        def acquire(self, timeout=None):
            return Acq()

    from db import connection

    monkeypatch.setattr(connection, "get_pool", lambda: Pool())

    async def no_mail(subject, body):
        return None

    monkeypatch.setattr(ops_alerts, "_notify", no_mail)
    ops_alerts._pending.clear()
    asyncio.run(ops_alerts.evaluate_once())
    assert calls == []                       # first sighting: not yet an incident
    asyncio.run(ops_alerts.evaluate_once())
    assert calls == ["workers"]              # confirmed on the second


def test_an_alert_that_cannot_be_emailed_never_raises(monkeypatch):
    import smtp_mailer

    def boom(**kw):
        raise OSError("smtp down")

    monkeypatch.setenv("ORACLE_ALERT_EMAIL", "ops@example.test")
    monkeypatch.setattr(smtp_mailer, "send", boom)
    assert asyncio.run(ops_alerts._notify("[Neoh] x", "y")) == "OSError"


def _record_with(state):
    """Run _record for one open incident whose component is now `state`."""
    executed = []

    class Conn:
        async def fetch(self, sql, *a):
            return [{"id": 9, "component": "mls"}]

        async def fetchrow(self, sql, *a):
            return None

        async def execute(self, sql, *a):
            executed.append(sql)

    async def no_mail(subject, body):
        return None

    import ops_alerts as oa

    original = oa._notify
    oa._notify = no_mail
    try:
        out = asyncio.run(oa._record(Conn(), {}, {}, {"mls": {"state": state}}))
    finally:
        oa._notify = original
    return out["resolved"], executed


def test_unknown_never_closes_an_incident():
    resolved, executed = _record_with(ch.UNKNOWN)
    assert resolved == [] and not any("resolved_at=now()" in s for s in executed)


def test_only_a_positively_healthy_component_closes_it():
    resolved, _ = _record_with(ch.HEALTHY)
    assert resolved == ["mls"]
