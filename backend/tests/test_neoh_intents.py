"""What the ⌘K box understood, decided in code — and never by a model.

The routing table is ordered, so the tests that matter most are the ones about
which pattern wins and which questions deliberately reach the model instead.
"""

from __future__ import annotations

import asyncio
import inspect

import neoh_intents as intents
import neoh_render as render
from tenancy import Role, TenantContext

CTX = TenantContext(
    agent_id="a@t.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)


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

        async def two_sarahs(_ctx, _q, _kinds, _limit):
            return {"results": [
                {"kind": "people", "id": "a", "label": "Sarah Chen", "sublabel": "buyer"},
                {"kind": "people", "id": "b", "label": "Sarah Chen", "sublabel": "seller"},
            ]}

        monkeypatch.setattr(search_api, "search", two_sarahs)
        out = asyncio.run(intents.ask(CTX, "show Sarah Chen"))
        assert out["blocks"][0]["primitive"] == "comparison"
        assert len(out["blocks"][0]["props"]["options"]) == 2

    def test_an_address_shaped_show_falls_to_properties(self, monkeypatch):
        """"Show 412 Delaware" is a property question wearing a person's grammar."""
        import search_api

        async def by_kind(_ctx, _q, kinds, _limit):
            if kinds == ["people"]:
                return {"results": []}
            return {"results": [
                {"kind": "properties", "id": "p1", "label": "412 Delaware Ave",
                 "sublabel": "Wilmington, DE", "href": "/property/p1"},
            ]}

        monkeypatch.setattr(search_api, "search", by_kind)
        out = asyncio.run(intents.ask(CTX, "show 412 Delaware Ave"))
        assert out["intent"] == "show_property"
        assert out["blocks"][0]["primitive"] == "property"

    def test_a_name_that_matches_nothing_says_so(self, monkeypatch):
        import search_api

        async def nothing(_ctx, _q, _kinds, _limit):
            return {"results": []}

        monkeypatch.setattr(search_api, "search", nothing)
        out = asyncio.run(intents.ask(CTX, "show Zebediah Nobody"))
        assert out["fallthrough"] is False
        assert "Nothing matches" in out["spoken"]
        assert out["blocks"] == []
