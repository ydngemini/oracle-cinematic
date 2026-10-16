from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import sales_api
from sales_api import (
    PlanCreate,
    PlanDefinition,
    PlanStep,
    ProviderSetupInput,
    _provider_payload,
    _sign_preview,
    _verify_preview,
)
from tenancy import Role, TenantContext


def test_smart_plan_accepts_all_supported_channels_and_rejects_unknown_fields():
    definition = PlanDefinition(
        steps=[
            PlanStep(key="wait_1", type="wait", delay_minutes=15),
            PlanStep(key="task_1", type="task", title="Review contact", priority="high"),
            PlanStep(key="email_1", type="email", subject="Next steps", body="Hello"),
            PlanStep(key="sms_1", type="sms", body="Are you available tomorrow?"),
            PlanStep(key="call_1", type="approved_call", body="Introduce yourself and confirm timing."),
        ]
    )

    plan = PlanCreate(name="Buyer follow-up", definition=definition)
    assert [step.type for step in plan.definition.steps] == [
        "wait",
        "task",
        "email",
        "sms",
        "approved_call",
    ]
    with pytest.raises(ValidationError, match="Extra inputs"):
        PlanStep.model_validate({"key": "wait_2", "type": "wait", "destination": "+13055550142"})


def test_smart_plan_requires_channel_specific_draft_fields_and_unique_keys():
    with pytest.raises(ValidationError, match="subject and body"):
        PlanStep(key="email_1", type="email", body="No subject")
    with pytest.raises(ValidationError, match="1-1600"):
        PlanStep(key="sms_1", type="sms", body="x" * 1601)
    with pytest.raises(ValidationError, match="unique"):
        PlanDefinition(
            steps=[
                PlanStep(key="wait_1", type="wait"),
                PlanStep(key="wait_1", type="wait"),
            ]
        )


def test_preview_signature_is_tenant_bound_tamper_evident_and_expires(monkeypatch):
    monkeypatch.setenv("ORACLE_SMART_PLAN_SIGNING_KEY", "s" * 64)
    payload = {
        "version": 1,
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "plan_id": "22222222-2222-2222-2222-222222222222",
        "contact_ids": ["33333333-3333-3333-3333-333333333333"],
        "start_at": "2026-08-03T18:00:00+00:00",
        "fingerprint": "f" * 64,
        "issued_at": int(sales_api.datetime.now(sales_api.timezone.utc).timestamp()),
    }
    token = _sign_preview(payload)
    assert _verify_preview(token) == payload

    raw, signature = token.split(".", 1)
    replacement = "0" if signature[-1] != "0" else "1"
    with pytest.raises(HTTPException, match="invalid"):
        _verify_preview(f"{raw}.{signature[:-1]}{replacement}")

    monkeypatch.setattr(sales_api, "_PREVIEW_TTL_SECONDS", -1)
    with pytest.raises(HTTPException, match="expired"):
        _verify_preview(token)


def test_provider_setup_is_structured_and_twilio_browser_call_ready():
    sid = "AC" + "a" * 32
    api_key = "SK" + "b" * 32
    app_sid = "AP" + "c" * 32
    body = ProviderSetupInput(
        account_label="team",
        account_sid=sid,
        auth_token="auth-token-value",
        api_key=api_key,
        api_secret="api-secret-value",
        from_number="+13055550142",
        twiml_app_sid=app_sid,
        sms_sender="+13055550143",
        sms_sender_type="twilio_registered",
    )
    payload = _provider_payload("twilio", body)

    assert payload["account_sid"] == sid
    assert payload["twiml_app_sid"] == app_sid
    assert payload["sms_sender_type"] == "twilio_registered"
    assert "account_label" not in payload
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProviderSetupInput.model_validate({"account_label": "x", "raw_secret_blob": "no"})


def test_sales_migration_forces_rls_and_keeps_revisions_immutable_and_destinations_out():
    migration = (
        Path(__file__).parents[1] / "db" / "migrations" / "0058_sales_ai_stack.sql"
    ).read_text(encoding="utf-8")

    for table in (
        "smart_plans",
        "smart_plan_revisions",
        "smart_plan_enrollments",
        "smart_plan_step_runs",
        "agent_call_intents",
    ):
        assert f"'{table}'" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "BEFORE UPDATE OR DELETE ON smart_plan_revisions" in migration
    assert "REVOKE DELETE, TRUNCATE" in migration

    call_table = migration.split("CREATE TABLE IF NOT EXISTS agent_call_intents", 1)[1]
    call_table = call_table.split("CREATE INDEX", 1)[0]
    assert "contact_id" in call_table
    assert "destination" not in call_table
    assert "phone" not in call_table


