"""The video provider seam, and the two claims it must never make.

Azure's sora-2 retires 2026-09-15, so the provider had to stop being inlined in
video_studio.py. Two defects are pinned here because both were live before the
seam existed:

1. An unconfigured provider was discovered inside the worker, so a user's daily
   quota was spent and the reel then failed — which reads as a bad generation
   rather than a deployment that was never wired.

2. `_store_video` omitted provenance/generator, so migration 0071's DEFAULT made
   the database record every fully model-generated marketing reel as
   provenance='captured'. That column's own comment says only 'captured' may
   support a claim that the media shows the actual home.
"""

import asyncio
import pathlib

import pytest

import video_providers as vp


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    # get_provider caches instances (each owns a semaphore), so a test that flips
    # the env var would otherwise see the previous test's provider.
    vp.reset_provider_cache()
    yield
    vp.reset_provider_cache()


class TestSelection:
    def test_defaults_to_sora_so_behaviour_is_unchanged(self, monkeypatch):
        # Flipping the default before Veo has a billing-enabled project would
        # turn a working studio into a 503.
        monkeypatch.delenv("ORACLE_VIDEO_PROVIDER", raising=False)
        assert vp.get_provider().name == "sora"

    def test_selects_veo_when_configured(self, monkeypatch):
        monkeypatch.setenv("ORACLE_VIDEO_PROVIDER", "veo")
        assert vp.get_provider().name == "veo"

    def test_unknown_name_fails_closed_and_names_itself(self, monkeypatch):
        monkeypatch.setenv("ORACLE_VIDEO_PROVIDER", "nope")
        provider = vp.get_provider()
        ready, why = provider.available()
        assert ready is False
        assert "nope" in why

    def test_instance_is_cached_so_the_concurrency_limit_is_real(self, monkeypatch):
        # A fresh instance per call would hand every request its own semaphore.
        monkeypatch.setenv("ORACLE_VIDEO_PROVIDER", "sora")
        assert vp.get_provider() is vp.get_provider()


class TestAvailability:
    def test_sora_names_the_missing_variable(self, monkeypatch):
        monkeypatch.delenv("ORACLE_AZURE_OPENAI_ENDPOINT", raising=False)
        ready, why = vp.SoraProvider().available()
        assert ready is False
        assert "ORACLE_AZURE_OPENAI_ENDPOINT" in why

    def test_sora_ready_when_configured(self, monkeypatch):
        monkeypatch.setenv("ORACLE_AZURE_OPENAI_ENDPOINT", "https://example.invalid")
        monkeypatch.setenv("ORACLE_SORA_DEPLOYMENT", "sora-2-estate")
        assert vp.SoraProvider().available() == (True, "")

    def test_veo_without_a_project_says_which_variable(self, monkeypatch):
        monkeypatch.delenv("ORACLE_VEO_PROJECT", raising=False)
        ready, why = vp.VeoProvider().available()
        assert ready is False
        assert "ORACLE_VEO_PROJECT" in why

    def test_veo_without_adc_says_how_to_fix_it(self, monkeypatch):
        # The reason string is forwarded verbatim to the operator, so it has to be
        # actionable rather than "unavailable".
        monkeypatch.setenv("ORACLE_VEO_PROJECT", "some-project")
        monkeypatch.setattr(vp.VeoProvider, "_adc_token", staticmethod(lambda: ""))
        ready, why = vp.VeoProvider().available()
        assert ready is False
        assert "application-default login" in why


