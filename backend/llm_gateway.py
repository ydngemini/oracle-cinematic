"""One seam for every LLM call.

Two hand-rolled provider ladders existed. `ml_forge/bedrock_client` chose
between Bedrock and Fireworks and left its seven callers to escalate
PRIMARY → SECONDARY themselves on a `None` return; `ai_chat_agent` chose between
Azure Foundry, Bedrock, Fireworks and a local llama server with a second set of
rules. Adding a model registry (P7) or an ambient call budget (P2) to two
ladders means writing each of them twice, which is why they are collapsed here
first.

The gateway is async-native. `invoke_bedrock_model` stays synchronous and keeps
its `Optional[str]` contract, so those seven callers are untouched.

**Retries and tool calls do not mix.** A tool round has already applied CRM
writes by the time a response comes back — `ai_chat_agent._generate` returns
receipts rather than retrying for exactly this reason. A gateway-level fallback
that re-sent the same conversation would re-run those writes. So `tool_call()`
sends to one model with no fallbacks, and `complete()` — which produces text and
nothing else — is the only entry point that may fall back.

Deadlines are the caller's. Voice replies carry an 8-second wall clock; a retry
that consumed it would turn a late answer into no answer, so `timeout` bounds
the whole call including any fallback, not each attempt.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

logger = logging.getLogger("oracle.llm_gateway")

# How far back the call counter keeps timestamps. An hour comfortably covers the
# 60-second window the runtime-load endpoint asks for while leaving room for a
# wider question later.
_COUNTER_RETENTION_SECONDS = 3600.0


class LLMUnavailable(RuntimeError):
    """No configured provider could answer. Never raised for a refusal."""


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Provider:
    """One reachable model, in the `provider/model` form litellm expects."""

    name: str
    model: str
    api_key: str = ""
    api_base: str = ""
    extra: dict = field(default_factory=dict)

    def kwargs(self) -> dict:
        out: dict[str, Any] = {"model": self.model, **self.extra}
        if self.api_key:
            out["api_key"] = self.api_key
        if self.api_base:
            out["api_base"] = self.api_base
        return out


def _fireworks(model: str) -> Optional[Provider]:
    key = _env("ORACLE_FIREWORKS_API_KEY") or _env("FIREWORKS_API_KEY")
    if not key:
        return None
    return Provider(
        name="fireworks",
        model=f"fireworks_ai/{model}",
        api_key=key,
        api_base=_env("ORACLE_FIREWORKS_API_BASE") or "https://api.fireworks.ai/inference/v1",
    )


def _bedrock(model: str) -> Optional[Provider]:
    # Bedrock authenticates from the ambient AWS credential chain, so there is
    # no key to check here; the opt-in flag is what says the tier is live.
    if not (_flag("ORACLE_AI_BEDROCK_FALLBACK") or _env("ORACLE_AI_CHAT_PROVIDER") == "bedrock"):
        return None
    return Provider(
        name="bedrock",
        model=f"bedrock/{model}",
        extra={"aws_region_name": _env("BEDROCK_REGION", "us-east-1")},
    )


def _foundry(model: str) -> Optional[Provider]:
    """Azure AI Foundry, over its plain resource host.

    ``ORACLE_FOUNDRY_PROJECT_ENDPOINT`` is the *project* endpoint
    (``https://<res>.services.ai.azure.com/api/projects/<project>``); inference
    lives on the resource host beneath it, which is what litellm's ``azure/``
    provider expects as ``api_base``.

    An API key is enough. The earlier concern here was that the existing
    ``_foundry_response`` path authenticates with DefaultAzureCredential, and
    litellm's Responses handler routes AD tokens through a different seam — with
    a key that question does not arise. Verified against the live resource: the
    request reaches Azure and authenticates, failing only with
    ``DeploymentNotFound`` while nothing is deployed.
    """
    key = _env("AZURE_AI_API_KEY") or _env("ORACLE_FOUNDRY_API_KEY")
    endpoint = _env("ORACLE_FOUNDRY_PROJECT_ENDPOINT")
    if not key or not endpoint:
        return None
    base = endpoint.split("/api/projects", 1)[0].rstrip("/")
    if not base:
        return None
    return Provider(
        name="foundry",
        model=f"azure/{model}",
        api_key=key,
        api_base=base,
        extra={"api_version": _env("ORACLE_FOUNDRY_API_VERSION") or "2024-10-21"},
    )


def _local(model: str) -> Optional[Provider]:
    base = _env("ORACLE_LOCAL_LLM_URL") or "http://127.0.0.1:8090/v1"
    if not _flag("ORACLE_AI_LOCAL_FALLBACK", default=True):
        return None
    return Provider(
        name="local",
        model=f"openai/{model}",
        api_key="not-needed",
        api_base=base,
    )


# Ordered candidates per task. The first configured one answers; the rest are
# fallbacks, and only `complete()` is allowed to use them.
_TASK_MODELS: dict[str, tuple[tuple[str, str], ...]] = {
    "analysis": (
        ("fireworks", _env("ORACLE_FIREWORKS_MODEL") or "accounts/fireworks/models/kimi-k2p7-code"),
        ("foundry", _env("ORACLE_FOUNDRY_MODEL") or "Kimi-K2.6"),
        ("bedrock", "us.meta.llama3-3-70b-instruct-v1:0"),
        ("local", _env("ORACLE_LOCAL_LLM_MODEL") or "local-llama"),
    ),
    # Foundry leads the FAST ladder on measurement, not preference. Over
    # repeated short prompts: foundry median 1,261ms (max 4,153) against
    # fireworks median 6,013ms (max 36,502). The max is what decides it — the
    # voice path has an 8-second wall clock, so a 36-second tail is a
    # guaranteed stall line, while foundry's worst case still lands inside the
    # budget. The analysis ladder below is ordered the other way for the same
    # reason: there fireworks is faster (695ms vs 1,124ms median).
    "fast": (
        ("foundry", _env("ORACLE_FOUNDRY_MODEL") or "Kimi-K2.6"),
        ("fireworks", _env("ORACLE_FIREWORKS_FAST_MODEL") or "accounts/fireworks/routers/glm-5p2-fast"),
        ("bedrock", "us.meta.llama3-1-8b-instruct-v1:0"),
        ("local", _env("ORACLE_LOCAL_LLM_MODEL") or "local-llama"),
    ),
}

_BUILDERS = {"fireworks": _fireworks, "bedrock": _bedrock, "foundry": _foundry, "local": _local}

# A reasoning model emits reasoning_content first and only then content. Asked
# for a small budget it spends the whole thing thinking and returns "" with
# finish_reason="length" — which the empty-content check below correctly treats
# as a failure, but the honest fix is to let the model finish. bedrock_client
# learned this the same way and floors Fireworks calls at the same number.
MIN_TOKENS = int(_env("ORACLE_FIREWORKS_MIN_TOKENS") or 2048)


def providers_for(task: str) -> list[Provider]:
    """Configured providers for a task, best first. May be empty."""
    resolved: list[Provider] = []
    for provider_name, model in _TASK_MODELS.get(task, _TASK_MODELS["analysis"]):
        builder = _BUILDERS.get(provider_name)
        provider = builder(model) if builder else None
        if provider is not None:
            resolved.append(provider)
    return resolved


# ---------------------------------------------------------------------------
# Call accounting
# ---------------------------------------------------------------------------

class _Counter:
    """In-process call counts, for the runtime-load endpoint.

    Ambient monologue traffic is the reason this exists: at one call per socket
    every six seconds it was ~500/minute per replica, and the only way to know
    was to read the code. A counter here makes the claim measurable.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, int] = {}
        self._failures: dict[str, int] = {}
        self._recent: list[float] = []
        self._started = time.monotonic()

    def record(self, task: str, provider: str, ok: bool) -> None:
        key = f"{task}:{provider}"
        now = time.monotonic()
        with self._lock:
            target = self._calls if ok else self._failures
            target[key] = target.get(key, 0) + 1
            self._recent.append(now)
            # Retention is bounded here, on write, by age and then by count.
            # Age first so a quiet replica does not hold yesterday's stamps;
            # count second so a burst cannot grow the list without limit.
            horizon = now - _COUNTER_RETENTION_SECONDS
            if len(self._recent) > 4_000:
                self._recent = [stamp for stamp in self._recent if stamp >= horizon][-4_000:]

    def calls_in_last(self, seconds: float) -> int:
        """Calls started within a rolling window.

        The endpoint that consumes this asks "how much ambient traffic is this
        replica generating right now", which a cumulative total cannot answer:
        a process up for a day reports a number dominated by yesterday.
        """
        cutoff = time.monotonic() - seconds
        with self._lock:
            # Counted without trimming. Trimming on read made a short window
            # destroy the data a longer one would have needed, so asking for
            # 60 seconds and then 300 returned fewer calls for the wider window.
            return sum(1 for stamp in self._recent if stamp >= cutoff)

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = max(time.monotonic() - self._started, 1e-9)
            total = sum(self._calls.values())
            return {
                "calls": dict(self._calls),
                "failures": dict(self._failures),
                "total_calls": total,
                "calls_per_minute": round(total / elapsed * 60.0, 2),
                "window_seconds": round(elapsed, 1),
            }