def test_personal_and_team_plan_access_is_role_safe():
    tenant_id = "11111111-1111-1111-1111-111111111111"
    owner = TenantContext("owner", tenant_id, Role.AGENT)
    other = TenantContext("other", tenant_id, Role.AGENT)
    broker = TenantContext("broker", tenant_id, Role.BROKER_OWNER)
    personal = {"scope": "personal", "owner_agent_id": "owner"}
    team = {"scope": "team", "owner_agent_id": "broker"}

    assert sales_api._plan_readable_by_agent(personal, owner)
    assert sales_api._plan_mutable_by_agent(personal, owner)
    assert not sales_api._plan_readable_by_agent(personal, other)
    assert sales_api._plan_readable_by_agent(team, other)
    assert not sales_api._plan_mutable_by_agent(team, other)
    assert sales_api._plan_mutable_by_agent(team, broker)


def test_a_saved_mail_server_needs_a_password_and_logs_in_as_the_from_address():
    with pytest.raises(HTTPException, match="password is required"):
        _provider_payload("smtp", ProviderSetupInput(host="mail.attacker.example", from_email="me@attacker.example"))
    payload = _provider_payload("smtp", ProviderSetupInput(
        host="smtp.gmail.com", port=587, from_email="jane@example.com", password="abcd efgh ijkl mnop"))
    assert payload["username"] == "jane@example.com"


