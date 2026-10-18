"""The generative UI generates arrangement, never content.

Everything in neoh_render is a pure transform, so the wording that matters —
the caveat on an uncertain number, the three different reasons a call queue can
be empty — is pinned here without a database.
"""

from __future__ import annotations

import pytest

import neoh_render as render


def _briefing(opportunities, *, portfolio=None, **extra):
    return {
        "attention": {
            "opportunities": opportunities,
            "portfolio": portfolio if portfolio is not None else {
                "total": 0, "caveat": "Priors, not this brokerage's outcomes.", "calibrated": False,
            },
        },
        **extra,
    }


def _opportunity(subject, ev, *, action_type="call", **extra):
    return {
        "kind": "follow_up", "subject": subject, "subject_id": f"id-{subject}",
        "subject_type": "client", "headline": f"{subject} went quiet",
        "why": "no contact in 21 days", "recommended_action": "Call them",
        "confidence": 0.6, "action_type": action_type,
        "economics": {"expected_value": ev}, **extra,
    }


class TestVocabulary:
    def test_a_block_outside_the_vocabulary_cannot_be_built(self):
        """The closed list is enforced where blocks are made, not where they
        are drawn — the frontend skipping an unknown primitive is the second
        line of defence, not the first."""
        with pytest.raises(ValueError):
            render.block("freeform_html", content="<h1>anything</h1>")
        assert render.block("metric", label="x")["primitive"] == "metric"

    def test_every_renderer_emits_only_known_primitives(self):
        out = render.who_to_call(_briefing([_opportunity("Sarah", 900.0)]))
        assert all(b["primitive"] in render.PRIMITIVES for b in out["blocks"])


class TestWhoToCall:
    def test_it_ranks_by_the_briefings_own_expected_value(self):
        """Ranking twice in one product would put the same card in two places."""
        out = render.who_to_call(_briefing([
            _opportunity("Marcus", 120.0),
            _opportunity("Sarah", 980.0),
            _opportunity("Dana", 400.0),
        ]))
        queue = next(b for b in out["blocks"] if b["primitive"] == "call_queue")
        assert [i["subject"] for i in queue["props"]["items"]] == ["Sarah", "Dana", "Marcus"]
        assert [i["rank"] for i in queue["props"]["items"]] == [1, 2, 3]
        assert out["spoken"].startswith("Sarah first")

    def test_the_queue_has_a_ceiling(self):
        out = render.who_to_call(_briefing([
            _opportunity(f"P{i}", float(i)) for i in range(20)
        ]))
        queue = next(b for b in out["blocks"] if b["primitive"] == "call_queue")
        assert len(queue["props"]["items"]) == render.MAX_QUEUE

    def test_an_opportunity_you_cannot_reach_out_on_is_not_in_a_call_queue(self):
        out = render.who_to_call(_briefing([
            _opportunity("Paperwork", 500.0, action_type="task"),
        ]))
        assert not any(b["primitive"] == "call_queue" for b in out["blocks"])

    def test_the_uncertainty_travels_as_a_prop_not_as_prose(self):
        """A number this uncertain shown bare is the false precision the
        expected-value module exists to refuse, so the caveat is a prop the
        renderer cannot drop by forgetting to concatenate it."""
        out = render.who_to_call(_briefing(
            [_opportunity("Sarah", 900.0)],
            portfolio={"total": 900.0, "caveat": "Priors, not outcomes.", "calibrated": False},
        ))
        metric = next(b for b in out["blocks"] if b["primitive"] == "metric")
        assert metric["props"]["caveat"] == "Priors, not outcomes."
        assert metric["props"]["calibrated"] is False

    def test_the_three_reasons_for_an_empty_queue_read_differently(self):
        """"Nobody to call" is a fact; "a detector broke" is an incomplete
        answer; "held back as too uncertain" is a third thing. Rendering them
        with one sentence would teach the agent to distrust all three."""
        quiet = render.who_to_call(_briefing([]))["spoken"]
        broken = render.who_to_call(_briefing([], detectors_failed=["distress"]))["spoken"]
        shy = render.who_to_call(_briefing([], suppressed_low_confidence=4))["spoken"]

        assert len({quiet, broken, shy}) == 3
        assert "detector" in broken and "incomplete" in broken
        assert "4" in shy and "uncertain" in shy
        assert "detector" not in quiet and "uncertain" not in quiet


