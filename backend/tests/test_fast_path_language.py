"""L05-05: the fast path's SUCCESS answers, the command fallback drafts and
the voice-language hint, in English and Spanish.

Owner decision 2026-10-14: English and Spanish at launch. L05-04 put the
refusals and failure answers in the request's language; these tests hold the
rest of the fast path to the same rule:

* every Spanish twin of a fast-path intent answers in Spanish (its spoken
  line and the product words in its blocks);
* record content (names, addresses, milestone titles, timeline subjects,
  recorded values) is never translated;
* the English answers are byte-identical to what da163ba produced
  (`fast_path_en_golden.json`, see fast_path_language_cases.py).
"""

from __future__ import annotations

import json
import re

import pytest

import expected_value
import intent_states
import neoh_intents as intents
import neoh_render as render
import semantic_language as lang_
from tests import fast_path_language_cases as cases

# Words that only appear in the English product sentences these answers used
# to contain. A Spanish answer containing one of them leaked English.
_ENGLISH_PRODUCT_WORDS = re.compile(
    r"\b(?:first|then|more|shortlist|holding|milestone|milestones|earliest|"
    r"Recorded|Worth|Acting|declared|behaviour|Behaviour|Expected|Unnamed|"
    r"Address unavailable|beds|baths|This person|This deal|For)\b"
)

#: Record content in the fixtures: it must come through untouched in Spanish.
_RECORD_CONTENT = ("Sarah Johnson", "123 Main St", "12 Oak St", "412 Delaware Ave",
                   "Called about the offer", '"6 months"', '"30 days"')


def _product_text(answer: dict) -> str:
    """The product's own words in an answer: its spoken line and the titles,
    labels, reads and captions of its blocks, minus the record content."""
    parts = [answer.get("spoken") or ""]
    for blk in answer.get("blocks") or []:
        props = blk.get("props") or {}
        for key in ("title", "label", "read", "subtitle"):
            if isinstance(props.get(key), str):
                parts.append(props[key])
        for opt in props.get("options") or []:
            parts.append(str(opt.get("label") or ""))
            parts.append(str(opt.get("detail") or ""))
    text = " ".join(parts)
    for content in _RECORD_CONTENT + ("inside the stated budget", "appraisal report due",
                                      "Appraisal report due", "Loan commitment", "move timeline"):
        text = text.replace(content, " ")
    return text


# ── 1. English is byte-identical to before ──────────────────────────────────

class TestEnglishUnchanged:
    def test_every_english_answer_and_draft_matches_the_golden_file(self):
        golden = json.loads(cases.GOLDEN.read_text())
        now = cases.english_outputs()
        assert set(now) == set(golden)
        for key in golden:
            assert now[key] == golden[key], key

    def test_the_golden_file_covers_every_fast_path_intent(self):
        golden = json.loads(cases.GOLDEN.read_text())
        answered = {v["intent"] for k, v in golden.items() if k.startswith("ask.")}
        assert {i.name for i in intents.INTENTS} <= answered | {"show_person"}
        assert {"who_to_call", "show_person", "show_property", "properties_for", "deal_blocker"} <= answered
        assert not any(v.get("fallthrough") for k, v in golden.items() if k.startswith("ask."))

    @pytest.mark.parametrize("calibrated,outcomes,expected", [
        (True, 42, ("Fitted from 42 of this brokerage's own outcomes. The uplifts are "
                    "differences in rates against the organic base rate, not causal "
                    "estimates; use the ordering, and read the amounts as expectations "
                    "with the intervals each card shows.")),
        (False, 1, ("Modelled from stated priors. 1 outcome recorded so far — "
                    f"{expected_value.MIN_OUTCOMES_FOR_PORTFOLIO - 1} more before the portfolio figure "
                    "is fitted rather than assumed. Use the ordering; treat the dollar amounts as "
                    "relative, not forecast.")),
        (False, 3, ("Modelled from stated priors. 3 outcomes recorded so far — "
                    f"{expected_value.MIN_OUTCOMES_FOR_PORTFOLIO - 3} more before the portfolio figure "
                    "is fitted rather than assumed. Use the ordering; treat the dollar amounts as "
                    "relative, not forecast.")),
        (False, 0, ("Modelled from stated priors, not fitted to this brokerage's own "
                    "closed deals — no outcomes have been recorded yet. Use the ordering; "
                    "treat the dollar amounts as relative, not forecast.")),
    ])
    def test_the_portfolio_caveat_reads_as_it_did(self, calibrated, outcomes, expected):
        """The three sentences expected_value.portfolio wrote inline at da163ba."""
        assert expected_value.portfolio_caveat(calibrated, outcomes) == expected
        if not calibrated:
            assert expected_value.portfolio([])["caveat"] == expected_value.portfolio_caveat(False, 0)