def test_the_test_email_goes_only_to_the_connected_address(monkeypatch):
    import asyncio
    import json as _json
    import smtp_mailer

    sent = []

    async def load(_ctx, provider, *, account_label, exact):
        assert (provider, account_label, exact) == ("smtp", "avery", True)
        return _json.dumps({"host": "smtp.gmail.com", "port": 587, "username": "avery@example.com",
                            "password": "app-pw", "from_email": "avery@example.com"})

    monkeypatch.setattr(sales_api, "_load_provider_credential", load)
    monkeypatch.setattr(smtp_mailer, "resolve_settings", lambda c: {"sender": c["from_email"]})
    monkeypatch.setattr(smtp_mailer, "send", lambda **kw: sent.append(kw) or "<id@x>")
    agent = TenantContext(agent_id="avery", tenant_id="00000000-0000-0000-0000-0000000000aa", role=Role.AGENT)
    result = asyncio.run(sales_api.send_test_email("avery", ctx=agent))
    assert result == {"sent": True, "sent_to": "avery@example.com", "credential_exposed": False}
    assert [m["recipient"] for m in sent] == ["avery@example.com"]
    for label in ("default", "someone-else"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(sales_api.send_test_email(label, ctx=agent))
        assert exc.value.status_code == 403


def _snapshot_for(monkeypatch, rows, role, agent_id, route=None):
    import asyncio
    import json as _json
    from contextlib import asynccontextmanager

    class Conn:
        async def fetch(self, *_a):
            return rows

        async def fetchrow(self, *_a):
            return route

    @asynccontextmanager
    async def tx(_ctx):
        yield Conn()

    async def load(_ctx, provider, *, account_label, exact):
        return _json.dumps({"from_email": f"{account_label}@example.com"})

    monkeypatch.setattr(sales_api, "tenant_tx", tx)
    monkeypatch.setattr(sales_api, "_load_provider_credential", load)
    ctx = TenantContext(agent_id=agent_id, tenant_id="00000000-0000-0000-0000-0000000000aa", role=role)
    return asyncio.run(sales_api._provider_snapshot(ctx))


def _cred_row(label):
    return {"provider": "smtp", "account_label": label, "expires_at": None, "last_validated_at": None,
            "disabled_at": None, "validation_status": "valid", "validation_error": None,
            "validated_capabilities": {"email": True}, "updated_at": None}


def test_an_agent_sees_whose_mailbox_sends_their_email(monkeypatch):
    own = _snapshot_for(monkeypatch, [_cred_row("avery"), _cred_row("default")], Role.AGENT, "avery")
    assert own["providers"]["smtp"]["own"] is True
    assert own["providers"]["smtp"]["sending_as"] == "avery@example.com"
    shared = _snapshot_for(monkeypatch, [_cred_row("default")], Role.AGENT, "avery")
    assert shared["providers"]["smtp"]["own"] is False, "the brokerage default is not the agent's own"
    assert shared["providers"]["smtp"]["sending_as"] == "default@example.com"


# ── T10.09 / AC10.05 / L10-11: the platform relay is Ready only after a send ─

@pytest.mark.parametrize("outcome, ready, state", [
    (None, False, "not_yet_verified"),
    ({"last_ok_at": "2026-10-16T00:00:00Z", "consecutive_failures": 0}, True, "verified_by_send"),
    ({"last_ok_at": "2026-10-15T00:00:00Z", "consecutive_failures": 2}, False, "failing"),
    ({"last_ok_at": None, "consecutive_failures": 1}, False, "failing"),
])
def test_the_platform_relay_is_ready_only_after_a_real_send(monkeypatch, outcome, ready, state):
    import provider_health

    monkeypatch.setenv("ORACLE_SMTP_FROM_EMAIL", "relay@neoh.test")

    async def last(provider):
        assert provider == provider_health.PLATFORM_SMTP
        return outcome

    monkeypatch.setattr(provider_health, "last_outcome", last)
    snap = _snapshot_for(monkeypatch, [], Role.AGENT, "avery")
    assert snap["channels"]["email"] is ready
    assert snap["platform_sender_state"] == state
    assert snap["platform_sender"] == "relay@neoh.test"


def test_no_relay_settings_is_not_set_up(monkeypatch):
    monkeypatch.delenv("ORACLE_SMTP_FROM_EMAIL", raising=False)
    monkeypatch.delenv("ORACLE_SMTP_USERNAME", raising=False)
    snap = _snapshot_for(monkeypatch, [], Role.AGENT, "avery")
    assert snap["channels"]["email"] is False and snap["platform_sender_state"] == "not_configured"


def test_a_tenant_mail_server_never_vouches_for_the_platform_relay(monkeypatch):
    import asyncio

    import command_providers
    import provider_health
    import smtp_mailer

    recorded = []

    async def record(provider, kind):
        recorded.append((provider, kind))

    monkeypatch.setattr(provider_health, "record", record)
    monkeypatch.setattr(smtp_mailer, "send", lambda **kw: "<id@relay>")
    draft = {"target": {"email": "client@example.com"}, "subject": "Hi", "body": "Hello"}
    asyncio.run(command_providers.send_smtp_email(draft, credentials={"host": "mail.brokerage.test"}))
    assert recorded == []
    asyncio.run(command_providers.send_smtp_email(draft, credentials=None))
    assert recorded == [("platform_smtp", "ok")]

    def refuse(**kw):
        raise smtp_mailer.SmtpConfigurationError("bad password")

    monkeypatch.setattr(smtp_mailer, "send", refuse)
    with pytest.raises(command_providers.ProviderConfigurationError):
        asyncio.run(command_providers.send_smtp_email(draft, credentials=None))
    assert recorded[-1] == ("platform_smtp", "credential_rejected")


# ── T10.09 / AC10.05: platform SMS and calls are Ready only after a send ────

_ROUTE = {"sms_sender_e164": "+13025550100", "sms_sender_type": "twilio_registered",
          "voice_caller_id_verified": False, "voice_caller_id_e164": None}


def _platform_twilio(monkeypatch, outcomes):
    import provider_health

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t" * 32)
    # The operator flag that used to make these Ready counts for nothing now.
    monkeypatch.setenv("ORACLE_TWILIO_CREDENTIALS_VALIDATED", "true")

    async def last(provider):
        return outcomes.get(provider)

    monkeypatch.setattr(provider_health, "last_outcome", last)


@pytest.mark.parametrize("outcome, ready, state", [
    (None, False, "not_yet_verified"),
    ({"last_ok_at": "2026-10-16T00:00:00Z", "consecutive_failures": 0}, True, "verified_by_send"),
    ({"last_ok_at": "2026-10-15T00:00:00Z", "consecutive_failures": 1}, False, "failing"),
])
def test_platform_sms_and_calls_are_ready_only_after_a_real_send(monkeypatch, outcome, ready, state):
    import provider_health

    _platform_twilio(monkeypatch, {provider_health.PLATFORM_TWILIO_SMS: outcome,
                                   provider_health.PLATFORM_TWILIO_VOICE: outcome})
    snap = _snapshot_for(monkeypatch, [], Role.AGENT, "avery", route=_ROUTE)
    assert snap["channels"]["sms"] is ready
    assert snap["channels"]["ai_call"] is ready
    assert snap["platform_channel_states"] == {"sms": state, "ai_call": state}


def test_a_proven_sms_account_does_not_vouch_for_calls(monkeypatch):
    import provider_health

    ok = {"last_ok_at": "2026-10-16T00:00:00Z", "consecutive_failures": 0}
    _platform_twilio(monkeypatch, {provider_health.PLATFORM_TWILIO_SMS: ok})
    snap = _snapshot_for(monkeypatch, [], Role.AGENT, "avery", route=_ROUTE)
    assert snap["channels"]["sms"] is True and snap["channels"]["ai_call"] is False
    # Without a registered sender on the route, a proven account still cannot text.
    snap = _snapshot_for(monkeypatch, [], Role.AGENT, "avery", route=None)
    assert snap["channels"]["sms"] is False


def test_no_platform_twilio_is_not_set_up(monkeypatch):
    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY", "TWILIO_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    snap = _snapshot_for(monkeypatch, [], Role.AGENT, "avery", route=_ROUTE)
    assert snap["channels"]["sms"] is False and snap["channels"]["ai_call"] is False
    assert snap["platform_channel_states"] == {"sms": "not_configured", "ai_call": "not_configured"}


class _TwilioRefusal(Exception):
    def __init__(self, status, code):
        super().__init__(f"HTTP {status}")
        self.status, self.code = status, code


def _fake_twilio(monkeypatch, behaviour):
    import sys
    import types

    import command_providers

    exceptions = types.ModuleType("twilio.base.exceptions")
    exceptions.TwilioRestException = _TwilioRefusal
    monkeypatch.setitem(sys.modules, "twilio", types.ModuleType("twilio"))
    monkeypatch.setitem(sys.modules, "twilio.base", types.ModuleType("twilio.base"))
    monkeypatch.setitem(sys.modules, "twilio.base.exceptions", exceptions)

    def create(**_kw):
        if isinstance(behaviour, Exception):
            raise behaviour
        return types.SimpleNamespace(sid=behaviour)

    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create),
                                   calls=types.SimpleNamespace(create=create))
    monkeypatch.setattr(command_providers, "_twilio_client", lambda *a: client)


