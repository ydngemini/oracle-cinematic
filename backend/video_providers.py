"""Video generation providers behind one seam.

Why this exists: Azure's `sora-2` v2025-12-08 deprecates 2026-09-15, and OpenAI's
Videos API shuts down 2026-09-24 with no recommended replacement. Before this
module, every Sora URL was inlined in `video_studio.py` and the only way to
substitute a provider was `monkeypatch` in a test — so a deprecation was a rewrite
rather than a config change.

The shape here deliberately mirrors `reconstruction_providers.py`, which already
solved the same problem for 3D capture: a base class, a `_PROVIDERS` registry, and
`available() -> (ready, reason)` whose reason is forwarded verbatim to the caller.

Honesty rule, inherited from that module and load-bearing here: a provider that is
not configured reports `available() == (False, why)` so the enqueue endpoint 503s
**before** quota is reserved. It never fabricates a clip, and it never lets a job
enqueue only to fail after the user's daily seconds have been spent.

Provenance: every provider declares `produces`, which `_store_video` writes to
`property_media.provenance`. For all real providers that value is `ai_generated` —
migration 0071 states that only `captured` may support a claim that media shows the
actual home, and a generated marketing reel never can.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger("oracle.video_providers")


class VideoProviderError(RuntimeError):
    """A provider could not produce a clip. Message reaches the job's error field."""


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class VideoProvider:
    """One video-generation backend.

    Concurrency lives on the provider because the limit is a property of the
    vendor, not of Oracle: Sora allows 2 pending jobs per resource, and a
    different backend has a different ceiling. Sharing one module-level
    semaphore across providers would silently apply Sora's cap to everyone.
    """

    name: str = "base"
    #: property_media.provenance for clips this provider makes.
    produces: str = "ai_generated"
    #: Concurrent in-flight generations permitted by the vendor.
    max_concurrent: int = 1

    def __init__(self) -> None:
        self._slots = asyncio.Semaphore(self.max_concurrent)

    def available(self) -> tuple[bool, str]:
        """(ready, reason-if-not). The enqueue endpoint 503s when not ready."""
        return (False, "not implemented")

    async def generate(
        self, *, prompt: str, size: str, seconds: int, image_bytes: Optional[bytes] = None
    ) -> bytes:
        raise NotImplementedError

    # Shared helper: providers wrap their own work in this so the semaphore is
    # held across submit→poll→download rather than only across submit.
    def _slot(self):
        return self._slots


class UnavailableProvider(VideoProvider):
    """Selected name did not resolve. Fails closed, and says which name."""

    produces = "ai_generated"

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    def available(self) -> tuple[bool, str]:
        return (False, f"unknown ORACLE_VIDEO_PROVIDER {self.name!r}")

    async def generate(self, **_kwargs) -> bytes:
        raise VideoProviderError(self.available()[1])


# ---------------------------------------------------------------------------
# Sora (Azure OpenAI) — the incumbent, unchanged in behaviour
# ---------------------------------------------------------------------------
class SoraProvider(VideoProvider):
    """Azure OpenAI Sora 2.

    DEPRECATION: Azure's sora-2 v2025-12-08 retires 2026-09-15. This provider is
    kept so nothing breaks before then; it is not the long-term path.

    Note the endpoint is Azure's version-less `/openai/v1/` surface — the old
    ORACLE_SORA_API_VERSION was read and never sent, so it has been dropped
    rather than left as a knob that does nothing.
    """

    name = "sora"
    produces = "ai_generated"
    max_concurrent = 2  # Azure: max 2 pending jobs per resource

    @property
    def _endpoint(self) -> str:
        return os.getenv("ORACLE_AZURE_OPENAI_ENDPOINT", "").rstrip("/")

    @property
    def _deployment(self) -> str:
        return os.getenv("ORACLE_SORA_DEPLOYMENT", "sora-2-estate")

    def available(self) -> tuple[bool, str]:
        if not self._endpoint:
            return (False, "set ORACLE_AZURE_OPENAI_ENDPOINT")
        if not self._deployment:
            return (False, "set ORACLE_SORA_DEPLOYMENT")
        return (True, "")

    async def generate(
        self, *, prompt: str, size: str, seconds: int, image_bytes: Optional[bytes] = None
    ) -> bytes:
        from video_studio import _sora_download, _sora_status, _sora_submit

        async with self._slot():
            job_id = await asyncio.to_thread(
                _sora_submit, prompt=prompt, size=size, seconds=seconds, image_bytes=image_bytes
            )
            deadline = time.monotonic() + _job_timeout()
            poll = _poll_seconds()
            while True:
                if time.monotonic() > deadline:
                    raise VideoProviderError(f"Sora job {job_id} timed out")
                status, error = await asyncio.to_thread(_sora_status, job_id)
                if status in ("succeeded", "completed"):
                    break
                if status in ("failed", "cancelled"):
                    raise VideoProviderError(error or f"Sora job {job_id} {status}")
                await asyncio.sleep(poll)
            return await asyncio.to_thread(_sora_download, job_id)


