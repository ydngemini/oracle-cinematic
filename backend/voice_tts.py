"""ElevenLabs speech for live Twilio calls — test numbers only.

Twilio renders `<Say voice="Polly.Joanna">` itself. Using a different voice
means handing Twilio a URL to fetch instead, so this module synthesizes the
line, caches the bytes, and hands back a `<Play>` URL that Twilio can GET from
`ORACLE_PUBLIC_BASE_URL`.

**Three gates, all of which must open, and all of which are closed by default.**
This reaches real people on real phone calls, and the key on this deployment is
an ElevenLabs *free* tier — whose licence does not permit commercial use. So the
constraint is not "remember to turn it off in production", it is that production
cannot turn it on by accident:

1. ``ORACLE_ELEVENLABS_TTS_ENABLED`` must be truthy. Default off.
2. The called number must appear in ``ORACLE_TTS_TEST_NUMBERS``. An empty list
   means no call qualifies — not "all calls qualify".
3. The month's synthesized characters must be under
   ``ORACLE_ELEVENLABS_CHAR_BUDGET`` (default 8,000 of a 10,000 free-tier
   allowance, leaving headroom). A retry loop against a paid tier is a bill; on
   a free tier it is a silent outage on whatever else uses the quota.

Every failure falls back to `<Say>`. A caller hearing Polly is a worse demo; a
caller hearing silence is a broken call, and the disclosure would go unspoken.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("oracle.voice_tts")

_API_BASE = "https://api.elevenlabs.io/v1"

# Flash is the low-latency model. A phone caller is waiting on the line, and
# ai_chat_agent already spends most of an 8-second budget on the reply text.
_DEFAULT_MODEL = "eleven_flash_v2_5"
# A *premade* voice. Free accounts are refused library voices over the API —
# measured: HTTP 402 `paid_plan_required`, "Free users cannot use library voices
# via the API". Sarah is the closest premade register to the Polly.Joanna the
# calls use today, so switching voices does not also change the persona.
_DEFAULT_VOICE = "EXAVITQu4vr4xnSDxMaL"  # Sarah — mature, reassuring, confident

_CACHE_TTL_SECONDS = 900.0
# Twilio fetches the URL moments after receiving the TwiML, so the window only
# has to outlive one call leg.
_MAX_CACHE_ENTRIES = 256
_MAX_CHARS_PER_LINE = 800


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str) -> bool:
    return _env(name).lower() in {"1", "true", "yes", "on"}


def api_key() -> str:
    return _env("ELEVENLABS_API_KEY")


def test_numbers() -> frozenset[str]:
    """Numbers permitted to hear a synthesized voice.

    Empty means none. The alternative reading — empty means unrestricted — is
    how a test feature ends up on a customer call.
    """
    raw = _env("ORACLE_TTS_TEST_NUMBERS")
    return frozenset(
        part.strip() for part in raw.replace(";", ",").split(",") if part.strip()
    )


def char_budget() -> int:
    try:
        return max(0, int(_env("ORACLE_ELEVENLABS_CHAR_BUDGET") or 8000))
    except ValueError:
        return 8000


class _Budget:
    """Characters synthesized this process, against a monthly allowance.

    Deliberately per-process and not persisted: this is a spend guard for a
    test feature, and a wrong number that under-counts is worse than one that
    resets on deploy. It cannot be the only control, which is why the number
    allowlist exists above it.
    """

    def __init__(self) -> None:
        self._used = 0
        self._lock = asyncio.Lock()

    async def claim(self, characters: int) -> bool:
        async with self._lock:
            if self._used + characters > char_budget():
                return False
            self._used += characters
            return True

    @property
    def used(self) -> int:
        return self._used


budget = _Budget()


class _AudioCache:
    """Rendered audio, held just long enough for Twilio to fetch it."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, bytes]] = {}
        self._lock = asyncio.Lock()

    async def put(self, token: str, audio: bytes) -> None:
        async with self._lock:
            now = time.monotonic()
            self._entries = {
                key: value for key, value in self._entries.items()
                if now - value[0] < _CACHE_TTL_SECONDS
            }
            if len(self._entries) >= _MAX_CACHE_ENTRIES:
                oldest = min(self._entries, key=lambda k: self._entries[k][0])
                self._entries.pop(oldest, None)
            self._entries[token] = (now, audio)

    async def get(self, token: str) -> Optional[bytes]:
        async with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                return None
            stamp, audio = entry
            if time.monotonic() - stamp >= _CACHE_TTL_SECONDS:
                self._entries.pop(token, None)
                return None
            return audio


cache = _AudioCache()


def eligible(to_number: str) -> bool:
    """Whether this call may hear a synthesized voice. All three gates."""
    if not _flag("ORACLE_ELEVENLABS_TTS_ENABLED"):
        return False
    if not api_key():
        return False
    allowed = test_numbers()
    if not allowed or str(to_number or "").strip() not in allowed:
        return False
    return True


async def synthesize(text: str, *, timeout: float = 6.0) -> Optional[bytes]:
    """Render one line to MP3, or None. Never raises.

    A voice line that fails must not take the call down — the caller still has
    to hear the AI disclosure, and `<Say>` can still deliver it.
    """
    line = " ".join(str(text or "").split())[:_MAX_CHARS_PER_LINE]
    if not line:
        return None
    if not await budget.claim(len(line)):
        logger.warning(
            "voice_tts: character budget exhausted (%d used of %d); using Say",
            budget.used, char_budget(),
        )
        return None

    voice = _env("ORACLE_ELEVENLABS_VOICE_ID") or _DEFAULT_VOICE
    model = _env("ORACLE_ELEVENLABS_MODEL") or _DEFAULT_MODEL
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API_BASE}/text-to-speech/{voice}",
                headers={"xi-api-key": api_key(), "accept": "audio/mpeg"},
                # output_format is a QUERY parameter. Sent in the body it is
                # silently ignored and the default 44.1kHz/128kbps is billed and
                # returned — four times the bytes, for a narrowband phone codec
                # that discards all of it.
                params={"output_format": "mp3_22050_32"},
                json={"text": line, "model_id": model},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status != 200:
                    body = (await response.text())[:200]
                    logger.warning(
                        "voice_tts: ElevenLabs returned %s: %s", response.status, body
                    )
                    return None
                audio = await response.read()
    except Exception as exc:  # noqa: BLE001 — a failed voice line is never fatal
        logger.warning("voice_tts: synthesis failed: %s", exc)
        return None
    return audio or None


async def play_url(text: str, *, to_number: str) -> Optional[str]:
    """A URL Twilio can `<Play>`, or None to fall back to `<Say>`."""
    if not eligible(to_number):
        return None
    public_base = _env("ORACLE_PUBLIC_BASE_URL").rstrip("/")
    if not public_base:
        # Twilio fetches this URL from the public internet. Without a base URL
        # there is nothing to hand it, and a relative path would 404 mid-call.
        logger.warning("voice_tts: ORACLE_PUBLIC_BASE_URL is unset; using Say")
        return None
    audio = await synthesize(text)
    if not audio:
        return None
    token = hashlib.sha256(audio).hexdigest()[:32]
    await cache.put(token, audio)
    return f"{public_base}/api/commands/webhooks/twilio/tts/{token}.mp3"
