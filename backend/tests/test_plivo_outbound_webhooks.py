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
