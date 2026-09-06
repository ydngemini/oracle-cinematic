"""Structured output must refuse rather than downgrade.

litellm runs with drop_params=True. That is right for optional niceties and
wrong for `response_format`: a provider that does not support it has the
parameter removed and answers with prose. Nothing raises. The caller — a
planner deciding what to do to real people — then mis-parses a paragraph.

So a request for structured output narrows the provider list instead of
falling through it, and raises when nothing is left.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

import llm_gateway as gateway


class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Response:
    """`_text_of` reads attributes, not dict keys — a dict returns "" and the
    gateway correctly treats that as a provider that answered with silence."""

    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Recorder:
    """Stands in for litellm, recording what each call was handed."""

    def __init__(self, text="{}"):
        self.calls: list[dict] = []
        self._text = text

    async def acompletion(self, **kwargs):
        self.calls.append(kwargs)
        return _Response(self._text)


def _provider(name, *, structured):
    return gateway.Provider(
        name=name, model=f"{name}/m", supports_response_format=structured,
    )


SCHEMA = {"type": "json_schema", "json_schema": {"name": "plan", "schema": {}}}


def test_drop_params_is_still_on_which_is_why_this_matters():
    """If this ever becomes False the silent-downgrade risk changes shape, and
    whoever changes it should read this test rather than discover it."""
    assert "drop_params = True" in inspect.getsource(gateway._litellm)


def test_a_provider_that_cannot_honour_the_format_is_skipped(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(gateway, "_litellm", lambda: recorder)
    monkeypatch.setattr(
        gateway, "providers_for",
        lambda _task: [_provider("bedrock", structured=False),
                       _provider("fireworks", structured=True)],
    )
    asyncio.run(gateway.complete("plan it", response_format=SCHEMA))

    assert len(recorder.calls) == 1, "the incapable provider was tried anyway"
    assert recorder.calls[0]["model"] == "fireworks/m"
    assert recorder.calls[0]["response_format"] == SCHEMA


def test_it_refuses_when_no_provider_can_honour_the_format(monkeypatch):
    """Refusing is the point. Prose that parses as a plan is the failure."""
    recorder = _Recorder()
    monkeypatch.setattr(gateway, "_litellm", lambda: recorder)
    monkeypatch.setattr(
        gateway, "providers_for",
        lambda _task: [_provider("bedrock", structured=False),
                       _provider("local", structured=False)],
    )
    with pytest.raises(gateway.LLMUnavailable) as caught:
        asyncio.run(gateway.complete("plan it", response_format=SCHEMA))

    assert recorder.calls == [], "a request was sent that could not be honoured"
    message = str(caught.value)
    assert "bedrock" in message and "local" in message, "name what was ruled out"
    assert "response_format" in message


def test_an_ordinary_call_still_falls_through_everything(monkeypatch):
    """Without response_format the old behaviour is unchanged: the worst case
    of a fallback is a duplicate paragraph, which is worth having."""
    recorder = _Recorder(text="prose")
    monkeypatch.setattr(gateway, "_litellm", lambda: recorder)
    monkeypatch.setattr(
        gateway, "providers_for",
        lambda _task: [_provider("bedrock", structured=False)],
    )
    assert asyncio.run(gateway.complete("hello")) == "prose"
    assert "response_format" not in recorder.calls[0], (
        "an unset format must not reach the provider as None"
    )


def test_the_capable_tiers_are_declared_not_assumed():
    """Each provider states its answer, so a new tier defaults to refusing
    rather than to silently downgrading."""
    assert gateway.Provider("x", "y").supports_response_format is False
    source = inspect.getsource(gateway)
    fireworks = source.split("def _fireworks(")[1].split("def _bedrock(")[0]
    assert "supports_response_format=True" in fireworks
    bedrock = source.split("def _bedrock(")[1].split("def _foundry(")[0]
    assert "supports_response_format=True" not in bedrock
    local = source.split("def _local(")[1].split("def providers_for(")[0]
    assert "supports_response_format=True" not in local