class TestVeoRequestShape:
    """Verified against google-genai's own Vertex converter, not inferred."""

    def _submit_capture(self, monkeypatch, **kwargs):
        captured = {}

        class _Resp:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"name": "projects/p/locations/l/operations/123"}

        def _post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["body"] = json
            return _Resp()

        import requests
        monkeypatch.setattr(requests, "post", _post)
        monkeypatch.setenv("ORACLE_VEO_PROJECT", "proj")
        monkeypatch.setenv("ORACLE_VEO_LOCATION", "us-central1")
        monkeypatch.setattr(vp.VeoProvider, "_adc_token", staticmethod(lambda: "tok"))
        provider = vp.VeoProvider()
        provider._submit(**kwargs)
        return captured

    def test_pins_v1_not_v1beta1(self, monkeypatch):
        # googleapis/python-genai#2079: Veo 3.1 GA fails when routed to v1beta1.
        c = self._submit_capture(
            monkeypatch, prompt="a house", size="1280x720", seconds=8, image_bytes=None
        )
        assert "/v1/projects/" in c["url"]
        assert "v1beta1" not in c["url"]
        assert c["url"].endswith(":predictLongRunning")

    def test_maps_size_to_aspect_ratio(self, monkeypatch):
        landscape = self._submit_capture(
            monkeypatch, prompt="p", size="1280x720", seconds=8, image_bytes=None
        )
        assert landscape["body"]["parameters"]["aspectRatio"] == "16:9"
        portrait = self._submit_capture(
            monkeypatch, prompt="p", size="720x1280", seconds=8, image_bytes=None
        )
        assert portrait["body"]["parameters"]["aspectRatio"] == "9:16"

    def test_refuses_a_size_it_cannot_map(self, monkeypatch):
        monkeypatch.setenv("ORACLE_VEO_PROJECT", "proj")
        monkeypatch.setattr(vp.VeoProvider, "_adc_token", staticmethod(lambda: "tok"))
        with pytest.raises(vp.VideoProviderError, match="aspect ratio"):
            vp.VeoProvider()._submit(
                prompt="p", size="640x480", seconds=8, image_bytes=None
            )

    def test_requests_native_audio(self, monkeypatch):
        # Veo generates synchronized speech; the pipeline has no separate TTS
        # step and must not grow one.
        c = self._submit_capture(
            monkeypatch, prompt="p", size="1280x720", seconds=8, image_bytes=None
        )
        assert c["body"]["parameters"]["generateAudio"] is True

    def test_image_to_video_is_base64_inline(self, monkeypatch):
        c = self._submit_capture(
            monkeypatch, prompt="p", size="1280x720", seconds=8, image_bytes=b"JPEGDATA"
        )
        image = c["body"]["instances"][0]["image"]
        assert image["mimeType"] == "image/jpeg"
        import base64
        assert base64.b64decode(image["bytesBase64Encoded"]) == b"JPEGDATA"

    def test_no_storage_uri_so_bytes_come_back_inline(self, monkeypatch):
        # _store_video wants mp4 bytes and already owns object storage; routing
        # through GCS would add a bucket and a second credential for no gain.
        c = self._submit_capture(
            monkeypatch, prompt="p", size="1280x720", seconds=8, image_bytes=None
        )
        assert "storageUri" not in c["body"]["parameters"]


class TestVeoPolling:
    def _poll_with(self, monkeypatch, payload):
        class _Resp:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return payload

        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
        monkeypatch.setenv("ORACLE_VEO_PROJECT", "proj")
        monkeypatch.setattr(vp.VeoProvider, "_adc_token", staticmethod(lambda: "tok"))
        return vp.VeoProvider()._poll("projects/p/locations/l/operations/1")

    def test_not_done_yet(self, monkeypatch):
        assert self._poll_with(monkeypatch, {"done": False}) == (False, None, "")

    def test_returns_decoded_bytes(self, monkeypatch):
        import base64
        payload = {
            "done": True,
            "response": {
                "videos": [{"bytesBase64Encoded": base64.b64encode(b"MP4").decode()}]
            },
        }
        done, data, error = self._poll_with(monkeypatch, payload)
        assert (done, data, error) == (True, b"MP4", "")

    def test_a_safety_filter_is_reported_as_such(self, monkeypatch):
        # A finished operation with no video is usually RAI filtering. Reporting
        # it as an empty success would look like a transport bug.
        payload = {
            "done": True,
            "response": {"videos": [], "raiMediaFilteredCount": 1,
                         "raiMediaFilteredReasons": ["blocked"]},
        }
        done, data, error = self._poll_with(monkeypatch, payload)
        assert done is True and data is None
        assert "filtered" in error.lower()

    def test_operation_error_is_surfaced(self, monkeypatch):
        payload = {"done": True, "error": {"message": "quota exhausted"}}
        done, data, error = self._poll_with(monkeypatch, payload)
        assert done is True and data is None
        assert "quota exhausted" in error