# ---------------------------------------------------------------------------
# Veo (Google Vertex AI) — the replacement
# ---------------------------------------------------------------------------
#: Vertex renders 16:9 / 9:16 rather than pixel sizes. Oracle's two allowed
#: sizes map cleanly; anything else is refused rather than guessed at.
_SIZE_TO_ASPECT = {"1280x720": "16:9", "720x1280": "9:16"}


class VeoProvider(VideoProvider):
    """Google Veo 3.1 on Vertex AI.

    Request shape verified against the google-genai SDK's own Vertex converter
    (`_GenerateVideosParameters_to_vertex`) rather than inferred from Sora:

        POST {base}/v1/projects/{p}/locations/{l}/publishers/google/models/{m}:predictLongRunning
        {"instances": [{"prompt": ..., "image": {"bytesBase64Encoded": ..., "mimeType": ...}}],
         "parameters": {"sampleCount": 1, "durationSeconds": N, "aspectRatio": "16:9",
                        "resolution": "720p", "generateAudio": true}}

    Poll with `POST {operation.name}:fetchPredictOperation`; the finished clip
    arrives as `bytesBase64Encoded` (inline) or `gcsUri` when `storageUri` was
    set. We deliberately do NOT set `storageUri`: `_store_video` wants mp4 bytes
    and already owns object storage, so routing through GCS would add a bucket
    and a second credential for no gain.

    **API version is pinned to v1 on purpose.** googleapis/python-genai#2079
    reports Veo 3.1 GA failing because the SDK routed to v1beta1.

    Auth is Application Default Credentials — the same ADC used elsewhere in this
    session. No key material is read from env.
    """

    name = "veo"
    produces = "ai_generated"
    #: Vertex quota is per-project and per-region; 2 is conservative until a live
    #: run establishes the real ceiling.
    max_concurrent = 2

    @property
    def _project(self) -> str:
        return os.getenv("ORACLE_VEO_PROJECT", "").strip()

    @property
    def _location(self) -> str:
        return os.getenv("ORACLE_VEO_LOCATION", "us-central1").strip()

    @property
    def _model(self) -> str:
        # veo-3.1-generate-001 is the GA id; -fast-generate-001 trades quality for
        # latency. Left configurable because model ids move faster than releases.
        return os.getenv("ORACLE_VEO_MODEL", "veo-3.1-generate-001").strip()

    def _base(self) -> str:
        return f"https://{self._location}-aiplatform.googleapis.com/v1"

    @staticmethod
    def _adc_token() -> str:
        """Fetch an ADC access token, or "" when ADC is not established."""
        try:
            import google.auth  # type: ignore
            import google.auth.transport.requests  # type: ignore
        except ImportError:
            return ""
        try:
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            creds.refresh(google.auth.transport.requests.Request())
            return creds.token or ""
        except Exception:  # noqa: BLE001 — absent/expired ADC is a config state
            return ""

    def available(self) -> tuple[bool, str]:
        if not self._project:
            return (False, "set ORACLE_VEO_PROJECT to a billing-enabled GCP project")
        try:
            import google.auth  # noqa: F401
        except ImportError:
            return (False, "google-auth is not installed (pip install google-auth)")
        if not self._adc_token():
            return (
                False,
                "no Application Default Credentials — run "
                "`gcloud auth application-default login`",
            )
        # Deliberately NOT probed here: whether the project's billing is enabled.
        # available() is called on a request path, and a network round-trip per
        # enqueue is too expensive. A disabled-billing project therefore fails at
        # submit with Vertex's own message, which is more specific than a guess.
        return (True, "")

    def _headers(self) -> dict[str, str]:
        token = self._adc_token()
        if not token:
            raise VideoProviderError(
                "no Application Default Credentials for Vertex AI — run "
                "`gcloud auth application-default login`"
            )
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _submit(self, *, prompt: str, size: str, seconds: int, image_bytes: Optional[bytes]) -> str:
        import requests

        aspect = _SIZE_TO_ASPECT.get(size)
        if aspect is None:
            raise VideoProviderError(f"Veo has no aspect ratio for size {size!r}")

        instance: dict[str, object] = {"prompt": prompt}
        if image_bytes is not None:
            instance["image"] = {
                "bytesBase64Encoded": base64.b64encode(image_bytes).decode("ascii"),
                "mimeType": "image/jpeg",
            }
        body = {
            "instances": [instance],
            "parameters": {
                "sampleCount": 1,
                "durationSeconds": seconds,
                "aspectRatio": aspect,
                "resolution": "720p",
                # Veo 3.1 generates synchronized audio natively; the pipeline has
                # no separate TTS step and must not grow one.
                "generateAudio": True,
            },
        }
        url = (
            f"{self._base()}/projects/{self._project}/locations/{self._location}"
            f"/publishers/google/models/{self._model}:predictLongRunning"
        )
        try:
            response = requests.post(
                url, json=body, headers=self._headers(), timeout=_timeout("submit")
            )
        except requests.RequestException as exc:
            raise VideoProviderError(f"Veo submit failed: {exc}") from exc
        if response.status_code not in (200, 201, 202):
            raise VideoProviderError(
                f"Veo submit returned {response.status_code}: {response.text[:300]}"
            )
        name = (response.json() or {}).get("name")
        if not name:
            raise VideoProviderError("Veo submit returned no operation name")
        return str(name)

    def _poll(self, operation: str) -> tuple[bool, Optional[bytes], str]:
        """Return (done, mp4_bytes_or_None, error)."""
        import requests

        url = f"{self._base()}/{operation}:fetchPredictOperation"
        try:
            response = requests.post(
                url,
                json={"operationName": operation},
                headers=self._headers(),
                timeout=_timeout("poll"),
            )
        except requests.RequestException as exc:
            raise VideoProviderError(f"Veo poll failed: {exc}") from exc
        if response.status_code != 200:
            raise VideoProviderError(
                f"Veo poll returned {response.status_code}: {response.text[:300]}"
            )
        body = response.json() or {}
        if not body.get("done"):
            return (False, None, "")
        if body.get("error"):
            err = body["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            return (True, None, str(message)[:400])

        videos = ((body.get("response") or {}).get("videos")) or []
        if not videos:
            # A finished operation with no video is usually a safety filter, not a
            # transport failure. Say so rather than reporting an empty success.
            filtered = (body.get("response") or {}).get("raiMediaFilteredCount")
            if filtered:
                reason = (body.get("response") or {}).get("raiMediaFilteredReasons")
                return (True, None, f"Veo filtered the generation: {reason or filtered}")
            return (True, None, "Veo returned no video for a completed operation")

        first = videos[0] or {}
        encoded = first.get("bytesBase64Encoded")
        if encoded:
            return (True, base64.b64decode(encoded), "")
        if first.get("gcsUri"):
            # Only reachable if storageUri is ever set; we do not set it.
            return (True, None, f"Veo returned a GCS URI ({first['gcsUri']}) — inline bytes expected")
        return (True, None, f"Veo response had no video bytes: {json.dumps(first)[:200]}")

    async def generate(
        self, *, prompt: str, size: str, seconds: int, image_bytes: Optional[bytes] = None
    ) -> bytes:
        async with self._slot():
            operation = await asyncio.to_thread(
                self._submit, prompt=prompt, size=size, seconds=seconds, image_bytes=image_bytes
            )
            deadline = time.monotonic() + _job_timeout()
            poll = _poll_seconds()
            while True:
                if time.monotonic() > deadline:
                    raise VideoProviderError(f"Veo operation {operation} timed out")
                done, payload, error = await asyncio.to_thread(self._poll, operation)
                if done:
                    if error or payload is None:
                        raise VideoProviderError(error or "Veo produced no video")
                    return payload
                await asyncio.sleep(poll)


# ---------------------------------------------------------------------------
# Shared timing knobs (read at call time so tests can monkeypatch env)
# ---------------------------------------------------------------------------
def _timeout(kind: str) -> int:
    return {
        "submit": int(os.getenv("ORACLE_VIDEO_SUBMIT_TIMEOUT_SECONDS", "60")),
        "poll": int(os.getenv("ORACLE_VIDEO_POLL_TIMEOUT_SECONDS", "30")),
        "download": int(os.getenv("ORACLE_VIDEO_DOWNLOAD_TIMEOUT_SECONDS", "120")),
    }[kind]


def _job_timeout() -> int:
    return int(os.getenv("ORACLE_VIDEO_JOB_TIMEOUT_SECONDS", "1800"))


def _poll_seconds() -> float:
    return float(os.getenv("ORACLE_VIDEO_POLL_SECONDS", "5"))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_PROVIDERS: dict[str, type[VideoProvider]] = {
    "sora": SoraProvider,
    "veo": VeoProvider,
}

_INSTANCES: dict[str, VideoProvider] = {}


def get_provider() -> VideoProvider:
    """The configured provider (ORACLE_VIDEO_PROVIDER, default 'sora').

    Default stays 'sora' so behaviour is unchanged until an operator moves it;
    flipping the default before Veo has a billing-enabled project would turn a
    working studio into a 503.

    Instances are cached because each owns a semaphore — rebuilding per call
    would hand every request its own concurrency budget and defeat the limit.
    """
    name = (os.getenv("ORACLE_VIDEO_PROVIDER", "sora") or "sora").strip().lower()
    cached = _INSTANCES.get(name)
    if cached is not None:
        return cached
    cls = _PROVIDERS.get(name)
    provider: VideoProvider = cls() if cls is not None else UnavailableProvider(name)
    _INSTANCES[name] = provider
    return provider


def reset_provider_cache() -> None:
    """Drop cached instances. For tests that flip ORACLE_VIDEO_PROVIDER."""
    _INSTANCES.clear()