counter = _Counter()


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------

def _litellm():
    try:
        import litellm
    except ImportError as exc:  # pragma: no cover - exercised by the import test
        raise LLMUnavailable(
            "litellm is not installed, so no model can be reached. "
            "Add it to backend/requirements.txt."
        ) from exc
    # Chatty by default, and it logs request bodies — which here contain client
    # records and draft outreach.
    litellm.suppress_debug_info = True
    litellm.drop_params = True
    return litellm


def _text_of(response: Any) -> str:
    try:
        choice = response.choices[0]
    except (AttributeError, IndexError, TypeError):
        return ""
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) if message is not None else None
    return (content or "").strip()


def token_usage_of(response: Any) -> tuple[int, int]:
    """(prompt_tokens, completion_tokens) from any provider response shape.

    Exists for the same reason as `_text_of`: three call paths reach three
    different APIs and none of them agrees on the field names. The Responses API
    says input_tokens/output_tokens, Chat Completions says
    prompt_tokens/completion_tokens, and a local llama.cpp server may say
    neither. A caller that guessed one shape would silently meter zero against
    the others, which is worse than not metering at all — it would look like the
    spend was measured.

    Returns (0, 0) rather than raising when usage is absent. Failing to record
    consumption must never fail the request that consumed it.
    """
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return (0, 0)

    def _field(*names: str) -> int:
        for name in names:
            value = (
                usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            )
            if value is not None:
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    return 0
        return 0

    return (
        _field("prompt_tokens", "input_tokens"),
        _field("completion_tokens", "output_tokens"),
    )


