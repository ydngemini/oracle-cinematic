"""Telnyx webhooks are authenticated by an authorized signing key and act
only for the brokerages that key speaks for (external audit 25.4).

Real Ed25519 keys: the platform's, its rotation predecessor, and two
brokerages' own accounts (A, B). Regression list from the audit: account A /
B keys, crossed destinations, an old rotated key, an unknown key, a tampered
body, a replayed timestamp, same-number reassignment.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

import messaging_api
import messaging_data
import telnyx_signing

TA, TB, TC = "aaaaaaaa-0000-4000-8000-00000000000a", "bbbbbbbb-0000-4000-8000-00000000000b", \
    "cccccccc-0000-4000-8000-00000000000c"


def _key():
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    return private, public


PLATFORM, PLATFORM_OLD, KEY_A, KEY_B, STRANGER = (_key() for _ in range(5))


def _signed(private, body: dict, *, ts=None, tamper=False):
    raw = json.dumps(body)
    ts = str(int(time.time()) if ts is None else ts)
    sig = base64.b64encode(private.sign(f"{ts}|{raw}".encode())).decode()
    if tamper:
        raw = raw.replace("hello", "HELLO")
    return raw, {"telnyx-signature-ed25519": sig, "telnyx-timestamp": ts}


def _inbound(to="+13025550100", text="hello"):
    return {"data": {"event_type": "message.received", "id": "evt1", "payload": {
        "id": "msg-1", "text": text, "from": {"phone_number": "+13025550142"},
        "to": [{"phone_number": to}], "media": []}}}


@pytest.fixture
def registry(monkeypatch):
    """Platform key (current + previous during rotation); A and B each run
    their own Telnyx account with their own key; C uses the platform's."""
    monkeypatch.setenv("TELNYX_PUBLIC_KEY", PLATFORM[1])
    monkeypatch.setenv("TELNYX_PUBLIC_KEY_PREVIOUS", PLATFORM_OLD[1])

    async def creds():
        return [(TA, {"api_key": "key-a", "public_key": KEY_A[1]}),
                (TB, {"api_key": "key-b", "public_key": KEY_B[1]})]

    monkeypatch.setattr(telnyx_signing, "_tenant_credentials", creds)
    telnyx_signing.reset_cache()
    yield
    telnyx_signing.reset_cache()


def verify(raw, headers):
    return asyncio.run(telnyx_signing.verify(raw, headers))


def test_each_account_key_speaks_only_for_its_own_brokerage(registry):
    _, va = verify(*_signed(KEY_A[0], _inbound()))
    assert va.may_act_for(TA) and not va.may_act_for(TB) and not va.may_act_for(TC)
    _, vb = verify(*_signed(KEY_B[0], _inbound()))
    assert vb.may_act_for(TB) and not vb.may_act_for(TA)
    # The platform key speaks for brokerages on the platform account only —
    # not for one that runs its own Telnyx account.
    _, vp = verify(*_signed(PLATFORM[0], _inbound()))
    assert vp.may_act_for(TC) and not vp.may_act_for(TA) and not vp.may_act_for(TB)


def test_the_previous_platform_key_works_only_while_it_is_configured(registry, monkeypatch):
    _, v = verify(*_signed(PLATFORM_OLD[0], _inbound()))
    assert v.platform and v.label == "platform-previous"
    monkeypatch.delenv("TELNYX_PUBLIC_KEY_PREVIOUS")      # rotation complete
    with pytest.raises(ValueError):
        verify(*_signed(PLATFORM_OLD[0], _inbound()))


@pytest.mark.parametrize("case", ["unknown_key", "tampered", "replayed", "future"])
def test_unauthenticated_events_are_refused(registry, case):
    if case == "unknown_key":
        raw, headers = _signed(STRANGER[0], _inbound())
    elif case == "tampered":
        raw, headers = _signed(KEY_A[0], _inbound(), tamper=True)
    elif case == "replayed":
        raw, headers = _signed(KEY_A[0], _inbound(), ts=int(time.time()) - 600)
    else:
        raw, headers = _signed(KEY_A[0], _inbound(), ts=int(time.time()) + 600)
    with pytest.raises(ValueError):
        verify(raw, headers)


