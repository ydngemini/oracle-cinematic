"""ElevenLabs on a live phone call — the gates that keep it off one.

This reaches real people on real calls, and the key on this deployment is an
ElevenLabs *free* tier whose licence does not permit commercial use. So the
requirement is not "remember to disable it in production" — production must not
be able to enable it by accident. Three independent gates, each closed by
default, each tested here for the failure direction rather than the happy path.

The second requirement is that no failure can silence the call. The first line
of every call is the FCC 24-17 AI disclosure; a caller who hears nothing is both
a broken call and a compliance failure, so every path through voice_tts returns
None and lets TwiML fall back to `<Say>`.
"""

from __future__ import annotations

import asyncio

import pytest

import voice_tts


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "ORACLE_ELEVENLABS_TTS_ENABLED", "ORACLE_TTS_TEST_NUMBERS",
        "ORACLE_ELEVENLABS_CHAR_BUDGET", "ORACLE_PUBLIC_BASE_URL",
        "ORACLE_ELEVENLABS_VOICE_ID", "ORACLE_ELEVENLABS_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test")
    monkeypatch.setattr(voice_tts, "budget", voice_tts._Budget())
    monkeypatch.setattr(voice_tts, "cache", voice_tts._AudioCache())


def _enable(monkeypatch, numbers="+13025550100"):
    monkeypatch.setenv("ORACLE_ELEVENLABS_TTS_ENABLED", "1")
    monkeypatch.setenv("ORACLE_TTS_TEST_NUMBERS", numbers)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://neoh.example")


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------

def test_it_is_off_unless_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("ORACLE_TTS_TEST_NUMBERS", "+13025550100")
    assert voice_tts.eligible("+13025550100") is False


def test_an_empty_allowlist_means_no_call_qualifies(monkeypatch):
    """The other reading — empty means unrestricted — is how a test feature
    ends up on a customer call."""
    monkeypatch.setenv("ORACLE_ELEVENLABS_TTS_ENABLED", "1")
    monkeypatch.setenv("ORACLE_TTS_TEST_NUMBERS", "")

    assert voice_tts.test_numbers() == frozenset()
    assert voice_tts.eligible("+13025550100") is False
    assert voice_tts.eligible("") is False


def test_a_number_off_the_allowlist_is_refused(monkeypatch):
    _enable(monkeypatch, numbers="+13025550100")
    assert voice_tts.eligible("+13025550100") is True
    assert voice_tts.eligible("+14045550199") is False


