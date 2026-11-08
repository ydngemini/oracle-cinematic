"""What the ⌘K box understood, decided in code — and never by a model.

The routing table is ordered, so the tests that matter most are the ones about
which pattern wins and which questions deliberately reach the model instead.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

import neoh_intents as intents
import neoh_render as render
import source_status as ss
from tenancy import Role, TenantContext

CTX = TenantContext(
    agent_id="a@t.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)


def _found(kinds, results=()):
    """What search_api.search returns when every leg asked was checked."""
    results = list(results)
    return {
        "results": results,
        "degraded": [],
        "source_status": {
            k: ss.checked(matched=sum(1 for r in results if r.get("kind") == k)) for k in kinds
        },
    }


def _route(text):
    for intent in intents.INTENTS:
        match = intent.pattern.match(text)
        if match:
            return intent.name, match.groupdict()
    return None, {}


class TestRouting:
    def test_the_products_core_question_in_the_shapes_people_type_it(self):
        for text in (
            "who should I call",
            "Who should I call first today?",
            "who do i call next",
            "who should I follow up with",
            "my call list",
            "next best action",
        ):
            assert _route(text)[0] == "who_to_call", text

    def test_a_person_and_the_properties_for_that_person_are_different_questions(self):
        """"find properties for Sarah" also matches the show-person pattern, so
        the specific one has to be listed first. This is the test that fails if
        the table is ever reordered alphabetically."""
        assert _route("show Sarah Chen") == ("show_person", {"name": "Sarah Chen"})
        name, groups = _route("find properties for Sarah Chen")
        assert name == "properties_for" and groups["name"] == "Sarah Chen"
        assert _route("show me houses for Marcus")[0] == "properties_for"

    def test_the_deal_question_captures_the_deal(self):
        name, groups = _route("what's holding 155 Main up")
        assert name == "deal_blocker" and groups["deal"] == "155 Main"
        assert _route("what is blocking the Delaware deal")[0] == "deal_blocker"

    def test_questions_with_no_interface_reach_the_model(self):
        """The fixed vocabulary is a fast path in front of a general one, not
        a limit on what may be asked. Anything these patterns do not cover has
        to fall through, or the box becomes narrower than the chat it replaced."""
        for text in (
            "summarize my pipeline in one line",
            "draft an email to Sarah",
            "what should I say to a seller who is worried about rates",
            "hello",
        ):
            assert _route(text)[0] is None, text
            assert asyncio.run(intents.ask(CTX, text))["fallthrough"] is True


class TestSafety:
    def test_no_model_is_reachable_from_this_module(self):
        """The generative half generates arrangement, never content. A model
        that could emit markup could emit a fabricated number inside a heading,
        and rendered it would be indistinguishable from a real one."""
        source = inspect.getsource(intents) + inspect.getsource(render)
        for forbidden in ("llm_gateway", "litellm", "complete(", "invoke_bedrock",
                          "mind_service", "openai", "bedrock_client"):
            assert forbidden not in source, f"{forbidden} is reachable from the ask path"

    def test_a_broken_resolver_costs_the_interface_not_the_question(self, monkeypatch):
        async def boom(_ctx, _match):
            raise RuntimeError("command_center is down")

        monkeypatch.setattr(intents.INTENTS[2], "resolve", boom)
        out = asyncio.run(intents.ask(CTX, "who should I call"))
        assert out["fallthrough"] is True, "a failed resolver must still reach the model"
        assert "failed" in out["reason"]

    def test_empty_and_oversized_input_are_handled_without_reaching_a_resolver(self):
        assert asyncio.run(intents.ask(CTX, ""))["fallthrough"] is True
        assert asyncio.run(intents.ask(CTX, "   "))["fallthrough"] is True
        assert intents.MAX_TEXT <= 400


class TestOpenRecord:
    def test_who_to_call_about_the_open_property_reaches_the_model(self, monkeypatch):
        """With a property open, "who should I call about this?" is about that
        property. The tenant-wide call queue answered a different question."""
        import command_center

        async def must_not_run(ctx, **kwargs):  # pragma: no cover - asserted below
            raise AssertionError("the generic call queue answered a record question")

        monkeypatch.setattr(command_center, "briefing", must_not_run)
        out = asyncio.run(intents.ask(CTX, "Who should I call about this?", has_record=True))
        assert out["fallthrough"] is True

    def test_record_aware_intents_still_answer_with_a_record_open(self, monkeypatch):
        import search_api

        async def nothing(_ctx, _q, kinds, _limit):
            return _found(kinds)

        monkeypatch.setattr(search_api, "search", nothing)
        out = asyncio.run(intents.ask(CTX, "show Zebediah Nobody", has_record=True))
        assert out.get("intent") == "show_person"


class TestResolvers:
    def test_who_to_call_reuses_the_briefing_rather_than_ranking_again(self, monkeypatch):
        """Two rankings in one product put the same card in two positions."""
        import command_center

        calls = []

        async def fake_briefing(ctx, **kwargs):
            calls.append(kwargs)
            return {
                "attention": {
                    "opportunities": [{
                        "kind": "follow_up", "subject": "Sarah Chen", "subject_id": "c1",
                        "subject_type": "client", "headline": "went quiet", "why": "21 days",
                        "recommended_action": "Call", "confidence": 0.6, "action_type": "call",
                        "economics": {"expected_value": 900.0},
                    }],
                    "portfolio": {"total": 900.0, "caveat": "Priors.", "calibrated": False},
                },
            }

        monkeypatch.setattr(command_center, "briefing", fake_briefing)
        out = asyncio.run(intents.ask(CTX, "who should I call"))
        assert calls, "the briefing was not the source"
        assert out["intent"] == "who_to_call" and out["fallthrough"] is False
        assert [b["primitive"] for b in out["blocks"]] == ["call_queue", "metric"]

    def test_an_ambiguous_name_asks_instead_of_guessing(self, monkeypatch):
        import search_api

        async def two_sarahs(_ctx, _q, kinds, _limit):
            return _found(kinds, [
                {"kind": "people", "id": "a", "label": "Sarah Chen", "sublabel": "buyer"},
                {"kind": "people", "id": "b", "label": "Sarah Chen", "sublabel": "seller"},
            ])

        monkeypatch.setattr(search_api, "search", two_sarahs)
        out = asyncio.run(intents.ask(CTX, "show Sarah Chen"))
        assert out["blocks"][0]["primitive"] == "comparison"
        assert len(out["blocks"][0]["props"]["options"]) == 2

    def test_an_address_shaped_show_falls_to_properties(self, monkeypatch):
        """"Show 412 Delaware" is a property question wearing a person's grammar."""
        import search_api

        async def by_kind(_ctx, _q, kinds, _limit):
            if kinds == ["people"]:
                return _found(kinds)
            return _found(kinds, [
                {"kind": "properties", "id": "p1", "label": "412 Delaware Ave",
                 "sublabel": "Wilmington, DE", "href": "/property/p1"},
            ])

        monkeypatch.setattr(search_api, "search", by_kind)
        out = asyncio.run(intents.ask(CTX, "show 412 Delaware Ave"))
        assert out["intent"] == "show_property"
        assert out["blocks"][0]["primitive"] == "property"

    def test_a_name_that_matches_nothing_says_so(self, monkeypatch):
        import search_api

        async def nothing(_ctx, _q, kinds, _limit):
            return _found(kinds)

        monkeypatch.setattr(search_api, "search", nothing)
        out = asyncio.run(intents.ask(CTX, "show Zebediah Nobody"))
        assert out["fallthrough"] is False
        assert "Nothing matches" in out["spoken"]
        assert out["blocks"] == []
        # A verified miss says so: the leg it rests on was checked.
        assert out["degraded"] is False
        assert out["source_status"]["people"]["status"] == ss.CHECKED