# ── 2. every Spanish twin answers in Spanish ────────────────────────────────

class TestSpanishTwins:
    @pytest.mark.parametrize("key,english,spanish,setup", cases.ASK_TWINS, ids=[t[0] for t in cases.ASK_TWINS])
    def test_the_spanish_twin_answers_in_spanish(self, key, english, spanish, setup):
        en = cases.ask(english, **setup)
        es = cases.ask(spanish, **setup)
        assert not es["fallthrough"], key
        assert es["intent"] == en["intent"], key
        # Same arrangement: the same primitives, in the same order.
        assert [b["primitive"] for b in es["blocks"]] == [b["primitive"] for b in en["blocks"]]
        # The spoken line changed language, unless it is only record content
        # (a single property's address).
        if en["spoken"] not in _RECORD_CONTENT:
            assert es["spoken"] != en["spoken"], key
        leaked = _ENGLISH_PRODUCT_WORDS.findall(_product_text(es))
        assert not leaked, (key, leaked, es["spoken"])

    @pytest.mark.parametrize("key,english,spanish,setup", cases.ASK_TWINS, ids=[t[0] for t in cases.ASK_TWINS])
    def test_record_content_is_never_translated(self, key, english, spanish, setup):
        en = json.dumps(cases.stable(cases.ask(english, **setup)), ensure_ascii=False)
        es = json.dumps(cases.stable(cases.ask(spanish, **setup)), ensure_ascii=False)
        for content in _RECORD_CONTENT:
            assert en.count(content) == es.count(content), (key, content)

    def test_the_deal_blocker_seen_on_staging(self):
        """'¿qué está frenando 123 Main?' got its headline in English (L05-04)."""
        out = cases.ask("¿qué está frenando 123 Main?", deals=[cases.hit("deals", "123 Main St", cases.DEAL_ID)],
                        deal=cases.transaction(), deal_milestones=cases.milestones("titled"))
        assert out["intent"] == "deal_blocker"
        assert out["spoken"] == "Appraisal report due es lo más próximo que sigue abierto en 123 Main St."
        # The milestone's own title is the brokerage's words, untouched.
        assert out["blocks"][1]["props"]["items"][0]["label"] == "appraisal report due"

    def test_a_deal_with_no_address_reads_naturally_in_spanish(self):
        start = render.deal_blocker(cases.transaction(None), [], "es")
        assert start["spoken"].startswith("Este trato no tiene hitos registrados")
        mid = render.deal_blocker(cases.transaction(None), cases.milestones("all_done"), "es")
        assert mid["spoken"] == "Nada está frenando este trato: todos los hitos están completados."
        assert mid["blocks"][0]["props"]["address"] == "Este trato"
        bare = render.deal_blocker(cases.transaction(), cases.milestones("bare"), "es")
        assert bare["spoken"] == "El siguiente hito es lo más próximo que sigue abierto en 123 Main St."

    @pytest.mark.parametrize("verdict", ["cannot_compare", "behaviour_only", "behaviour_ahead",
                                         "behaviour_behind", "consistent"])
    def test_every_reading_has_its_spanish_sentence(self, verdict):
        picture = cases.intent_picture(verdict)
        en = render.person(cases.RECORD, picture, [])
        es = render.person(cases.RECORD, picture, [], "es")
        assert es["spoken"] == lang_.MESSAGES[f"intent.{verdict}"]["es"].format(
            declared=f"{picture['declared']['score'] or 0:.0%}", observed=f"{picture['observed']['score'] or 0:.0%}")
        assert es["blocks"][0]["props"]["read"] == es["spoken"]
        assert en["spoken"] == picture["latent"]["summary"]
        # The percentages are the same numbers in both languages.
        assert re.findall(r"\d+%", es["spoken"]) == re.findall(r"\d+%", en["spoken"])

    def test_an_unknown_reading_keeps_its_stored_sentence(self):
        picture = {"latent": {"verdict": "something_new", "summary": "A sentence from elsewhere."}}
        assert render.person(cases.RECORD, picture, [], "es")["spoken"] == "A sentence from elsewhere."

    def test_the_stored_reading_is_still_english(self):
        """intent_states stores English (the Command Center shows it); only the
        fast path re-renders it."""
        assert intent_states.reconcile_summary("consistent", None, None) == \
            cases.intent_picture("consistent")["latent"]["summary"]
        assert intent_states.reconcile_summary("behaviour_ahead", None, 0.5) is None
        assert intent_states.reconcile_summary("nope", 0.1, 0.2) is None

    def test_the_portfolio_caveat_is_re_rendered_from_its_counts(self):
        portfolio = expected_value.portfolio([])
        attention = {"portfolio": portfolio, "opportunities": []}
        en = render._portfolio_metric(attention)
        es = render._portfolio_metric(attention, "es")
        assert en["caveat"] == portfolio["caveat"]
        assert es["caveat"] == lang_.t("portfolio.caveat.none", "es")
        assert es["label"] == "Valor esperado de actuar en todo ello"
        # Counts missing (an older shape): the caveat stands rather than vanish.
        assert render._portfolio_metric({"portfolio": {"caveat": "x"}}, "es")["caveat"] == "x"

    def test_enum_words_and_timeline_types(self):
        assert render._enum_word("stage", "under_contract", "es") == "bajo contrato"
        assert render._enum_word("stage", "under_contract", "en") == "under_contract"
        assert render._enum_word("stage", "custom_stage", "es") == "custom_stage"
        assert intents._interaction_word("portal_view", "es") == "visita al portal"
        assert intents._interaction_word("portal_view", "en") == "portal view"
        assert intents._interaction_word("brokerage_custom", "es") == "brokerage custom"

    def test_a_dispute_without_its_beliefs_falls_back_to_the_generic_line(self):
        assert render._dispute_label({"question": "Q?"}, "en") == "Q?"
        assert render._dispute_label({"question": "Q?"}, "es") == "Dos registros no coinciden"


