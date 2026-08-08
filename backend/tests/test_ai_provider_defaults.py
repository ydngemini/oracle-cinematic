"""Which inference plane an unconfigured deployment talks to.

ai_chat_agent used to default ORACLE_AI_CHAT_PROVIDER to "bedrock", so any
environment that forgot the variable routed prompts to AWS. It also kept Bedrock
as an unconditional middle tier between Foundry and the local model, which on an
Azure-only deployment is a guaranteed round-trip to a cloud with no credentials.
"""

from __future__ import annotations

import importlib

import pytest

import ai_chat_agent


def _reload(monkeypatch, **env):
    for key in ("ORACLE_AI_CHAT_PROVIDER", "ORACLE_AI_BEDROCK_FALLBACK"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(ai_chat_agent)


@pytest.fixture(autouse=True)
def _restore_module():
    yield
    importlib.reload(ai_chat_agent)


def test_defaults_to_azure_foundry(monkeypatch):
    assert _reload(monkeypatch).AI_PROVIDER == "azure-foundry"


def test_bedrock_fallback_is_off_by_default(monkeypatch):
    assert _reload(monkeypatch).BEDROCK_FALLBACK_ENABLED is False


def test_converse_refuses_to_reach_aws_when_the_tier_is_disabled(monkeypatch):
    """The caller catches this and drops to the local model, so a disabled tier
    degrades instead of erroring the request out."""
    mod = _reload(monkeypatch)

    with pytest.raises(RuntimeError, match="ORACLE_AI_BEDROCK_FALLBACK"):
        mod._converse([], "system", None)


@pytest.mark.parametrize("enabled", ["1", "true", "YES", "on"])
def test_bedrock_tier_can_be_re_enabled(monkeypatch, enabled):
    mod = _reload(monkeypatch, ORACLE_AI_BEDROCK_FALLBACK=enabled)

    assert mod.BEDROCK_FALLBACK_ENABLED is True
    # Past the guard it tries the real client, which is exactly what we want to
    # prove — the failure is now about AWS config, not about the tier being off.
    with pytest.raises(Exception) as excinfo:
        mod._converse([], "system", None)
    assert "ORACLE_AI_BEDROCK_FALLBACK" not in str(excinfo.value)


def test_explicit_bedrock_selection_still_honoured(monkeypatch):
    """Operators mid-migration can pin the old plane without editing code."""
    assert _reload(monkeypatch, ORACLE_AI_CHAT_PROVIDER="bedrock").AI_PROVIDER == "bedrock"