class TestPossibleMatchesEndToEnd:
    """T11.03 / F11.04: "find properties for Sarah" through the whole path.

    The candidates come from the real client_ai_automation._property_candidates
    (so they are the production shape: review-only owner-name hits), are stored
    as reconcile_client stores them, and are read back by the real
    _candidates_for; only the database is stubbed. The golden cases stub
    _candidates_for itself with a confirmed-shortlist shape, so they cannot see
    this path.
    """

    @staticmethod
    def _stub(monkeypatch):
        import json
        from contextlib import asynccontextmanager
        from datetime import datetime, timezone

        import client_ai_automation
        import db.connection
        import search_api

        refreshed = datetime(2026, 10, 1, tzinfo=timezone.utc)
        rows = [
            {"id": f"aaaaaaaa-0000-4000-8000-00000000000{n}", "parcel_id": f"P-{n}",
             "address": f"{n} Elm St", "city": "Austin", "state": "TX", "zip_code": "78701",
             "owner_name": "SARAH CHEN", "source_name": "county", "record_refreshed_at": refreshed,
             "verification_required": True}
            for n in range(1, 6)  # the producer's LIMIT 5
        ]

        class _Records:
            async def fetch(self, _sql, *_args):
                return rows

        candidates = asyncio.run(
            client_ai_automation._property_candidates(_Records(), "Sarah Chen", [], 0),
        )
        assert len(candidates) == 5 and all(c["requires_review"] for c in candidates)
        state = {
            "enabled": True, "status": "complete", "score_mode": "auto", "stage_mode": "auto",
            "score_version": None, "score_breakdown": "[]", "normalized_preferences": "{}",
            "summary": None, "next_actions": "[]", "data_gaps": "[]", "evidence": "[]",
            "property_candidates": json.dumps(candidates), "model_id": None,
            "last_evaluated_at": None, "last_error_code": None,
            "source_status": json.dumps({client_ai_automation.PROPERTY_SOURCE: (
                client_ai_automation.property_lookup_status(
                    client_ai_automation.SOURCE_CHECKED, matched=len(candidates),
                    checked_at="2026-10-01T00:00:00+00:00",
                )
            )}),
        }

        class _State:
            async def fetchrow(self, sql, *_args):
                assert "client_ai_state" in sql
                return state

            async def fetch(self, sql, *_args):
                # The linked-house read: this client has no house linked.
                assert "FROM leads l" in sql
                return []

        @asynccontextmanager
        async def fake_tx(_ctx):
            yield _State()

        async def one_sarah(_ctx, _q, kinds, _limit):
            return _found(kinds, [{"kind": "people", "id": "c1", "label": "Sarah Chen", "sublabel": "buyer"}])

        monkeypatch.setattr(db.connection, "tenant_tx", fake_tx)
        monkeypatch.setattr(search_api, "search", one_sarah)
        return candidates

    def test_review_only_hits_are_possible_matches_never_a_shortlist(self, monkeypatch):
        candidates = self._stub(monkeypatch)
        out = asyncio.run(intents.ask(CTX, "find properties for Sarah Chen"))
        assert out["intent"] == "properties_for" and out["fallthrough"] is False
        assert "shortlist" not in out["spoken"]
        assert out["spoken"].startswith("At least 5 possible matches for Sarah Chen in public records")
        assert "Showing 3 of 5 here." in out["spoken"]
        block = out["blocks"][0]["props"]
        assert block["title"] == "Possible matches for Sarah Chen"
        assert [o["id"] for o in block["options"]] == [c["public_record_id"] for c in candidates[:3]]
        for option, record in zip(block["options"], candidates):
            assert option["href"] is None
            assert option["detail"].startswith(
                f"Austin, TX 78701 · Parcel {record['parcel_id']} · Possible match by owner name (ignoring")
            assert "exact" not in option["detail"]

    def test_and_in_spanish(self, monkeypatch):
        self._stub(monkeypatch)
        out = asyncio.run(intents.ask(CTX, "busca propiedades para Sarah Chen"))
        assert out["intent"] == "properties_for" and out["fallthrough"] is False
        assert out["spoken"].startswith("Al menos 5 posibles coincidencias para Sarah Chen")
        assert "Aquí ves 3 de 5." in out["spoken"] and "lista" not in out["spoken"]
        block = out["blocks"][0]["props"]
        assert block["title"] == "Posibles coincidencias para Sarah Chen"
        for option in block["options"]:
            assert option["href"] is None
            assert option["detail"].startswith("Austin, TX 78701 · Parcela ")
            assert "Posible coincidencia por nombre del propietario" in option["detail"]
            assert "exacto" not in option["detail"]