@pytest.mark.parametrize("send, provider_name", [("sms", "platform_twilio_sms"), ("call", "platform_twilio_voice")])
def test_only_the_platform_account_records_its_own_twilio_outcome(monkeypatch, send, provider_name):
    import asyncio

    import command_providers
    import provider_health

    recorded = []

    async def record(provider, kind):
        recorded.append((provider, kind))

    monkeypatch.setattr(provider_health, "record", record)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "1" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t" * 32)
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+13025550199")
    monkeypatch.setenv("ORACLE_TWILIO_TWIML_URL", "https://neoh.test/twiml")
    monkeypatch.delenv("ORACLE_TWILIO_ACCOUNT_TIER", raising=False)
    draft = {"target": {"phone": "+13025550111"}, "body": "Hello"}
    platform = {"sms_sender": "+13025550100", "sms_sender_type": "twilio_registered"} if send == "sms" else {}
    tenant = {**platform, "account_sid": "AC" + "2" * 32, "auth_token": "u" * 32}

    def go(credentials):
        fn = command_providers.send_twilio_sms if send == "sms" else command_providers.place_twilio_call
        return asyncio.run(fn(draft, credentials=credentials))

    _fake_twilio(monkeypatch, "SM1")
    go(tenant)
    assert recorded == [], "a brokerage's own Twilio account never vouches for the platform's"
    go(platform)
    assert recorded == [(provider_name, "ok")]

    _fake_twilio(monkeypatch, _TwilioRefusal(401, 20003))
    with pytest.raises(command_providers.ProviderRejectedError):
        go(platform)
    assert recorded[-1] == (provider_name, "credential_rejected")

    # A refusal about this recipient, or an answer Twilio may have acted on,
    # says nothing about the platform account: nothing is recorded.
    for refusal in (_TwilioRefusal(400, 21211), _TwilioRefusal(400, 21610), _TwilioRefusal(503, None)):
        _fake_twilio(monkeypatch, refusal)
        with pytest.raises(command_providers.ProviderRequestError):
            go(platform)
    assert len(recorded) == 2


