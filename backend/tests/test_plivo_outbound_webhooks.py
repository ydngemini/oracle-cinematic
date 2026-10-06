"""The outbound Plivo webhooks: reachable, and authenticated.

Two defects, both from commit 899f604, found while building the release smoke
test's callback check:

1. **Unreachable.** The handlers live under /api/telephony, but the URLs Neoh
   handed Plivo when placing a call were built as /api/commands/…, where no
   route exists. Every AI-placed outbound call through the primary voice
   carrier asked Plivo to fetch its instructions from a 404.

2. **Unauthenticated.** Neither handler validated Plivo's V3 signature. The
   answer route returned the media-stream URL AND bridge token for any posted
   CallUUID with live state; the status route ran call cleanup for any posted
   UUID. The four INBOUND Plivo routes always validated.
"""

from __future__ import annotations

import pathlib
import re

import plivo.utils as plivo_utils
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import plivo_call_handler
import telephony_api

BACKEND = pathlib.Path(__file__).resolve().parent.parent
BASE = "https://neoh.example"
PLATFORM_TOKEN = "platform-token-" + "p" * 20
TENANT_TOKEN = "tenant-token-" + "t" * 22
UUID = "11111111-2222-3333-4444-555555555555"
TENANT = "aaaaaaaa-0000-4000-8000-00000000000a"


# ── 1. Every callback URL we hand a provider is a real route ───────────────

def _route_regexes():
    import server

    out = []
    for r in server.app.routes:
        path = getattr(r, "path", None)
        if not path:
            continue
        rx = "^" + re.sub(r"\\{[^}]+\\}", "[^/]+", re.escape(path)) + "$"
        out.append((re.compile(rx), set(getattr(r, "methods", None) or [])))
    return out


def _resolves(path: str, method: str = "POST") -> bool:
    return any(rx.match(path) and method in methods for rx, methods in _route_regexes())


def test_the_outbound_plivo_urls_resolve_to_mounted_routes():
    assert _resolves(telephony_api.PLIVO_OUTBOUND_ANSWER_PATH)
    assert _resolves(telephony_api.PLIVO_OUTBOUND_STATUS_PATH)


def test_commands_api_imports_the_paths_instead_of_retyping_them():
    """The defect was two hand-typed strings that disagreed. The URL now comes
    from the module that owns the route."""
    src = (BACKEND / "commands_api.py").read_text(encoding="utf-8")
    assert "PLIVO_OUTBOUND_ANSWER_PATH" in src and "PLIVO_OUTBOUND_STATUS_PATH" in src
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert '/webhooks/plivo"' not in code and "/webhooks/plivo/status\"" not in code


@pytest.mark.parametrize("module", ["commands_api.py", "telephony_api.py"])
def test_every_literal_callback_url_resolves(module):
    """The general form: any `f"{base}/api/…"` URL a module builds for a
    provider must name a mounted route. Catches the next re-typed path, not
    just this one."""
    src = (BACKEND / module).read_text(encoding="utf-8")
    # Join implicitly concatenated literals ("…/inbound/"\n  f"{key}/transfer")
    # so a URL split across lines is checked whole, not truncated.
    src = re.sub(r'"\s*\n\s*f?"', "", src)
    paths = set(re.findall(r'f"\{base\}(/api/[^"]+)"', src))
    unresolved = []
    for p in paths:
        concrete = re.sub(r"\{[^}]+\}", "x", p)   # an interpolated segment
        concrete = concrete.split("?")[0]
        if not (_resolves(concrete, "POST") or _resolves(concrete, "GET")):
            unresolved.append(p)
    assert not unresolved, f"{module} hands providers URLs with no route: {unresolved}"


# ── 2. Authentication ──────────────────────────────────────────────────────