def test_no_api_key_means_no_synthesis(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert voice_tts.eligible("+13025550100") is False


# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------

def test_the_character_budget_stops_synthesis_before_it_is_spent(monkeypatch):
    """A free tier has 10,000 characters a month. A retry loop would burn the
    whole allowance in one call, and the failure would show up somewhere else."""
    _enable(monkeypatch)
    monkeypatch.setenv("ORACLE_ELEVENLABS_CHAR_BUDGET", "20")
    calls: list = []

    async def _never_called(*a, **k):
        calls.append(a)

    monkeypatch.setattr(voice_tts.aiohttp if hasattr(voice_tts, "aiohttp") else voice_tts,
                        "_unused", None, raising=False)

    first = asyncio.run(voice_tts.budget.claim(15))
    second = asyncio.run(voice_tts.budget.claim(15))
    assert first is True and second is False
    assert voice_tts.budget.used == 15

    # And synthesize() refuses rather than calling out.
    assert asyncio.run(voice_tts.synthesize("x" * 50)) is None
    assert not calls


def test_a_long_line_is_truncated_rather_than_billed_in_full(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("ORACLE_ELEVENLABS_CHAR_BUDGET", "100000")
    sent: dict = {}

    class _Response:
        status = 200

        async def read(self):
            return b"audio"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def post(self, url, **kwargs):
            sent.update(kwargs.get("json") or {})
            return _Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _Session())

    asyncio.run(voice_tts.synthesize("word " * 500))
    assert len(sent["text"]) <= voice_tts._MAX_CHARS_PER_LINE


# ---------------------------------------------------------------------------
# Nothing may silence the call
# ---------------------------------------------------------------------------

def test_a_provider_error_returns_none_rather_than_raising(monkeypatch):
    _enable(monkeypatch)

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("network down")

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", _Boom)

    assert asyncio.run(voice_tts.synthesize("hello")) is None
    assert asyncio.run(voice_tts.play_url("hello", to_number="+13025550100")) is None


def test_a_missing_public_base_url_falls_back_instead_of_serving_a_dead_link(monkeypatch):
    """Twilio fetches this URL from the public internet. A relative path would
    404 mid-call, which is worse than Polly."""
    monkeypatch.setenv("ORACLE_ELEVENLABS_TTS_ENABLED", "1")
    monkeypatch.setenv("ORACLE_TTS_TEST_NUMBERS", "+13025550100")

    assert asyncio.run(voice_tts.play_url("hello", to_number="+13025550100")) is None


def test_the_twiml_helper_emits_say_whenever_tts_declines(monkeypatch):
    import commands_api

    async def _no_url(text, *, to_number):
        return None

    monkeypatch.setattr(voice_tts, "play_url", _no_url)
    element = asyncio.run(commands_api._spoken("Hello there", to_number="+1555"))

    assert element.startswith('<Say voice="Polly.Joanna">')
    assert "Hello there" in element


def test_the_twiml_helper_emits_play_when_tts_answers(monkeypatch):
    import commands_api

    async def _url(text, *, to_number):
        return "https://neoh.example/api/commands/webhooks/twilio/tts/abc.mp3"

    monkeypatch.setattr(voice_tts, "play_url", _url)
    element = asyncio.run(commands_api._spoken("Hello", to_number="+13025550100"))

    assert element == "<Play>https://neoh.example/api/commands/webhooks/twilio/tts/abc.mp3</Play>"


def test_spoken_text_is_xml_escaped_on_both_paths(monkeypatch):
    """A property address with an ampersand must not break the TwiML document."""
    import commands_api

    async def _no_url(text, *, to_number):
        return None

    monkeypatch.setattr(voice_tts, "play_url", _no_url)
    element = asyncio.run(commands_api._spoken("Smith & Co <test>", to_number="+1555"))

    assert "&amp;" in element and "&lt;test&gt;" in element


# ---------------------------------------------------------------------------
# The audio route
# ---------------------------------------------------------------------------

def test_the_audio_token_is_derived_from_the_audio_not_the_text(monkeypatch):
    """Unauthenticated because Twilio fetches it directly. A guessable token
    would let anyone enumerate lines other callers heard."""
    _enable(monkeypatch)

    async def _audio(text, timeout=6.0):
        return b"rendered-bytes"

    monkeypatch.setattr(voice_tts, "synthesize", _audio)
    url = asyncio.run(voice_tts.play_url("hello", to_number="+13025550100"))

    import hashlib
    expected = hashlib.sha256(b"rendered-bytes").hexdigest()[:32]
    assert url.endswith(f"/{expected}.mp3")
    assert asyncio.run(voice_tts.cache.get(expected)) == b"rendered-bytes"


def test_expired_audio_is_gone_rather_than_stale(monkeypatch):
    # Both the store and the read must run on the same fake clock. Storing on
    # the real monotonic() and reading on a fixed 10_000 made the test depend on
    # host uptime: past ~2.8 hours the real store time exceeds the fake "now",
    # the age goes negative, and the entry reads as fresh. It failed on a box up
    # for 7 hours and passed after a reboot, which is the worst kind of red.
    now = 10_000.0
    monkeypatch.setattr(voice_tts.time, "monotonic", lambda: now)
    cache = voice_tts._AudioCache()
    asyncio.run(cache.put("tok", b"a"))
    assert asyncio.run(cache.get("tok")) == b"a"          # fresh before the TTL

    now = 10_000.0 + voice_tts._CACHE_TTL_SECONDS + 1
    assert asyncio.run(cache.get("tok")) is None