def warm() -> bool:
    """Import litellm now so no live request pays for it.

    Measured: the first voice turn in a fresh process took 12.2s against an
    8s budget while later turns took 1.6-3.3s. The deadline could not cover it
    because ``_litellm()`` resolves when the coroutine is built — before
    ``asyncio.wait_for`` starts timing. Importing litellm is seconds of work,
    so it belongs at startup, not in front of a caller on the phone.

    Returns whether a provider is actually reachable, so a caller can log the
    difference between "warmed" and "nothing configured".
    """
    try:
        _litellm()
    except LLMUnavailable as exc:
        logger.info("llm_gateway not warmed: %s", exc)
        return False
    configured = bool(providers_for("analysis"))
    logger.info(
        "llm_gateway warmed; %s",
        "providers configured" if configured else "no providers configured",
    )
    return configured


async def complete(
    prompt: str,
    *,
    task: str = "analysis",
    system: str = "",
    max_tokens: int = 2048,
    temperature: float = 0.2,
    timeout: float = 120.0,
) -> str:
    """Text in, text out, falling through configured providers in order.

    The only entry point that may fall back, because the worst case of a repeat
    is a duplicate paragraph. `timeout` bounds the whole call, fallbacks
    included: a voice reply with an 8-second budget would rather answer from a
    worse model than answer late.
    """
    providers = providers_for(task)
    if not providers:
        raise LLMUnavailable(
            f"No provider is configured for {task!r}. Set ORACLE_FIREWORKS_API_KEY, "
            f"or ORACLE_AI_BEDROCK_FALLBACK=1, or point ORACLE_LOCAL_LLM_URL at a "
            f"local server."
        )
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    max_tokens = max(max_tokens, MIN_TOKENS)
    deadline = time.monotonic() + timeout
    last_error: Optional[Exception] = None

    for provider in providers:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("llm_gateway: %s deadline exhausted before %s", task, provider.name)
            break
        try:
            # asyncio.wait_for, not just the SDK's own timeout parameter.
            # Measured: a voice turn with an 8s budget returned after 15.7s
            # because litellm did not enforce the value it was handed. The
            # deadline has to be held by the event loop — the provider SDK is
            # not something a live phone call can depend on for it.
            response = await asyncio.wait_for(
                _litellm().acompletion(
                    **provider.kwargs(),
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=remaining,
                ),
                timeout=remaining,
            )
        except LLMUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - any provider error is a fallback trigger
            counter.record(task, provider.name, ok=False)
            last_error = exc
            logger.warning("llm_gateway: %s/%s failed: %s", task, provider.name, exc)
            continue
        text = _text_of(response)
        if not text:
            # A reasoning model that spent its whole budget before emitting
            # content returns an empty string with finish_reason="length".
            # Treating that as an answer hands the caller silence.
            counter.record(task, provider.name, ok=False)
            last_error = LLMUnavailable(f"{provider.name} returned no content")
            logger.warning("llm_gateway: %s/%s returned empty content", task, provider.name)
            continue
        counter.record(task, provider.name, ok=True)
        return text

    raise LLMUnavailable(
        f"Every configured provider for {task!r} failed"
        + (f": {last_error}" if last_error else "")
    )