class TestPerson:
    def test_a_contradiction_is_surfaced_as_the_question_that_resolves_it(self):
        out = render.person(
            {"id": "c1", "full_name": "Sarah Chen", "client_type": "buyer", "stage": "active"},
            {
                "latent": {"summary": "Says browsing, acts ready.", "confidence": 0.62},
                "disputes": [{"question": "Is the budget 400k or 550k?", "predicate": "budget"}],
            },
            [{"label": "Called", "at": None, "done": True}],
        )
        assert out["spoken"] == "Says browsing, acts ready."
        evidence = next(b for b in out["blocks"] if b["primitive"] == "evidence")
        assert evidence["props"]["items"][0]["label"] == "Is the budget 400k or 550k?"

    def test_more_than_one_match_is_a_question_not_a_guess(self):
        out = render.person_choices(
            [{"id": "a", "label": "Sarah Chen"}, {"id": "b", "label": "Sarah Chen"}], "sarah",
        )
        block = out["blocks"][0]
        assert block["primitive"] == "comparison"
        assert len(block["props"]["options"]) == 2
        assert "2 people match" in out["spoken"]


class TestDeal:
    def test_the_blocker_is_the_earliest_open_milestone(self):
        out = render.deal_blocker(
            {"id": "d1", "property_address": "12 Main St", "status": "under_contract"},
            [
                {"title": "Appraisal", "due_at": "2026-10-01", "completed_at": None},
                {"title": "Inspection", "due_at": "2026-09-05", "completed_at": None},
                {"title": "Offer", "due_at": "2026-08-01", "completed_at": "2026-08-01"},
            ],
        )
        assert "Inspection" in out["spoken"]
        deal = next(b for b in out["blocks"] if b["primitive"] == "deal")
        assert deal["props"]["open_count"] == 2 and deal["props"]["total_count"] == 3

    def test_no_milestones_is_not_the_same_as_all_done(self):
        """One is a gap in the file, the other is a fact about the deal."""
        empty = render.deal_blocker({"id": "d", "property_address": "12 Main"}, [])
        done = render.deal_blocker(
            {"id": "d", "property_address": "12 Main"},
            [{"title": "Closing", "completed_at": "2026-08-01"}],
        )
        assert "gap in the file" in empty["spoken"]
        assert "every milestone is done" in done["spoken"].lower()
        assert empty["spoken"] != done["spoken"]


