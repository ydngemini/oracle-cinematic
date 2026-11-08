"""
Bedrock Client — routes synthetic data generation through AWS Bedrock Runtime.

Supports:
  - anthropic.claude-3-sonnet (Messages API format)
  - meta.llama3-70b-instruct (Llama prompt format)

Implements exponential backoff on throttling so harvester loops never crash.
"""

import json
import logging
import time
import os
from typing import Optional

logger = logging.getLogger("oracle.ml_forge.bedrock")

AWS_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
# llama3-3-70b is on-demand-disabled; both must use the cross-region inference
# profile (us.* prefix), which is what Bedrock actually authorizes for invoke.
PRIMARY_MODEL = "us.meta.llama3-3-70b-instruct-v1:0"
SECONDARY_MODEL = "us.meta.llama3-1-8b-instruct-v1:0"
MAX_RETRIES = 6
BASE_DELAY = 1.0
MAX_DELAY = 64.0

# ── Fireworks AI ──────────────────────────────────────────────────────────────
# Every AI feature outside the chat agent (tour copy, CMA, voice intel,
# disposition, synthetic lawyer, client enterprise, observability) funnels
# through invoke_bedrock_model — or, when a brokerage pays for the call,
# through invoke_async under its spend scope (T18.07). Routing it here fixes
# all of them at once.
FIREWORKS_API_KEY = os.environ.get("ORACLE_FIREWORKS_API_KEY", "") or os.environ.get(
    "FIREWORKS_API_KEY", ""
)
FIREWORKS_URL = os.environ.get(
    "ORACLE_FIREWORKS_URL", "https://api.fireworks.ai/inference/v1/chat/completions"
)
# The callers escalate PRIMARY -> SECONDARY on a None return, so keep two tiers.
FIREWORKS_PRIMARY = os.environ.get(
    "ORACLE_FIREWORKS_MODEL", "accounts/fireworks/models/kimi-k3"
)
FIREWORKS_SECONDARY = os.environ.get(
    "ORACLE_FIREWORKS_FAST_MODEL", "accounts/fireworks/routers/glm-5p2-fast"
)
# Reasoning models spend the budget on reasoning_content before emitting any
# content. Callers here ask for as little as 700, which comes back empty with
# finish_reason="length" — floor it so the model can actually answer.
FIREWORKS_MIN_TOKENS = int(os.environ.get("ORACLE_FIREWORKS_MIN_TOKENS", "2048") or 2048)
FIREWORKS_TIMEOUT = float(os.environ.get("ORACLE_FIREWORKS_TIMEOUT", "120") or 120)
_PROVIDER = os.environ.get("ORACLE_AI_CHAT_PROVIDER", "").strip().lower()
_FIREWORKS_OPT_IN = os.environ.get(
    "ORACLE_AI_FIREWORKS_FALLBACK", "0"
).strip().lower() in ("1", "true", "yes", "on")
# An unset provider stays on Bedrock, matching ai_chat_agent — opting in is
# always explicit, so an unset variable never silently reroutes prompts.
FIREWORKS_ENABLED = bool(FIREWORKS_API_KEY) and (
    _PROVIDER == "fireworks" or _FIREWORKS_OPT_IN
)

_client = None


def _fireworks_model_for(model_id: str) -> str:
    """Map a Bedrock model id onto the matching Fireworks tier."""
    return FIREWORKS_SECONDARY if model_id == SECONDARY_MODEL else FIREWORKS_PRIMARY


