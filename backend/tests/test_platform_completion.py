"""Regression coverage for the final cross-cutting platform completion work."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
import urllib.error
import urllib.parse
from pathlib import Path

import pytest
from pydantic import ValidationError

import config
import ws_hub
from auth import _issue_jwt
from commands_api import (
    GoogleOAuthStart,
    _exchange_google_oauth_code,
    _oauth_return_url,
    _pkce_challenge,
    _refresh_google_oauth_token,
)
from data_integrations.cache import (
    IntegrationCache,
    IntegrationCacheUnavailable,
    canonical_request_hash,
    reset_shared_cache,
)
from harvesters import base as harvester_base
from harvesters.base import RateLimiter, SocrataHarvester
from harvesters.property_adapter import PropertyRecord
from intelligence_engine import forecast_micro_market
from tenancy import Role


class _FakeWebSocket:
    def __init__(self, *, protocols: str = "", query: dict[str, str] | None = None):
        self.headers = {"sec-websocket-protocol": protocols}
        self.query_params = query or {}
        self.frames: list[dict] = []

    async def send_text(self, payload: str) -> None:
        self.frames.append(json.loads(payload))


def test_main_websocket_requires_claim_derived_identity_in_production(monkeypatch):
    # A separate demo server exists at the workspace root. Isolate this import
    # so a module cached by another test cannot mask backend/server.py.
    backend_root = str(Path(__file__).resolve().parents[1])
    monkeypatch.syspath_prepend(backend_root)
    previously_loaded_server = sys.modules.pop("server", None)
    try:
        backend_server = importlib.import_module("server")
    finally:
        sys.modules.pop("server", None)
        if previously_loaded_server is not None:
            sys.modules["server"] = previously_loaded_server

    _resolve_websocket_identity = backend_server._resolve_websocket_identity

    monkeypatch.setattr(config, "IS_DEV", False)
    assert _resolve_websocket_identity(_FakeWebSocket()) is None
    assert _resolve_websocket_identity(_FakeWebSocket(protocols="oracle.jwt, invalid")) is None
    query_token = _issue_jwt(
        "agent-a",
        "11111111-1111-1111-1111-111111111111",
        Role.AGENT.value,
    )
    assert _resolve_websocket_identity(
        _FakeWebSocket(query={"token": query_token})
    ) is None

    token = _issue_jwt(
        "agent-a",
        "11111111-1111-1111-1111-111111111111",
        Role.AGENT.value,
    )
    resolved = _resolve_websocket_identity(
        _FakeWebSocket(
            protocols=f"oracle.jwt, {token}",
            query={"tenant_id": "22222222-2222-2222-2222-222222222222"},
        )
    )
    assert resolved is not None
    ctx, used_subprotocol = resolved
    assert used_subprotocol is True
    assert ctx.agent_id == "agent-a"
    assert ctx.tenant_id == "11111111-1111-1111-1111-111111111111"

    non_admin_firehose = _issue_jwt(
        "agent-a", ws_hub.FIREHOSE_TENANT_ID, Role.AGENT.value
    )
    assert (
        _resolve_websocket_identity(
            _FakeWebSocket(protocols=f"oracle.jwt, {non_admin_firehose}")
        )
        is None
    )


def test_websocket_hub_is_tenant_scoped_and_mirrors_remote_frames_to_admin():
    async def exercise() -> None:
        tenant_id = "11111111-1111-1111-1111-111111111111"
        tenant_socket = _FakeWebSocket()
        admin_socket = _FakeWebSocket()
        ws_hub._sockets.clear()
        ws_hub._pool = None
        ws_hub._listener_started = False
        ws_hub.register(tenant_id, tenant_socket)
        ws_hub.register(ws_hub.FIREHOSE_TENANT_ID, admin_socket)
        await ws_hub.broadcast(tenant_id, {"type": "JOB_PROGRESS", "progress": 20})
        assert tenant_socket.frames[-1]["progress"] == 20
        assert admin_socket.frames[-1]["source_tenant"] == tenant_id

        await ws_hub._receive_notification(
            json.dumps(
                {
                    "origin": "another-ecs-replica",
                    "tenant_id": tenant_id,
                    "payload": {"type": "NEGOTIATION_TELEMETRY", "threshold": "green"},
                }
            )
        )
        assert tenant_socket.frames[-1]["threshold"] == "green"
        assert admin_socket.frames[-1]["source_tenant"] == tenant_id
        ws_hub._sockets.clear()

    asyncio.run(exercise())


class _BrokenAcquire:
    async def __aenter__(self):
        raise OSError("postgres unavailable")

    async def __aexit__(self, *_args):
        return False


class _BrokenPool:
    def acquire(self):
        return _BrokenAcquire()


class _MemoryCache(IntegrationCache):
    def __init__(self):
        super().__init__(object())
        self.values: dict[str, dict] = {}

    async def get(self, key: str):
        value = self.values.get(key)
        self._metrics["hits" if value is not None else "misses"] += 1
        return value

    async def get_stale(self, _key: str):
        return None

    async def set(self, key: str, value: dict, *_args, **_kwargs):
        self.values[key] = value
        self._metrics["writes"] += 1


def test_mandatory_cache_fails_closed_and_deduplicates_concurrent_requests():
    async def exercise() -> None:
        upstream_calls = 0
        broken = IntegrationCache(_BrokenPool())

        async def should_not_run():
            nonlocal upstream_calls
            upstream_calls += 1
            return {"bad": True}

        with pytest.raises(IntegrationCacheUnavailable):
            await broken.get_or_fetch("mls", {"listing": "1"}, should_not_run)
        assert upstream_calls == 0

        reset_shared_cache()
        cache = _MemoryCache()

        async def fetch_once():
            nonlocal upstream_calls
            upstream_calls += 1
            await asyncio.sleep(0)
            return {"listing": "source-backed"}

        values = await asyncio.gather(
            *(
                cache.get_or_fetch("mls", {"listing": "same"}, fetch_once)
                for _ in range(6)
            )
        )
        assert upstream_calls == 1
        assert values == [{"listing": "source-backed"}] * 6
        assert cache.metrics()["deduplicated"] >= 1

    asyncio.run(exercise())


class _FixtureCache:
    def __init__(self):
        self.values: dict[str, dict] = {}
        self._metrics = {"hits": 0, "misses": 0}

    def metrics(self):
        return dict(self._metrics)

    async def get_or_fetch(self, source, request, fetcher, **_kwargs):
        key = canonical_request_hash(source, request)
        if key in self.values:
            self._metrics["hits"] += 1
            return self.values[key]
        self._metrics["misses"] += 1
        self.values[key] = await fetcher()
        return self.values[key]


class _FixtureSocrata(SocrataHarvester):
    STATE = "ZZ"
    SOURCE_LABEL = "Municipal fixture"
    SOURCE_KEY = "municipal_violation_fixture"
    RESOURCE_URL = "https://municipal.example/records.json"

    def map_record(self, row: dict):
        parcel = str(row.get("parcel_id") or row.get("parcel") or "")
        address = str(row.get("address") or row.get("site_address") or "")
        if not parcel or not address:
            return None
        return PropertyRecord(
            parcel_id=parcel,
            address=address,
            city="Fixture",
            state=self.STATE,
            zip_code="00000",
            owner_name="Public record owner",
            owner_type="individual",
            estimated_value=100_000,
            equity_percent=0,
            is_absentee_owner=False,
            distress_flags=["open_violation"],
            last_sale_date=None,
        )


def test_municipal_fixture_replay_covers_checkpoint_schema_drift_retry_and_cache(
    monkeypatch,
):
    dataset = [
        {"parcel_id": "P-1", "address": "1 Main St"},
        {"parcel": "P-2", "site_address": "2 Main St", "new_field": "schema drift"},
        {"address": "missing parcel"},
        {"parcel_id": "P-4", "address": "4 Main St"},
    ]
    url_calls: list[str] = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def urlopen(request, timeout):
        del timeout
        url_calls.append(request.full_url)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        offset = int(query["$offset"][0])
        limit = int(query["$limit"][0])
        return Response(dataset[offset : offset + limit])

    monkeypatch.setattr(harvester_base.urllib.request, "urlopen", urlopen)
    async def inline_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(harvester_base.asyncio, "to_thread", inline_thread)
    monkeypatch.setattr(harvester_base, "BASE_BACKOFF", 0)
    monkeypatch.setattr(harvester_base, "MAX_BACKOFF", 0)
    monkeypatch.setattr(harvester_base, "REQUEST_JITTER", 0)
    cache = _FixtureCache()

    async def exercise():
        first = _FixtureSocrata(
            "11111111-1111-1111-1111-111111111111",
            cache=cache,
            limiter=RateLimiter(0, 0),
        )
        first_result = await first.harvest(max_records=2, persist=False)
        assert first_result["parsed"] == 2
        assert first_result["checkpoint"] == 2
        assert first_result["checkpoint_complete"] is False

        duplicate = _FixtureSocrata(
            "11111111-1111-1111-1111-111111111111",
            cache=cache,
            limiter=RateLimiter(0, 0),
        )
        duplicate_result = await duplicate.harvest(max_records=2, persist=False)
        assert duplicate_result["cache_hits"] == 1
        assert len(url_calls) == 1

        resumed = _FixtureSocrata(
            "11111111-1111-1111-1111-111111111111",
            cache=cache,
            limiter=RateLimiter(0, 0),
        )
        resumed_result = await resumed.harvest(
            max_records=10, persist=False, checkpoint=first_result["checkpoint"]
        )
        assert resumed_result["parsed"] == 1
        assert resumed_result["skipped"] == 1
        assert resumed_result["checkpoint"] is None
        assert resumed_result["checkpoint_complete"] is True

        attempts = 0

        def flaky_urlopen(request, timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 429, "throttled", {"Retry-After": "0"}, None
                )
            return urlopen(request, timeout)

        monkeypatch.setattr(harvester_base.urllib.request, "urlopen", flaky_urlopen)
        retried = _FixtureSocrata(
            "11111111-1111-1111-1111-111111111111",
            cache=_FixtureCache(),
            limiter=RateLimiter(0, 0),
        )
        retry_result = await retried.harvest(max_records=1, persist=False)
        assert retry_result["retries"] == 1
        assert attempts == 2

    asyncio.run(exercise())


def test_forecast_requires_and_reports_all_seven_public_market_indicators():
    indicators = [
        "permits",
        "crime_aggregate",
        "flood_exposure",
        "census_trends",
        "sales",
        "inventory",
        "commercial_activity",
    ]
    observations = [
        {"indicator": indicator, "year": year, "value": 100 + index * 3 + year - 2022}
        for index, indicator in enumerate(indicators)
        for year in (2022, 2023, 2024)
    ]
    result = forecast_micro_market(observations, horizon_years=5)
    assert result["source_coverage"]["coverage"] == 1.0
    assert result["source_coverage"]["missing_indicators"] == []
    assert set(result["indicator_trends"]) == set(indicators)
    assert len(result["forecast"]) == 5


class _FakeOAuthResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, **_kwargs):
        return self.payload


class _FakeOAuthSession:
    responses: list[_FakeOAuthResponse] = []
    posts: list[dict] = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, _url, *, data):
        self.posts.append(dict(data))
        return self.responses.pop(0)


def test_google_oauth_pkce_exchange_refresh_and_local_return_paths(monkeypatch):
    import commands_api

    challenge = _pkce_challenge("a" * 64)
    assert len(challenge) == 43
    assert "=" not in challenge
    assert GoogleOAuthStart(return_path="/profile?tab=brokerage").return_path.startswith("/")
    with pytest.raises(ValidationError):
        GoogleOAuthStart(return_path="https://attacker.example/callback")

    monkeypatch.setenv("ORACLE_BASE_URL", "https://app.neoh.example")
    assert _oauth_return_url("/profile?tab=brokerage", "connected") == (
        "https://app.neoh.example/profile?tab=brokerage&google=connected"
    )
    _FakeOAuthSession.posts = []
    _FakeOAuthSession.responses = [
        _FakeOAuthResponse(200, {"access_token": "access", "refresh_token": "refresh"}),
        _FakeOAuthResponse(200, {"access_token": "renewed", "expires_in": 3600}),
    ]
    monkeypatch.setattr(commands_api.aiohttp, "ClientSession", _FakeOAuthSession)

    async def exercise():
        exchanged = await _exchange_google_oauth_code(
            code="code",
            code_verifier="verifier",
            client_id="client",
            client_secret="secret",
            redirect_uri="https://api.neoh.example/callback",
        )
        refreshed = await _refresh_google_oauth_token(
            refresh_token="refresh", client_id="client", client_secret="secret"
        )
        assert exchanged["access_token"] == "access"
        assert refreshed["access_token"] == "renewed"

    asyncio.run(exercise())
    assert _FakeOAuthSession.posts[0]["code_verifier"] == "verifier"
    assert _FakeOAuthSession.posts[1]["grant_type"] == "refresh_token"


def _install_fake_vllm(monkeypatch):
    class SamplingParams:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class LoRARequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Tokenizer:
        def apply_chat_template(self, messages, **_kwargs):
            return json.dumps(messages)

    class LLM:
        def __init__(self, **_kwargs):
            pass

        def get_tokenizer(self):
            return Tokenizer()

        def generate(self, prompts, _sampling, *, lora_request):
            del lora_request
            raw = json.dumps(
                {
                    "arv_estimate": 200000,
                    "rehab_estimate": 20000,
                    "mao_formula": "0.70 * ARV - rehab",
                    "mao": 120000,
                    "verdict": "Proceed",
                    "rationale": "Source-backed canary passed.",
                }
            )
            return [types.SimpleNamespace(outputs=[types.SimpleNamespace(text=raw)]) for _ in prompts]

    class GuidedDecodingParams:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    vllm = types.ModuleType("vllm")
    vllm.LLM = LLM
    vllm.SamplingParams = SamplingParams
    lora = types.ModuleType("vllm.lora")
    request = types.ModuleType("vllm.lora.request")
    request.LoRARequest = LoRARequest
    sampling = types.ModuleType("vllm.sampling_params")
    sampling.GuidedDecodingParams = GuidedDecodingParams
    monkeypatch.setitem(sys.modules, "vllm", vllm)
    monkeypatch.setitem(sys.modules, "vllm.lora", lora)
    monkeypatch.setitem(sys.modules, "vllm.lora.request", request)
    monkeypatch.setitem(sys.modules, "vllm.sampling_params", sampling)


def test_local_vllm_hot_swaps_validated_state_and_agent_scopes(monkeypatch, tmp_path: Path):
    _install_fake_vllm(monkeypatch)
    from ml_forge.edge_forge.local_vllm_adapter import BASE_MODEL, EdgeUnderwriter

    state_path = tmp_path / "DE"
    agent_path = tmp_path / "agents" / "agent-a"
    for path, version in ((state_path, "de-v1"), (agent_path, "agent-a-v2")):
        path.mkdir(parents=True)
        (path / "adapter_config.json").write_text(
            json.dumps(
                {"base_model_name_or_path": BASE_MODEL, "r": 8, "model_version": version}
            ),
            encoding="utf-8",
        )

    underwriter = EdgeUnderwriter(adapter_root=str(tmp_path), gpu_memory_utilization=0.8)
    state_result = underwriter.hot_swap_state_lora("de").underwrite_batch([{"parcel": "1"}])[0]
    assert state_result["adapter_scope"] == "state"
    assert state_result["state"] == "DE"
    agent_result = underwriter.hot_swap_agent_lora("agent-a").underwrite_batch([{"parcel": "1"}])[0]
    assert agent_result["adapter_scope"] == "agent"
    assert agent_result["agent_id"] == "agent-a"
    canary = underwriter.canary_evaluate_agent(
        "agent-a", [{"case_id": "fixed-1", "input": {}, "expected_verdict": "Proceed"}]
    )
    assert canary["passed"] is True
    assert canary["model_version"] == "agent-a-v2"
    assert underwriter.telemetry()["active_adapter_scope"] == "agent"

    with pytest.raises(ValueError):
        EdgeUnderwriter(adapter_root=str(tmp_path), gpu_memory_utilization=0.99)