class TestProperties:
    def test_an_empty_shortlist_explains_itself(self):
        out = render.properties_for("Sarah", [])
        assert "shortlist" in out["spoken"] and out["blocks"] == []

    def test_the_shortlist_is_capped_and_priced(self):
        out = render.properties_for("Sarah", [
            {"id": str(i), "address": f"{i} Main St", "price": 350000 + i} for i in range(9)
        ])
        options = out["blocks"][0]["props"]["options"]
        assert len(options) == render.MAX_COMPARISON
        assert "$350,000" in options[0]["detail"]

    # An exact owner-name hit in public records (client_ai_automation.
    # _property_candidates) is review-only: nobody has confirmed the record is
    # this client's, so it is never said as their shortlist (T11.03, F11.04).
    REVIEW_ONLY = {"public_record_id": "r1", "address": "9 Elm St", "owner_name": "SARAH CHEN",
                   "match_basis": "exact owner name", "requires_review": True}

    def test_review_only_owner_name_hits_are_possible_matches_not_a_shortlist(self):
        out = render.properties_for("Sarah Chen", [self.REVIEW_ONLY], {"status": "checked"})
        option = out["blocks"][0]["props"]["options"][0]
        assert "possible match" in out["spoken"].lower()
        assert "shortlist" not in out["spoken"]
        assert "possible match" in out["blocks"][0]["props"]["title"].lower()
        assert option["detail"].startswith("Possible match by owner name")
        assert option["id"] == "r1"
        # An unconfirmed record is not linked to a property page.
        assert option["href"] is None

    def test_review_only_hits_are_possible_matches_in_spanish_too(self):
        zip_basis = dict(self.REVIEW_ONLY, public_record_id="r2",
                         match_basis="exact owner name and target ZIP")
        out = render.properties_for("Sarah Chen", [self.REVIEW_ONLY, zip_basis], {"status": "checked"}, "es")
        options = out["blocks"][0]["props"]["options"]
        assert out["spoken"].startswith("2 posibles coincidencias para Sarah Chen")
        assert "lista" not in out["spoken"]
        assert options[0]["detail"].startswith("Posible coincidencia por nombre del propietario (")
        assert options[1]["detail"].endswith(") y código postal objetivo")
        assert [o["id"] for o in options] == ["r1", "r2"]

    def test_a_mixed_list_counts_the_shortlist_and_the_possible_matches_apart(self):
        out = render.properties_for("Sarah", [
            {"id": "p1", "address": "12 Oak St", "price": 425000}, self.REVIEW_ONLY,
        ], {"status": "checked"})
        assert out["spoken"].startswith("1 on Sarah's shortlist.")
        assert "1 possible match for Sarah" in out["spoken"]
        options = out["blocks"][0]["props"]["options"]
        assert options[0]["href"] == "/property/p1" and "possible" not in options[0]["detail"].lower()
        assert options[1]["href"] is None and "possible match" in options[1]["detail"].lower()

    def test_a_review_only_hit_with_no_recorded_basis_does_not_invent_one(self):
        bare = {k: v for k, v in self.REVIEW_ONLY.items() if k != "match_basis"}
        option = render.properties_for("Sarah Chen", [bare], {"status": "checked"})["blocks"][0]["props"]["options"][0]
        assert option["detail"] == "Possible match"

    @staticmethod
    def _record(n, **over):
        """Every key client_ai_automation._property_candidates writes."""
        return {
            "public_record_id": f"r{n}", "parcel_id": f"P-{n}", "address": f"{n} Elm St",
            "city": "Austin", "state": "TX", "zip_code": "78701", "owner_name": "SARAH CHEN",
            "source_name": "county", "record_refreshed_at": "2026-10-01T00:00:00+00:00",
            "match_basis": "exact owner name", "requires_review": True, **over,
        }

    def test_every_possible_match_is_counted_not_only_the_ones_shown(self):
        """The producer stores up to 5 (LIMIT 5); the panel shows 3. The count
        is of the records, and the answer says when only some are shown."""
        five = [self._record(n) for n in range(1, 6)]
        out = render.properties_for("Sarah Chen", five, {"status": "checked", "matched": 5})
        assert len(out["blocks"][0]["props"]["options"]) == render.MAX_COMPARISON
        # Five is the producer's cap, so it is said as a floor (L11-07).
        assert out["spoken"].startswith("At least 5 possible matches for Sarah Chen in public records")
        assert "Showing 3 of 5 here." in out["spoken"]
        es = render.properties_for("Sarah Chen", five, {"status": "checked", "matched": 5}, "es")
        assert es["spoken"].startswith("Al menos 5 posibles coincidencias para Sarah Chen")
        assert "Aquí ves 3 de 5." in es["spoken"]

    def test_below_the_producers_cap_the_count_is_exact(self):
        four = [self._record(n) for n in range(1, 5)]
        out = render.properties_for("Sarah Chen", four, {"status": "checked", "matched": 4})
        assert out["spoken"].startswith("4 possible matches for Sarah Chen in public records")

    def test_the_renderers_cap_is_the_producers(self):
        import client_ai_automation

        assert render.PRODUCER_CANDIDATE_LIMIT == client_ai_automation.PROPERTY_CANDIDATE_LIMIT

    def test_a_mixed_list_counts_every_possible_match_too(self):
        out = render.properties_for("Sarah", [
            {"id": "p1", "address": "12 Oak St"}, {"id": "p2", "address": "14 Oak St"},
            *[self._record(n) for n in range(1, 5)],
        ], {"status": "checked"})
        assert out["spoken"].startswith("2 on Sarah's shortlist. 4 possible matches for Sarah")
        assert "Showing 1 of 4 here." in out["spoken"]

    def test_all_possible_matches_shown_adds_no_showing_note(self):
        out = render.properties_for("Sarah Chen", [self._record(1), self._record(2)], {"status": "checked"})
        assert "Showing" not in out["spoken"]

    def test_the_owner_name_basis_is_not_called_exact(self):
        """_property_candidates matches on the name lower-cased with everything
        outside a-z and 0-9 removed, so "Ann Avery" also finds "ANNA VERY" and
        accented letters are dropped. That is not an exact match."""
        both = [self._record(1), self._record(2, match_basis="exact owner name and target ZIP")]
        en = render.properties_for("Sarah Chen", both, {"status": "checked"})["blocks"][0]["props"]["options"]
        es = render.properties_for("Sarah Chen", both, {"status": "checked"}, "es")["blocks"][0]["props"]["options"]
        for option in en + es:
            assert "exact" not in option["detail"].lower(), option["detail"]
        assert en[0]["detail"].endswith(
            "Possible match by owner name (ignoring case, spaces, punctuation and letters outside a–z)")
        assert en[1]["detail"].endswith(
            "Possible match by owner name (ignoring case, spaces, punctuation and letters outside a–z) "
            "and target ZIP")
        assert es[0]["detail"].endswith(
            "Posible coincidencia por nombre del propietario "
            "(sin distinguir mayúsculas, espacios, signos ni letras fuera de la a–z)")
        assert es[1]["detail"].endswith(
            "Posible coincidencia por nombre del propietario "
            "(sin distinguir mayúsculas, espacios, signos ni letras fuera de la a–z) y código postal objetivo")

    @pytest.mark.parametrize("key", ["id", "property_id"])
    def test_a_review_only_hit_is_never_linked_even_with_a_property_id(self, key):
        """Whatever a writer adds, an unconfirmed record does not open a
        property page."""
        option = render.properties_for(
            "Sarah Chen", [self._record(1, **{key: "p9"})], {"status": "checked"},
        )["blocks"][0]["props"]["options"][0]
        assert option["href"] is None

    def test_same_street_records_are_told_apart_by_place(self):
        """No target ZIPs: the lookup spans the dataset, so one street name can
        come back from two cities."""
        austin = self._record(1, address="9 Elm St")
        dallas = self._record(2, address="9 Elm St", city="Dallas", zip_code="75201")
        options = render.properties_for("Sarah Chen", [austin, dallas], {"status": "checked"})[
            "blocks"][0]["props"]["options"]
        assert options[0]["detail"].startswith("Austin, TX 78701 · Parcel P-1 · Possible match by")
        assert options[1]["detail"].startswith("Dallas, TX 75201 · Parcel P-2 · Possible match by")
        es = render.properties_for("Sarah Chen", [austin], {"status": "checked"}, "es")[
            "blocks"][0]["props"]["options"][0]
        assert es["detail"].startswith("Austin, TX 78701 · Parcela P-1 · Posible coincidencia por")

    def test_two_parcels_at_one_street_address_are_told_apart_by_parcel(self):
        """Condo parcels share a street address and city (L11-07)."""
        a = self._record(1, address="100 Main St", parcel_id="P-100-A")
        b = self._record(2, address="100 Main St", parcel_id="P-100-B")
        options = render.properties_for("Sarah Chen", [a, b], {"status": "checked"})["blocks"][0]["props"]["options"]
        assert options[0]["detail"] != options[1]["detail"]
        assert "Parcel P-100-A" in options[0]["detail"] and "Parcel P-100-B" in options[1]["detail"]

    def test_a_record_with_no_street_address_is_labelled_by_its_parcel(self):
        no_street = self._record(1, address=None, parcel_id="12-345-678")
        neither = self._record(2, address=None, parcel_id=None, city=None, state=None, zip_code=None)
        options = render.properties_for("Sarah Chen", [no_street, neither], {"status": "checked"})[
            "blocks"][0]["props"]["options"]
        assert options[0]["label"] == "Parcel 12-345-678"
        assert options[1]["label"] == "Address unavailable"
        # No place on record: none is invented.
        assert options[1]["detail"].startswith("Possible match by")
        es = render.properties_for("Sarah Chen", [no_street], {"status": "checked"}, "es")[
            "blocks"][0]["props"]["options"][0]
        assert es["label"] == "Parcela 12-345-678"


