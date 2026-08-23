"""Licensed exterior imagery, and the claims it must never make.

The failure this guards against is specific: the easy way to get a photo of a
house is to take it from a listing portal, and docs/data-access-tiers.md records
that as prohibited — both sites' terms forbid it, CoStar v. Zillow is live
litigation over reuse of listing photos, and the photos are copyrighted works
owned by the photographer. This module exists so the licensed routes are the
only routes.

The second failure is subtler and is what most of these tests cover: street-level
imagery is EXTERIOR ONLY. A streetside frame presented as though it showed the
interior is the same class of defect as a generated video stored as 'captured'.
"""

import asyncio

import pytest

from data_integrations.base import DataIntegrationError
from data_integrations.property_imagery import (
    ImageryAuthError,
    ImageryConfigurationError,
    PropertyImagerySource,
)


def _src(monkeypatch, *, google="", mapillary=""):
    monkeypatch.setenv("GOOGLE_STREETVIEW_API_KEY", google)
    monkeypatch.setenv("MAPILLARY_TOKEN", mapillary)
    return PropertyImagerySource()


class TestReadiness:
    def test_unconfigured_names_both_options_and_the_key_restriction(self, monkeypatch):
        ready, why = _src(monkeypatch).available()
        assert ready is False
        # The referrer trap is the most common cause of a 403 here, so the
        # reason has to name it rather than say "unavailable".
        assert "GOOGLE_STREETVIEW_API_KEY" in why and "referrer" in why

    def test_either_provider_is_enough(self, monkeypatch):
        assert _src(monkeypatch, google="k").available() == (True, "")
        assert _src(monkeypatch, mapillary="t").available() == (True, "")

    def test_unconfigured_raises_a_config_error_not_an_empty_result(self, monkeypatch):
        # "Nothing configured" and "no imagery here" are different facts; an
        # empty list for the first would read as a finding about the property.
        with pytest.raises(ImageryConfigurationError):
            asyncio.run(_src(monkeypatch).fetch(address="1 Main St"))


class TestExteriorOnly:
    def _google_ok(self, monkeypatch):
        src = _src(monkeypatch, google="k")

        async def _json(url, timeout=None):
            return {"status": "OK", "pano_id": "PANO1",
                    "location": {"lat": 39.1, "lng": -75.5}, "date": "2025-04"}

        monkeypatch.setattr(src, "_get_json", _json)
        return src

    def test_every_result_is_marked_exterior(self, monkeypatch):
        out = asyncio.run(self._google_ok(monkeypatch).fetch(address="1 Main St"))
        assert out["exterior_only"] is True
        assert out["images"] and all(i["interior"] is False for i in out["images"])

    def test_no_result_ever_claims_to_be_interior(self, monkeypatch):
        # The load-bearing assertion: a streetside frame must never be usable as
        # evidence of what the inside looks like.
        out = asyncio.run(self._google_ok(monkeypatch).fetch(address="1 Main St"))
        assert not any(i["interior"] for i in out["images"])

    def test_attribution_is_always_present(self, monkeypatch):
        # Google requires attribution and Mapillary is CC-BY-SA; rendering the
        # image without it is out of licence, so it can never be empty.
        out = asyncio.run(self._google_ok(monkeypatch).fetch(address="1 Main St"))
        assert all(i["attribution"].strip() for i in out["images"])

    def test_camera_position_is_not_reported_as_the_property(self, monkeypatch):
        # Street View's location is where the car was, a street's width from the
        # house. The field is named for the camera so it cannot be mistaken.
        img = asyncio.run(self._google_ok(monkeypatch).fetch(address="1 Main St"))["images"][0]
        assert "captured_lat" in img and "lat" not in img


class TestCoverageVersusFailure:
    def test_no_coverage_is_reported_honestly_not_as_an_error(self, monkeypatch):
        src = _src(monkeypatch, google="k")

        async def _json(url, timeout=None):
            return {"status": "ZERO_RESULTS"}

        monkeypatch.setattr(src, "_get_json", _json)
        out = asyncio.run(src.fetch(address="1 Nowhere Rd"))
        assert out["matched"] is False
        assert out["images"] == []
        # And it must point at the only real routes to interior photos.
        assert "MLS" in out["reason"] or "owner" in out["reason"]

    def test_a_rejected_key_is_not_reported_as_no_coverage(self, monkeypatch):
        """REQUEST_DENIED means the credential is wrong, which is permanent
        until fixed. Returning 'no imagery' would send someone looking for a
        property problem instead of a key problem."""
        src = _src(monkeypatch, google="k")

        async def _json(url, timeout=None):
            return {"status": "REQUEST_DENIED"}

        monkeypatch.setattr(src, "_get_json", _json)
        with pytest.raises(ImageryAuthError, match="referrer"):
            asyncio.run(src.fetch(address="1 Main St"))

    def test_blank_address_is_refused(self, monkeypatch):
        with pytest.raises(DataIntegrationError):
            asyncio.run(_src(monkeypatch, google="k").fetch(address="   "))


class TestMapillaryFallback:
    def test_only_consulted_when_street_view_found_nothing(self, monkeypatch):
        src = _src(monkeypatch, google="k", mapillary="t")
        calls = []

        async def _json(url, timeout=None):
            calls.append(url)
            if "streetview" in url:
                return {"status": "OK", "pano_id": "P", "location": {"lat": 1, "lng": 2}}
            raise AssertionError("Mapillary queried despite Street View coverage")

        monkeypatch.setattr(src, "_get_json", _json)
        out = asyncio.run(src.fetch(address="1 Main St", lat=1.0, lng=2.0))
        assert out["images"][0]["source"] == "google_streetview"

    def test_used_where_street_view_has_no_car(self, monkeypatch):
        src = _src(monkeypatch, google="k", mapillary="t")

        async def _json(url, timeout=None):
            if "streetview" in url:
                return {"status": "ZERO_RESULTS"}
            return {"data": [{"id": "m1", "thumb_1024_url": "https://img/x.jpg",
                              "captured_at": "2024-06-01",
                              "geometry": {"coordinates": [-75.5, 39.1]},
                              "creator": {"username": "someone"}}]}

        monkeypatch.setattr(src, "_get_json", _json)
        out = asyncio.run(src.fetch(address="1 Main St", lat=39.1, lng=-75.5))
        assert out["matched"] is True
        img = out["images"][0]
        assert img["source"] == "mapillary"
        # CC-BY-SA: the contributor must be credited by name.
        assert "someone" in img["attribution"] and "CC BY-SA" in img["attribution"]
        assert img["interior"] is False

    def test_skipped_without_coordinates(self, monkeypatch):
        # Mapillary is a bbox search; with no lat/lng there is nothing to search.
        src = _src(monkeypatch, mapillary="t")
        out = asyncio.run(src.fetch(address="1 Main St"))
        assert out["matched"] is False