def test_no_key_at_all_is_a_configuration_error(monkeypatch):
    monkeypatch.delenv("TELNYX_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("TELNYX_PUBLIC_KEY_PREVIOUS", raising=False)

    async def none():
        return []

    monkeypatch.setattr(telnyx_signing, "_tenant_credentials", none)
    telnyx_signing.reset_cache()
    with pytest.raises(telnyx_signing.NoSigningKeys):
        verify(*_signed(PLATFORM[0], _inbound()))


def test_a_newly_saved_brokerage_key_is_picked_up_on_a_miss(registry, monkeypatch):
    with pytest.raises(ValueError):
        verify(*_signed(STRANGER[0], _inbound()))

    async def creds():
        return [(TC, {"api_key": "key-c", "public_key": STRANGER[1]})]

    monkeypatch.setattr(telnyx_signing, "_tenant_credentials", creds)
    monkeypatch.setattr(telnyx_signing, "_last_miss_reload", 0.0)
    _, v = verify(*_signed(STRANGER[0], _inbound()))
    assert v.may_act_for(TC)


# ── the webhook ─────────────────────────────────────────────────────────────

@pytest.fixture
def webhook(registry, monkeypatch):
    state = {"route_tenant": TA, "recorded": [], "delivered": [], "suppressed": [], "message_tenant": TA}

    async def route(_to):
        return {"tenant_id": state["route_tenant"], "agent_id": "agent@x.test"} if state["route_tenant"] else None

    async def record_inbound(**kw):
        state["recorded"].append(kw["tenant_id"])

    async def owners(_to):
        return [{"tenant_id": TA, "agent_id": "a"}, {"tenant_id": TB, "agent_id": "b"}]

    async def message_tenant(_p, _id):
        return state["message_tenant"]

    async def delivered(**kw):
        state["delivered"].append(kw["provider_message_id"])
        return True

    import outreach_compliance

    async def suppress(ctx, **_kw):
        state["suppressed"].append(ctx.tenant_id)

    monkeypatch.setattr(messaging_data, "resolve_messaging_route_by_number", route)
    monkeypatch.setattr(messaging_data, "record_inbound_message", record_inbound)
    monkeypatch.setattr(messaging_data, "resolve_opt_out_owners_by_number", owners)
    monkeypatch.setattr(messaging_data, "tenant_for_provider_message", message_tenant)
    monkeypatch.setattr(messaging_data, "record_delivery_update", delivered)
    monkeypatch.setattr(outreach_compliance.ConsentLedger, "suppress", staticmethod(suppress))
    app = FastAPI()
    app.include_router(messaging_api.router)
    state["client"] = TestClient(app)
    return state


def post(state, raw, headers):
    return state["client"].post("/api/messaging/webhooks/telnyx", content=raw,
                                headers={**headers, "content-type": "application/json"})


def test_a_message_to_its_own_brokerage_is_recorded(webhook):
    assert post(webhook, *_signed(KEY_A[0], _inbound())).status_code == 204
    assert webhook["recorded"] == [TA]


def test_a_crossed_destination_is_refused(webhook):
    # B's account signs an event for a number that routes to A.
    assert post(webhook, *_signed(KEY_B[0], _inbound())).status_code == 403
    assert webhook["recorded"] == []


def test_a_reassigned_number_does_not_follow_its_old_account(webhook):
    # The number moved from A to B; A's account still sends events for it.
    webhook["route_tenant"] = TB
    assert post(webhook, *_signed(KEY_A[0], _inbound())).status_code == 403
    assert post(webhook, *_signed(KEY_B[0], _inbound())).status_code == 204
    assert webhook["recorded"] == [TB]


def test_the_platform_key_cannot_speak_for_a_brokerage_with_its_own_account(webhook):
    assert post(webhook, *_signed(PLATFORM[0], _inbound())).status_code == 403
    webhook["route_tenant"] = TC
    assert post(webhook, *_signed(PLATFORM[0], _inbound())).status_code == 204


def test_an_orphan_stop_is_recorded_only_for_brokerages_the_key_speaks_for(webhook):
    webhook["route_tenant"] = None
    assert post(webhook, *_signed(KEY_A[0], _inbound(text="STOP"))).status_code == 204
    assert webhook["suppressed"] == [TA]


def test_a_delivery_receipt_from_another_account_is_not_applied(webhook):
    receipt = {"data": {"event_type": "message.finalized", "id": "evt2", "payload": {
        "id": "msg-9", "to": [{"phone_number": "+13025550142", "status": "delivered"}], "errors": []}}}
    assert post(webhook, *_signed(KEY_B[0], receipt)).status_code == 403
    assert webhook["delivered"] == []
    assert post(webhook, *_signed(KEY_A[0], receipt)).status_code == 204
    assert webhook["delivered"] == ["msg-9"]


def test_an_unauthenticated_webhook_is_400(webhook):
    assert post(webhook, *_signed(STRANGER[0], _inbound())).status_code == 400


# ── review round 1 (25.4) ───────────────────────────────────────────────────

def test_a_stop_on_a_refused_route_is_still_recorded_for_the_keys_own_holders(webhook):
    # B's account signs a STOP to a number that routes to A: the message is
    # not logged for A, but the opt-out is kept for the holder B speaks for.
    assert post(webhook, *_signed(KEY_B[0], _inbound(text="STOP"))).status_code == 204
    assert webhook["recorded"] == [] and webhook["suppressed"] == [TB]


def test_a_platform_signed_stop_is_kept_even_for_an_own_account_brokerage(webhook):
    # The platform key cannot speak for A's messages, but a STOP it signed is
    # recorded for every holder: over-suppression is the safe direction.
    assert post(webhook, *_signed(PLATFORM[0], _inbound(text="STOP"))).status_code == 204
    assert sorted(webhook["suppressed"]) == sorted([TA, TB])


def test_a_receipt_for_a_message_not_written_yet_is_retried_not_applied_unchecked(webhook):
    webhook["message_tenant"] = None
    receipt = {"data": {"event_type": "message.finalized", "id": "evt3", "payload": {
        "id": "msg-new", "to": [{"phone_number": "+13025550142", "status": "delivered"}], "errors": []}}}
    response = post(webhook, *_signed(KEY_A[0], receipt))
    assert response.status_code == 503 and response.headers.get("retry-after") == "30"
    assert webhook["delivered"] == []


def test_a_10dlc_nudge_is_ignored_unless_the_key_speaks_for_its_brokerage(registry, monkeypatch):
    synced = []

    async def owner(_provider_id):
        return TA

    async def sync(tenant_id, **_k):
        synced.append(tenant_id)

    monkeypatch.setattr(messaging_data, "tenant_for_registration", owner)
    monkeypatch.setattr(messaging_api, "sync_registration_for_tenant", sync)
    _, vb = verify(*_signed(KEY_B[0], _inbound()))
    asyncio.run(messaging_api._refresh_for_provider_id("C1", vb))
    assert synced == []
    _, va = verify(*_signed(KEY_A[0], _inbound()))
    asyncio.run(messaging_api._refresh_for_provider_id("C1", va))
    assert synced == [TA]


def _registry_with(monkeypatch, creds, **env):
    for name in ("TELNYX_PUBLIC_KEY", "TELNYX_PUBLIC_KEY_PREVIOUS", "TELNYX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    async def load():
        return creds

    monkeypatch.setattr(telnyx_signing, "_tenant_credentials", load)
    telnyx_signing.reset_cache()


def test_own_account_mirrors_how_the_app_resolves_a_brokerage_credential(monkeypatch):
    TD, TE = "dddddddd-0000-4000-8000-00000000000d", "eeeeeeee-0000-4000-8000-00000000000e"
    _registry_with(monkeypatch, [
        (TA, {"api_key": "key-a", "public_key": KEY_A[1]}),     # its own account
        (TC, {"api_key": "key-c"}),                             # no own key: the platform key is its key
        (TD, {"api_key": "platform-api", "public_key": KEY_B[1]}),  # the platform's own account
        (TE, None),                                             # unreadable: fails closed (STOPs still kept)
    ], TELNYX_PUBLIC_KEY=PLATFORM[1], TELNYX_API_KEY="platform-api")
    _, vp = verify(*_signed(PLATFORM[0], _inbound()))
    assert not vp.may_act_for(TA) and vp.may_act_for(TC) and vp.may_act_for(TD)
    assert not vp.may_act_for(TE) and vp.may_suppress_for(TE)
    _, vd = verify(*_signed(KEY_B[0], _inbound()))
    assert vd.may_act_for(TD)


def test_a_key_shared_by_the_platform_and_a_brokerage_speaks_for_both(monkeypatch):
    _registry_with(monkeypatch, [(TA, {"api_key": "key-a", "public_key": PLATFORM[1]})],
                   TELNYX_PUBLIC_KEY=PLATFORM[1])
    _, v = verify(*_signed(PLATFORM[0], _inbound()))
    assert v.platform and TA in v.tenants and v.may_act_for(TA) and v.may_act_for(TC)


def test_a_failed_load_keeps_the_last_good_keys(registry, monkeypatch):
    _, va = verify(*_signed(KEY_A[0], _inbound()))
    assert va.may_act_for(TA)

    async def down():
        raise telnyx_signing.CredentialsUnavailable("database down")

    monkeypatch.setattr(telnyx_signing, "_tenant_credentials", down)
    asyncio.run(telnyx_signing._registry(force=True))
    _, va = verify(*_signed(KEY_A[0], _inbound()))
    assert va.may_act_for(TA) and not va.may_act_for(TB)   # the last good set, not 'no brokerage keys'
    _, vp = verify(*_signed(PLATFORM[0], _inbound()))
    assert not vp.may_act_for(TA)            # A still recorded as own-account


def test_many_keys_and_a_forged_flood_do_not_rebuild_clients(monkeypatch):
    import messaging_provider

    tenants = [(f"{i:08d}-0000-4000-8000-000000000000", _key()) for i in range(12)]
    _registry_with(monkeypatch, [(t, {"api_key": f"k{i}", "public_key": key[1]})
                                 for i, (t, key) in enumerate(tenants)], TELNYX_PUBLIC_KEY=PLATFORM[1])
    builds = []
    real = messaging_provider._verification_client

    def counting(api_key, public_key):
        builds.append(public_key)
        return real(api_key, public_key)

    monkeypatch.setattr(messaging_provider, "_verification_client", counting)
    last_tenant, last_key = tenants[-1]
    _, v = verify(*_signed(last_key[0], _inbound()))
    assert v.may_act_for(last_tenant) and len(builds) <= 1      # one parse, not one client per key
    builds.clear()
    monkeypatch.setattr(telnyx_signing, "_last_miss_reload", time.monotonic())
    for _ in range(20):
        with pytest.raises(ValueError):
            verify(*_signed(STRANGER[0], _inbound()))
    assert builds == []                                          # forgeries never reach the SDK


def test_an_unreadable_credential_defers_rather_than_refuses(monkeypatch, webhook):
    """Re-review: refusing for an unreadable credential would lose the message
    for good (403 is never retried successfully); it is retried instead."""
    async def creds():
        return [(TA, None)]

    monkeypatch.setattr(telnyx_signing, "_tenant_credentials", creds)
    telnyx_signing.reset_cache()
    response = post(webhook, *_signed(PLATFORM[0], _inbound()))
    assert response.status_code == 503 and response.headers.get("retry-after") == "60"
    assert webhook["recorded"] == []
    # ...while a STOP is still recorded at once
    assert post(webhook, *_signed(PLATFORM[0], _inbound(text="STOP"))).status_code == 204
    assert TA in webhook["suppressed"]


def test_the_registry_is_built_once_per_load(registry, monkeypatch):
    builds = []
    real = telnyx_signing._build

    def counting(credentials):
        builds.append(1)
        return real(credentials)

    monkeypatch.setattr(telnyx_signing, "_build", counting)
    for _ in range(5):
        verify(*_signed(KEY_A[0], _inbound()))
    assert len(builds) == 1
    monkeypatch.setenv("TELNYX_PUBLIC_KEY_PREVIOUS", KEY_B[1])     # a platform key change rebuilds
    verify(*_signed(KEY_A[0], _inbound()))
    assert len(builds) == 2