class TestLinkedHousesEndToEnd:
    """T11.03 / F11.04: "find properties for Sarah" for a client whose house
    is linked, through the real _properties_for, _candidates_for and
    _linked_houses_for; only the database is stubbed.

    The producer stores [] with status not_applicable once a house is linked
    (client_ai_automation._property_candidates_bounded), so the answer comes
    from the linked read: the client's leads with the newest decision per
    record (property_match_api.current_heads_sql, as Houses reads it), then a
    primary-key read of those records to check each still sits on the
    house's parcel. Verified only for a current confirmation.
    """

    CLIENT = "c1"
    LEAD = "aaaaaaaa-0000-4000-8000-000000000001"
    RECORD = "bbbbbbbb-0000-4000-8000-000000000001"
    AT = "2026-10-17T16:00:00+00:00"

    def _stub(self, monkeypatch, *, heads=(), records=None, listing=False, fail=False):
        import json
        from contextlib import asynccontextmanager

        import client_ai_automation
        import db.connection
        import search_api

        state = {
            "enabled": True, "status": "complete", "score_mode": "auto", "stage_mode": "auto",
            "score_version": None, "score_breakdown": "[]", "normalized_preferences": "{}",
            "summary": None, "next_actions": "[]", "data_gaps": "[]", "evidence": "[]",
            "property_candidates": "[]", "model_id": None, "last_evaluated_at": None, "last_error_code": None,
            "source_status": json.dumps({client_ai_automation.PROPERTY_SOURCE: (
                client_ai_automation.property_lookup_status(
                    client_ai_automation.SOURCE_NOT_APPLICABLE, reason="client already has a linked property"))}),
        }
        rows = [{"kind": "lead", "id": self.LEAD, "lead_id": self.LEAD, "address": "1 Elm St",
                 "parcel_id": "P-1", "state": "TX", "heads": json.dumps(list(heads)) if heads else None,
                 "total": 2 if listing else 1}]
        if listing:
            rows.append({"kind": "listing", "id": "cccccccc-0000-4000-8000-000000000001", "lead_id": None,
                         "address": "7 Manual Rd", "parcel_id": None, "state": None, "heads": None, "total": 2})
        on_file = [{"id": self.RECORD, "parcel_id": "P-1", "state": "TX"}] if records is None else records
        queries: list[tuple[str, tuple]] = []

        class _Conn:
            async def fetchrow(self, sql, *_args):
                assert "client_ai_state" in sql
                return state

            async def fetch(self, sql, *args):
                queries.append((sql, args))
                if fail:
                    raise RuntimeError("leads table on fire")
                if "FROM leads l" in sql:
                    return rows
                if "FROM public_property_records" in sql:
                    return [r for r in on_file if r["id"] in args[0]]
                raise AssertionError(sql)

        @asynccontextmanager
        async def fake_tx(_ctx):
            yield _Conn()

        async def one_sarah(_ctx, _q, kinds, _limit):
            return _found(kinds, [{"kind": "people", "id": self.CLIENT, "label": "Sarah Chen", "sublabel": "seller"}])

        monkeypatch.setattr(db.connection, "tenant_tx", fake_tx)
        monkeypatch.setattr(search_api, "search", one_sarah)
        return queries

    def _head(self, decision, *, same_client=True, record=None):
        return {"id": "h1", "record_id": record or self.RECORD, "decision": decision,
                "same_client": same_client, "decided_at": self.AT}

    def _option(self, monkeypatch, lang="en", **stub):
        queries = self._stub(monkeypatch, **stub)
        out = asyncio.run(intents.ask(CTX, "busca propiedades para Sarah Chen" if lang == "es"
                                      else "find properties for Sarah Chen"))
        assert out["intent"] == "properties_for" and out["fallthrough"] is False
        return out, out["blocks"][0]["props"]["options"][0], queries

    def test_a_confirmed_house_still_on_its_parcel_reads_verified(self, monkeypatch):
        out, option, queries = self._option(monkeypatch, heads=[self._head("confirmed")])
        assert out["spoken"] == "1 house is linked to Sarah Chen. It is confirmed as theirs."
        assert "shortlist" not in out["spoken"]
        assert option["verified"] is True
        assert option["confirmation"] == {"state": "confirmed", "decided_at": self.AT}
        assert option["href"] == f"/property/{self.LEAD}"
        assert option["detail"] == "Parcel P-1 · Confirmed as Sarah Chen's house · 2026-10-17"
        # The read: this client's houses, by RLS alone, the heads as Houses reads them.
        leads_sql, leads_args = queries[0]
        import re

        import property_match_api

        collapsed = re.sub(r"\s+", " ", leads_sql)
        assert leads_args == (self.CLIENT,)
        assert "WHERE l.seller_client_id = $1::uuid" in collapsed
        assert re.sub(r"\s+", " ", property_match_api.current_heads_sql("l")).strip() in collapsed
        assert f"LIMIT {render.MAX_COMPARISON}" in collapsed and "count(*) OVER ()" in collapsed
        for absent in ("tenant_id", "decided_by", "decided_role", "evidence", "reason"):
            assert absent not in collapsed, absent
        # Then the records by primary key: no parcel join on the public catalog.
        records_sql, records_args = queries[1]
        assert re.sub(r"\s+", " ", records_sql).strip() == (
            "SELECT id::text AS id, parcel_id, state FROM public_property_records WHERE id = ANY($1::uuid[])")
        assert records_args == ([self.RECORD],)
        assert len(queries) == 2

    def test_and_in_spanish(self, monkeypatch):
        out, option, _ = self._option(monkeypatch, "es", heads=[self._head("confirmed")])
        assert out["spoken"] == "1 casa está vinculada a Sarah Chen. Está confirmada como suya."
        assert out["blocks"][0]["props"]["title"] == "Casas vinculadas a Sarah Chen"
        assert option["detail"] == "Parcela P-1 · Confirmada como la casa de Sarah Chen · 2026-10-17"

    def test_a_record_re_keyed_to_another_parcel_is_not_verified(self, monkeypatch):
        _, option, _ = self._option(monkeypatch, heads=[self._head("confirmed")],
                                    records=[{"id": self.RECORD, "parcel_id": "P-9", "state": "TX"}])
        assert option["verified"] is False and option["confirmation"]["state"] == "none"
        assert option["detail"] == "Parcel P-1 · Linked to Sarah Chen · not confirmed as theirs"

    def test_a_record_in_another_state_is_not_verified(self, monkeypatch):
        _, option, _ = self._option(monkeypatch, heads=[self._head("confirmed")],
                                    records=[{"id": self.RECORD, "parcel_id": "P-1", "state": "DE"}])
        assert option["verified"] is False

    def test_a_purged_record_is_not_verified(self, monkeypatch):
        _, option, _ = self._option(monkeypatch, heads=[self._head("confirmed")], records=[])
        assert option["verified"] is False and option["confirmation"]["state"] == "none"

    @pytest.mark.parametrize("head,state,spoken", [
        ({"decision": "rejected"}, "rejected",
         "It is not confirmed as theirs, and a public record on its parcel was marked not their house."),
        ({"decision": "withdrawn"}, "none", "It is not confirmed as theirs."),
        ({"decision": "confirmed", "same_client": False}, "out_of_date", "It is not confirmed as theirs."),
    ])
    def test_anything_else_is_not_verified(self, monkeypatch, head, state, spoken):
        out, option, _ = self._option(
            monkeypatch, heads=[self._head(head["decision"], same_client=head.get("same_client", True))])
        assert out["spoken"] == f"1 house is linked to Sarah Chen. {spoken}"
        assert option["verified"] is False and option["confirmation"]["state"] == state

    def test_one_rejected_record_beside_an_undecided_sibling_is_said_of_the_record(self, monkeypatch):
        """r1 rejected and r2, on the same parcel, never decided: Neoh reads
        only r1's head and says that a record on the house's parcel was
        rejected, never that the linked house is not the client's (0168: "R is
        not C's house")."""
        sibling = "bbbbbbbb-0000-4000-8000-000000000002"
        out, option, queries = self._option(
            monkeypatch, heads=[self._head("rejected")],
            records=[{"id": self.RECORD, "parcel_id": "P-1", "state": "TX"},
                     {"id": sibling, "parcel_id": "P-1", "state": "TX"}])
        assert queries[1][1] == ([self.RECORD],), "only the decided record is read"
        assert option["verified"] is False and option["confirmation"]["state"] == "rejected"
        assert out["spoken"] == ("1 house is linked to Sarah Chen. It is not confirmed as theirs, and a public "
                                 "record on its parcel was marked not their house.")
        assert option["detail"] == ("Parcel P-1 · Linked to Sarah Chen · not confirmed as theirs · a public record "
                                    "on its parcel was marked not their house")

    def test_a_house_with_no_decision_reads_no_records(self, monkeypatch):
        out, option, queries = self._option(monkeypatch)
        assert option["verified"] is False and option["confirmation"] == {"state": "none", "decided_at": None}
        assert len(queries) == 1
        assert out["spoken"] == "1 house is linked to Sarah Chen. It is not confirmed as theirs."

    def test_a_manual_house_is_counted_beside_the_lead(self, monkeypatch):
        out, _, _ = self._option(monkeypatch, heads=[self._head("confirmed")], listing=True)
        assert out["spoken"] == "2 houses are linked to Sarah Chen. 1 of 2 confirmed as theirs."
        options = out["blocks"][0]["props"]["options"]
        assert [(o["label"], o["verified"], o["href"]) for o in options] == [
            ("1 Elm St", True, f"/property/{self.LEAD}"), ("7 Manual Rd", False, None)]

    def test_a_failed_linked_read_goes_to_chat_never_to_nothing_linked(self, monkeypatch):
        self._stub(monkeypatch, fail=True)
        out = asyncio.run(intents.ask(CTX, "find properties for Sarah Chen"))
        assert out["fallthrough"] is True
        assert "on fire" not in str(out)

    def test_no_client_reads_nothing(self):
        assert asyncio.run(intents._linked_houses_for(CTX, None)) == ([], 0)