def _sign(path: str, params: dict[str, str], token: str, nonce: str = "nonce-1") -> dict:
    url = f"{BASE}{path}"
    sig = plivo_utils.signature_v3.get_signature_v3(
        token,
        plivo_utils.signature_v3.construct_post_url(url, dict(params)).decode("utf-8"),
        nonce,
    ).decode("utf-8")
    return {"X-Plivo-Signature-V3": sig, "X-Plivo-Signature-V3-Nonce": nonce}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("PLIVO_AUTH_TOKEN", PLATFORM_TOKEN)
    monkeypatch.delenv("PLIVO_AUTH_TOKEN_PREVIOUS", raising=False)

    state = {"value": {"tenant_id": TENANT, "account_id": "MAPLATFORM",
                        "direction": "outbound", "qwen_realtime_enabled": False}}
    cleaned: list[str] = []

    async def fake_load(uuid, wait_for_initialization=False):
        return state["value"]

    async def fake_cleanup(uuid):
        cleaned.append(uuid)

    async def fake_creds(ctx):
        return {"auth_id": "MATENANT", "auth_token": TENANT_TOKEN}

    monkeypatch.setattr(plivo_call_handler, "load_plivo_call_state", fake_load)
    monkeypatch.setattr(plivo_call_handler, "cleanup_plivo_call", fake_cleanup)
    monkeypatch.setattr(plivo_call_handler, "plivo_qwen_enabled", lambda s=None: False)
    monkeypatch.setattr(telephony_api, "_plivo_credentials", fake_creds)

    app = FastAPI()
    app.include_router(telephony_api.router)
    c = TestClient(app)
    c.state_box, c.cleaned = state, cleaned
    return c


ANSWER = telephony_api.PLIVO_OUTBOUND_ANSWER_PATH
STATUS = telephony_api.PLIVO_OUTBOUND_STATUS_PATH


def test_answer_refuses_an_unsigned_request_for_a_live_call(client):
    """It used to hand out the stream URL and bridge token here."""
    r = client.post(ANSWER, data={"CallUUID": UUID})
    assert r.status_code == 400
    assert "stream" not in r.text.lower() and "token" not in r.text.lower()


def test_answer_accepts_a_correctly_signed_request(client):
    params = {"CallUUID": UUID}
    r = client.post(ANSWER, data=params, headers=_sign(ANSWER, params, PLATFORM_TOKEN))
    # Past validation; realtime is off in this fixture, so it declines politely.
    assert r.status_code == 200 and "unavailable" in r.text


def test_answer_for_an_unknown_call_hangs_up_without_detail(client):
    client.state_box["value"] = None
    r = client.post(ANSWER, data={"CallUUID": UUID})
    assert r.status_code == 200 and "cannot be connected safely" in r.text


def test_status_refuses_an_unsigned_request_and_does_not_clean_up(client):
    """It used to tear down any live call whose UUID was posted to it."""
    r = client.post(STATUS, data={"CallUUID": UUID, "CallStatus": "completed"})
    assert r.status_code == 400
    assert client.cleaned == []


def test_status_cleans_up_when_signed(client):
    params = {"CallUUID": UUID, "CallStatus": "completed"}
    r = client.post(STATUS, data=params, headers=_sign(STATUS, params, PLATFORM_TOKEN))
    assert r.status_code == 204 and client.cleaned == [UUID]


REQUEST = "99999999-8888-7777-6666-555555555555"


def test_an_outbound_call_is_found_by_its_request_id_and_aliased(monkeypatch):
    """Placing a call returns the REQUEST id and the worker stores state under
    it; Plivo's answer callback names the CALL id and sends RequestUUID too.
    The first real staging call was refused as unmanaged because only CallUUID
    was looked up (2026-10-04)."""
    import asyncio

    store = {REQUEST: {"tenant_id": TENANT, "direction": "outbound"}}
    saved = {}

    async def fake_load(uuid, wait_for_initialization=False):
        return store.get(uuid)

    async def fake_save(uuid, state):
        saved[uuid] = state

    monkeypatch.setattr(plivo_call_handler, "load_plivo_call_state", fake_load)
    monkeypatch.setattr(plivo_call_handler, "_save_call_state", fake_save)
    state = asyncio.run(plivo_call_handler.resolve_outbound_plivo_call_state(UUID, REQUEST))
    assert state["tenant_id"] == TENANT and state["request_uuid"] == REQUEST
    # Resolving runs before the signature check, so it never writes.
    assert saved == {}
    asyncio.run(plivo_call_handler.bind_outbound_plivo_call_id(UUID, state))
    assert saved[UUID]["request_uuid"] == REQUEST  # later lookups by CallUUID hit directly
    assert asyncio.run(plivo_call_handler.resolve_outbound_plivo_call_state(UUID, "")) is None


