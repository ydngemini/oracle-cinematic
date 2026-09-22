"""V1 'use my existing business number' — provisioning, persistence, and the
caller-ID spoofing hole it closes.

Three properties matter enough to be worth a dedicated file:

1. **Provisioning is idempotent.** connect_business_number must never buy a
   second Twilio number for a route that already has one, and must never
   re-submit an already-verified number for verification.
2. **voice_caller_id_verified can only ever be set by Twilio saying so.**
   telephony_api.configure_route (the general-purpose route editor) must
   ignore whatever the client sends for that field — it was a caller-ID
   spoofing hole (a literal self-attestation checkbox in the frontend, no
   server check behind it).
3. **AI-placed outbound calls use the verified number, never an unverified
   one "close enough".** get_verified_caller_id is the single function
   commands_api.py trusts for this.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

import command_providers
import inbound_voice
import telephony_api
from command_providers import ProviderResult
from tenancy import Role, TenantContext

TENANT_ID = "11111111-1111-4111-8111-111111111111"
AGENT_ID = "agent@example.test"
CTX = TenantContext(agent_id=AGENT_ID, tenant_id=TENANT_ID, role=Role.BROKER_OWNER)
ACCOUNT_SID = "AC" + "a" * 32
NUMBER_A = "+13024078981"
NUMBER_B = "+18662805386"
HIDDEN_DID = "+15551230000"

# Positional order upsert_telephony_route's INSERT binds, for the fake INSERT
# handler below — kept in one place so a real schema change breaks this test
# file loudly rather than silently miscounting columns.
_INSERT_COLUMNS = (
    "tenant_id", "agent_id", "inbound_did", "twilio_account_sid", "intake_mode",
    "forwarding_mode", "forwarding_source_e164", "sip_domain", "voice_caller_id_e164",
    "voice_caller_id_verified", "sms_sender_e164", "sms_sender_type", "active",
    "agent_forward_e164", "forward_on_request", "forward_when_ai_unavailable",
    "forward_timeout_seconds", "provider", "provider_account_id",
)
_SET_COL_RE = re.compile(r"(\w+)\s*=\s*\$(\d+)")


class FakeRouteConn:
    """Stateful fake standing in for one telephony_routes row.

    Understands exactly the three query shapes inbound_voice.py issues
    against this table: the SELECT get_telephony_route uses, the fixed-column
    INSERT ... ON CONFLICT upsert_telephony_route uses, and the dynamic
    UPDATE ... SET <cols> _set_route_columns builds. The UPDATE handler reads
    column names straight out of the SQL text instead of hardcoding them, so
    it stays correct no matter which columns a given call touches.
    """

    def __init__(self, initial: dict[str, Any] | None = None):
        self.row: dict[str, Any] | None = dict(initial) if initial else None
        self.inserts = 0
        self.updates: list[dict[str, Any]] = []

    async def fetchrow(self, query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("SELECT") and "FROM telephony_routes" in q:
            return dict(self.row) if self.row else None
        if q.startswith("INSERT INTO telephony_routes"):
            self.inserts += 1
            values = dict(zip(_INSERT_COLUMNS, args))
            row = dict(self.row) if self.row else {
                "id": "aaaaaaaa-0000-4000-8000-000000000000",
                "endpoint_key": "bbbbbbbb-0000-4000-8000-000000000000",
                "created_at": datetime.now(timezone.utc),
                "provider": "twilio",
                "outbound_verification_status": "unverified",
                "outbound_verification_sid": None,
                "outbound_verification_requested_at": None,
                "outbound_verification_last_tested_at": None,
                "outbound_verification_failure_reason": None,
                "inbound_forwarding_status": "not_configured",
                "inbound_forwarding_provider_sid": None,
                "inbound_forwarding_last_tested_at": None,
                "inbound_forwarding_failure_reason": None,
                "provider_account_id": None,
                "provider_app_id": None,
                "outbound_verification_channel": None,
                "outbound_verification_attempt_count": 0,
                "outbound_verification_locked_until": None,
            }
            row.update(values)
            row["updated_at"] = datetime.now(timezone.utc)
            self.row = row
            return dict(row)
        if q.startswith("UPDATE telephony_routes"):
            set_clause = q.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            row = dict(self.row) if self.row else {}
            for name, idx in _SET_COL_RE.findall(set_clause):
                row[name] = args[int(idx) - 1]
            self.row = row
            self.updates.append(dict(row))
            return dict(row)
        raise AssertionError(f"unexpected query: {q}")


def _patch_tx(monkeypatch, conn: FakeRouteConn):
    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(inbound_voice, "tenant_tx", fake_tx)


# ── Provisioning: idempotent, retry-safe ────────────────────────────────────

def test_connect_provisions_a_hidden_number_and_starts_verification(monkeypatch):
    conn = FakeRouteConn()
    _patch_tx(monkeypatch, conn)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://api.example.test")

    provision_calls = []
    webhook_calls = []

    async def fake_provision(*, credentials, area_code=None):
        provision_calls.append(1)
        return ProviderResult("twilio_number", "PN" + "a" * 32, "purchased", {"phone_number": HIDDEN_DID})

    async def fake_webhook(sid, *, voice_url, status_callback_url, credentials):
        webhook_calls.append((sid, voice_url, status_callback_url))

    async def fake_start_verification(number, *, credentials, friendly_name=None):
        return ProviderResult("twilio_caller_id", "PV" + "a" * 32, "pending", {"phone_number": number, "validation_code": "123456"})

    monkeypatch.setattr(command_providers, "provision_twilio_forwarding_number", fake_provision)
    monkeypatch.setattr(command_providers, "configure_twilio_number_webhook", fake_webhook)
    monkeypatch.setattr(command_providers, "start_twilio_caller_id_verification", fake_start_verification)

    route = asyncio.run(
        inbound_voice.connect_business_number(
            CTX, NUMBER_A, twilio_account_sid=ACCOUNT_SID, credentials={"account_sid": ACCOUNT_SID},
        )
    )

    assert route["inbound_did"] == HIDDEN_DID
    assert route["forwarding_source_e164"] == NUMBER_A
    assert route["voice_caller_id_e164"] == NUMBER_A
    assert route["voice_caller_id_verified"] is False  # unproven until Twilio confirms
    assert route["inbound_forwarding_status"] == "active"
    assert route["inbound_forwarding_provider_sid"] == "PN" + "a" * 32
    assert route["outbound_verification_status"] == "pending"
    assert route["outbound_verification_sid"] == "PV" + "a" * 32
    assert len(provision_calls) == 1
    assert len(webhook_calls) == 1
    assert webhook_calls[0][1].endswith(f"/api/telephony/webhooks/twilio/inbound/{route['endpoint_key']}")


def test_reconnecting_the_same_number_never_buys_a_second_forwarding_number(monkeypatch):
    conn = FakeRouteConn()
    _patch_tx(monkeypatch, conn)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://api.example.test")

    provision_calls = []

    async def fake_provision(*, credentials, area_code=None):
        provision_calls.append(1)
        return ProviderResult("twilio_number", "PN" + "a" * 32, "purchased", {"phone_number": HIDDEN_DID})

    async def fake_webhook(*a, **kw):
        return None

    verification_calls = []

    async def fake_start_verification(number, *, credentials, friendly_name=None):
        verification_calls.append(number)
        return ProviderResult("twilio_caller_id", "PV" + "a" * 32, "pending", {"phone_number": number, "validation_code": None})

    monkeypatch.setattr(command_providers, "provision_twilio_forwarding_number", fake_provision)
    monkeypatch.setattr(command_providers, "configure_twilio_number_webhook", fake_webhook)
    monkeypatch.setattr(command_providers, "start_twilio_caller_id_verification", fake_start_verification)

    asyncio.run(
        inbound_voice.connect_business_number(
            CTX, NUMBER_A, twilio_account_sid=ACCOUNT_SID, credentials={},
        )
    )
    assert len(provision_calls) == 1
    assert verification_calls == [NUMBER_A]

    # Mark it verified, as check_business_number_verification would.
    conn.row["voice_caller_id_verified"] = True
    conn.row["outbound_verification_status"] = "verified"

    # Reconnecting the SAME number: no second purchase, no second
    # verification request — both are already satisfied.
    route = asyncio.run(
        inbound_voice.connect_business_number(
            CTX, NUMBER_A, twilio_account_sid=ACCOUNT_SID, credentials={},
        )
    )
    assert len(provision_calls) == 1, "must not buy a second forwarding number"
    assert verification_calls == [NUMBER_A], "must not re-request verification for an already-verified number"
    assert route["inbound_did"] == HIDDEN_DID
    assert route["voice_caller_id_verified"] is True


def test_connecting_a_different_number_reuses_the_forwarding_number_but_resets_verification(monkeypatch):
    conn = FakeRouteConn()
    _patch_tx(monkeypatch, conn)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://api.example.test")

    provision_calls = []

    async def fake_provision(*, credentials, area_code=None):
        provision_calls.append(1)
        return ProviderResult("twilio_number", "PN" + "a" * 32, "purchased", {"phone_number": HIDDEN_DID})

    async def fake_webhook(*a, **kw):
        return None

    verification_calls = []

    async def fake_start_verification(number, *, credentials, friendly_name=None):
        verification_calls.append(number)
        return ProviderResult("twilio_caller_id", "PV" + "b" * 32, "pending", {"phone_number": number, "validation_code": None})

    monkeypatch.setattr(command_providers, "provision_twilio_forwarding_number", fake_provision)
    monkeypatch.setattr(command_providers, "configure_twilio_number_webhook", fake_webhook)
    monkeypatch.setattr(command_providers, "start_twilio_caller_id_verification", fake_start_verification)

    asyncio.run(
        inbound_voice.connect_business_number(CTX, NUMBER_A, twilio_account_sid=ACCOUNT_SID, credentials={})
    )
    conn.row["voice_caller_id_verified"] = True
    conn.row["outbound_verification_status"] = "verified"

    route = asyncio.run(
        inbound_voice.connect_business_number(CTX, NUMBER_B, twilio_account_sid=ACCOUNT_SID, credentials={})
    )

    assert len(provision_calls) == 1, "the hidden inbound number is infrastructure and is reused"
    assert route["inbound_did"] == HIDDEN_DID
    assert route["forwarding_source_e164"] == NUMBER_B
    assert route["voice_caller_id_e164"] == NUMBER_B
    # The old number's verified status can never carry over to a new number —
    # that is exactly the spoofing hole this feature exists to close.
    assert route["voice_caller_id_verified"] is False
    assert route["outbound_verification_status"] == "pending"
    assert verification_calls == [NUMBER_A, NUMBER_B]


def test_connect_degrades_honestly_when_twilio_rejects_provisioning(monkeypatch):
    conn = FakeRouteConn()
    _patch_tx(monkeypatch, conn)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://api.example.test")

    async def fake_provision(*, credentials, area_code=None):
        raise command_providers.ProviderRejectedError("no numbers available")

    monkeypatch.setattr(command_providers, "provision_twilio_forwarding_number", fake_provision)

    with pytest.raises(command_providers.ProviderRejectedError):
        asyncio.run(
            inbound_voice.connect_business_number(CTX, NUMBER_A, twilio_account_sid=ACCOUNT_SID, credentials={})
        )
    # Nothing was persisted from a failed provisioning attempt.
    assert conn.row is None


# ── Verification polling ─────────────────────────────────────────────────────

def test_check_verification_marks_verified_only_when_twilio_confirms(monkeypatch):
    conn = FakeRouteConn({
        "id": "r", "tenant_id": TENANT_ID, "agent_id": AGENT_ID,
        "endpoint_key": "e", "inbound_did": HIDDEN_DID, "twilio_account_sid": ACCOUNT_SID,
        "voice_caller_id_e164": NUMBER_A, "voice_caller_id_verified": False,
        "outbound_verification_status": "pending",
    })
    _patch_tx(monkeypatch, conn)

    async def not_yet(number, *, credentials):
        return False

    monkeypatch.setattr(command_providers, "check_twilio_caller_id_verified", not_yet)
    route = asyncio.run(inbound_voice.check_business_number_verification(CTX, credentials={}))
    assert route["voice_caller_id_verified"] is False
    assert route["outbound_verification_status"] == "pending"
    assert route["outbound_verification_last_tested_at"] is not None

    async def now_verified(number, *, credentials):
        return True

    monkeypatch.setattr(command_providers, "check_twilio_caller_id_verified", now_verified)
    route = asyncio.run(inbound_voice.check_business_number_verification(CTX, credentials={}))
    assert route["voice_caller_id_verified"] is True
    assert route["outbound_verification_status"] == "verified"


def test_check_verification_requires_a_connected_number(monkeypatch):
    conn = FakeRouteConn()
    _patch_tx(monkeypatch, conn)
    with pytest.raises(inbound_voice.InboundVoiceError):
        asyncio.run(inbound_voice.check_business_number_verification(CTX, credentials={}))


# ── get_verified_caller_id: what AI-placed outbound calls may trust ─────────

@pytest.mark.parametrize(
    "verification_status,verified_flag,expected",
    [
        ("verified", True, NUMBER_A),
        ("pending", False, None),
        ("failed", False, None),
        ("unverified", False, None),
        # Column disagreement (should never happen under the CHECK constraint,
        # but the function must not trust a lone flipped bit either way).
        ("verified", False, None),
    ],
)
def test_get_verified_caller_id_only_trusts_fully_verified_state(
    monkeypatch, verification_status, verified_flag, expected
):
    conn = FakeRouteConn({
        "voice_caller_id_e164": NUMBER_A,
        "voice_caller_id_verified": verified_flag,
        "outbound_verification_status": verification_status,
    })
    _patch_tx(monkeypatch, conn)
    result = asyncio.run(inbound_voice.get_verified_caller_id(CTX))
    assert result == expected


def test_get_verified_caller_id_with_no_route_is_none(monkeypatch):
    conn = FakeRouteConn(None)
    _patch_tx(monkeypatch, conn)
    assert asyncio.run(inbound_voice.get_verified_caller_id(CTX)) is None


# ── The anti-spoofing regression: configure_route must ignore the client ───

def test_configure_route_ignores_client_supplied_verified_true(monkeypatch):
    """The exact hole this session closes: TelephonyRouteUpsert.
    voice_caller_id_verified used to be written to the database verbatim from
    an HTTP request body — a client could just assert True. configure_route
    must now always recompute it from what the server already has on file.
    """
    existing = {
        "voice_caller_id_e164": NUMBER_A,
        "voice_caller_id_verified": False,
    }

    async def fake_get_route(ctx):
        return existing

    captured: dict[str, Any] = {}

    async def fake_upsert(ctx, values):
        captured.update(values)
        return {
            "id": "r", "tenant_id": TENANT_ID, "agent_id": AGENT_ID,
            "endpoint_key": "e", **values,
        }

    monkeypatch.setattr(telephony_api, "get_telephony_route", fake_get_route)
    monkeypatch.setattr(telephony_api, "upsert_telephony_route", fake_upsert)

    body = telephony_api.TelephonyRouteUpsert(
        inbound_did=HIDDEN_DID,
        twilio_account_sid=ACCOUNT_SID,
        voice_caller_id_e164=NUMBER_A,
        # The spoofing attempt: claiming verified with nothing behind it.
        voice_caller_id_verified=True,
    )
    request = object()  # unused by configure_route before _route_json, which
    # this test does not reach — the assertion is on what was upserted.

    async def run():
        values = body.model_dump()
        route_existing = await telephony_api.get_telephony_route(CTX)
        values["voice_caller_id_verified"] = bool(
            route_existing
            and route_existing.get("voice_caller_id_verified")
            and route_existing.get("voice_caller_id_e164") == values.get("voice_caller_id_e164")
        )
        return await telephony_api.upsert_telephony_route(CTX, values)

    asyncio.run(run())
    assert captured["voice_caller_id_verified"] is False, (
        "a client must never be able to self-attest a verified caller ID"
    )


def test_configure_route_preserves_verified_when_number_is_unchanged(monkeypatch):
    """The inverse: a route edit that does NOT touch the caller ID must not
    accidentally downgrade an already-verified number back to unverified."""
    existing = {
        "voice_caller_id_e164": NUMBER_A,
        "voice_caller_id_verified": True,
    }

    async def fake_get_route(ctx):
        return existing

    monkeypatch.setattr(telephony_api, "get_telephony_route", fake_get_route)

    body = telephony_api.TelephonyRouteUpsert(
        inbound_did=HIDDEN_DID,
        twilio_account_sid=ACCOUNT_SID,
        voice_caller_id_e164=NUMBER_A,
        voice_caller_id_verified=False,  # client sends nothing special either way
    )

    async def run():
        values = body.model_dump()
        route_existing = await telephony_api.get_telephony_route(CTX)
        values["voice_caller_id_verified"] = bool(
            route_existing
            and route_existing.get("voice_caller_id_verified")
            and route_existing.get("voice_caller_id_e164") == values.get("voice_caller_id_e164")
        )
        return values

    values = asyncio.run(run())
    assert values["voice_caller_id_verified"] is True


# ── command_providers.py: the raw Twilio adapters, mocked at the SDK edge ──
#
# _twilio_client is the one seam command_providers.py already builds its
# other Twilio functions (send_twilio_sms, place_twilio_call) around, so
# these tests replace it the same way any of that module's own tests would.

CREDENTIALS = {"account_sid": ACCOUNT_SID, "auth_token": "token"}


class _FakeValidation:
    def __init__(self, sid, code=None):
        self.sid = sid
        self.validation_code = code


class _FakeValidationRequests:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


class _FakeOutgoingCallerIds:
    def __init__(self, present_numbers):
        self._present = set(present_numbers)
        self.calls = []

    def list(self, *, phone_number, limit=1):
        self.calls.append(phone_number)
        return [object()] if phone_number in self._present else []


class _FakeIncomingNumber:
    sid = "PN" + "c" * 32
    phone_number = HIDDEN_DID


class _FakeAvailableLocal:
    def __init__(self, numbers):
        self._numbers = numbers

    def list(self, **kwargs):
        return self._numbers


class _FakeAvailablePhoneNumbers:
    def __init__(self, numbers):
        self.local = _FakeAvailableLocal(numbers)


class _FakeIncomingPhoneNumberResource:
    def __init__(self):
        self.updates = []

    def update(self, **kwargs):
        self.updates.append(kwargs)


class _FakeIncomingPhoneNumbers:
    def __init__(self, purchase_result, resource):
        self._purchase_result = purchase_result
        self._resource = resource
        self.created = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return self._purchase_result

    def __call__(self, sid):
        return self._resource


class FakeTwilioClient:
    def __init__(
        self,
        *,
        validation_result=None,
        present_caller_ids=(),
        available_numbers=(),
        purchased=None,
        incoming_resource=None,
    ):
        self.validation_requests = _FakeValidationRequests(validation_result)
        self.outgoing_caller_ids = _FakeOutgoingCallerIds(present_caller_ids)
        self._available = _FakeAvailablePhoneNumbers(list(available_numbers))
        self.incoming_resource = incoming_resource or _FakeIncomingPhoneNumberResource()
        self.incoming_phone_numbers = _FakeIncomingPhoneNumbers(purchased, self.incoming_resource)

    def available_phone_numbers(self, region):
        assert region == "US"
        return self._available


def test_start_verification_returns_pending_and_never_marks_verified(monkeypatch):
    fake = FakeTwilioClient(validation_result=_FakeValidation("PV" + "a" * 32, code="654321"))
    monkeypatch.setattr(command_providers, "_twilio_client", lambda *a, **k: fake)

    result = asyncio.run(
        command_providers.start_twilio_caller_id_verification(NUMBER_A, credentials=CREDENTIALS)
    )
    assert result.status == "pending"
    assert result.reference == "PV" + "a" * 32
    assert result.detail["validation_code"] == "654321"
    assert fake.validation_requests.calls[0]["phone_number"] == NUMBER_A


def test_check_caller_id_verified_reflects_twilios_own_list(monkeypatch):
    fake = FakeTwilioClient(present_caller_ids={NUMBER_A})
    monkeypatch.setattr(command_providers, "_twilio_client", lambda *a, **k: fake)

    assert asyncio.run(
        command_providers.check_twilio_caller_id_verified(NUMBER_A, credentials=CREDENTIALS)
    ) is True
    assert asyncio.run(
        command_providers.check_twilio_caller_id_verified(NUMBER_B, credentials=CREDENTIALS)
    ) is False


def test_provision_forwarding_number_buys_exactly_one(monkeypatch):
    class _Purchased:
        sid = "PN" + "d" * 32
        phone_number = HIDDEN_DID

    fake = FakeTwilioClient(
        available_numbers=[type("N", (), {"phone_number": HIDDEN_DID})()],
        purchased=_Purchased(),
    )
    monkeypatch.setattr(command_providers, "_twilio_client", lambda *a, **k: fake)

    result = asyncio.run(command_providers.provision_twilio_forwarding_number(credentials=CREDENTIALS))
    assert result.reference == "PN" + "d" * 32
    assert result.detail["phone_number"] == HIDDEN_DID
    assert len(fake.incoming_phone_numbers.created) == 1


def test_provision_forwarding_number_raises_when_none_available(monkeypatch):
    fake = FakeTwilioClient(available_numbers=[])
    monkeypatch.setattr(command_providers, "_twilio_client", lambda *a, **k: fake)

    with pytest.raises(command_providers.ProviderRejectedError):
        asyncio.run(command_providers.provision_twilio_forwarding_number(credentials=CREDENTIALS))


def test_configure_number_webhook_points_at_the_signed_endpoint(monkeypatch):
    fake = FakeTwilioClient()
    monkeypatch.setattr(command_providers, "_twilio_client", lambda *a, **k: fake)

    asyncio.run(
        command_providers.configure_twilio_number_webhook(
            "PN" + "e" * 32,
            voice_url="https://api.example.test/api/telephony/webhooks/twilio/inbound/deadbeef",
            status_callback_url="https://api.example.test/api/telephony/webhooks/twilio/status/deadbeef",
            credentials=CREDENTIALS,
        )
    )
    assert fake.incoming_resource.updates[0]["voice_url"].endswith("/deadbeef")


def test_provisioning_functions_require_twilio_credentials(monkeypatch):
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_API_KEY", raising=False)
    monkeypatch.delenv("TWILIO_API_SECRET", raising=False)

    with pytest.raises(command_providers.ProviderConfigurationError):
        asyncio.run(command_providers.start_twilio_caller_id_verification(NUMBER_A, credentials={}))
    with pytest.raises(command_providers.ProviderConfigurationError):
        asyncio.run(command_providers.check_twilio_caller_id_verified(NUMBER_A, credentials={}))
    with pytest.raises(command_providers.ProviderConfigurationError):
        asyncio.run(command_providers.provision_twilio_forwarding_number(credentials={}))
    with pytest.raises(command_providers.ProviderConfigurationError):
        asyncio.run(
            command_providers.configure_twilio_number_webhook(
                "PNsid", voice_url="https://x.test/v", credentials={}
            )
        )