class TestEnqueueGate:
    """A provider that cannot generate must refuse BEFORE quota is reserved."""

    def test_unconfigured_provider_503s_and_forwards_the_reason(self, monkeypatch):
        import video_providers
        import video_studio_api as api
        from fastapi import HTTPException

        class _Down:
            name = "veo"
            produces = "ai_generated"

            def available(self):
                return (False, "set ORACLE_VEO_PROJECT to a billing-enabled GCP project")

        monkeypatch.setattr(video_providers, "get_provider", lambda: _Down())

        # tenant_tx must NOT be reached: the whole point is refusing before the
        # quota transaction opens. If it is, this raises AssertionError instead.
        def _must_not_open(*_a, **_k):
            raise AssertionError("quota transaction opened despite an unavailable provider")

        monkeypatch.setattr(api, "tenant_tx", _must_not_open)
        monkeypatch.setattr(api, "_verify_images_owned", _noop_async)

        with pytest.raises(HTTPException) as error:
            asyncio.run(
                api.create_video_job(
                    api.VideoJobCreate(
                        kind="text", size="720x1280",
                        property={"address": "1 A St"},
                        lead_id="11111111-1111-4111-8111-111111111111",
                    ),
                    _Ctx(),
                )
            )
        assert error.value.status_code == 503
        # The operator-facing reason is forwarded verbatim, as tour_api.py does.
        assert "ORACLE_VEO_PROJECT" in str(error.value.detail)


async def _noop_async(*_a, **_k):
    return None


class _Ctx:
    tenant_id = "00000000-0000-0000-0000-000000000000"
    role = "admin"
    user_id = "11111111-1111-4111-8111-111111111111"


class TestProvenance:
    def test_a_generated_reel_is_never_recorded_as_a_capture(self):
        """The defect: _store_video omitted provenance, so migration 0071's
        DEFAULT 'captured' made the database assert a model-generated marketing
        reel was a real capture of the property."""
        for provider in (vp.SoraProvider(), vp.VeoProvider()):
            assert provider.produces == "ai_generated", provider.name

    def test_store_video_writes_provenance_and_generator(self, monkeypatch):
        import video_studio

        source = pathlib.Path(video_studio.__file__).read_text()
        insert = source[source.index("INSERT INTO property_media"):]
        insert = insert[: insert.index('"""')]
        # Asserted against the statement rather than a live DB because the row is
        # written inside tenant_tx; the claim is that the columns are supplied at
        # all, which is exactly what was missing.
        assert "provenance" in insert and "generator" in insert


class TestKlingDurationAccuracy:
    """Kling accepts 5s or 10s and nothing else.

    Oracle's ORACLE_VIDEO_CLIP_SECONDS defaults to 8, which Kling rejects. The
    tempting fix is to round 8 up to 10 — that is refused on purpose: it bills
    25% more than was asked for and returns a reel of a different length than
    the caller requested. A one-minute reel is 6 clips x 10s, stitched.
    """

    def test_accepts_the_two_lengths_kling_supports(self):
        provider = vp.FalKlingProvider()
        provider.check_seconds(5)
        provider.check_seconds(10)   # no raise

    def test_refuses_the_default_8s_rather_than_rounding(self, monkeypatch):
        provider = vp.FalKlingProvider()
        with pytest.raises(vp.VideoProviderError) as error:
            provider.check_seconds(8)
        # The message must name the fix, not just the failure.
        assert "ORACLE_VIDEO_CLIP_SECONDS" in str(error.value)
        assert "5, 10" in str(error.value)

    def test_refuses_a_60s_single_clip(self):
        # 60s is a reel length, not a clip length. Asking the vendor for it
        # would fail at the API; saying so here explains why.
        with pytest.raises(vp.VideoProviderError):
            vp.FalKlingProvider().check_seconds(60)

    def test_a_provider_with_no_constraint_accepts_anything(self):
        # Sora takes a free integer; the check must not impose Kling's rule on it.
        assert vp.SoraProvider().allowed_seconds is None
        vp.SoraProvider().check_seconds(8)   # no raise

    def test_generate_refuses_before_spending_a_request(self, monkeypatch):
        provider = vp.FalKlingProvider()
        monkeypatch.setenv("FAL_KEY", "test-key")

        def _must_not_submit(*_a, **_k):
            raise AssertionError("submitted to fal with an invalid duration")

        monkeypatch.setattr(provider, "_submit", _must_not_submit)
        with pytest.raises(vp.VideoProviderError, match="accepts clips of"):
            asyncio.run(provider.generate(prompt="p", size="1280x720", seconds=8))


