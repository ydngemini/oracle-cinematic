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

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, BotoCoreError

logger = logging.getLogger("oracle.ml_forge.bedrock")

AWS_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
# llama3-3-70b is on-demand-disabled; both must use the cross-region inference
# profile (us.* prefix), which is what Bedrock actually authorizes for invoke.
PRIMARY_MODEL = "us.meta.llama3-3-70b-instruct-v1:0"
SECONDARY_MODEL = "us.meta.llama3-1-8b-instruct-v1:0"
MAX_RETRIES = 6
BASE_DELAY = 1.0
MAX_DELAY = 64.0

_client = None


_BOTO_CONFIG = Config(
    connect_timeout=10,
    read_timeout=120,
    retries={"max_attempts": 0},  # retries handled manually with backoff below
)


def _get_client():
    global _client
    if _client is None:
        kwargs = {"region_name": AWS_REGION, "config": _BOTO_CONFIG}
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


def invoke_bedrock_model(
    model_id: str,
    prompt: str,
    max_tokens: int = 2048,
) -> Optional[str]:
    """Invoke a Bedrock model with automatic retry on throttling.

    Returns the generated text, or None if all retries are exhausted.
    """
    client = _get_client()
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
    """
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