def _invoke_fireworks(model_id: str, prompt: str, max_tokens: int) -> Optional[str]:
    """Fireworks call shaped like invoke_bedrock_model: text, or None on failure."""
    import urllib.error
    import urllib.request

    body = json.dumps(
        {
            "model": _fireworks_model_for(model_id),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max(max_tokens, FIREWORKS_MIN_TOKENS),
            "temperature": 0.6,
        }
    ).encode()
    request = urllib.request.Request(
        FIREWORKS_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {FIREWORKS_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=FIREWORKS_TIMEOUT) as response:
                data = json.loads(response.read())
            choice = (data.get("choices") or [{}])[0]
            text = str((choice.get("message") or {}).get("content") or "")
            if not text.strip():
                # Nothing usable — let the caller escalate rather than hand a
                # downstream parser an empty string it will misread as valid.
                logger.error(
                    "Fireworks returned no content (finish_reason=%s)",
                    choice.get("finish_reason"),
                )
                return None
            return text
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
                logger.warning(
                    "Fireworks %s (attempt %d/%d), retrying in %.1fs",
                    exc.code, attempt + 1, MAX_RETRIES, delay,
                )
                time.sleep(delay)
                continue
            logger.error("Fireworks HTTPError %s: %s", exc.code, exc.read()[:300])
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error("Fireworks request failed: %s", exc)
            return None
    logger.error("Fireworks: exhausted %d retries for %s", MAX_RETRIES, model_id)
    return None


def _boto_errors():
    """Resolve botocore's exception classes on demand.

    Importing them at module scope pulled the whole AWS SDK into every process
    that touched voice_intel, which on an Azure-only deployment is pure startup
    cost for a code path that never runs."""
    from botocore.exceptions import BotoCoreError, ClientError

    return ClientError, BotoCoreError


def _get_client():
    global _client
    if _client is None:
        import boto3  # lazy — see _boto_errors
        from botocore.config import Config

        boto_config = Config(
            connect_timeout=10,
            read_timeout=120,
            retries={"max_attempts": 0},  # retries handled manually with backoff below
        )
        kwargs = {"region_name": AWS_REGION, "config": boto_config}
        # Optional dedicated Bedrock credentials. Lets Bedrock invoke a different
        # AWS account than the rest of the app (e.g. the account where Bedrock is
        # actually enabled) without disturbing S3/other AWS_* default-chain usage.
        ak = os.environ.get("BEDROCK_AWS_ACCESS_KEY_ID")
        sk = os.environ.get("BEDROCK_AWS_SECRET_ACCESS_KEY")
        if ak and sk:
            kwargs["aws_access_key_id"] = ak
            kwargs["aws_secret_access_key"] = sk
        _client = boto3.client("bedrock-runtime", **kwargs)
    return _client


def _build_payload(model_id: str, prompt: str, max_tokens: int) -> dict:
    if "anthropic" in model_id:
        return {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": [
                {"role": "user", "content": prompt}
            ],
        }

    if "meta.llama" in model_id:
        return {
            "prompt": f"<|begin_of_text|><|start_header_id|>user<|end_header_id|>\n\n{prompt}<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n",
            "max_gen_len": max_tokens,
            "temperature": 0.6,
            "top_p": 0.9,
        }

    raise ValueError(f"Unsupported model_id: {model_id}")


def _parse_response(model_id: str, response_body: dict) -> str:
    if "anthropic" in model_id:
        content_blocks = response_body.get("content", [])
        return "".join(
            block.get("text", "") for block in content_blocks if block.get("type") == "text"
        )

    if "meta.llama" in model_id:
        return response_body.get("generation", "")

    return json.dumps(response_body)


# The gateway is opt-in while both paths exist: an unset variable never
# silently reroutes prompts, which is the same rule FIREWORKS_ENABLED follows.
GATEWAY_ENABLED = os.environ.get(
    "ORACLE_LLM_GATEWAY", "1"
).strip().lower() in ("1", "true", "yes", "on")


def _gateway_enabled() -> bool:
    if not GATEWAY_ENABLED:
        return False
    # litellm is imported lazily inside the gateway, so importing llm_gateway
    # proves nothing about whether a model is reachable. Without this check a
    # deployment that has not installed litellm yet took the gateway path on
    # every call, failed, logged a warning and then did the direct call anyway.
    import importlib.util

    if importlib.util.find_spec("litellm") is None:
        return False
    import llm_gateway

    return bool(llm_gateway.providers_for("analysis"))


def _via_gateway(prompt: str, *, task: str, max_tokens: int) -> Optional[str]:
    """Call the gateway from this synchronous seam.

    Every caller already runs this function inside ``asyncio.to_thread``, so
    there is no running loop on this thread and ``asyncio.run`` is safe. The
    gateway raises if that assumption is ever wrong rather than deadlocking.
    """
    import llm_gateway

    try:
        return llm_gateway.complete_sync(prompt, task=task, max_tokens=max_tokens)
    except RuntimeError as exc:
        logger.warning("bedrock_client: gateway unusable from this context: %s", exc)
        return None