class TestLinkedHouses:
    """T11.03 / F11.04: the houses linked to the client, with the broker
    owner's decision read as Houses reads it (neoh_intents._linked_houses_for).

    A linked client has no stored candidates (the producer stores [] with
    status not_applicable once a house is linked), so Neoh used to answer
    "Nothing is on {name}'s shortlist yet" about a client whose house is on
    record. Only a current confirmation reads verified; who decided is never
    shown."""

    AT = "2026-10-17T16:00:00+00:00"

    @classmethod
    def _house(cls, n, state="none", *, verified=None, kind="lead", **over):
        lead = f"lead-{n}" if kind == "lead" else None
        return {
            "kind": kind, "id": lead or f"listing-{n}", "lead_id": lead,
            "address": f"{n} Elm St", "parcel_id": f"P-{n}" if kind == "lead" else None,
            "state": "TX" if kind == "lead" else None,
            "verified": (state == "confirmed") if verified is None else verified,
            "confirmation": {"state": state, "decided_at": None if state == "none" else cls.AT},
            **over,
        }

    def _ask(self, houses, *, total=None, candidates=(), lookup=None, lang=None):
        return render.properties_for(
            "Sarah Chen", list(candidates), lookup if lookup is not None else {"status": "not_applicable"},
            lang, linked=houses, linked_total=len(houses) if total is None else total)

    def test_a_confirmed_linked_house_reads_verified(self):
        out = self._ask([self._house(1, "confirmed")])
        assert out["intent"] == "properties_for"
        assert out["spoken"] == "1 house is linked to Sarah Chen. It is confirmed as theirs."
        block = out["blocks"][0]["props"]
        assert block["title"] == "Houses linked to Sarah Chen"
        assert block["options"] == [{
            "id": "lead-1", "label": "1 Elm St",
            "detail": "Parcel P-1 · Confirmed as Sarah Chen's house · 2026-10-17",
            "href": "/property/lead-1", "kind": "linked_house", "verified": True,
            "confirmation": {"state": "confirmed", "decided_at": self.AT},
        }]

    @pytest.mark.parametrize("state,detail,spoken", [
        ("none", "Linked to Sarah Chen · not confirmed as theirs", "It is not confirmed as theirs."),
        ("out_of_date", "Linked to Sarah Chen · not confirmed as theirs", "It is not confirmed as theirs."),
        ("rejected", "Linked to Sarah Chen · not confirmed as theirs · a public record on its parcel was "
                     "marked not their house",
         "It is not confirmed as theirs, and a public record on its parcel was marked not their house."),
    ])
    def test_anything_but_a_current_confirmation_is_not_verified(self, state, detail, spoken):
        out = self._ask([self._house(1, state)])
        assert out["spoken"] == f"1 house is linked to Sarah Chen. {spoken}"
        option = out["blocks"][0]["props"]["options"][0]
        assert option["verified"] is False
        assert option["confirmation"]["state"] == state
        assert option["detail"] == f"Parcel P-1 · {detail}"
        assert "Confirmed" not in option["detail"]
        # Still the brokerage's own lead: it opens, it is just not confirmed.
        assert option["href"] == "/property/lead-1"

    def test_verified_is_never_read_from_a_state_that_is_not_confirmed(self):
        option = self._ask([self._house(1, "rejected", verified=True)])["blocks"][0]["props"]["options"][0]
        assert option["verified"] is False and "Confirmed" not in option["detail"]

    def test_a_linked_house_is_never_called_a_shortlist_or_nothing(self):
        for state in ("confirmed", "rejected", "none"):
            spoken = self._ask([self._house(1, state)])["spoken"]
            assert "shortlist" not in spoken and "Nothing" not in spoken and "possible" not in spoken

    def test_every_linked_house_is_counted_and_the_confirmed_ones_among_those_shown(self):
        houses = [self._house(1, "confirmed"), self._house(2, "rejected"), self._house(3, "out_of_date")]
        out = self._ask(houses, total=5)
        assert out["spoken"] == ("5 houses are linked to Sarah Chen. Showing 3 of 5 here. "
                                 "1 of 3 confirmed as theirs. 1 of 3 have a public record marked not their house.")
        options = out["blocks"][0]["props"]["options"]
        assert [o["id"] for o in options] == ["lead-1", "lead-2", "lead-3"]
        assert [o["verified"] for o in options] == [True, False, False]

    def test_a_rejection_is_said_of_the_record_never_of_the_linked_house(self):
        """0168's rejection claims only "R is not C's house", for one record.
        Several public records can sit on one parcel (the catalog dedupes by
        source), and Neoh reads only the decided ones: r1 rejected beside an
        undecided r2 reaches the renderer as a rejected house. The house the
        brokerage linked is never said to be not the client's; Houses says it
        of r1's card alone."""
        for houses in ([self._house(1, "rejected")],
                       [self._house(1, "rejected"), self._house(2, "rejected")]):
            out = self._ask(houses)
            for text in (out["spoken"], *(o["detail"] for o in out["blocks"][0]["props"]["options"])):
                assert "a public record" in text, text
                for whole_house in ("It is marked not", "but marked not", "of 2 marked not"):
                    assert whole_house not in text, text
        two = self._ask([self._house(1, "rejected"), self._house(2)])
        assert two["spoken"] == ("2 houses are linked to Sarah Chen. 0 of 2 confirmed as theirs. "
                                 "1 of 2 have a public record marked not their house.")
        assert [o["verified"] for o in two["blocks"][0]["props"]["options"]] == [False, False]

    def test_none_confirmed_is_said_as_a_count(self):
        out = self._ask([self._house(1), self._house(2, "out_of_date")])
        assert out["spoken"] == "2 houses are linked to Sarah Chen. 0 of 2 confirmed as theirs."

    def test_more_houses_than_the_panel_holds_are_capped(self):
        houses = [self._house(n, "confirmed") for n in range(1, 6)]
        out = self._ask(houses)
        assert len(out["blocks"][0]["props"]["options"]) == render.MAX_COMPARISON
        assert "Showing 3 of 5 here." in out["spoken"] and "3 of 3 confirmed as theirs." in out["spoken"]

    def test_a_manual_listing_is_linked_never_verified_and_opens_nothing(self):
        """A manual house is a listings row with no parcel: no decision can be
        made on it, and /property/ opens a lead's sheet, not a listing."""
        option = self._ask([self._house(1, kind="listing")])["blocks"][0]["props"]["options"][0]
        assert option == {
            "id": "listing-1", "label": "1 Elm St", "detail": "Linked to Sarah Chen · not confirmed as theirs",
            "href": None, "kind": "linked_house", "verified": False,
            "confirmation": {"state": "none", "decided_at": None},
        }

    def test_a_house_with_no_street_address_is_labelled_by_its_parcel(self):
        options = self._ask([self._house(1, address=None), self._house(2, address=None, parcel_id=None)])[
            "blocks"][0]["props"]["options"]
        assert options[0]["label"] == "Parcel P-1"
        assert options[0]["detail"] == "Linked to Sarah Chen · not confirmed as theirs"
        assert options[1]["label"] == "Address unavailable"

    def test_candidates_stored_before_the_link_are_not_said_beside_it(self):
        """The producer keeps no owner-name candidates for a linked client,
        and linking queues the reconcile that clears them. A leftover can be
        the very record just confirmed: it is never said as a possible match
        next to "confirmed as theirs"."""
        same_record = {"public_record_id": "r1", "parcel_id": "P-1", "address": "1 Elm St",
                       "match_basis": "exact owner name", "requires_review": True}
        out = self._ask([self._house(1, "confirmed")], candidates=[same_record], lookup={"status": "checked"})
        assert out["spoken"] == "1 house is linked to Sarah Chen. It is confirmed as theirs."
        block = out["blocks"][0]["props"]
        assert block["title"] == "Houses linked to Sarah Chen"
        assert [o["id"] for o in block["options"]] == ["lead-1"]

    def test_a_stale_lookup_failure_is_not_read_over_a_linked_house(self):
        """The owner-name lookup does not run for a linked client; an old
        failure says nothing about the house on record."""
        out = self._ask([self._house(1, "confirmed")], lookup={"status": "timed_out"})
        assert out["spoken"] == "1 house is linked to Sarah Chen. It is confirmed as theirs."

    def test_in_spanish(self):
        houses = [self._house(1, "confirmed"), self._house(2, "rejected"), self._house(3)]
        out = self._ask(houses, total=4, lang="es")
        assert out["spoken"] == ("4 casas están vinculadas a Sarah Chen. Aquí ves 3 de 4. "
                                 "1 de 3 confirmadas como suyas. "
                                 "1 de 3 tienen un registro público marcado como que no es su casa.")
        block = out["blocks"][0]["props"]
        assert block["title"] == "Casas vinculadas a Sarah Chen"
        assert [o["detail"] for o in block["options"]] == [
            "Parcela P-1 · Confirmada como la casa de Sarah Chen · 2026-10-17",
            "Parcela P-2 · Vinculada a Sarah Chen · no confirmada como suya · un registro público de esa "
            "parcela se marcó como que no es su casa",
            "Parcela P-3 · Vinculada a Sarah Chen · no confirmada como suya",
        ]
        one = self._ask([self._house(1, "confirmed")], lang="es")["spoken"]
        assert one == "1 casa está vinculada a Sarah Chen. Está confirmada como suya."
        for state, said in (("rejected", "No está confirmada como suya, y un registro público de esa parcela "
                                         "se marcó como que no es su casa."),
                            ("none", "No está confirmada como suya.")):
            assert self._ask([self._house(1, state)], lang="es")["spoken"].endswith(said)
        for text in (out["spoken"], one, block["title"], *(o["detail"] for o in block["options"])):
            assert "lista" not in text and "house" not in text and "linked" not in text

    @pytest.mark.parametrize("candidates,lookup", [
        ([], None), ([], {"status": "not_applicable"}), ([], {"status": "timed_out"}),
        ([{"id": "p1", "address": "12 Oak St"}], {"status": "checked"}),
        ([TestProperties.REVIEW_ONLY], {"status": "checked"}),
    ])
    def test_no_linked_house_leaves_the_answer_exactly_as_it_was(self, candidates, lookup):
        before = render.properties_for("Sarah", candidates, lookup)
        assert render.properties_for("Sarah", candidates, lookup, linked=None) == before
        assert render.properties_for("Sarah", candidates, lookup, linked=[], linked_total=0) == before


class TestFallthrough:
    def test_a_miss_is_a_fallthrough_never_an_empty_panel(self):
        out = render.fallthrough("no pattern matched")
        assert out["fallthrough"] is True
        assert out["blocks"] == [] and out["intent"] is None


def test_portfolio_metric_reads_the_expected_value_total():
    # expected_value.portfolio returns total_expected_value; the metric read
    # "total" and was empty on every call-queue answer (L05-05).
    import neoh_render

    metric = neoh_render._portfolio_metric({"portfolio": {"total_expected_value": 1012.0, "calibrated": False}})
    assert metric["value"] == 1012.0
