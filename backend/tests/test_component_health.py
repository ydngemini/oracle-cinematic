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


def test_mls_auth_failure_is_reported_as_auth_not_down(monkeypatch):
    monkeypatch.setenv("ORACLE_MLS_ENABLED", "1")   # feed health is only computed while MLS is offered
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


# ── Property data (L19-04, T19.07 / AC19.07) ─────────────────────────────────

def test_harvesters_report_open_circuits_and_overdue_sources(monkeypatch):
    monkeypatch.delenv("ORACLE_RECOVERY_MODE", raising=False)
    assert ch._property_harvesters({"enabled": 0})["state"] == ch.NOT_CONFIGURED
    assert ch._property_harvesters({"enabled": 3, "open_circuits": 0, "overdue": 0})["state"] == ch.HEALTHY
    degraded = ch._property_harvesters({"enabled": 3, "open_circuits": 1, "overdue": 2})
    assert degraded["state"] == ch.DEGRADED and degraded["detail"] == {"sources": 3, "open_circuits": 1, "overdue": 2}
    assert ch._property_harvesters({"enabled": 3, "open_circuits": 0, "overdue": 2})["state"] == ch.STALE


def test_market_data_goes_stale_after_three_missed_refreshes(monkeypatch):
    monkeypatch.delenv("ORACLE_RECOVERY_MODE", raising=False)
    monkeypatch.setenv("ORACLE_MARKET_RESEARCH_INTERVAL_HOURS", "24")
    assert ch._market_data({"states": 0})["state"] == ch.NOT_CONFIGURED
    assert ch._market_data({"states": 50, "age_s": 3600})["state"] == ch.HEALTHY
    stale = ch._market_data({"states": 50, "age_s": 4 * 86400})
    assert stale["state"] == ch.STALE and stale["detail"]["limit_s"] == 3 * 86400


def test_paused_syncs_in_recovery_mode_are_reported_not_paged(monkeypatch):
    monkeypatch.setenv("ORACLE_RECOVERY_MODE", "1")
    harvest = ch._property_harvesters({"enabled": 3, "open_circuits": 0, "overdue": 3})
    market = ch._market_data({"states": 50, "age_s": 30 * 86400})
    for comp in (harvest, market):
        assert comp["state"] == ch.NOT_CONFIGURED and "recovery mode" in comp["summary"]
        assert comp["state"] not in ch.ALERTING
    # A tripped circuit is a real failure even in recovery mode.
    assert ch._property_harvesters({"enabled": 3, "open_circuits": 1, "overdue": 3})["state"] == ch.DEGRADED


def test_a_valuation_key_alone_is_never_reported_healthy(monkeypatch):
    monkeypatch.delenv("RENTCAST_API_KEY", raising=False)
    monkeypatch.delenv("ATTOM_API_KEY", raising=False)
    assert ch._valuation_providers()["state"] == ch.NOT_CONFIGURED
    monkeypatch.setenv("RENTCAST_API_KEY", "k")
    comp = ch._valuation_providers()
    assert comp["state"] == ch.UNKNOWN and comp["detail"] == {"configured": 1}
    assert "rentcast" not in str(comp).lower(), "counts only, no provider names in detail"


def test_an_unobserved_optional_component_does_not_degrade_the_system(monkeypatch):
    from db import connection

    monkeypatch.setenv("RENTCAST_API_KEY", "k")

    async def healthy(*_a, **_k):
        return ch._c(ch.HEALTHY, "ok")

    async def rest():
        out = {k: ch._c(ch.HEALTHY, "ok") for k in ch._DB_KEYS}
        out["valuation_providers"] = ch._valuation_providers([])   # configured, never called
        return out

    monkeypatch.setattr(ch, "_database", healthy)
    monkeypatch.setattr(ch, "_realtime", healthy)
    monkeypatch.setattr(ch, "_valkey", healthy)
    monkeypatch.setattr(ch, "_db_components", rest)
    snap = asyncio.run(ch.snapshot(fresh=True))
    assert snap["components"]["valuation_providers"]["state"] == ch.UNKNOWN
    assert snap["state"] == ch.HEALTHY
    # A stale optional component still counts.
    async def stale_rest():
        out = await rest()
        out["market_data"] = ch._c(ch.STALE, "stale")
        return out

    monkeypatch.setattr(ch, "_db_components", stale_rest)
    assert asyncio.run(ch.snapshot(fresh=True))["state"] == ch.DEGRADED


def test_no_database_leaves_the_property_components_unknown(monkeypatch):
    from db import connection

    monkeypatch.setattr(connection, "get_pool", lambda: None)
    snap = asyncio.run(ch.snapshot(fresh=True))
    for name in ("property_harvesters", "market_data", "valuation_providers"):
        assert snap["components"][name]["state"] == ch.UNKNOWN, name


def test_valuation_health_follows_the_recorded_outcomes(monkeypatch):
    monkeypatch.setenv("RENTCAST_API_KEY", "k")
    monkeypatch.setenv("ATTOM_API_KEY", "k")

    def row(provider, failures=0, kind=None):
        return {"provider": provider, "consecutive_failures": failures, "last_failure_kind": kind}

    assert ch._valuation_providers([row("rentcast")])["state"] == ch.HEALTHY
    assert ch._valuation_providers([row("rentcast", 1, "credential_rejected")])["state"] == ch.AUTH_FAILED
    assert ch._valuation_providers([row("attom", 1, "rate_limited")])["state"] == ch.RATE_LIMITED
    assert ch._valuation_providers([row("rentcast", 2, "http_error")])["state"] == ch.HEALTHY
    degraded = ch._valuation_providers([row("rentcast", 3, "timeout"), row("attom")])
    assert degraded["state"] == ch.DEGRADED and degraded["detail"] == {"configured": 2, "observed": 2, "failing": 1}
    monkeypatch.delenv("ATTOM_API_KEY")
    # A provider whose key was removed is not reported from an old row.
    assert ch._valuation_providers([row("attom", 5, "credential_rejected")])["state"] == ch.UNKNOWN


def test_provider_outcomes_map_from_the_avm_status_entries():
    import provider_health as ph
    import source_status as ss

    assert ph.kind_for(ss.checked(matched=1)) == ph.OK
    assert ph.kind_for(ss.checked(matched=0)) == ph.OK
    assert ph.kind_for(ss.unavailable(ss.CREDENTIAL_REJECTED)) == "credential_rejected"
    assert ph.kind_for(ss.unavailable(ss.RATE_LIMITED)) == "rate_limited"
    assert ph.kind_for(ss.unavailable(ss.UPSTREAM_HTTP_ERROR)) == "http_error"
    assert ph.kind_for(ss.unavailable(ss.BAD_RESPONSE)) == "bad_response"
    assert ph.kind_for(ss.not_applicable(ss.NOT_CONFIGURED, required=False)) is None
    assert ph.kind_for(ss.not_applicable(ss.NO_ADDRESS)) is None
    assert ph.kind_for(None) is None


def test_recording_never_raises_without_a_database(monkeypatch):
    import provider_health as ph
    from db import connection

    monkeypatch.setattr(connection, "get_pool", lambda: None)
    asyncio.run(ph.record("rentcast", ph.OK))
    asyncio.run(ph.record("rentcast", "credential_rejected"))
    asyncio.run(ph.record("unknown-provider", ph.OK))