class TestFailedLegsAreNotNoResults:
    """T05.11 / AC05.07: a search leg that raised is reported as a failure,
    in the C6 vocabulary, and never as "Nothing matches".

    These run the REAL search_api.search with one leg forced to raise, so the
    status is the one search produces, not a hand-written fake of it.
    """

    @staticmethod
    def _leg(monkeypatch, kind, exc):
        import search_api

        async def boom(_ctx, _q, _limit):
            raise exc

        monkeypatch.setitem(search_api.LEGS, kind, boom)

    def _assert_failed(self, out, kind, status):
        assert out["fallthrough"] is False
        assert out["degraded"] is True
        assert out["source_status"][kind]["status"] == status
        assert out["source_status"][kind]["checked_at"] is None
        for phrase in ("Nothing matches", "No one matches", "No deal matches"):
            assert phrase not in out["spoken"], out["spoken"]
        assert "not a “no match”" in out["spoken"]
        # The surface only draws answers with blocks; a failure must be drawn,
        # not handed to the model as if the lookup had been clean.
        assert out["blocks"] and out["blocks"][0]["primitive"] == "evidence"

    def test_show_person_with_a_timed_out_people_leg(self, monkeypatch):
        self._leg(monkeypatch, "people", asyncio.TimeoutError())
        out = asyncio.run(intents.ask(CTX, "show Sarah Chen"))
        self._assert_failed(out, "people", ss.TIMED_OUT)
        assert out["source_status"]["people"]["reason"] == ss.UPSTREAM_TIMEOUT
        assert "timed out" in out["spoken"]

    def test_show_person_with_an_erroring_people_leg(self, monkeypatch):
        self._leg(monkeypatch, "people", RuntimeError("clients table on fire"))
        out = asyncio.run(intents.ask(CTX, "show Sarah Chen"))
        self._assert_failed(out, "people", ss.UNAVAILABLE)
        # Exception text never reaches the answer (it can carry SQL or a name).
        assert "on fire" not in str(out)

    def test_a_statement_timeout_reads_as_timed_out(self, monkeypatch):
        class QueryCanceled(Exception):
            sqlstate = "57014"

        self._leg(monkeypatch, "deals", QueryCanceled("canceling statement due to statement timeout"))
        out = asyncio.run(intents.ask(CTX, "what is holding up 155 Main"))
        self._assert_failed(out, "deals", ss.TIMED_OUT)

    def test_properties_for_with_a_failed_people_leg(self, monkeypatch):
        self._leg(monkeypatch, "people", RuntimeError("down"))
        out = asyncio.run(intents.ask(CTX, "find homes for Sarah Chen"))
        assert out["intent"] == "properties_for"
        self._assert_failed(out, "people", ss.UNAVAILABLE)

    def test_address_fallback_with_a_failed_properties_leg(self, monkeypatch):
        import search_api

        async def nobody(_ctx, _q, _limit):
            return []

        monkeypatch.setitem(search_api.LEGS, "people", nobody)
        self._leg(monkeypatch, "properties", asyncio.TimeoutError())
        out = asyncio.run(intents.ask(CTX, "show 412 Delaware Ave"))
        self._assert_failed(out, "properties", ss.TIMED_OUT)
        # The people leg answered: it is reported as checked beside the failure.
        assert out["source_status"]["people"]["status"] == ss.CHECKED

    def test_a_clean_miss_through_the_real_search_is_checked(self, monkeypatch):
        import search_api

        async def nobody(_ctx, _q, _limit):
            return []

        monkeypatch.setitem(search_api.LEGS, "deals", nobody)
        out = asyncio.run(intents.ask(CTX, "what is holding up 155 Main"))
        assert "No deal matches" in out["spoken"]
        assert out["degraded"] is False
        assert out["source_status"]["deals"]["status"] == ss.CHECKED
        assert out["source_status"]["deals"]["matched"] == 0

    def test_a_search_answer_without_status_is_not_a_verified_miss(self, monkeypatch):
        """A caller that returns hits but no status cannot be read as checked."""
        import search_api

        async def legacy(_ctx, _q, _kinds, _limit):
            return {"results": []}

        monkeypatch.setattr(search_api, "search", legacy)
        out = asyncio.run(intents.ask(CTX, "show Sarah Chen"))
        assert out["degraded"] is True
        assert out["source_status"]["people"]["reason"] == ss.STATUS_MISSING
        assert "Nothing matches" not in out["spoken"]

    def test_degraded_list_wins_over_a_stale_checked_status(self, monkeypatch):
        import search_api

        async def contradictory(_ctx, _q, kinds, _limit):
            out = _found(kinds)
            out["degraded"] = ["people"]
            return out

        monkeypatch.setattr(search_api, "search", contradictory)
        out = asyncio.run(intents.ask(CTX, "show Sarah Chen"))
        assert out["degraded"] is True
        assert out["source_status"]["people"]["status"] == ss.UNAVAILABLE
