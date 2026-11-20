"""Normalized component health and alerting (resilience 2026-10-02)."""

from __future__ import annotations

import asyncio

import pytest

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
            return {"id": 1, "notified_at": None, "notify_error": None, "inserted": True}

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


# ── L19-06: the platform email relay is judged by its own sends (0164) ──────

@pytest.mark.parametrize("rows, state", [
    ([], "UNKNOWN"),
    ([{"provider": "platform_smtp", "consecutive_failures": 0, "last_failure_kind": None}], "HEALTHY"),
    ([{"provider": "platform_smtp", "consecutive_failures": 2, "last_failure_kind": "credential_rejected"}], "AUTH_FAILED"),
    ([{"provider": "platform_smtp", "consecutive_failures": 1, "last_failure_kind": "timeout"}], "DEGRADED"),
    ([{"provider": "rentcast", "consecutive_failures": 0, "last_failure_kind": None}], "UNKNOWN"),
])
def test_the_platform_email_relay_reads_its_own_outcome(monkeypatch, rows, state):
    import component_health as ch

    monkeypatch.setenv("ORACLE_SMTP_FROM_EMAIL", "relay@neoh.test")
    assert ch._platform_email_relay(rows)["state"] == state


def test_no_relay_settings_is_not_configured(monkeypatch):
    import component_health as ch

    monkeypatch.delenv("ORACLE_SMTP_FROM_EMAIL", raising=False)
    monkeypatch.delenv("ORACLE_SMTP_USERNAME", raising=False)
    assert ch._platform_email_relay([{"provider": "platform_smtp", "consecutive_failures": 3}])["state"] == "NOT_CONFIGURED"
    assert ch.ROLE["platform_email_relay"] == "optional" and "platform_email_relay" in ch._DB_KEYS


def test_an_unfinalized_inbound_call_degrades_health():
    """RUN-02: an inbound call still 'active' after it ended may carry an
    unrecorded opt-out; it must show, not sit silently."""
    assert ch._inbound_calls(0)["state"] == ch.HEALTHY
    degraded = ch._inbound_calls(2)
    assert degraded["state"] == ch.DEGRADED and degraded["detail"] == {"unfinalized": 2}
    assert "opt-out" in degraded["summary"]
    assert "inbound_calls" in ch._DB_KEYS, "unknown, never healthy, when the database is down"


def test_an_unmetered_billable_engagement_degrades_health():
    """BILL-001 (revenue): a delivered speed-to-lead engagement with no
    lead_engaged row is an unbilled engagement; the sweep re-meters, and one
    it could not fix shows here."""
    assert ch._billing_meter(0)["state"] == ch.HEALTHY
    degraded = ch._billing_meter(3)
    assert degraded["state"] == ch.DEGRADED and degraded["detail"] == {"unmetered": 3}
    assert "billing_meter" in ch._DB_KEYS


def test_10dlc_registration_trouble_pages_an_operator(monkeypatch):
    """25.2: a lost campaign, a status unreadable for a day, two weeks pending,
    or a submission/assignment whose outcome no row reflects."""
    ok = {"lost": 0, "unreadable": 0, "stalled": 0, "unresolved": 0, "receipt_contradictions": 0,
          "hosting_stalled": 0}
    assert ch._messaging_registration(ok)["state"] == ch.HEALTHY
    for key in ok:
        out = ch._messaging_registration({**ok, key: 1})
        assert out["state"] == ch.DEGRADED, key
    assert "operator" in ch._messaging_registration({**ok, "unresolved": 2})["summary"]
    assert "messaging_registration" in ch._DB_KEYS


def test_messaging_registration_reports_the_most_severe_first_with_every_count():
    out = ch._messaging_registration({"lost": 1, "receipt_contradictions": 2, "hosting_stalled": 3})
    assert "suspended or expired" in out["summary"]
    assert out["detail"]["receipt_contradictions"] == 2 and out["detail"]["hosting_stalled"] == 3
    assert ch._messaging_registration({})["detail"]["lost"] == 0


def test_no_recipient_is_not_a_delivered_alert(monkeypatch):
    """Audit 30.3: with no ORACLE_ALERT_EMAIL nothing was sent, so the
    incident must not read as notified."""
    monkeypatch.delenv("ORACLE_ALERT_EMAIL", raising=False)
    assert asyncio.run(ops_alerts.send_email("s", "b")) == ops_alerts.RECIPIENT_MISSING
    assert asyncio.run(ops_alerts._notify("[Neoh] x", "y")) == ops_alerts.RECIPIENT_MISSING


def test_an_unsent_incident_goes_out_once_a_recipient_is_set(monkeypatch):
    row = {"id": 4, "notified_at": None, "notify_error": None, "inserted": True}
    sent, updates = [], []

    class Conn:
        async def fetchrow(self, sql, *a):
            return dict(row)

        async def fetch(self, sql, *a):
            return []

        async def execute(self, sql, *a):
            if sql.startswith("UPDATE ops_alerts SET notified_at"):
                updates.append(a[1])
                row["notify_error"] = a[1]
                if a[1] is None:
                    row["notified_at"] = "now"

    async def relay(subject, body, *, to=None):
        sent.append(subject)
        return None

    def run():
        return asyncio.run(ops_alerts._record(Conn(), {"workers": {"state": "UNAVAILABLE", "summary": "x"}},
                                              {"workers": {}}, {}))

    monkeypatch.delenv("ORACLE_ALERT_EMAIL", raising=False)
    run()
    assert updates == [ops_alerts.RECIPIENT_MISSING] and row["notified_at"] is None and sent == []
    run()  # still nobody to send to: not re-attempted, not re-logged
    assert updates == [ops_alerts.RECIPIENT_MISSING]
    monkeypatch.setenv("ORACLE_ALERT_EMAIL", "ops@example.test")
    monkeypatch.setattr(ops_alerts, "send_email", relay)
    run()
    assert sent == ["[Neoh] workers UNAVAILABLE"] and updates[-1] is None and row["notified_at"] == "now"
    run()  # delivered once: not sent again
    assert len(sent) == 1