class TestKlingTransport:
    def _capture_submit(self, monkeypatch, **kwargs):
        captured = {}

        class _Resp:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"request_id": "req-123"}

        def _post(url, json=None, headers=None, timeout=None):
            captured.update(url=url, body=json, headers=headers)
            return _Resp()

        import requests
        monkeypatch.setattr(requests, "post", _post)
        monkeypatch.setenv("FAL_KEY", "test-key")
        vp.FalKlingProvider()._submit(**kwargs)
        return captured

    def test_uses_fal_queue_endpoint_and_key_auth(self, monkeypatch):
        c = self._capture_submit(
            monkeypatch, prompt="a house", size="1280x720", seconds=10, image_bytes=None
        )
        assert c["url"].startswith("https://queue.fal.run/")
        assert c["headers"]["Authorization"] == "Key test-key"

    def test_duration_is_sent_as_a_string_enum(self, monkeypatch):
        # fal's schema types duration as "5"/"10", not a number.
        c = self._capture_submit(
            monkeypatch, prompt="p", size="1280x720", seconds=10, image_bytes=None
        )
        assert c["body"]["duration"] == "10"
        assert isinstance(c["body"]["duration"], str)

    def test_image_is_inlined_as_a_data_uri(self, monkeypatch):
        # Avoids uploading the property photo to third-party storage as a
        # separately-retained object.
        c = self._capture_submit(
            monkeypatch, prompt="p", size="720x1280", seconds=5, image_bytes=b"JPEGDATA"
        )
        assert c["body"]["image_url"].startswith("data:image/jpeg;base64,")
        import base64
        payload = c["body"]["image_url"].split(",", 1)[1]
        assert base64.b64decode(payload) == b"JPEGDATA"

    def test_aspect_ratio_follows_the_requested_size(self, monkeypatch):
        assert self._capture_submit(
            monkeypatch, prompt="p", size="720x1280", seconds=5, image_bytes=None
        )["body"]["aspect_ratio"] == "9:16"

    def test_a_rejected_key_says_so_rather_than_reporting_an_outage(self, monkeypatch):
        class _Resp:
            status_code = 401
            text = "unauthorized"

            @staticmethod
            def json():
                return {}

        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
        monkeypatch.setenv("FAL_KEY", "bad-key")
        with pytest.raises(vp.VideoProviderError, match="FAL_KEY"):
            vp.FalKlingProvider()._submit(
                prompt="p", size="1280x720", seconds=10, image_bytes=None
            )

    def test_availability_names_the_missing_key(self, monkeypatch):
        monkeypatch.delenv("FAL_KEY", raising=False)
        ready, why = vp.FalKlingProvider().available()
        assert ready is False and "FAL_KEY" in why

    def test_selected_by_either_name(self, monkeypatch):
        for alias in ("fal-kling", "kling"):
            vp.reset_provider_cache()
            monkeypatch.setenv("ORACLE_VIDEO_PROVIDER", alias)
            assert vp.get_provider().name == "fal-kling"


class TestSixtySecondReel:
    def test_six_ten_second_clips_reach_one_minute(self):
        """The reel-length ceiling is MAX_IMAGES x CLIP_SECONDS, because one clip
        is generated per image. 4 x 8s could only ever reach 32s."""
        import video_studio

        assert video_studio.MAX_IMAGES >= 6
        assert video_studio.MAX_IMAGES * 10 >= 60

    def test_a_60s_reel_fits_the_daily_quota_at_least_once(self):
        import video_studio

        assert video_studio.DAILY_QUOTA_SECONDS >= 60