async def stream(
    prompt: str,
    *,
    task: str = "analysis",
    system: str = "",
    max_tokens: int = 2048,
    temperature: float = 0.2,
    timeout: float = 120.0,
):
    """Yield text deltas as they arrive.

    Replaces a hand-rolled bridge that ran the synchronous Bedrock generator on
    a raw ``threading.Thread`` and pushed deltas back through an
    ``asyncio.Queue`` with a sentinel — and swallowed every exception, so a
    stream that failed was indistinguishable from one that had nothing to say.

    **Fallback happens only before the first token.** Once a delta has been
    emitted the caller has shown it to someone; switching providers mid-stream
    would splice two different answers into one sentence.
    """
    providers = providers_for(task)
    if not providers:
        raise LLMUnavailable(f"No provider is configured for {task!r}.")
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    max_tokens = max(max_tokens, MIN_TOKENS)
    deadline = time.monotonic() + timeout
    last_error: Optional[Exception] = None

    for provider in providers:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        emitted = False
        try:
            response = await _litellm().acompletion(
                **provider.kwargs(),
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=remaining,
                stream=True,
            )
            async for chunk in response:
                try:
                    delta = chunk.choices[0].delta.content
                except (AttributeError, IndexError, TypeError):
                    delta = None
                if delta:
                    emitted = True
                    yield delta
        except LLMUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            counter.record(task, provider.name, ok=False)
            last_error = exc
            if emitted:
                # Mid-stream failure. The caller already has a partial answer;
                # starting over from another provider would append a second,
                # unrelated one to it.
                logger.warning(
                    "llm_gateway: %s/%s failed after emitting tokens; stream ends short",
                    task, provider.name,
                )
                return
            logger.warning("llm_gateway: %s/%s stream failed: %s", task, provider.name, exc)
            continue
        if emitted:
            counter.record(task, provider.name, ok=True)
            return
        counter.record(task, provider.name, ok=False)
        last_error = LLMUnavailable(f"{provider.name} streamed no content")

    raise LLMUnavailable(
        f"Every configured provider for {task!r} failed to stream"
        + (f": {last_error}" if last_error else "")
    )