# ── 3. command fallback drafts follow the command's language ────────────────

class TestFallbackDrafts:
    TWINS = [
        ("EMAIL", "Email Sarah about the offer of $425,000", "Envíale un correo a Sarah sobre la oferta de $425,000"),
        ("SMS", "Text Sarah about the offer", "Mándale un texto a Sarah sobre la oferta"),
        ("CALL", "Call Sarah about the offer", "Llama a Sarah sobre la oferta"),
        ("CALENDAR", "Schedule a meeting with Sarah", "Agenda una reunión con Sarah"),
    ]

    def _drafts(self, intent_name, text, full):
        import commands_api as c

        client = {"id": cases.CLIENT_ID, "full_name": "Sarah Johnson"} if full else None
        prop = {"id": cases.PROP_ID, "address": "12 Oak St"} if full else None
        profile = {"name": "Alex Agent", "signature": "Alex Agent, Neoh Realty"} if full else {}
        return c._fallback_draft(c.ParsedIntent[intent_name], text, profile=profile, client=client,
                                 property_context=prop)

    @pytest.mark.parametrize("intent_name,english,spanish", TWINS)
    @pytest.mark.parametrize("full", [True, False])
    def test_spanish_command_spanish_draft(self, intent_name, english, spanish, full):
        en = json.dumps(self._drafts(intent_name, english, full), ensure_ascii=False)
        es = json.dumps(self._drafts(intent_name, spanish, full), ensure_ascii=False)
        assert en != es
        for word in ("Hi ", "following up", "Follow-up", "your real estate", "this is ", "there"):
            assert word not in es, (word, es)
        if full:
            # Record content and the agent's own words come through as stored.
            assert "Sarah" in es
            if intent_name != "CALENDAR":
                assert "Alex Agent" in es
            if intent_name in ("EMAIL", "SMS"):
                assert "12 Oak St" in es and "Alex Agent, Neoh Realty" in es

    def test_the_spanish_email_reads_as_a_letter(self):
        draft = self._drafts("EMAIL", self.TWINS[0][2], True)
        assert draft["subject"] == "Seguimiento sobre 12 Oak St"
        assert draft["body"] == ("Hola, Sarah:\n\nTe escribo para dar seguimiento a 12 Oak St. "
                                 "Me gustaría hablar de una cantidad propuesta de $425,000.00."
                                 "\n\nAlex Agent, Neoh Realty")
        assert self._drafts("EMAIL", "Envíale un correo", False)["body"].startswith(
            "Hola:\n\nTe escribo para dar seguimiento a tus planes inmobiliarios.")

    def test_an_assignment_in_spanish_is_an_assignment(self):
        """Defect found on the way: 'contrato de cesión' used to draft a
        seller_purchase, because only the English 'assign' was looked for."""
        import commands_api as c

        draft = c._fallback_draft(c.ParsedIntent.CONTRACT, "Prepara un contrato de cesión para Alex",
                                  profile={}, client=None, property_context=None)
        assert draft["document_type"] == "assignment" and draft["doc_id"] == "assignment-standard"
        assert c._assignment_requested("Draft an assignment contract")
        assert not c._assignment_requested("Prepara un contrato de compraventa")