def invoke_bedrock_model(
    model_id: str,
    prompt: str,
    max_tokens: int = 2048,
) -> Optional[str]:
    """Invoke the configured model with automatic retry on throttling.

    Returns the generated text, or None if all retries are exhausted. When
    Fireworks is selected the request goes there instead of Bedrock; the
    signature and the None-on-failure contract are unchanged, so the callers
    that escalate PRIMARY -> SECONDARY keep working untouched.

    Never under a spend scope (T18.07). asyncio.to_thread copies the caller's
    scope into this thread: the gateway would hold from a nested event loop,
    and any failure — a budget refusal included, which complete_sync reads as
    "no answer" — would send the call down the direct path below, unheld,
    unmetered and retried up to MAX_RETRIES times. A caller spending for a
    brokerage awaits invoke_async instead; reaching here under a scope fails
    closed, before any provider is contacted.
    """
    import llm_gateway

    if llm_gateway.spend_scope.get() is not None:
        raise RuntimeError(
            "invoke_bedrock_model() was called under a spend scope; await invoke_async() "
            "so the call is held and metered."
        )
    # Through the gateway when it can reach a provider; the direct Bedrock and
    # Fireworks paths below stay as they were so a deployment without litellm
    # installed keeps working exactly as before.
    #
    # The caller's PRIMARY -> SECONDARY escalation is preserved by mapping the
    # two model ids onto the gateway's two task ladders, so the seven callers
    # that branch on a None return are untouched.
    if _gateway_enabled():
        task = "fast" if model_id == SECONDARY_MODEL else "analysis"
        answer = _via_gateway(prompt, task=task, max_tokens=max_tokens)
        if answer is not None:
            return answer
        logger.warning("bedrock_client: gateway could not answer; using the direct path")

    if FIREWORKS_ENABLED:
        return _invoke_fireworks(model_id, prompt, max_tokens)
    client = _get_client()
    # Safe to resolve now: _get_client() has already imported the SDK.
    ClientError, BotoCoreError = _boto_errors()
    payload = _build_payload(model_id, prompt, max_tokens)
    body = json.dumps(payload)

    for attempt in range(MAX_RETRIES):
        try:
            response = client.invoke_model(
                modelId=model_id,
                contentType="application/json",
                accept="application/json",
                body=body,
            )
            response_body = json.loads(response["body"].read())
            return _parse_response(model_id, response_body)

        except ClientError as e:
            error_code = e.response["Error"]["Code"]

            if error_code in ("ThrottlingException", "TooManyRequestsException"):
                delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
                logger.warning(
                    f"Bedrock throttled (attempt {attempt + 1}/{MAX_RETRIES}), "
                    f"retrying in {delay:.1f}s"
                )
                time.sleep(delay)
                continue

            if error_code == "ModelTimeoutException":
                delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
                logger.warning(
                    f"Bedrock timeout (attempt {attempt + 1}/{MAX_RETRIES}), "
                    f"retrying in {delay:.1f}s"
                )
                time.sleep(delay)
                continue

            logger.error(f"Bedrock ClientError [{error_code}]: {e}")
            return None

        except BotoCoreError as e:
            logger.error(f"BotoCoreError: {e}")
            return None

    logger.error(f"Bedrock: exhausted {MAX_RETRIES} retries for model {model_id}")
    return None


# ── Held calls (T18.07) ───────────────────────────────────────────────────────
# A caller spending for a brokerage runs under llm_gateway.spend_scope
# (ai_budget.spending) and awaits invoke_async: every hosted call is held
# before it is sent and metered by the usage its provider reports.

def _messages(system: str, prompt: str) -> list[dict]:
    return ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]


def _fireworks_once(model_id: str, system: str, prompt: str, max_tokens: int) -> dict:
    """One Fireworks request, no retry: the raw response, usage included.
    Raises on any failure, so the caller's hold is released."""
    import urllib.request

    body = json.dumps(
        {
            "model": _fireworks_model_for(model_id),
            "messages": _messages(system, prompt),
            "max_tokens": max(max_tokens, FIREWORKS_MIN_TOKENS),
            "temperature": 0.6,
        }
    ).encode()
    request = urllib.request.Request(
        FIREWORKS_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {FIREWORKS_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=FIREWORKS_TIMEOUT) as response:
        return json.loads(response.read())


def _bedrock_converse_once(model_id: str, system: str, prompt: str, max_tokens: int) -> dict:
    """One Bedrock Converse request, no retry (the client's own retries are
    off): Converse reports usage (inputTokens/outputTokens), invoke_model's
    Llama body does not. Bounded by the client's 10 s connect and 120 s read
    timeouts, below ai_budget.HOLD_SECONDS."""
    kwargs: dict = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.6, "topP": 0.9},
    }
    if system:
        kwargs["system"] = [{"text": system}]
    return _get_client().converse(**kwargs)


def _fireworks_text(data: dict) -> str:
    choice = (data.get("choices") or [{}])[0]
    return str((choice.get("message") or {}).get("content") or "")


def _converse_text(data: dict) -> str:
    content = ((data.get("output") or {}).get("message") or {}).get("content") or []
    return "".join(str(block.get("text") or "") for block in content if isinstance(block, dict))