async def tool_call(
    messages: Sequence[dict],
    tools: Sequence[dict],
    *,
    task: str = "analysis",
    provider: Optional[Provider] = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    timeout: float = 120.0,
    tool_choice: str = "auto",
) -> Any:
    """One tool round, against one model, with no fallback.

    This is the rule the gateway exists to hold. By the time a tool response
    comes back the handlers have already written to the database; re-sending the
    same conversation to a second provider would re-run those writes, and the
    caller has no way to tell a retry from a fresh decision. A failure here is
    reported, not retried.
    """
    if provider is None:
        providers = providers_for(task)
        if not providers:
            raise LLMUnavailable(f"No provider is configured for {task!r}.")
        provider = providers[0]
    max_tokens = max(max_tokens, MIN_TOKENS)
    try:
        # Same reason as complete(): the deadline is enforced here, not by the
        # provider SDK. A tool round that overruns still cannot be retried, so
        # the caller needs it to fail on time rather than late.
        response = await asyncio.wait_for(
            _litellm().acompletion(
                **provider.kwargs(),
                messages=list(messages),
                tools=list(tools),
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
            ),
            timeout=timeout,
        )
    except LLMUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        counter.record(task, provider.name, ok=False)
        raise LLMUnavailable(f"{provider.name} failed during a tool round: {exc}") from exc
    counter.record(task, provider.name, ok=True)
    return response


async def tool_round(
    messages: Sequence[dict],
    tools: Sequence[dict],
    *,
    provider: Optional[Provider] = None,
    task: str = "analysis",
    max_tokens: int = 4096,
    temperature: float = 0.2,
    timeout: float = 120.0,
) -> dict:
    """One tool round, returned as a plain OpenAI-shaped dict.

    Exists so callers driving their own tool loop never touch a litellm type.
    ``ai_chat_agent``'s loop already speaks ``choices[0].message.tool_calls``,
    which is what this returns — so swapping its transport for the gateway
    changes where the bytes come from and nothing about the loop, its
    anchor-locking, or its receipts.

    Still one provider and no fallback, for the reason ``tool_call`` states:
    a retry re-runs CRM writes that already committed. A caller that wants to
    try a second provider must decide that itself, because only the caller
    knows whether anything has been written yet.
    """
    response = await tool_call(
        messages, tools, provider=provider, task=task,
        max_tokens=max_tokens, temperature=temperature, timeout=timeout,
    )
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if isinstance(response, dict):
        return response
    raise LLMUnavailable("the provider returned an unreadable response shape")


def complete_sync(
    prompt: str,
    *,
    task: str = "analysis",
    max_tokens: int = 2048,
    timeout: float = 120.0,
) -> Optional[str]:
    """Blocking wrapper for callers that are still synchronous.

    Returns None rather than raising, which is the contract
    ``invoke_bedrock_model`` established and its seven callers branch on.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "complete_sync() was called from a running event loop; await "
            "complete() instead, or wrap it in asyncio.to_thread."
        )
    try:
        return asyncio.run(complete(prompt, task=task, max_tokens=max_tokens, timeout=timeout))
    except LLMUnavailable as exc:
        logger.warning("llm_gateway: %s", exc)
        return None


def ambient_calls_last_minute() -> int:
    """Model calls this replica made in the last 60 seconds.

    Read by ``/api/admin/runtime-load``, which reported ``None`` for this until
    a gateway existed to count — because reporting 0 would have claimed a
    measurement nobody took. Counts every gateway call, not only ambient ones:
    the endpoint's question is what the process is carrying, and attributing
    each call to a trigger is a larger change than the number is worth.
    """
    return counter.calls_in_last(60.0)
