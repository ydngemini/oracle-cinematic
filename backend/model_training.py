"""RunPod GPU execution client for consented LoRA training jobs."""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Mapping


class RunPodTrainingError(RuntimeError):
    pass


def _request_json(url: str, *, method: str, body: Mapping[str, Any] | None = None) -> dict:
    api_key = os.getenv("RUNPOD_API_KEY", "")
    if not api_key:
        raise RunPodTrainingError("RUNPOD_API_KEY is not configured")
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Oracle-ModelForge/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1_000]
        raise RunPodTrainingError(f"RunPod HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RunPodTrainingError("RunPod request failed or returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RunPodTrainingError("RunPod response was not an object")
    return parsed


async def runpod_train(
    manifest: Mapping[str, Any],
    reporter,
    *,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    endpoint_id = os.getenv("RUNPOD_TRAINING_ENDPOINT_ID", "")
    if not endpoint_id:
        raise RunPodTrainingError("RUNPOD_TRAINING_ENDPOINT_ID is not configured")
    base = f"https://api.runpod.ai/v2/{endpoint_id}"
    response = await asyncio.to_thread(
        _request_json,
        f"{base}/run",
        method="POST",
        body={"input": dict(manifest)},
    )
    run_id = str(response.get("id") or "")
    if not run_id:
        raise RunPodTrainingError("RunPod did not return a job id")

    timeout = max(60, min(12 * 3600, int(timeout_seconds or os.getenv("RUNPOD_TRAINING_TIMEOUT", "7200"))))
    started = time.monotonic()
    await reporter.progress(10, f"RunPod job {run_id} submitted")
    while time.monotonic() - started < timeout:
        await asyncio.sleep(5)
        status = await asyncio.to_thread(
            _request_json,
            f"{base}/status/{run_id}",
            method="GET",
        )
        state = str(status.get("status") or "").upper()
        elapsed = time.monotonic() - started
        await reporter.progress(
            min(90, 10 + (elapsed / timeout) * 80),
            f"RunPod training: {state or 'UNKNOWN'}",
        )
        if state == "COMPLETED":
            output = status.get("output")
            if not isinstance(output, dict):
                raise RunPodTrainingError("RunPod completed without an artifact manifest")
            checksum = str(output.get("artifact_sha256") or "")
            if len(checksum) != 64 or any(ch not in "0123456789abcdef" for ch in checksum.lower()):
                raise RunPodTrainingError("RunPod artifact checksum is missing or invalid")
            if not isinstance(output.get("model_card"), dict):
                raise RunPodTrainingError("RunPod output is missing a model card")
            artifact_uri = str(output.get("artifact_uri") or "")
            if not artifact_uri.startswith("s3://"):
                raise RunPodTrainingError("RunPod output must reference a private s3:// artifact")
            return {"runpod_job_id": run_id, **output}
        if state in {"FAILED", "CANCELLED", "TIMED_OUT"}:
            raise RunPodTrainingError(f"RunPod training ended with {state}")
    raise RunPodTrainingError(f"RunPod training timed out after {timeout} seconds")