async def invoke_async(
    model_id: str,
    prompt: str,
    max_tokens: int = 2048,
    *,
    system: str = "",
) -> Optional[str]:
    """invoke_bedrock_model for async callers, held and metered under a spend
    scope. Text, or None when no provider answered (the same contract, so
    PRIMARY -> SECONDARY escalation on None is unchanged).

    With no scope it is exactly the old seam, in a worker thread. Under one:
      * the gateway when it can reach a provider — each provider attempt is
        held and settled by llm_gateway.complete. A budget refusal raises
        SpendRefused (never a fallback); a gateway that could not answer
        returns None — the unheld direct path is not tried after it;
      * otherwise one direct request, no retry, held through
        spend_guarded_thread and metered by its reported usage (Fireworks'
        JSON, or Bedrock Converse rather than invoke_model).
    """
    import asyncio

    import llm_gateway

    if llm_gateway.spend_scope.get() is None:
        joined = f"{system}\n\n{prompt}" if system else prompt
        return await asyncio.to_thread(invoke_bedrock_model, model_id, joined, max_tokens)
    if _gateway_enabled():
        task = "fast" if model_id == SECONDARY_MODEL else "analysis"
        try:
            return await llm_gateway.complete(prompt, task=task, system=system, max_tokens=max_tokens)
        except llm_gateway.SpendRefused:
            raise
        except llm_gateway.LLMUnavailable as exc:
            logger.warning("bedrock_client: no provider answered a held call: %s", type(exc).__name__)
            return None
    try:
        if FIREWORKS_ENABLED:
            ceiling = max(max_tokens, FIREWORKS_MIN_TOKENS)
            data = await llm_gateway.spend_guarded_thread(
                "fireworks", llm_gateway.estimate_tokens(_messages(system, prompt), completion=ceiling),
                _fireworks_once, model_id, system, prompt, max_tokens)
            text = _fireworks_text(data)
        else:
            data = await llm_gateway.spend_guarded_thread(
                "bedrock", llm_gateway.estimate_tokens(_messages(system, prompt), completion=max_tokens),
                _bedrock_converse_once, model_id, system, prompt, max_tokens)
            text = _converse_text(data)
    except llm_gateway.SpendRefused:
        raise
    except Exception as exc:  # noqa: BLE001 — the None contract; the hold was released
        logger.error("bedrock_client: held direct call failed (%s)", type(exc).__name__)
        return None
    return text if text.strip() else None


def _stream_fireworks(
    model_id: str,
    system_text: str,
    user_text: str,
    max_tokens: int,
    temperature: float,
):
    """Fireworks SSE streaming, yielding the same plain text deltas."""
    import urllib.error
    import urllib.request

    messages = []
    if system_text:
        messages.append({"role": "system", "content": system_text})
    messages.append({"role": "user", "content": user_text})

    body = json.dumps(
        {
            "model": _fireworks_model_for(model_id),
            "messages": messages,
            # The monologue asks for as few as 80 tokens; a reasoning model
            # spends that on reasoning_content and streams nothing at all.
            "max_tokens": max(max_tokens, FIREWORKS_MIN_TOKENS),
            "temperature": temperature,
            "stream": True,
        }
    ).encode()
    request = urllib.request.Request(
        FIREWORKS_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {FIREWORKS_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=FIREWORKS_TIMEOUT) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = (event.get("choices") or [{}])[0].get("delta") or {}
                text = delta.get("content")
                if text:
                    yield text
    except Exception as exc:  # noqa: BLE001
        # Callers treat this as a best-effort stream; log and end it rather than
        # propagating into the monologue loop.
        logger.error("Fireworks stream failed: %s", exc)
        return


def stream_converse(
    model_id: str,
    system_text: str,
    user_text: str,
    max_tokens: int = 80,
    temperature: float = 0.7,
):
    """Stream text deltas from a Bedrock model via the converse_stream API.

    Synchronous generator: yields each text delta as it arrives. Uses the
    provider-agnostic Converse API so no model-specific prompt formatting
    (Llama special tokens etc.) is needed.

    Routes to Fireworks when selected; the generator contract is identical, so
    agent_mind's monologue bridge is unaffected.
    """
    if FIREWORKS_ENABLED:
        yield from _stream_fireworks(model_id, system_text, user_text, max_tokens, temperature)
        return
    client = _get_client()

    kwargs = {
        "modelId": model_id,
        "messages": [
            {"role": "user", "content": [{"text": user_text}]}
        ],
        "inferenceConfig": {
            "maxTokens": max_tokens,
            "temperature": temperature,
            "topP": 0.9,
        },
    }
    if system_text:
        kwargs["system"] = [{"text": system_text}]

    response = client.converse_stream(**kwargs)
    for event in response.get("stream", []):
        block = event.get("contentBlockDelta")
        if not block:
            continue
        delta = block.get("delta", {})
        text = delta.get("text")
        if text:
            yield text
