"""Operator API — who is live, who needs action, and a support bundle that
never carries customer content.

Route tests run on a substring-keyed fake connection (the house pattern, see
test_brokerage_onboarding.py). What they prove is the application's decisions:
the capability mapping, the needs-action list, the redaction, the gate. What
Postgres decides — RLS letting the platform login read across tenants — is
proved in tests/rls_*.sql, not here.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import brokerage_onboarding
import operator_api as op
from tenancy import Role, TenantContext, require_context

ADMIN = TenantContext(agent_id="ops@neoh.test", tenant_id="00000000-0000-0000-0000-000000000000",
                      role=Role.PLATFORM_ADMIN)
OWNER = TenantContext(agent_id="owner@a.test", tenant_id="aaaaaaaa-0000-0000-0000-00000000000a",
                      role=Role.BROKER_OWNER)
AGENT = TenantContext(agent_id="agent@a.test", tenant_id="aaaaaaaa-0000-0000-0000-00000000000a",
                      role=Role.AGENT)

T_LIVE = "11111111-1111-1111-1111-111111111111"
T_NEW = "22222222-2222-2222-2222-222222222222"
NOW = datetime.now(timezone.utc)


class FakeConn:
    """Answers by SQL substring, first match wins; records every statement."""

    def __init__(self, *, rows=(), fetch=(), vals=(), fail=()):
        self.rows, self.fetches, self.vals = list(rows), list(fetch), list(vals)
        self.fail = list(fail)
        self.statements: list[str] = []

    def _pick(self, table, query, default):
        for needle in self.fail:
            if needle in query:
                raise RuntimeError(f"boom: {needle}")
        for needle, value in table:
            if needle in query:
                return value(query) if callable(value) else value
        return default

    async def fetchrow(self, query, *args):
        self.statements.append(query)
        return self._pick(self.rows, query, None)

    async def fetch(self, query, *args):
        self.statements.append(query)
        return self._pick(self.fetches, query, [])

    async def fetchval(self, query, *args):
        self.statements.append(query)
        return self._pick(self.vals, query, None)

    async def execute(self, query, *args):
        self.statements.append(query)
        return "SET"

    def transaction(self):
        @asynccontextmanager
        async def tx():
            yield self
        return tx()


@pytest.fixture
def use_conn(monkeypatch):
    def install(conn, setups=None):
        @asynccontextmanager
        async def tx(_ctx):
            yield conn
        monkeypatch.setattr(op, "tenant_tx", tx)

        async def fake_setup(_conn, ctx):
            if setups is None or ctx.tenant_id not in setups:
                raise RuntimeError("no setup")
            return setups[ctx.tenant_id]
        monkeypatch.setattr(brokerage_onboarding, "compute_setup_state", fake_setup)
        return conn
    return install


def run(coro):
    return asyncio.run(coro)


def setup_state(messaging="READY", **caps):
    base = {"brokerage_profile": "READY", "agent_invites": "READY", "contact_import": "NOT_STARTED",
            "email_calendar": "NOT_STARTED", "phone": "READY", "mls": "NOT_STARTED",
            "billing": "READY", "readiness": "READY"}
    base.update(caps)
    return {"capabilities": base, "recommended_next": None, "messaging": messaging,
            "mls": {"status": base["mls"], "detail": "", "feeds": []},
            "team": {"active_members": 3, "pending_invitations": 0, "accepted_invitations": 2}}


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------

def test_safe_code_passes_codes_and_refuses_prose():
    assert op.safe_code("provider_timeout") == "provider_timeout"
    assert op.safe_code("RATE_LIMIT:429") == "RATE_LIMIT:429"
    assert op.safe_code("Hi John, call me at 302-555-0100") == "unclassified"
    assert op.safe_code(None) is None


def test_scrub_removes_credentials_emails_and_numbers():
    text = op.scrub("401 for owner@brokerage.test token=abc123 at +1 (302) 555-0100 "
                    "sk_live_abcdef and https://x.test/?access_token=QWERTYUIOPASDFGHJKLZXCVBNM")
    assert "owner@brokerage.test" not in text
    assert "abc123" not in text and "sk_live" not in text and "QWERTY" not in text
    assert "555" not in text
    assert len(op.scrub("x" * 500, 40)) <= 41


def test_mask_keeps_only_last_four():
    assert op.mask_e164("+13025550199") == "•••0199"
    assert op.mask_e164(None) is None


def test_capabilities_map_to_operator_states_with_product_reasons():
    setup = setup_state(messaging="NOT_STARTED", phone="IN_PROGRESS", mls="BLOCKED",
                        email_calendar="READY")
    caps = op.map_capabilities(setup, subscription={"status": "past_due"},
                               credentials={"email": {"active": 0, "broken": 1}},
                               registration={"rejected": False, "pending": False})
    assert set(caps) == set(op.OPERATOR_CAPABILITIES)
    assert caps["phone"] == {"state": op.NEEDS_SETUP, "reason": "Caller ID verification pending"}
    assert caps["messaging"]["state"] == op.NOT_USED
    assert caps["email"]["state"] == op.NEEDS_ATTENTION
    # No calendar credential, but the brokerage said it was done: READY, labelled as such.
    assert caps["calendar"]["state"] == op.READY and "self-reported" in caps["calendar"]["reason"]
    assert caps["mls"]["state"] == op.NEEDS_SETUP and "developer" in caps["mls"]["reason"]
    assert caps["billing"]["state"] == op.NEEDS_ATTENTION and "past due" in caps["billing"]["reason"]


def test_rejected_10dlc_outranks_a_ready_route():
    setup = setup_state()
    caps = op.map_capabilities(setup, subscription={"status": "active"}, credentials={},
                               registration={"rejected": True, "pending": False})
    assert caps["messaging"]["state"] == op.NEEDS_ATTENTION
    assert "10DLC" in caps["messaging"]["reason"]


def test_no_subscription_is_explicit():
    caps = op.map_capabilities(setup_state(), subscription=None, credentials={}, registration={})
    assert caps["billing"] == {"state": op.NEEDS_SETUP, "reason": "No subscription"}


def test_needs_action_lists_lifecycle_work_billing_and_setup():
    setup = setup_state(phone="NOT_STARTED", billing="NOT_STARTED", readiness="BLOCKED")
    caps = op.map_capabilities(setup, subscription=None, credentials={}, registration={})
    items = op.compute_needs_action(
        lifecycle_state="suspended", setup=setup, capabilities=caps, subscription=None,
        work={"failed_jobs_24h": 3, "unresolved_side_effects": 1})
    messages = [i["message"] for i in items]
    assert messages[0] == "Account suspended"
    assert "1 call/text/email needs reconciliation" in messages
    assert "3 background jobs failed in the last 24h" in messages
    assert "No subscription" in messages
    assert "Setup incomplete: phone, billing" in messages


def test_erased_tenant_needs_nothing_but_its_state():
    items = op.compute_needs_action(lifecycle_state="erased", setup=None, capabilities={},
                                    subscription=None, work={"failed_jobs_24h": 9})
    assert [i["code"] for i in items] == ["lifecycle_erased"]


def test_status_vocabulary():
    assert op.brokerage_status("active", setup_state(), {"status": "active"}) == "live"
    assert op.brokerage_status("active", setup_state(readiness="BLOCKED"), None) == "onboarding"
    assert op.brokerage_status("active", setup_state(), {"status": "canceled"}) == "canceled"
    assert op.brokerage_status("closing", setup_state(), {"status": "active"}) == "closing"


# ---------------------------------------------------------------------------
# GET /brokerages
# ---------------------------------------------------------------------------

def _tenants(_q):
    return [
        {"id": T_LIVE, "name": "Lockwood Realty", "slug": "lockwood", "created_at": NOW,
         "lifecycle_state": "active", "erase_after": None, "total": 2},
        {"id": T_NEW, "name": "Brand New Homes", "slug": "bnh", "created_at": NOW,
         "lifecycle_state": "active", "erase_after": None, "total": 2},
    ]


def _list_conn(**extra):
    return FakeConn(fetch=[
        ("FROM tenants t", _tenants),
        ("FROM subscriptions", [{"tenant_id": T_LIVE, "status": "active", "plan": "oracle_swarm",
                                 "current_period_end": NOW, "updated_at": NOW}]),
        ("FROM users", [{"tenant_id": T_LIVE, "total": 4, "active": 3},
                        {"tenant_id": T_NEW, "total": 1, "active": 1}]),
        ("FROM automation_jobs", [{"tenant_id": T_NEW, "failed_jobs_24h": 2, "retrying_jobs": 1}]),
        ("FROM command_executions", []),
        ("FROM tenant_messaging_brands", []),
        ("FROM interaction_logs", [{"tenant_id": T_LIVE, "last_activity_at": NOW}]),
        ("FROM provider_credentials", [{"tenant_id": T_LIVE, "provider": "google", "active": 1,
                                        "broken": 0, "calendar_scope": True}]),
    ], **extra)


def test_brokerages_answers_who_is_live_and_who_needs_action(use_conn):
    conn = use_conn(_list_conn(), setups={
        T_LIVE: setup_state(),
        T_NEW: setup_state(phone="IN_PROGRESS", billing="NOT_STARTED", readiness="BLOCKED"),
    })
    out = run(op.list_brokerages(limit=100, offset=0, ctx=ADMIN))

    assert conn.statements[0].startswith("SET LOCAL statement_timeout")
    by_id = {b["id"]: b for b in out["brokerages"]}
    live, new = by_id[T_LIVE], by_id[T_NEW]
    assert live["status"] == "live" and live["needs_action"] == []
    assert live["capabilities"]["calendar"]["state"] == op.READY
    assert live["agents"] == {"total": 4, "active": 3}
    assert new["status"] == "onboarding"
    assert new["subscription"] == {"status": "none"}
    codes = [i["code"] for i in new["needs_action"]]
    assert "billing_none" in codes and "jobs_failed" in codes and "setup_incomplete" in codes
    assert new["work"]["retrying_jobs"] == 1
    # Most urgent first.
    assert out["brokerages"][0]["id"] == T_NEW
    assert out["summary"] == {"live": 1, "onboarding": 1, "needs_action": 1}
    assert out["page"] == {"limit": 100, "offset": 0, "total": 2, "has_more": False}
    assert "usage_limits" in out["not_tracked"]
    assert out["unavailable"] == []


def test_one_broken_section_degrades_only_itself(use_conn):
    use_conn(_list_conn(fail=["FROM automation_jobs"]), setups={T_LIVE: setup_state(), T_NEW: setup_state()})
    out = run(op.list_brokerages(limit=100, offset=0, ctx=ADMIN))
    assert out["unavailable"] == ["jobs"]
    assert len(out["brokerages"]) == 2


def test_a_tenant_whose_setup_cannot_be_read_is_flagged_not_hidden(use_conn):
    use_conn(_list_conn(), setups={T_LIVE: setup_state()})
    out = run(op.list_brokerages(limit=100, offset=0, ctx=ADMIN))
    new = next(b for b in out["brokerages"] if b["id"] == T_NEW)
    for cap in ("phone", "messaging", "mls"):
        assert new["capabilities"][cap]["state"] == op.NEEDS_ATTENTION, cap
    assert "setup" in out["unavailable"]


# ---------------------------------------------------------------------------
# GET /brokerages/{id}/diagnostics — the no-content guarantee
# ---------------------------------------------------------------------------

S = "SENTINEL"


def _diag_conn():
    secret_row_extras = {
        "password_hash": f"{S}_HASH", "token_ciphertext": f"{S}_TOKEN".encode(),
        "refresh_ciphertext": f"{S}_REFRESH".encode(), "body": f"{S}_BODY",
        "transcript_ciphertext": f"{S}_TRANSCRIPT".encode(), "payload": {"x": f"{S}_PAYLOAD"},
        "metadata": {"note": f"{S}_META"}, "account_label": f"{S}mailbox@x.test",
        "to_e164": "+13025559999", "twilio_account_sid": f"AC{S}",
    }
    return FakeConn(
        rows=[
            ("FROM tenants WHERE id", {"id": T_LIVE, "name": "Lockwood Realty", "slug": "lockwood",
                                       "created_at": NOW, "lifecycle_state": "active",
                                       "lifecycle_changed_at": None, "closure_requested_at": None,
                                       "erase_after": None, "erased_at": None, **secret_row_extras}),
            ("FROM subscriptions", {"status": "past_due", "plan": "oracle_swarm", "current_period_end": NOW,
                                    "updated_at": NOW, "created_at": NOW, "stripe_customer_id": "cus_123",
                                    "stripe_subscription_id": f"sub_{S}", **secret_row_extras}),
            ("FROM users", {"total": 4, "active": 3, "owners": 1, **secret_row_extras}),
            ("FROM inbound_voice_calls", {"total": 5, "failed": 1, "unsuccessful": 2,
                                          "caller_phone_ciphertext": f"{S}".encode()}),
            ("FROM tenant_messaging_brands", {"provider": "telnyx", "status": "failed",
                                              "failure_reason": f"EIN 123456789 mismatch for {S.lower()}@x.test",
                                              "submitted_at": NOW, "approved_at": None,
                                              "ein": f"{S}_EIN", "phone": "+13025558888"}),
            ("FROM sms_messages", {"outbound": 10, "failed": 2}),
        ],
        fetch=[
            ("FROM provider_credentials", [{"provider": "google", "validation_status": "valid",
                                            "disabled_at": None, "expires_at": NOW, "last_validated_at": NOW,
                                            "calendar_scope": True, **secret_row_extras}]),
            ("FROM telephony_routes", [{"agent_id": "agent@lockwood.test", "provider": "plivo",
                                        "inbound_did": "+13025550199", "voice_caller_id_e164": "+13025550177",
                                        "active": True, "outbound_verification_status": "failed",
                                        "outbound_verification_failure_reason":
                                            "Number +13025550188 rejected token=" + S + "TOKEN123",
                                        "inbound_forwarding_status": "active",
                                        "inbound_forwarding_failure_reason": None,
                                        "forwarding_mode": "none", "updated_at": NOW, **secret_row_extras}]),
            ("FROM messaging_routes", [{"agent_id": "agent@lockwood.test", "provider": "telnyx", "active": True,
                                        "eligibility_status": "eligible", "hosted_order_status": "failed",
                                        "hosted_order_failure_reason": f"Carrier said {S}{'Z' * 30}",
                                        "loa_document_state": "required", "invoice_document_state": "not_required",
                                        "updated_at": NOW, **secret_row_extras}]),
            ("FROM tenant_messaging_campaigns", [{"provider": "telnyx", "status": "rejected", "usecase": "MIXED",
                                                  "failure_reason": "call +1 302 555 0111",
                                                  "submitted_at": NOW, "approved_at": None,
                                                  "description": f"{S}_CAMPAIGN_COPY"}]),
            ("FROM mls_sync_status", [{"mls_id": "actris", "mls_name": "ACTRIS", "provider": "bridge",
                                       "dataset": "actris", "feed_type": "Bridge_API_v2",
                                       "license_classification": "licensed_property_listing",
                                       "last_success_at": NOW - timedelta(hours=1), "last_attempt_at": NOW,
                                       "last_sync_at": NOW, "last_error_class": "auth",
                                       "last_error": f"401 https://api.test/?access_token={S}ACCESSTOKEN99",
                                       "consecutive_failures": 4, "backfill_complete": True,
                                       "backfill_cursor_key": None, "backfill_records": 52622,
                                       "listings_synced": 100, "stale_after_minutes": 60,
                                       "notes": json.dumps({"rejected": {"invalid_record": 3},
                                                            "rejected_on_write": 2}),
                                       "updated_at": NOW, "agreement_ref": f"{S}_AGREEMENT"}]),
            ("FROM automation_jobs", [{"job_type": "outreach.sms", "state": "dead_letter",
                                       "last_error_code": "provider_timeout", "n": 2, "last_at": NOW,
                                       "last_error": f"{S}: Hi John, call me at 555-0100"},
                                      {"job_type": "outreach.email", "state": "queued",
                                       "last_error_code": f"{S} prose with spaces", "n": 1, "last_at": NOW}]),
            ("FROM command_executions", [{"command_type": "SMS", "state": "reconciliation_required",
                                          "provider": "telnyx", "n": 1, "last_at": NOW,
                                          "draft": {"body": f"{S}_DRAFT"}, "target": {"to": "+13025557777"}}]),
            ("FROM ai_chat_messages", [{"error_code": "RATE_LIMITED", "n": 2,
                                        "content_ciphertext": f"{S}".encode()}]),
            ("FROM ai_tool_operations", [{"tool_name": "update_client", "error_code": None, "n": 1,
                                          "result": {"x": f"{S}_RESULT"}}]),
            ("FROM audit_ledger", [{"category": "admin_action", "action": "role_change", "created_at": NOW,
                                    "metadata": {"reason": f"{S}_AUDIT"}}]),
            ("FROM process_heartbeats", [{"role": "worker", "git_sha": "abc1234", "age_seconds": 10,
                                          "uptime_seconds": 100}]),
        ],
        vals=[
            ("FROM automation_jobs", 2),
            ("FROM email_outbox", 1),
            ("schema_migrations", "0124_job_claim_aging.sql"),
        ],
    )


def test_diagnostics_bundle_answers_support_questions(use_conn, monkeypatch):
    monkeypatch.setenv("ORACLE_GIT_SHA", "abc1234")
    use_conn(_diag_conn(), setups={T_LIVE: setup_state(phone="NEEDS_ACTION")})
    out = run(op.brokerage_diagnostics(T_LIVE, ctx=ADMIN))

    assert out["tenant"]["id"] == T_LIVE
    assert out["plan"]["status"] == "past_due" and out["plan"]["stripe_customer_ref"] == "cus_123"
    assert out["agents"] == {"total": 4, "active": 3, "owners": 1}
    assert out["capabilities"]["phone"]["state"] == op.NEEDS_ATTENTION
    assert out["capabilities"]["messaging"]["state"] == op.NEEDS_ATTENTION  # 10DLC rejected
    assert out["capabilities"]["calendar"]["state"] == op.READY
    assert out["voice"]["routes"][0]["business_number"] == "•••0199"
    assert out["voice"]["inbound_calls_24h"]["failed"] == 1
    assert out["messaging"]["brand_10dlc"]["status"] == "failed"
    feed = out["mls"][0]
    assert feed["health"] == "AUTH_ERROR" and feed["licensed"] is True
    assert feed["rejections"] == {"validation": 3, "on_write": 2, "by_reason": {"invalid_record": 3}}
    jobs = out["jobs"]
    assert jobs["failed_24h"] == 2
    assert {g["error_code"] for g in jobs["groups"]} == {"provider_timeout", "unclassified"}
    assert {g["state"] for g in jobs["groups"]} == {"dead_letter", "retrying"}
    assert out["side_effects"]["unresolved"] == 1
    assert out["provider_errors"]["ai_responses"] == [{"code": "RATE_LIMITED", "count": 2}]
    assert out["recent_audit"][0] == {"category": "admin_action", "action": "role_change", "at": NOW.isoformat()}
    assert out["release"]["api"]["git_sha"] == "abc1234"
    assert out["release"]["workers"]["live_git_shas"] == ["abc1234"]
    assert "message bodies" in out["excluded"]
    assert out["unavailable"] == []


def test_diagnostics_never_contain_content_secrets_or_contact_numbers(use_conn):
    use_conn(_diag_conn(), setups={T_LIVE: setup_state()})
    out = run(op.brokerage_diagnostics(T_LIVE, ctx=ADMIN))
    blob = json.dumps(out, default=str)
    assert S not in blob and S.lower() not in blob
    for number in ("3025550199", "3025550177", "3025550188", "3025557777", "3025558888",
                   "3025559999", "555-0100", "123456789", "302 555 0111"):
        assert number not in blob, number
    for key in ("password_hash", "token_ciphertext", "body", "transcript", "account_label",
                "draft", "payload", "metadata", "last_error\"", "content_ciphertext"):
        assert f'"{key}' not in blob, key


def test_diagnostics_404_and_422(use_conn):
    use_conn(FakeConn(), setups={})
    with pytest.raises(HTTPException) as missing:
        run(op.brokerage_diagnostics(T_LIVE, ctx=ADMIN))
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as bad:
        run(op.brokerage_diagnostics("not-a-uuid", ctx=ADMIN))
    assert bad.value.status_code == 422


# ---------------------------------------------------------------------------
# Billing exceptions, MLS feeds, comms, AI, pilot metrics
# ---------------------------------------------------------------------------

def test_billing_exceptions_classifies_and_skips_the_platform_tenant(use_conn, monkeypatch):
    import billing_usage

    monkeypatch.setattr(billing_usage, "metering_configured", lambda: False)
    use_conn(FakeConn(
        fetch=[
            ("WITH latest AS", [
                {"id": ADMIN.tenant_id, "name": "Platform", "lifecycle_state": "active", "created_at": NOW,
                 "status": None, "plan": None, "current_period_end": None, "updated_at": None},
                {"id": T_NEW, "name": "Brand New", "lifecycle_state": "active", "created_at": NOW,
                 "status": None, "plan": None, "current_period_end": None, "updated_at": None},
                {"id": T_LIVE, "name": "Lockwood", "lifecycle_state": "active", "created_at": NOW,
                 "status": "past_due", "plan": "oracle_swarm", "current_period_end": NOW, "updated_at": NOW},
                {"id": "33333333-3333-3333-3333-333333333333", "name": "Gone", "lifecycle_state": "active",
                 "created_at": NOW, "status": "canceled", "plan": "oracle_swarm",
                 "current_period_end": NOW, "updated_at": NOW},
            ]),
            ("JOIN LATERAL", [{"id": T_LIVE, "name": "Lockwood", "status": "active",
                               "reactivated_at": NOW, "canceled_at": NOW - timedelta(days=3)}]),
            ("GROUP BY event_type", [{"event_type": "invoice.paid", "n": 4, "last_at": NOW}]),
            ("FROM billing_usage_events u", []),
        ],
        rows=[("FROM billing_usage_events", {"backlog": 40, "oldest": NOW, "erroring": 0})],
        vals=[("max(received_at)", NOW)],
    ))
    out = run(op.billing_exceptions(ctx=ADMIN))
    assert [r["tenant_id"] for r in out["no_subscription"]] == [T_NEW]
    assert out["payment_issue"][0]["status"] == "past_due"
    assert out["canceled"][0]["name"] == "Gone"
    assert out["reactivated_90d"][0]["tenant_id"] == T_LIVE
    # Metering off: unreported rows are history, not a backlog to page about.
    assert out["usage_metering"]["state"] == "off" and out["usage_metering"]["unreported"] == 40
    hooks = out["webhooks"]
    assert "invoice.payment_failed" in hooks["handled_event_types"]
    assert hooks["processed_30d"][0]["event_type"] == "invoice.paid"
    assert "whsec" not in json.dumps(hooks) and "sk_" not in json.dumps(hooks)


def test_mls_feeds_reports_every_feed_and_its_entitled_tenants(use_conn):
    use_conn(FakeConn(fetch=[
        ("FROM mls_sync_status", [
            {"mls_id": "actris", "mls_name": "ACTRIS", "provider": "bridge", "dataset": "actris",
             "feed_type": "Bridge_API_v2", "license_classification": "licensed_property_listing",
             "last_success_at": NOW - timedelta(minutes=5), "last_attempt_at": NOW, "last_sync_at": NOW,
             "last_error_class": None, "last_error": None, "consecutive_failures": 0,
             "backfill_complete": True, "backfill_cursor_key": None, "backfill_records": 10,
             "listings_synced": 10, "stale_after_minutes": 1440,
             "notes": json.dumps({"rejected": {"missing_price": 4}}), "updated_at": NOW},
            {"mls_id": "sample", "mls_name": "", "provider": "reso", "dataset": "sample",
             "feed_type": "RESO_Web_API", "license_classification": "developer_listing_dataset",
             "last_success_at": None, "last_attempt_at": NOW, "last_sync_at": None,
             "last_error_class": None, "last_error": None, "consecutive_failures": 0,
             "backfill_complete": False, "backfill_cursor_key": "k", "backfill_records": 3,
             "listings_synced": 0, "stale_after_minutes": 1440, "notes": "not json", "updated_at": NOW},
        ]),
        ("FROM mls_feed_entitlements", [
            {"mls_id": "actris", "n": 1, "tenants": [json.dumps({"tenant_id": T_LIVE, "name": "Lockwood"})]},
            {"mls_id": "ghost", "n": 1, "tenants": [{"tenant_id": T_NEW, "name": "Brand New"}]},
        ]),
    ]))
    out = run(op.mls_feeds(ctx=ADMIN))
    actris, sample = out["feeds"]
    assert actris["health"] == "READY" and actris["licensed"] is True
    assert actris["entitled_tenants"] == {"count": 1, "tenants": [{"tenant_id": T_LIVE, "name": "Lockwood"}]}
    assert actris["rejections"]["validation"] == 4
    assert sample["licensed"] is False and sample["health"] == "BACKFILLING"
    assert sample["backfill"]["in_progress"] is True
    assert sample["entitled_tenants"]["count"] == 0
    assert out["entitlements_without_feed"][0]["mls_id"] == "ghost"
    assert out["summary"]["licensed"] == 1


def test_comms_masks_numbers_and_names_problems(use_conn, monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "f" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", f"{S}_AUTH")
    for name in ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN", "TELNYX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    use_conn(FakeConn(fetch=[
        ("FROM telephony_routes", [{"tenant_id": T_LIVE, "routes": 2, "providers": ["plivo"],
                                    "sample_number": "+13025550199", "verified": 1, "pending": 1,
                                    "routing_errors": 1}]),
        ("FROM messaging_routes", [{"tenant_id": T_LIVE, "routes": 1, "providers": ["telnyx"], "live": 0,
                                    "pending": 0, "needs_action": 1}]),
        ("FROM tenant_messaging_brands", [{"tenant_id": T_LIVE, "brand_status": "verified",
                                           "campaign_statuses": ["rejected"]}]),
        ("FROM sms_messages", [{"tenant_id": T_LIVE, "outbound": 10, "failed": 5}]),
        ("FROM inbound_voice_calls", []),
        ("FROM command_executions", [{"tenant_id": T_LIVE, "command_type": "SMS", "failed": 2}]),
        ("FROM tenants", [{"id": T_LIVE, "name": "Lockwood"}]),
    ]))
    out = run(op.comms(ctx=ADMIN))
    row = out["brokerages"][0]
    assert row["name"] == "Lockwood"
    assert row["voice"]["business_number"] == "•••0199" and row["voice"]["providers"] == ["plivo"]
    assert "caller ID verification pending" in row["verification_pending"]
    assert "10DLC registration rejected" in row["problems"]
    assert "many texts failing in the last 24h" in row["problems"]
    assert row["failures_24h"]["neoh_texts_failed"] == 2
    assert out["platform_providers"] == {"twilio": True, "plivo": False, "telnyx": False}
    blob = json.dumps(out)
    assert S not in blob and "3025550199" not in blob


def test_ai_reports_providers_without_keys(use_conn, monkeypatch):
    import component_health
    import llm_gateway

    monkeypatch.setenv("ORACLE_FIREWORKS_API_KEY", f"fw-{S}-KEY")
    monkeypatch.setattr(llm_gateway, "counter", llm_gateway._Counter())

    class RateLimitError(Exception):
        status_code = 429

    llm_gateway.counter.record("analysis", "fireworks", ok=True)
    llm_gateway.counter.record("analysis", "fireworks", ok=False, exc=RateLimitError())

    async def snap(**_k):
        return {"components": {"ai": {"state": "HEALTHY", "summary": "Neoh responding"}}}
    monkeypatch.setattr(component_health, "snapshot", snap)
    use_conn(FakeConn(
        rows=[("percentile_cont", {"completed": 10, "failed": 1, "p50": 1.234, "p95": 6.0}),
              ("FROM ai_tool_operations", {"total": 20, "failed": 2})],
        fetch=[("GROUP BY error_code", [{"error_code": "rate_limited", "n": 1}]),
               ("GROUP BY model_id", [{"model_id": "accounts/fireworks/models/x", "n": 10}]),
               ("GROUP BY tool_name", [{"tool_name": "update_client", "error_code": "conflict", "n": 2}])],
    ))
    out = run(op.ai_operations(ctx=ADMIN))
    assert out["gateway"]["configured"] is True
    assert out["gateway"]["current"]["analysis"]["provider"] == "fireworks"
    fw = out["gateway"]["providers"]["fireworks"]
    assert fw == {"calls": 1, "failures": 1, "rate_limited": 1, "failure_rate": 0.5}
    assert out["chat_24h"]["latency_seconds"] == {"p50": 1.23, "p95": 6.0, "n": 10}
    assert out["chat_24h"]["rate_limited"] == 1
    assert out["tools_24h"]["failure_rate"] == 0.1
    assert out["health"]["state"] == "HEALTHY"
    assert S not in json.dumps(out)


def test_pilot_metrics_are_few_and_honest_about_attribution(use_conn):
    use_conn(FakeConn(fetch=[
        ("UNION", [{"tenant_id": T_LIVE, "n": 3}]),
        ("agent_days", [{"tenant_id": T_LIVE, "turns": 40, "agent_days": 9}]),
        ("FROM ai_tool_operations", [{"tenant_id": T_LIVE, "n": 12}]),
        ("FROM command_executions", [{"tenant_id": T_LIVE, "command_type": "CALL", "n": 2},
                                     {"tenant_id": T_LIVE, "command_type": "SMS", "n": 5}]),
        ("FROM marketplace_matches", []),
        ("FROM outcome_events", [{"tenant_id": T_LIVE, "closed": 4, "examined": 4, "associated": 1,
                                  "associated_value": 450000, "associated_unpriced": 0,
                                  "other_associated": 2}]),
        ("FROM tenants", [{"id": T_LIVE, "name": "Lockwood"}]),
    ]))
    out = run(op.pilot_metrics(days=7, deal_days=90, ctx=ADMIN))
    t = out["total"]
    assert len(t) <= 8
    assert t["active_agents"] == 3
    assert t["neoh_conversation_turns"] == 40
    assert t["neoh_completed_actions"] == 19
    assert t["outreach_through_neoh"] == {"calls": 2, "texts": 5, "emails": 0, "calendar": 0}
    assert t["deal_activity"]["associated"] == 1 and t["deal_activity"]["closed"] == 4
    assert t["deal_activity"]["associated_deal_value"] == 450000
    note = out["definitions"]["deal_activity"]
    assert "not causation" in note and "not commission revenue" in note
    assert "time_saved" in out["not_tracked"]


def test_release_flags_a_worker_on_another_build(use_conn, monkeypatch):
    monkeypatch.setenv("ORACLE_GIT_SHA", "new1234")
    use_conn(FakeConn(
        fetch=[("FROM process_heartbeats", [
            {"role": "worker", "git_sha": "old9999", "age_seconds": 5, "uptime_seconds": 50}])],
        vals=[("schema_migrations", "0001_init_tenancy.sql")],
    ))
    out = run(op.release(ctx=ADMIN))
    codes = {w["code"] for w in out["warnings"]}
    assert "worker_build_differs" in codes and "migrations_behind" in codes
    assert out["expected_migration_head"] > out["migration_head"]


# ---------------------------------------------------------------------------
# Gate and posture
# ---------------------------------------------------------------------------

def _routes():
    return [r for r in op.router.routes if "GET" in getattr(r, "methods", set())]


@pytest.mark.parametrize("ctx", [AGENT, OWNER], ids=["agent", "broker_owner"])
def test_every_operator_route_is_platform_admin_only(ctx):
    app = FastAPI()
    app.include_router(op.router)
    app.dependency_overrides[require_context] = lambda: ctx
    client = TestClient(app)
    paths = [r.path.replace("{tenant_id}", T_LIVE) for r in _routes()]
    assert len(paths) == 8, paths
    for path in paths:
        resp = client.get(path)
        assert resp.status_code == 403, (path, resp.status_code)


def _sql_literals() -> str:
    """Every string constant in the module that is SQL (docstrings excluded)."""
    tree = ast.parse(inspect.getsource(op))
    return "\n".join(
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and ("SELECT" in node.value or "FROM " in node.value or " WHERE " in node.value)
    )


def test_operator_sql_never_duplicates_the_rls_predicate():
    sql = _sql_literals()
    assert "FROM tenants" in sql  # the walk found the queries
    assert "app_current_tenant" not in sql


def test_operator_sql_never_selects_content_columns():
    src = _sql_literals()
    for column in ("content_ciphertext", "token_ciphertext", "refresh_ciphertext", "password_hash",
                   "transcript_ciphertext", "summary_ciphertext", "caller_phone_ciphertext",
                   "draft_payload", "SELECT * "):
        assert column not in src, column
