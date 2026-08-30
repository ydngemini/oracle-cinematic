"""Inference spend is recorded, on a plan that does not charge for it yet.

Requests were already capped — 20/min/agent in rate_limiter — but SPEND was not,
and nothing recorded it. On a flat $299 plan the first sign of a runaway
conversation would have been the provider invoice.

billing_usage records consumption regardless of pricing model, precisely so the
history exists before a pricing decision. This adds the two metrics that were
missing: prompt and completion tokens, kept separate because every provider
prices them differently and summing them loses the ratio that says whether the
prompt or the answer is the expensive half.
"""

from __future__ import annotations

import asyncio

import pytest

import billing_usage
import llm_gateway
from tenancy import Role, TenantContext

CTX = TenantContext(
    agent_id="agent@tenant.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)


class _Usage:
    def __init__(self, **fields):
        for k, v in fields.items():
            setattr(self, k, v)


class _Response:
    def __init__(self, usage=None):
        if usage is not None:
            self.usage = usage


# ── shape tolerance ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "response,expected",
    [
        # Chat Completions
        (_Response(_Usage(prompt_tokens=100, completion_tokens=40)), (100, 40)),
        # Responses API — the primary chat path speaks this one
        (_Response(_Usage(input_tokens=80, output_tokens=25)), (80, 25)),
        # A dict, as a local llama server may return
        ({"usage": {"prompt_tokens": 5, "completion_tokens": 2}}, (5, 2)),
        # No usage reported at all
        (_Response(), (0, 0)),
        ({}, (0, 0)),
        # Present but junk — a string where a count belongs
        (_Response(_Usage(prompt_tokens="lots")), (0, 0)),
        # Negative is not a measurement
        (_Response(_Usage(prompt_tokens=-5, completion_tokens=3)), (0, 3)),
    ],
)
def test_usage_is_read_from_every_shape(response, expected):
    """Three call paths reach three APIs and none agrees on the field names.

    A caller that guessed one shape would silently meter zero against the
    others — worse than not metering, because it would look measured.
    """
    assert llm_gateway.token_usage_of(response) == expected


# ── recording ────────────────────────────────────────────────────────────────

def _capture(monkeypatch):
    recorded = []

    async def _record(ctx, *, metric, quantity, idempotency_key, **_kw):
        recorded.append((metric, quantity, idempotency_key))
        return True

    monkeypatch.setattr(billing_usage, "record_usage", _record)
    return recorded


def test_prompt_and_completion_are_recorded_separately(monkeypatch):
    recorded = _capture(monkeypatch)

    result = asyncio.run(
        billing_usage.record_inference(
            CTX,
            _Response(_Usage(input_tokens=1200, output_tokens=300)),
            idempotency_key="chat:abc:foundry:0",
        )
    )

    assert result == (1200, 300)
    assert recorded == [
        ("ai_prompt_tokens", 1200, "chat:abc:foundry:0:prompt"),
        ("ai_completion_tokens", 300, "chat:abc:foundry:0:completion"),
    ]


def test_both_metrics_are_declared(monkeypatch):
    """record_usage refuses a metric outside METRICS, so an undeclared one
    would be silently dropped at runtime rather than caught here."""
    assert "ai_prompt_tokens" in billing_usage.METRICS
    assert "ai_completion_tokens" in billing_usage.METRICS


def test_a_call_reporting_no_usage_records_nothing(monkeypatch):
    """Zero is a real measurement — "this call cost nothing". A provider that
    omits the field has not made that measurement, and writing a zero would
    claim it did."""
    recorded = _capture(monkeypatch)

    assert asyncio.run(
        billing_usage.record_inference(CTX, _Response(), idempotency_key="k")
    ) == (0, 0)
    assert recorded == []


def test_the_idempotency_keys_differ_per_round(monkeypatch):
    """A turn calls the model once per tool round. Sharing a key would collapse
    every round into one row and under-report the expensive half."""
    recorded = _capture(monkeypatch)

    for round_index in range(3):
        asyncio.run(
            billing_usage.record_inference(
                CTX,
                _Response(_Usage(input_tokens=10, output_tokens=1)),
                idempotency_key=f"chat:abc:foundry:{round_index}",
            )
        )

    keys = [k for _, _, k in recorded]
    assert len(set(keys)) == len(keys), f"rounds collapsed onto one key: {keys}"


def test_every_foundry_model_call_is_metered():
    """A path that forgets to meter is invisible, and invisible spend on a flat
    plan is the whole problem. Pinned structurally rather than by hoping."""
    import ast
    import inspect

    import ai_chat_agent

    tree = ast.parse(inspect.getsource(ai_chat_agent))
    target = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_foundry_generate"
    )
    calls = [
        ast.unparse(n.func)
        for n in ast.walk(target)
        if isinstance(n, ast.Call)
    ]
    model_calls = sum(1 for c in calls if "asyncio.to_thread" in c)
    metered = sum(1 for c in calls if "record_inference" in c)

    assert metered == model_calls, (
        f"{model_calls} model call(s) in _foundry_generate but {metered} metered"
    )
