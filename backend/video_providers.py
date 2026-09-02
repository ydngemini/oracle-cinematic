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
    #: Clip lengths the vendor accepts. None = any length.
    #: Declared rather than assumed because vendors differ sharply: Sora takes a
    #: free integer, Kling takes exactly 5 or 10. A provider that silently
    #: rounded to its nearest legal value would bill for a length nobody asked
    #: for and return a reel that is not the duration the caller requested.
    allowed_seconds: Optional[tuple[int, ...]] = None

    def check_seconds(self, seconds: int) -> None:
        """Raise unless this provider accepts a clip of `seconds`."""
        if self.allowed_seconds and seconds not in self.allowed_seconds:
            allowed = ", ".join(str(v) for v in self.allowed_seconds)
            raise VideoProviderError(
                f"{self.name} accepts clips of {allowed}s only — {seconds}s was requested. "
                f"Set ORACLE_VIDEO_CLIP_SECONDS to one of: {allowed}."
            )

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
# Kling via fal.ai — the commercial path that needs no cloud subscription
# ---------------------------------------------------------------------------
class FalKlingProvider(VideoProvider):
    """Kling on fal.ai.

    Chosen because it needs only an API key: no GCP project, no billing account,
    no ADC. Veo-via-Vertex is the better model on paper but is unreachable while
    every GCP billing account is closed, and a provider that cannot run is worth
    nothing on a deadline.

    Transport is fal's REST queue (the JS SDK is not used):

        POST https://queue.fal.run/{model}                              -> request_id
        GET  https://queue.fal.run/{model}/requests/{id}/status         -> IN_QUEUE | IN_PROGRESS | COMPLETED
        GET  https://queue.fal.run/{model}/requests/{id}                -> {"video": {"url": ...}}

    Auth is `Authorization: Key $FAL_KEY`.

    **Duration is 5s or 10s — nothing else.** Oracle's ORACLE_VIDEO_CLIP_SECONDS
    defaults to 8, which Kling rejects, so `allowed_seconds` refuses it up front
    rather than letting the vendor fail mid-job. We deliberately do NOT round 8
    up to 10: that would bill 25% more than asked and hand back a reel of a
    different length than the caller requested. A one-minute reel is therefore
    6 clips x 10s, stitched by the existing PyAV path.

    The finished video comes back as a URL rather than bytes, so unlike Sora and
    Veo this provider has a fetch step.
    """

    name = "fal-kling"
    produces = "ai_generated"
    #: fal queues rather than rejecting over-submission, so this is throughput
    #: shaping rather than a hard vendor cap.
    max_concurrent = 3
    allowed_seconds = (5, 10)

    @property
    def _key(self) -> str:
        return os.getenv("FAL_KEY", "").strip()

    @property
    def _model(self) -> str:
        """fal model slug. Configurable because fal versions models in the path,
        so a new Kling release is a config change rather than a code change."""
        return os.getenv(
            "ORACLE_FAL_VIDEO_MODEL", "fal-ai/kling-video/v2.5-turbo/pro/image-to-video"
        ).strip().strip("/")

    def available(self) -> tuple[bool, str]:
        if not self._key:
            return (False, "set FAL_KEY (fal.ai API key)")
        if not self._model:
            return (False, "set ORACLE_FAL_VIDEO_MODEL")
        return (True, "")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Key {self._key}", "Content-Type": "application/json"}

    def _submit(self, *, prompt: str, size: str, seconds: int, image_bytes: Optional[bytes]) -> str:
        import requests

        aspect = _SIZE_TO_ASPECT.get(size)
        if aspect is None:
            raise VideoProviderError(f"Kling has no aspect ratio for size {size!r}")

        body: dict[str, object] = {
            "prompt": prompt,
            # fal's schema types duration as a string enum, not a number.
            "duration": str(seconds),
            "aspect_ratio": aspect,
        }
        if image_bytes is not None:
            # fal accepts a data URI wherever it accepts an image URL, which
            # avoids uploading the property photo to third-party storage as a
            # separate, separately-retained object.
            body["image_url"] = (
                "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
            )
        try:
            response = requests.post(
                f"https://queue.fal.run/{self._model}",
                json=body, headers=self._headers(), timeout=_timeout("submit"),
            )
        except requests.RequestException as exc:
            raise VideoProviderError(f"fal submit failed: {exc}") from exc
        if response.status_code == 401:
            raise VideoProviderError("fal rejected FAL_KEY — rotate or check the key")
        if response.status_code not in (200, 201, 202):
            raise VideoProviderError(
                f"fal submit returned {response.status_code}: {response.text[:300]}"
            )
        request_id = (response.json() or {}).get("request_id")
        if not request_id:
            raise VideoProviderError("fal submit returned no request_id")
        return str(request_id)

    def _status(self, request_id: str) -> tuple[str, str]:
        """Return (status, error). status is fal's own enum, uppercased."""
        import requests

        url = f"https://queue.fal.run/{self._model}/requests/{request_id}/status"
        try:
            response = requests.get(url, headers=self._headers(), timeout=_timeout("poll"))
        except requests.RequestException as exc:
            raise VideoProviderError(f"fal status poll failed: {exc}") from exc
        if response.status_code != 200:
            raise VideoProviderError(
                f"fal status returned {response.status_code}: {response.text[:300]}"
            )
        body = response.json() or {}
        return str(body.get("status", "")).upper(), str(body.get("error") or "")[:400]

    def _result(self, request_id: str) -> bytes:
        import requests

        url = f"https://queue.fal.run/{self._model}/requests/{request_id}"
        try:
            response = requests.get(url, headers=self._headers(), timeout=_timeout("poll"))
        except requests.RequestException as exc:
            raise VideoProviderError(f"fal result fetch failed: {exc}") from exc
        if response.status_code != 200:
            raise VideoProviderError(
                f"fal result returned {response.status_code}: {response.text[:300]}"
            )
        payload = response.json() or {}
        video_url = ((payload.get("video") or {}) or {}).get("url")
        if not video_url:
            raise VideoProviderError(f"fal result had no video url: {json.dumps(payload)[:200]}")
        try:
            media = requests.get(video_url, timeout=_timeout("download"))
        except requests.RequestException as exc:
            raise VideoProviderError(f"fal video download failed: {exc}") from exc
        if media.status_code != 200:
            raise VideoProviderError(f"fal video download returned {media.status_code}")
        if not media.content:
            raise VideoProviderError("fal returned an empty video")
        return media.content

    async def generate(
        self, *, prompt: str, size: str, seconds: int, image_bytes: Optional[bytes] = None
    ) -> bytes:
        self.check_seconds(seconds)
        async with self._slot():
            request_id = await asyncio.to_thread(
                self._submit, prompt=prompt, size=size, seconds=seconds, image_bytes=image_bytes
            )
            deadline = time.monotonic() + _job_timeout()
            poll = _poll_seconds()
            while True:
                if time.monotonic() > deadline:
                    raise VideoProviderError(f"fal request {request_id} timed out")
                status, error = await asyncio.to_thread(self._status, request_id)
                if status == "COMPLETED":
                    break
                if status in ("FAILED", "ERROR", "CANCELLED"):
                    raise VideoProviderError(error or f"fal request {request_id} {status}")
                await asyncio.sleep(poll)
            return await asyncio.to_thread(self._result, request_id)


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
class MockProvider(VideoProvider):
    """Generates a real, playable clip locally. Bills nothing, calls nobody.

    Video Studio has a long path — quota, script drafting, per-clip generation,
    stitching, captions, storage, the property_media row — and every provider
    that could exercise it end to end costs money per run. So a whole class of
    wiring bug was only ever discoverable by paying for it, one clip at a time.

    This closes that. It is deliberately a *real* encode rather than a canned
    file: the stitcher, the caption burner and the container checks downstream
    all care about actual frames, and a placeholder that skipped them would
    leave exactly the parts most likely to break untested.

    **The output is unmistakable on sight.** Every frame is stamped MOCK with
    the prompt that produced it. A convincing-looking clip from a provider that
    generated nothing is worse than no clip: it would be indistinguishable from
    a real reel in a listing, in a review, or in a screenshot. `produces` stays
    ai_generated for the same reason — migration 0071 says only `captured` may
    support a claim about the actual home, and this shows a colour field.

    Never a fallback. It is selected explicitly with ORACLE_VIDEO_PROVIDER=mock,
    because a provider that silently substituted itself for a failing vendor
    would hand someone a stamped placeholder while they believed they had a
    finished video.
    """

    name = "mock"
    produces = "ai_generated"
    max_concurrent = 4          # nothing external to rate-limit against
    allowed_seconds = None      # no vendor constraint to model

    def available(self) -> tuple[bool, str]:
        try:
            import av  # noqa: F401
        except ImportError:
            return (False, "PyAV is not installed, so the mock provider cannot encode")
        return (True, "")

    async def generate(
        self, *, prompt: str, size: str, seconds: int, image_bytes: Optional[bytes] = None
    ) -> bytes:
        return await asyncio.to_thread(self._encode, prompt, size, seconds)

    @staticmethod
    def _encode(prompt: str, size: str, seconds: int) -> bytes:
        import io

        import av
        from PIL import Image, ImageDraw

        try:
            width, height = (int(v) for v in str(size).lower().split("x", 1))
        except (TypeError, ValueError):
            width, height = 1280, 720
        # H.264 requires even dimensions; an odd size fails inside the encoder
        # with a message that says nothing about the size.
        width, height = max(2, width - width % 2), max(2, height - height % 2)

        fps = 24
        frames = max(1, int(seconds) * fps)
        label = (prompt or "").strip()[:60] or "mock clip"

        buffer = io.BytesIO()
        with av.open(buffer, mode="w", format="mp4") as container:
            stream = container.add_stream("libx264", rate=fps)
            stream.width, stream.height = width, height
            stream.pix_fmt = "yuv420p"

            for index in range(frames):
                # A visibly moving gradient, so a stuck or dropped-frame bug in
                # the stitcher is obvious rather than hidden behind a still.
                shade = int(255 * (index / max(1, frames - 1)))
                image = Image.new("RGB", (width, height), (shade // 3, shade // 2, shade))
                draw = ImageDraw.Draw(image)
                draw.text((16, 16), "MOCK - NOT A REAL GENERATION", fill=(255, 255, 255))
                draw.text((16, 40), label, fill=(255, 255, 255))
                draw.text((16, 64), f"frame {index + 1}/{frames}", fill=(255, 255, 255))
                for packet in stream.encode(av.VideoFrame.from_image(image)):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)

        return buffer.getvalue()


class ElevenLabsVideoProvider(VideoProvider):
    """Video generation on ElevenLabs.

    ElevenLabs aggregates other labs' video models rather than training its own,
    so this reaches Veo 3.1 and Seedance through a key you already hold for TTS.
    That is the whole reason it earns a slot next to FalKlingProvider: it gets
    Veo WITHOUT a GCP project, billing account or ADC, which is precisely the
    wall VeoProvider has been stuck behind.

    Transport is submit-and-poll, the same shape as fal:

        POST /v1/flows/video                 -> {"id": ..., "status": "pending"}
        GET  /v1/flows/video/{id}            -> content_url when complete

    Auth is the `xi-api-key` header, shared with voice_tts.

    **Durations are 4, 6 and 8 seconds** — note this is a DIFFERENT constraint
    from Kling's 5-or-10, and it happens to include Oracle's own
    ORACLE_VIDEO_CLIP_SECONDS default of 8. Switching here means that default
    stops needing an override.

    ByteDance (Seedance) is disabled by default for API requests and needs
    explicit approval from ElevenLabs, so the practical model is Veo.

    ⚠ API video generation requires a **Pro plan or above**. `available()`
    checks the plan rather than only the key, because a free-tier key
    authenticates perfectly and then fails at generation time — the failure this
    codebase keeps getting bitten by, where a config problem reports as
    something else. The tier check turns that into an accurate refusal up front.
    """

    name = "elevenlabs"
    produces = "ai_generated"
    max_concurrent = 2
    #: ElevenLabs' documented set. 8 is Oracle's own default.
    allowed_seconds = (4, 6, 8)

    _BASE = "https://api.elevenlabs.io/v1"
    #: Plans that may call the video API. A free key authenticates fine and only
    #: fails when a job is submitted, so this is checked before we promise.
    _PAID_TIERS = frozenset({
        "starter", "creator", "pro", "scale", "business", "enterprise", "growing_business",
    })

    @property
    def _key(self) -> str:
        return os.getenv("ELEVENLABS_API_KEY", "").strip()

    @property
    def _model(self) -> str:
        """Configurable: ElevenLabs versions models in this field, so a newer
        Veo is a config change rather than a code change."""
        return os.getenv(
            "ORACLE_ELEVENLABS_VIDEO_MODEL", "veo-3.1-fast-generate-001"
        ).strip()

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self._key, "Content-Type": "application/json"}

    def available(self) -> tuple[bool, str]:
        if not self._key:
            return (False, "set ELEVENLABS_API_KEY")
        if not self._model:
            return (False, "set ORACLE_ELEVENLABS_VIDEO_MODEL")
        tier = self._tier()
        if tier is None:
            # Network or auth problem. Do not claim unavailable on a transient
            # failure — say what could not be established.
            return (False, "could not read the ElevenLabs subscription tier")
        if tier.lower() not in self._PAID_TIERS:
            return (
                False,
                f"ElevenLabs video needs a paid plan; this key is on '{tier}'. "
                "Upgrade at elevenlabs.io/pricing, or use ORACLE_VIDEO_PROVIDER=fal-kling.",
            )
        return (True, "")

    def _tier(self) -> Optional[str]:
        import requests

        try:
            r = requests.get(
                f"{self._BASE}/user/subscription",
                headers={"xi-api-key": self._key},
                timeout=_timeout("poll"),
            )
            r.raise_for_status()
            return str((r.json() or {}).get("tier") or "")
        except Exception:  # noqa: BLE001 - caller turns None into an honest message
            return None

    def _submit(self, *, prompt: str, size: str, seconds: int, image_bytes: Optional[bytes]) -> str:
        import base64

        import requests

        aspect = _SIZE_TO_ASPECT.get(size)
        if aspect is None:
            raise VideoProviderError(f"ElevenLabs has no aspect ratio for size {size!r}")

        body: dict[str, object] = {
            "model_id": self._model,
            "prompt": prompt,
            "duration_secs": seconds,
            "aspect_ratio": aspect,
            # Native audio, so a reel does not need a separate voiceover pass.
            "generate_audio": True,
        }
        if image_bytes:
            body["image"] = base64.b64encode(image_bytes).decode("ascii")

        r = requests.post(
            f"{self._BASE}/flows/video", headers=self._headers(),
            json=body, timeout=_timeout("submit"),
        )
        if r.status_code in (401, 403):
            raise VideoProviderError(
                "ElevenLabs rejected the key for video. Video generation requires a "
                "paid plan even when the same key works for text-to-speech."
            )
        r.raise_for_status()
        job_id = (r.json() or {}).get("id")
        if not job_id:
            raise VideoProviderError("ElevenLabs returned no job id")
        return str(job_id)

    def _poll(self, job_id: str) -> tuple[str, Optional[str], str]:
        """(status, content_url, error)."""
        import requests

        r = requests.get(
            f"{self._BASE}/flows/video/{job_id}",
            headers={"xi-api-key": self._key}, timeout=_timeout("poll"),
        )
        r.raise_for_status()
        data = r.json() or {}
        return (
            str(data.get("status") or "").lower(),
            data.get("content_url"),
            str(data.get("error") or ""),
        )

    def _download(self, url: str) -> bytes:
        import requests

        r = requests.get(url, timeout=_timeout("download"))
        r.raise_for_status()
        return r.content

    async def generate(
        self, *, prompt: str, size: str, seconds: int, image_bytes: Optional[bytes] = None
    ) -> bytes:
        self.check_seconds(seconds)
        async with self._slot():
            job_id = await asyncio.to_thread(
                self._submit, prompt=prompt, size=size, seconds=seconds, image_bytes=image_bytes
            )
            deadline = time.monotonic() + _job_timeout()
            poll = _poll_seconds()
            while True:
                if time.monotonic() > deadline:
                    raise VideoProviderError(f"ElevenLabs job {job_id} timed out")
                status, content_url, error = await asyncio.to_thread(self._poll, job_id)
                if status in ("complete", "completed", "succeeded") and content_url:
                    break
                if status in ("failed", "error", "cancelled"):
                    raise VideoProviderError(error or f"ElevenLabs job {job_id} {status}")
                await asyncio.sleep(poll)
            # Like fal and unlike Sora, the result is a signed URL, not bytes.
            return await asyncio.to_thread(self._download, content_url)


_PROVIDERS: dict[str, type[VideoProvider]] = {
    # Explicit selection only — never a fallback for a failing vendor.
    "mock": MockProvider,
    "sora": SoraProvider,
    "veo": VeoProvider,
    "fal-kling": FalKlingProvider,
    "kling": FalKlingProvider,   # convenience alias
    "elevenlabs": ElevenLabsVideoProvider,
    "11labs": ElevenLabsVideoProvider,   # convenience alias
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