def test_an_unsigned_answer_never_binds_a_call_id(client, monkeypatch):
    """A forged answer naming a real request id must not alias that call's
    state under an attacker-chosen CallUUID."""
    bound = []

    async def by_request(uuid, request_uuid="", wait_for_initialization=False):
        return {**client.state_box["value"], "request_uuid": REQUEST}

    async def fake_bind(uuid, state):
        bound.append(uuid)

    monkeypatch.setattr(plivo_call_handler, "resolve_outbound_plivo_call_state", by_request)
    monkeypatch.setattr(plivo_call_handler, "bind_outbound_plivo_call_id", fake_bind)
    r = client.post(ANSWER, data={"CallUUID": UUID, "RequestUUID": REQUEST})
    assert r.status_code == 400
    assert bound == []


def test_answer_accepts_a_call_known_only_by_its_request_id(client, monkeypatch):
    async def by_request(uuid, request_uuid="", wait_for_initialization=False):
        return client.state_box["value"] if request_uuid == REQUEST else None

    monkeypatch.setattr(plivo_call_handler, "resolve_outbound_plivo_call_state", by_request)
    params = {"CallUUID": UUID, "RequestUUID": REQUEST}
    r = client.post(ANSWER, data=params, headers=_sign(ANSWER, params, PLATFORM_TOKEN))
    assert r.status_code == 200 and "cannot be connected safely" not in r.text
    # With realtime off, the answered call still opens with the AI disclosure.
    assert "automated AI assistant" in r.text


@pytest.mark.parametrize("response, expected", [
    (type("R", (), {"request_uuid": "req-1"})(), "req-1"),
    ({"request_uuid": "req-2"}, "req-2"),
    ({"request_uuid": ["req-3"]}, "req-3"),
    ({"call_uuid": "call-4"}, "call-4"),
    ({"api_id": "x", "message": "call fired"}, ""),
    (None, ""),
])
def test_the_call_id_is_read_from_every_response_shape(response, expected):
    from voice_provider import plivo_identifier

    assert plivo_identifier(response, "request_uuid", "call_uuid") == expected


class _LiveCalls:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def list_ids(self, **kwargs):
        self.calls.append(kwargs)
        return self.answers.pop(0) if self.answers else {"calls": []}


def test_a_placed_call_with_no_returned_id_is_found_among_live_calls():
    from types import SimpleNamespace

    from voice_provider import _find_placed_call

    live = _LiveCalls([{"calls": []}, {"calls": ["call-xyz"]}])
    client = SimpleNamespace(live_calls=live)
    assert _find_placed_call(client, "+13025550100", pause_seconds=0) == "call-xyz"
    assert live.calls[0] == {"call_direction": "outbound", "to_number": "13025550100"}


def test_two_live_calls_to_the_number_are_never_guessed_between():
    from types import SimpleNamespace

    from voice_provider import _find_placed_call

    client = SimpleNamespace(live_calls=_LiveCalls([{"calls": ["a", "b"]}]))
    assert _find_placed_call(client, "+13025550100", pause_seconds=0) == ""
    client = SimpleNamespace(live_calls=_LiveCalls([]))
    assert _find_placed_call(client, "+13025550100", attempts=2, pause_seconds=0) == ""


def test_the_tenant_token_counts_only_for_the_account_that_placed_the_call(client):
    """Same rule the inbound routes apply: resolve broadly, confirm with the
    secret of the account that actually owns the call."""
    params = {"CallUUID": UUID, "CallStatus": "completed"}
    # Placed by the PLATFORM account; the tenant's token must not verify it.
    r = client.post(STATUS, data=params, headers=_sign(STATUS, params, TENANT_TOKEN))
    assert r.status_code == 400 and client.cleaned == []
    # Placed by the TENANT's account; now its token does.
    client.state_box["value"] = {**client.state_box["value"], "account_id": "MATENANT"}
    r = client.post(STATUS, data=params, headers=_sign(STATUS, params, TENANT_TOKEN))
    assert r.status_code == 204 and client.cleaned == [UUID]


def test_status_for_an_unknown_call_does_nothing(client):
    client.state_box["value"] = None
    r = client.post(STATUS, data={"CallUUID": UUID, "CallStatus": "completed"})
    assert r.status_code == 204 and client.cleaned == []