# ── 4. the voice-language hint ──────────────────────────────────────────────

class TestLanguageHint:
    @pytest.mark.parametrize("value,expected", [
        ("es", "es"), ("es-MX", "es"), ("ES_us", "es"), ("en-US", "en"), ("en", "en"),
        ("fr-FR", None), ("", None), (None, None), (7, None), ("español", None),
    ])
    def test_normalise_hint(self, value, expected):
        assert lang_.normalise_hint(value) == expected

    def test_the_text_wins_when_it_is_conclusive(self):
        assert lang_.reply_language("muéstrame a Sarah Chen, por favor", hint="en") == "es"
        assert lang_.reply_language("show me Sarah Chen please", hint="es") == "en"

    def test_the_hint_decides_a_turn_too_short_to_tell(self):
        assert lang_.reply_language("Sarah", hint="es") == "es"
        assert lang_.reply_language("ok", previous="en", hint="es") == "es"
        assert lang_.reply_language("Sarah", hint="fr") == "en"

    def test_without_a_hint_the_l05_04_rule_is_unchanged(self):
        assert lang_.reply_language("ok", previous="es") == "es"
        assert lang_.reply_language("Sarah", previous=None) == "en"

    def test_the_ask_route_accepts_the_hint_and_the_pattern_still_decides(self):
        from intelligence_os_api import NeohAsk

        assert NeohAsk(text="who should I call", lang="es-MX").lang == "es-MX"
        with pytest.raises(ValueError):
            NeohAsk(text="x", lang="x" * 17)
        with cases.data(brief=cases.briefing(1)):
            import asyncio

            out = asyncio.run(intents.ask(cases.CTX, "who should I call", lang_hint="es"))
        assert out["spoken"] == "Sarah Johnson first."

    def test_the_chat_frame_carries_a_normalised_hint(self):
        import uuid

        from ai_chat_models import ChatSendFrame

        base = {"type": "AI_CHAT_SEND", "version": 1, "request_id": str(uuid.uuid4()), "content": "Sarah"}
        assert ChatSendFrame.model_validate({**base, "lang": "es-ES"}).lang == "es"
        assert ChatSendFrame.model_validate({**base, "lang": "klingon"}).lang is None
        assert ChatSendFrame.model_validate(base).lang is None

    def test_prepare_turn_reads_the_hint_for_a_short_turn(self, monkeypatch):
        import asyncio

        import semantic_turn

        async def no_parse(*_a, **_k):
            return semantic_turn.Parsed(None, "no_provider", "", 0, semantic_turn.Lookup(None))

        async def nothing_known(*_a, **_k):
            return []

        async def no_persist(*_a, **_k):
            return None

        monkeypatch.setattr(semantic_turn, "_parse", no_parse)
        monkeypatch.setattr(semantic_turn, "_known_records", nothing_known)
        monkeypatch.setattr(semantic_turn, "_persist", no_persist)
        bundle = {"messages": [{"role": "user", "content": "Sarah"}]}
        plain = asyncio.run(semantic_turn.prepare_turn(cases.CTX, bundle, "a1"))
        hinted = asyncio.run(semantic_turn.prepare_turn(cases.CTX, {**bundle, "lang_hint": "es"}, "a1"))
        assert plain.lang == "en" and hinted.lang == "es"