# ── T07.04 / L07-03: which channels definitively need setup, and what ───────

def _cred(provider, label="default", caps=None, status="valid"):
    return {"provider": provider, "account_label": label, "expires_at": None, "last_validated_at": None,
            "disabled_at": None, "validation_status": status, "validation_error": None,
            "validated_capabilities": caps or {}, "updated_at": None}


def _gaps_for(monkeypatch, rows, route=None, *, twilio_env=False, smtp_env=False, outcomes=None):
    import asyncio
    import json as _json
    from contextlib import asynccontextmanager

    import provider_health

    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY", "TWILIO_API_SECRET",
                 "ORACLE_SMTP_FROM_EMAIL", "ORACLE_SMTP_USERNAME"):
        monkeypatch.delenv(name, raising=False)
    if twilio_env:
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t" * 32)
    if smtp_env:
        monkeypatch.setenv("ORACLE_SMTP_FROM_EMAIL", "relay@neoh.test")

    class Conn:
        async def fetch(self, *_a):
            return rows

        async def fetchrow(self, *_a):
            return route

    @asynccontextmanager
    async def tx(_ctx):
        yield Conn()

    async def load(_ctx, provider, *, account_label, exact):
        return _json.dumps({"from_email": f"{account_label}@example.com"})

    async def last(provider):
        return (outcomes or {}).get(provider)

    monkeypatch.setattr(sales_api, "tenant_tx", tx)
    monkeypatch.setattr(sales_api, "_load_provider_credential", load)
    monkeypatch.setattr(provider_health, "last_outcome", last)
    ctx = TenantContext(agent_id="avery", tenant_id="00000000-0000-0000-0000-0000000000aa", role=Role.AGENT)
    return asyncio.run(sales_api.channel_setup_gaps(ctx))


_TEXT_ROUTE = {"sms_sender_e164": "+13025550100", "sms_sender_type": "twilio_registered",
               "voice_caller_id_verified": False, "voice_caller_id_e164": None}


def test_with_nothing_set_up_every_channel_names_what_it_needs(monkeypatch):
    gaps = _gaps_for(monkeypatch, [], None)
    assert set(gaps) == set(sales_api.CHANNELS_FOR_TOOLS)
    assert "Providers" in gaps["email"] and "mail server" in gaps["email"]
    assert "Business number" in gaps["sms"] and "text number" in gaps["sms"]
    assert "Twilio" in gaps["voice"]
    assert "Google Calendar" in gaps["calendar"]


def test_configured_but_not_yet_proven_by_a_send_is_not_a_gap(monkeypatch):
    # The platform Twilio account is set but has never sent (L10-12): the first
    # text or call is how it gets proven, so the tools stay offered.
    gaps = _gaps_for(monkeypatch, [], _TEXT_ROUTE, twilio_env=True, smtp_env=True)
    assert "sms" not in gaps and "voice" not in gaps and "email" not in gaps
    assert set(gaps) == {"calendar"}


def test_a_text_number_without_any_twilio_account_is_still_a_gap(monkeypatch):
    gaps = _gaps_for(monkeypatch, [], _TEXT_ROUTE)
    assert "Twilio" in gaps["sms"] and "Business number" not in gaps["sms"]


def test_the_brokerages_own_accounts_close_the_gaps(monkeypatch):
    rows = [_cred("smtp", caps={"email": True}), _cred("twilio", caps={"sms": True, "voice": True}),
            _cred("google")]
    assert _gaps_for(monkeypatch, rows, _TEXT_ROUTE) == {}
    # A Twilio account validated for texts only still cannot place Neoh calls.
    rows = [_cred("twilio", caps={"sms": True})]
    gaps = _gaps_for(monkeypatch, rows, _TEXT_ROUTE)
    assert "sms" not in gaps and "Twilio voice" in gaps["voice"]