# ── Launch policy over every state the module emits (audit 30.2) ──────────

_ROLE_OF = {"core": "database", "important": "mls", "optional": "market_data", "unlisted": "a_future_component"}
_EXPECTED = {
    #             core        important   optional    unlisted (= important)
    ch.HEALTHY:        ("ok",      "ok",      "ok",      "ok"),
    ch.NOT_CONFIGURED: ("block",   "ok",      "ok",      "ok"),
    ch.UNAVAILABLE:    ("block",   "block",   "warn",    "block"),
    ch.AUTH_FAILED:    ("block",   "block",   "warn",    "block"),
    ch.STALE:          ("block",   "block",   "warn",    "block"),
    ch.UNKNOWN:        ("block",   "warn",    "warn",    "warn"),
    ch.DEGRADED:       ("warn",    "warn",    "warn",    "warn"),
    ch.RATE_LIMITED:   ("warn",    "warn",    "warn",    "warn"),
    ch.RECOVERING:     ("warn",    "warn",    "warn",    "warn"),
    "DOWN":            ("block",   "block",   "block",   "block"),  # not a state this module defines
}


def test_the_table_covers_every_defined_state():
    # Every module-level state constant, not just the hand-written set.
    constants = {v for k, v in vars(ch).items()
                 if k.isupper() and isinstance(v, str) and v == k and not k.startswith("_")}
    assert constants == set(ch.KNOWN_STATES)
    assert set(_EXPECTED) - {"DOWN"} == set(ch.KNOWN_STATES)


def test_the_snapshot_and_the_gate_agree_on_every_component_role():
    assert set(ch._DB_KEYS) | {"database", "realtime_fanout", "valkey", "outbound_opt_outs"} <= set(ch.ROLE)


def test_a_malformed_health_body_blocks():
    assert ch.launch_assessment(["not", "a", "dict"])["blocking"]
    assert ch.launch_assessment({"workers": "UNAVAILABLE"})["blocking"] == ["workers=MISSING"]


@pytest.mark.parametrize("state", sorted(_EXPECTED))
@pytest.mark.parametrize("role_index,role", list(enumerate(_ROLE_OF)))
def test_launch_assessment_by_state_and_role(state, role_index, role):
    name = _ROLE_OF[role]
    assert ch.ROLE.get(name, "important") == ("important" if role == "unlisted" else role)
    verdict = ch.launch_assessment({name: {"state": state}})
    got = ("block" if verdict["blocking"] else "warn" if verdict["warning"] else "ok")
    assert got == _EXPECTED[state][role_index], (state, role, verdict)


def test_media_jobs_alert_on_a_backlog_or_a_job_that_stopped_moving():
    """Audit 30.9: job_queue watches automation_jobs only; the worker's voice
    and reconstruction loops read their own tables."""
    import component_health as ch

    calm = {"voice_oldest_queued_s": 60, "voice_silent": 0, "recon_oldest_queued_s": 1800, "recon_silent": 0}
    assert ch._media_jobs(calm, worker_live=True)["state"] == ch.HEALTHY
    assert ch._media_jobs({k: None for k in calm}, worker_live=True)["state"] == ch.HEALTHY
    voice_backlog = ch._media_jobs({**calm, "voice_oldest_queued_s": ch.VOICE_QUEUED_MAX_S + 1}, worker_live=True)
    assert voice_backlog["state"] == ch.DEGRADED and voice_backlog["state"] in ch.ALERTING
    assert "voice walkthroughs waiting" in voice_backlog["summary"]
    # No live worker: the backlog is the `workers` alert's job, not a second page.
    assert ch._media_jobs({**calm, "recon_oldest_queued_s": ch.RECON_QUEUED_MAX_S + 1},
                          worker_live=False)["state"] == ch.HEALTHY
    hung = ch._media_jobs({**calm, "voice_silent": 1, "recon_silent": 2}, worker_live=False)
    assert hung["state"] == ch.DEGRADED and hung["detail"]["recon_silent"] == 2
    assert "media_jobs" in ch._DB_KEYS and ch.role_of("media_jobs") == "important"


def test_an_export_archive_kept_past_expiry_for_a_day_alerts():
    """Audit 29.6: a delete that keeps failing must reach a person."""
    import component_health as ch

    assert ch._export_cleanup(0)["state"] == ch.HEALTHY
    bad = ch._export_cleanup(2)
    assert bad["state"] == ch.DEGRADED and bad["state"] in ch.ALERTING and bad["detail"]["overdue"] == 2
    assert "export_cleanup" in ch._DB_KEYS and ch.role_of("export_cleanup") == "important"
    assert "interval '1 day'" in ch._SQL_EXPORT_CLEANUP and "artifact_key IS NOT NULL" in ch._SQL_EXPORT_CLEANUP
