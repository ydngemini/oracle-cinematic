"""A shared link must open, and must open ONLY what it was given.

The dossier used to list photo URLs pointing at `/api/media/{id}`, which needs
an agent JWT — so every link an agent sent a homeowner listed files the
recipient could not fetch. The portal read fixes that, and everything about it
is a permission decision, so the guards are asserted rather than assumed.
"""
from __future__ import annotations

import inspect

import pytest

import client_portal
from client_portal import PortalAssetScope, _portal_media_url


class TestUrlRewriting:
    def test_agent_media_urls_become_portal_urls(self):
        assert _portal_media_url("/api/media/abc-123") == "/api/portal/media/abc-123"

    def test_external_urls_are_left_alone(self):
        # A CDN or provider URL is already fetchable; rewriting it would break it.
        cdn = "https://cdn.example.com/tour.sog"
        assert _portal_media_url(cdn) == cdn

    def test_nothing_in_nothing_out(self):
        assert _portal_media_url(None) is None
        assert _portal_media_url("") is None


class TestScope:
    def test_a_tour_is_never_shared_by_default(self):
        scope = PortalAssetScope()
        assert scope.tour is False
        assert scope.media is False
        assert scope.summary is True

    def test_a_tour_is_its_own_grant_not_part_of_media(self):
        """Sharing a kitchen photo and letting someone walk the house are
        different decisions; one must not imply the other."""
        scope = PortalAssetScope(media=True)
        assert scope.tour is False

    def test_the_scope_is_a_closed_allow_list(self):
        with pytest.raises(Exception):
            PortalAssetScope(everything=True)


@pytest.fixture(scope="module")
def source():
    return inspect.getsource(client_portal.read_scoped_media)


class TestTheReadIsGuarded:
    """Four checks, each closing a different hole. Asserted structurally
    because the route needs a database; the behaviour they encode is what
    matters and it must not be quietly deleted."""

    def test_a_revoked_or_expired_link_cannot_read_bytes(self, source):
        # Revocation has to reach the file, or "revoke" means "hide the index".
        assert "revoked_at IS NULL" in source
        assert "access_expires_at > now()" in source

    def test_media_must_belong_to_this_portals_lead(self, source):
        # Without this a valid link to one property reads every file in the
        # tenant by id.
        assert "lead_id=$2::uuid" in source

    def test_a_missing_row_and_another_propertys_row_answer_identically(self, source):
        # Otherwise the difference between 403 and 404 is an id oracle.
        assert source.count('detail="Not found."') >= 1
        assert "AND lead_id=$2::uuid" in source

    def test_the_scope_gates_which_kind_may_be_read(self, source):
        assert '"tour" if row["kind"] == "splat" else "media"' in source
        assert "scope.get(needed)" in source

    def test_it_never_caches_a_private_capture_publicly(self, source):
        assert "private" in source
        assert "nosniff" in source


def test_the_dossier_offers_the_tour_through_the_portals_own_route():
    """The tour URL a client receives must be one a client can fetch."""
    source = inspect.getsource(client_portal.read_scoped_dossier)
    assert 'scope.get("tour")' in source
    assert "_portal_media_url(splat_url)" in source
    # And it carries the honesty fields, so a stand-in space is never presented
    # to a homeowner as a capture of their own home.
    assert '"is_this_property"' in source
    assert '"disclosure"' in source


class TestTheAiCanAskButNotMint:
    """A portal link is a passwordless grant to walk through someone's home.

    The AI can request one; only a human decision creates it. A tool that
    could mint the link would make its own approval decorative.
    """

    @staticmethod
    def _source():
        import ai_tools_gated
        return inspect.getsource(ai_tools_gated._share_property_tour)

    def test_the_tool_is_registered_and_gated(self):
        import ai_tools_gated
        from ai_tool_policy import TOOL_RISK
        from platform_policy import APPROVAL_REQUIRED, ActionRisk

        assert "share_property_tour" in ai_tools_gated.TOOLS_HANDLED
        risk = TOOL_RISK["share_property_tour"]
        assert risk is ActionRisk.OUTREACH
        # The artefact leaves the brokerage, so it must be approval-gated.
        assert risk in APPROVAL_REQUIRED

    def test_it_never_creates_a_link_itself(self):
        source = self._source()
        assert "create_approval" in source
        assert '"link_created": False' in source
        # It must not reach the minting path at all.
        assert "create_portal_link" not in source
        assert "INSERT INTO client_portals" not in source

    def test_it_refuses_a_property_with_no_tour_before_spending_attention(self):
        source = self._source()
        assert 'tiers.get("splat") or tiers.get("pano")' in source
        assert "Nothing was requested." in source

    def test_a_stand_in_space_is_never_offered_as_the_clients_home(self):
        source = self._source()
        assert 'tour.get("is_this_property") is False' in source

    def test_the_granted_scope_is_recorded_on_the_approval(self):
        # So the approver sees it, and it cannot widen between request and mint.
        source = self._source()
        assert '"asset_scope": {"summary": True, "media": True, "tour": True}' in source

    def test_only_the_decision_route_mints_and_it_uses_the_immutable_draft(self):
        source = inspect.getsource(client_portal.decide_tour_link)
        assert 'action_type"] != "portal.tour_link"' in source
        assert "decide_approval" in source
        # Built from draft_payload, not from anything the caller passes here,
        # so the scope shown to the approver is the scope granted.
        assert 'draft = approval["draft_payload"]' in source
        assert 'if body.decision != "approved":' in source
