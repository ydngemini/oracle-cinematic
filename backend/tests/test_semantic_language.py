"""English and Spanish at launch: the semantic layer in both (L05-04).

Owner decision 2026-10-14. What these tests hold:

* the parser contract accepts Spanish (and mixed) requests with verbatim,
  non-ASCII evidence, and refuses translated or accent-stripped quotes;
* every stated restriction in the bilingual fixture set
  (`eval_data/semantic_frames_v1.jsonl`: 65 Spanish, 33 mixed, 31 English)
  is enforced — by the parser's frame, or, when the parser forgets it or
  fails, by the deterministic backstop — and a request that states none gets
  none;
* a request in a language neither layer can vouch for still goes to the
  parser and, when the parser cannot answer, takes the strict pair;
* each ⌘K fast-path command and each explicit command verb has a Spanish twin
  that is handled the same way;
* refusals and failure answers come back in the request's language
  (English by default), with the same machine codes;
* the T05.13 harness scores English, Spanish and mixed separately.

No model is called anywhere here: every gateway is a fake.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from unittest import mock

import pytest

import ai_chat_store
import frame_constraints as fc
import neoh_intents as intents
import neoh_render as render
import semantic_eval
import semantic_frame as sf
import semantic_language as lang_
import semantic_turn
import source_status as ss
from tests.test_frame_constraints import (  # noqa: F401 — fixtures
    ASSISTANT, CTX, NOW, SARAH, FakeGateway, _bundle, _final, _ledger_refusals, _stored,
    _tool_call, chat, turn_env,
)

CASES = semantic_eval.load_cases()
DEV = [c for c in CASES if c.split == "dev"]


def _ids(cases):
    return [c.id for c in cases]


# ── the fixture set itself ──────────────────────────────────────────────────

class TestFixtureSet:
    def test_it_has_the_owner_requested_breadth(self):
        by = {g: [c for c in DEV if c.lang == g] for g in semantic_eval.GROUPS}
        assert len(by["es"]) >= 40 and len(by["mixed"]) >= 20 and len(by["en"]) >= 20
        for group in ("es", "mixed"):
            kinds = {k for c in by[group] for k in c.expect["constraints"]}
            assert kinds == set(fc.KINDS), group     # every kind, in both
            assert any(not c.expect["constraints"] for c in by[group])  # and negatives

    def test_labels_use_the_closed_vocabulary(self):
        for c in CASES:
            assert set(c.expect["constraints"]) <= set(fc.KINDS), c.id
            for g in c.expect.get("goals", []):
                assert g.lstrip("!") in sf.GOAL_EFFECT, c.id


# ── 1. the deterministic backstop reads both languages ──────────────────────

class TestBackstop:
    @pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
    def test_each_utterance_yields_exactly_its_constraint_kinds(self, case):
        got = sorted(c.kind for c in lang_.cues(case.text))
        assert got == sorted(case.expect["constraints"])

    @pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
    def test_each_quote_is_verbatim(self, case):
        for cue in lang_.cues(case.text):
            assert cue.quote and cue.quote in lang_.nfc(case.text)

    @pytest.mark.parametrize("text,kind", [
        ("No le mandes nada todavía", "no_send"),
        ("solo borradores", "draft_only"),
        ("espera mi aprobación", "approval_required"),
        ("no antes del viernes", "not_before"),
        ("no contactes a los Henderson", "do_not_contact"),
        ("NO LE MANDES NADA TODAVIA", "no_send"),          # no accents, capitals
        ("no le mandes nada todavía", "no_send"),     # decomposed í
    ])
    def test_the_five_owner_examples(self, text, kind):
        assert [c.kind for c in lang_.cues(text)] == [kind]

    @pytest.mark.parametrize("text", [
        "¿Por qué no le mandas las casas?",   # a suggestion, not a restriction
        "Why don't you send her the listing?",
        "¿No le mandaste nada?",              # a question about the past
        "No olvides llamar a Dana",
        "No sé qué casas le gustan",
    ])
    def test_suggestions_and_questions_are_not_restrictions(self, text):
        assert lang_.cues(text) == ()

    def test_a_qualified_dont_is_the_qualifier_not_a_blanket_ban(self):
        assert [c.kind for c in lang_.cues("No le mandes nada hasta el lunes")] == ["not_before"]
        assert [c.kind for c in lang_.cues("No envíes nada hasta que yo lo revise")] == ["approval_required"]
        # A clause break keeps both: two separate instructions.
        both = {c.kind for c in lang_.cues("No la llames. Espera mi aprobación para el correo")}
        assert both == {"do_not_contact", "approval_required"}

    def test_a_draft_request_with_a_dont_send_is_drafts_only(self):
        assert [c.kind for c in lang_.cues("Prepara el correo pero no lo mandes")] == ["draft_only"]
        assert [c.kind for c in lang_.cues("Draft the email but don't send it")] == ["draft_only"]


class TestDetection:
    @pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
    def test_reply_language(self, case):
        found = lang_.detect(case.text)
        assert found.lang != "other"
        if case.lang == "es":
            assert found.reply == "es"
        if case.lang == "en":
            assert found.reply == "en"

    @pytest.mark.parametrize("text", [
        "Não mande nada para ela ainda", "Ne lui envoie rien pour l'instant",
        "Schick ihr noch nichts", "不要给她发消息",
    ])
    def test_other_languages_are_not_mistaken_for_english_or_spanish(self, text):
        assert lang_.detect(text).lang == "other" and not lang_.recognised(text)

    @pytest.mark.parametrize("text", ["ok", "Sarah Chen", "412 Delaware Ave", ""])
    def test_too_little_to_tell_is_english_and_still_recognised(self, text):
        assert lang_.reply_language(text) == "en" and lang_.recognised(text)

    def test_a_short_turn_keeps_the_conversation_language(self):
        assert lang_.reply_language("ok", previous="es") == "es"
        assert lang_.reply_language("Sarah", previous=None) == "en"

    def test_fold_keeps_positions(self):
        text = "Mándale “eso” a José’s"
        assert len(lang_.fold(text)) == len(lang_.nfc(text))


# ── 2. the parser contract: verbatim, non-ASCII evidence ────────────────────

def _one_constraint_answer(quote, kind="no_send", goal="send_message"):
    ev = [{"source": "utterance", "quote": quote}]
    return json.dumps({
        "goals": [{"label": goal, "negated": True, "evidence": ev}], "mentions": [], "times": [],
        "constraints": [{"kind": kind, "text": kind, "time": None, "mention": None, "evidence": ev}],
        "scope": "unspecified", "required_data": [], "effect": "read",
        "unfilled_slots": [], "ambiguities": [],
    }, ensure_ascii=False)


class TestParserContract:
    @pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
    def test_the_labelled_frame_validates_with_spanish_quotes(self, case):
        frame = sf.frame_from_model(semantic_eval.oracle_answer(case), case.text, case.known)
        kinds = sorted(c.kind for c in frame.constraints)
        assert kinds == sorted(case.expect["constraints"])

    def test_a_decomposed_request_matches_a_composed_quote(self):
        request = "No le mandes nada todavía"       # í as i + combining accent
        frame = sf.frame_from_model(_one_constraint_answer("nada todavía"), request)
        assert frame.constraints[0].kind == "no_send"

    def test_a_typographic_apostrophe_matches_a_plain_one(self):
        frame = sf.frame_from_model(_one_constraint_answer("Don't message her"), "Don’t message her yet")
        assert frame.constraints[0].kind == "no_send"

    @pytest.mark.parametrize("quote", [
        "nada todavia",              # accents dropped: not verbatim
        "don't send her anything",   # translated
        "No le envíes nada",         # paraphrased
    ])
    def test_a_quote_that_is_not_in_the_request_is_refused(self, quote):
        with pytest.raises(sf.FrameRejected) as err:
            sf.frame_from_model(_one_constraint_answer(quote), "No le mandes nada todavía")
        assert err.value.reason == "ungrounded_evidence"

    def test_the_prompt_tells_the_model_both_languages_and_not_to_translate(self):
        assert "Spanish" in sf.SYSTEM and "Never translate a quote" in sf.SYSTEM
        for example in ("no le mandes nada todavía", "solo borradores", "espera mi aprobación",
                        "no antes del viernes", "no contactes a"):
            assert example in sf.SYSTEM

    def test_the_request_reaches_the_model_in_its_own_characters(self):
        gw = FakeGateway(_one_constraint_answer("todavía"))
        asyncio.run(sf.extract_frame("No le mandes nada todavía", now=NOW,
                                     timezone="America/New_York", gateway=gw))
        assert gw.calls == 1


# ── 3. end to end through the chat turn, per fixture ────────────────────────

class _LabelGW(semantic_eval.LabelGateway):
    pass


def _turn(text, gateway, monkeypatch, history=()):
    monkeypatch.setattr(semantic_turn, "GATEWAY", gateway)
    return asyncio.run(semantic_turn.prepare_turn(CTX, _bundle(text, history=history), ASSISTANT, now=NOW))


def _contact_refused(turn):
    refusal = fc.check_tool(turn.active, "draft_sms", targets=frozenset({SARAH}), now=NOW, lang=turn.lang)
    return refusal


class TestTurns:
    @pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
    def test_a_correct_parser_is_enforced_as_stated(self, case, chat, monkeypatch):
        turn = _turn(case.text, _LabelGW(CASES), monkeypatch)
        assert turn.status == "parsed" and turn.backstop == []
        assert semantic_eval._preserved(case.expect["constraints"], frozenset(c.kind for c in turn.active))
        assert bool(turn.active) == bool(case.expect["constraints"])

    @pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
    def test_a_parser_that_forgets_the_constraint_is_backed_up(self, case, chat, monkeypatch):
        turn = _turn(case.text, _LabelGW(CASES, forget_constraints=True), monkeypatch)
        kinds = frozenset(c.kind for c in turn.active)
        assert semantic_eval._preserved(case.expect["constraints"], kinds)
        if not case.expect["constraints"]:
            assert turn.active == []              # nothing invented
        if set(case.expect["constraints"]) & {"no_send", "do_not_contact"}:
            refusal = _contact_refused(turn)
            assert refusal is not None
            assert refusal.message == lang_.t(f"refuse.{refusal.kind}", turn.lang,
                                              **({"who": fc._who(next(c for c in turn.active if c.kind == refusal.kind), turn.lang)}
                                                 if refusal.kind == "do_not_contact" else {}))

    @pytest.mark.parametrize("case", [c for c in CASES if c.lang != "en"],
                             ids=_ids([c for c in CASES if c.lang != "en"]))
    def test_a_parser_that_translates_its_quotes_is_refused_and_backed_up(self, case, chat, monkeypatch):
        turn = _turn(case.text, _LabelGW(CASES, translate=True), monkeypatch)
        assert turn.frame is None and turn.status == "unavailable"
        assert semantic_eval._preserved(case.expect["constraints"], frozenset(c.kind for c in turn.active))

    def test_the_stored_summary_carries_the_language_and_no_words(self, chat, monkeypatch):
        text = "No le mandes nada a Sarah todavía"
        _turn(text, FakeGateway(sf.FrameUnavailable("down")), monkeypatch)
        stored = _stored(chat)
        assert stored["lang"] == "es" and stored["detected"] == "es" and stored["backstop"] == ["no_send"]
        blob = json.dumps(stored, ensure_ascii=False)
        assert "Sarah" not in blob and "mandes" not in blob and "todavía" not in blob

    def test_a_later_short_turn_answers_in_the_conversation_language(self, chat, monkeypatch):
        first = _turn("No le mandes nada todavía", FakeGateway(sf.FrameUnavailable("x")), monkeypatch)
        prior = [{"id": str(uuid.uuid4()), "role": "user", "content": "No le mandes nada todavía"},
                 {"id": str(uuid.uuid4()), "role": "assistant", "content": "Entendido.",
                  "metadata": {"semantic_frame": first.summary()}}]
        turn = _turn("ok", FakeGateway(sf.FrameUnavailable("x")), monkeypatch, history=prior)
        assert turn.lang == "es" and [c.kind for c in turn.active] == ["no_send"]


# ── 4. fail closed for a language nobody here can read ──────────────────────

def _frame_answer(text, constraints=()):
    ev = [{"source": "utterance", "quote": text}]
    return json.dumps({
        "goals": [{"label": "question", "negated": False, "evidence": ev}], "mentions": [], "times": [],
        "constraints": [{"kind": k, "text": k, "time": None, "mention": None, "evidence": ev}
                        for k in constraints],
        "scope": "unspecified", "required_data": [], "effect": "read",
        "unfilled_slots": [], "ambiguities": [],
    }, ensure_ascii=False)


class TestUnrecognisedLanguage:
    PT = "Não mande nada para ela ainda"
    FR = "Montre-moi les maisons pour elle"

    @pytest.mark.parametrize("failure", [sf.FrameUnavailable("down"), "not json"])
    def test_parser_failure_takes_the_strict_pair(self, chat, monkeypatch, failure):
        answers = [failure, failure] if isinstance(failure, str) else [failure]
        turn = _turn(self.PT, FakeGateway(*answers), monkeypatch)
        assert turn.detected == "other" and turn.frame is None
        assert sorted(c.kind for c in turn.active) == ["approval_required", "no_send"]
        assert turn.backstop == ["no_send", "approval_required"]
        assert _contact_refused(turn).code == "FRAME_NO_SEND"
        assert turn.lang == "en"     # answered in the default language

    def test_it_still_goes_to_the_parser_and_the_parser_reading_stands(self, chat, monkeypatch):
        gw = FakeGateway(_frame_answer(self.PT, ["no_send"]))
        turn = _turn(self.PT, gw, monkeypatch)
        assert gw.calls == 1 and turn.status == "parsed" and turn.backstop == []
        assert [c.kind for c in turn.active] == ["no_send"]

    def test_a_parsed_request_with_no_restriction_gets_none(self, chat, monkeypatch):
        turn = _turn(self.FR, FakeGateway(_frame_answer(self.FR)), monkeypatch)
        assert turn.detected == "other" and turn.active == []

    @pytest.mark.parametrize("text", ["ok", "Sarah Chen", "What did she say about the kitchen?",
                                      "¿Qué dijo de la cocina?"])
    def test_a_recognised_request_with_no_restriction_is_not_made_strict(self, chat, monkeypatch, text):
        turn = _turn(text, FakeGateway(sf.FrameUnavailable("down")), monkeypatch)
        assert turn.active == []

    def test_a_mission_objective_in_another_language_fails_closed_too(self, monkeypatch):
        monkeypatch.delenv(fc.ENV_FLAG, raising=False)
        monkeypatch.setattr(semantic_turn, "GATEWAY", FakeGateway(sf.FrameUnavailable("x")))
        stated = asyncio.run(semantic_turn.mission_constraints(CTX, self.PT, now=NOW))
        kinds = sorted(c["kind"] for c in stated["semantic_frame"]["active"])
        assert kinds == ["approval_required", "no_send"]


# ── 5. refusals and failure answers in the request's language ───────────────

class TestRefusalLanguage:
    def test_every_message_exists_in_both_languages_with_the_same_slots(self):
        import string

        for key, entry in lang_.MESSAGES.items():
            assert set(entry) == set(lang_.LANGS), key
            slots = [{f.lower() for _, f, _, _ in string.Formatter().parse(entry[g]) if f}
                     for g in lang_.LANGS]
            assert slots[0] == slots[1], key
            if key != "prompt.reply_in":
                assert all(entry[g].strip() for g in lang_.LANGS), key

    @pytest.mark.parametrize("kind,tool,code", [
        ("no_send", "draft_sms", "FRAME_NO_SEND"),
        ("do_not_contact", "call_contact", "FRAME_DO_NOT_CONTACT"),
        ("draft_only", "call_contact", "FRAME_DRAFT_ONLY"),
        ("approval_required", "update_client", "FRAME_APPROVAL_REQUIRED"),
        ("not_before", "draft_email", "FRAME_NOT_BEFORE"),
    ])
    def test_tool_refusals(self, kind, tool, code):
        later = datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc)
        stated = [fc.ActiveConstraint(kind=kind, origin="t", stated_at=NOW,
                                      not_before=later if kind == "not_before" else None)]
        en = fc.check_tool(stated, tool, targets=frozenset(), now=NOW)
        es = fc.check_tool(stated, tool, targets=frozenset(), now=NOW, lang="es")
        assert en.code == es.code == code
        assert en.message == fc.check_tool(stated, tool, targets=frozenset(), now=NOW, lang="en").message
        assert es.message != en.message and es.message == lang_.MESSAGES[f"refuse.{kind}"]["es"].format(
            who=lang_.t("who.anyone", "es"), t="2026-10-16 04:00 UTC")
        assert es.receipt()["error"] == es.message and es.receipt()["constraint"] == kind
        # An unknown language is answered in English.
        assert fc.check_tool(stated, tool, targets=frozenset(), now=NOW, lang="pt").message == en.message

    def test_the_english_wording_is_unchanged(self):
        stated = [fc.ActiveConstraint(kind="no_send", origin="t", stated_at=NOW)]
        assert fc.check_tool(stated, "draft_sms", targets=frozenset(), now=NOW).message.startswith(
            "You asked me not to send anything yet, so I did not prepare this message or request.")

    def test_the_turn_binding_carries_the_language_to_the_chokepoint(self):
        semantic_turn.bind(ASSISTANT, [fc.ActiveConstraint(kind="no_send", origin="t", stated_at=NOW)], "es")
        assert semantic_turn.language_for(ASSISTANT) == "es"
        assert semantic_turn.language_for(str(uuid.uuid4())) == "en"
        semantic_turn.bind(ASSISTANT, [], "xx")
        assert semantic_turn.language_for(ASSISTANT) == "en"

    def test_spanish_chat_turn_refusal_reaches_the_model_in_spanish(self, turn_env, chat, monkeypatch):
        import llm_gateway

        seen = []

        async def capture(messages, tools, **_kw):
            for m in messages:
                if m.get("role") == "tool":
                    try:
                        seen.append(json.loads(m.get("content") or "{}"))
                    except ValueError:
                        pass
            return turn_env["script"].pop(0)

        monkeypatch.setattr(llm_gateway, "tool_round", capture)
        turn_env["script"][:] = [_tool_call("draft_sms", {"client_id": SARAH, "body": "Hola"}), _final("ok")]
        semantic_turn.GATEWAY = FakeGateway(sf.FrameUnavailable("down"))   # backstop only
        assistant = str(uuid.uuid4())
        asyncio.run(turn_env["agent"]._generate(CTX, _bundle("No le mandes nada todavía"), assistant))
        assert turn_env["dispatched"] == []
        [row] = _ledger_refusals(chat)
        assert row[6] == "FRAME_NO_SEND"
        assert {"ok": False, "refused": True, "constraint": "no_send",
                "error": lang_.MESSAGES["refuse.no_send"]["es"]} in seen

    def test_mission_step_reasons(self):
        stated = [fc.ActiveConstraint(kind="no_send", origin="mission", stated_at=NOW)]
        assert fc.check_mission_step(stated, "sms", subject_ids=frozenset(), now=NOW).withhold == \
            "stated constraint: no sends"
        es = fc.check_mission_step(stated, "sms", subject_ids=frozenset(), now=NOW, lang="es")
        assert es.withhold == "restricción indicada: no enviar nada"

    def test_a_spanish_mission_objective_stores_its_language(self, monkeypatch):
        monkeypatch.delenv(fc.ENV_FLAG, raising=False)
        monkeypatch.setattr(semantic_turn, "GATEWAY", FakeGateway(sf.FrameUnavailable("x")))
        stated = asyncio.run(semantic_turn.mission_constraints(CTX, "Reactiva mi cartera, solo borradores", now=NOW))
        assert stated["semantic_frame"]["lang"] == "es"
        assert [c["kind"] for c in stated["semantic_frame"]["active"]] == ["draft_only"]
        assert semantic_turn.mission_language({"constraints": json.dumps(stated)}) == "es"
        assert semantic_turn.mission_language({"constraints": "garbage"}) == "en"

    def test_prompt_block_asks_the_model_to_explain_in_spanish(self):
        stated = [fc.ActiveConstraint(kind="no_send", origin="t", stated_at=NOW)]
        assert "in Spanish" in fc.prompt_block(stated, "es")
        assert "Spanish" not in fc.prompt_block(stated, "en")

    FAILED = {"people": ss.failure(TimeoutError())}

    def test_sources_failed(self):
        en = render.sources_failed("show_person", "Sarah", self.FAILED)
        es = render.sources_failed("show_person", "Sarah", self.FAILED, "es")
        assert en["spoken"].startswith("I couldn't search people for “Sarah”: the search timed out.")
        assert es["spoken"].startswith("No pude buscar personas para “Sarah”: la búsqueda agotó el tiempo de espera.")
        assert "sin coincidencias" in es["spoken"] and es["degraded"] is True
        assert es["blocks"][0]["props"]["items"][0]["detail"].startswith("No se comprobó")
        assert en["source_status"] == es["source_status"]

    def test_misses_choices_and_empty_queue(self):
        assert render.no_match("show_person", "Sarah", "es")["spoken"] == "Nada coincide con “Sarah”."
        assert render.no_match("show_person", "Sarah")["spoken"] == "Nothing matches “Sarah”."
        assert render.no_match("deal_blocker", "Elm", "es")["spoken"] == "Ningún trato coincide con “Elm”."
        two = [{"id": "1", "label": "Sarah A"}, {"id": "2", "label": "Sarah B"}]
        es = render.person_choices(two, "Sarah", "es")
        assert es["spoken"] == "2 personas coinciden con “Sarah”." and es["blocks"][0]["props"]["title"] == "¿Cuál?"
        nobody = render.who_to_call({"detectors_failed": ["a", "b"], "opportunities": []}, "es")
        assert "2 detectores fallaron" in nobody["spoken"]
        failed = render.properties_for("Sarah", [], {"status": "timed_out"}, "es")
        assert failed["spoken"].startswith("No pude consultar los registros públicos de Sarah")

    def test_the_fast_path_answers_a_failed_spanish_search_in_spanish(self, monkeypatch):
        import search_api

        async def failing(ctx, query, kinds, limit):
            return {"results": [], "degraded": ["people"],
                    "source_status": {"people": ss.failure(TimeoutError())}}

        monkeypatch.setattr(search_api, "search", failing)
        es = asyncio.run(intents.ask(CTX, "muéstrame a Sarah Chen"))
        en = asyncio.run(intents.ask(CTX, "show Sarah Chen"))
        assert es["spoken"].startswith("No pude buscar personas para “Sarah Chen”")
        assert en["spoken"].startswith("I couldn't search people for “Sarah Chen”")
        assert es["degraded"] and en["degraded"] and not es["fallthrough"]

    def test_a_verified_spanish_miss(self, monkeypatch):
        import search_api

        async def empty(ctx, query, kinds, limit):
            return {"results": [], "degraded": [], "source_status": {k: ss.checked(matched=0) for k in kinds}}

        monkeypatch.setattr(search_api, "search", empty)
        monkeypatch.setattr(search_api, "looks_like_address", lambda q: False)
        out = asyncio.run(intents.ask(CTX, "busca casas para Zelda"))
        assert out["spoken"] == "Nadie coincide con “Zelda”." and out["degraded"] is False


# ── 6. fast-path parity: every handled English command has a Spanish twin ───

def _route(text):
    for intent in intents.INTENTS:
        match = intent.pattern.match(text)
        if match:
            return intent.name, intent.lang, match.groupdict()
    return None, None, {}


FAST_PATH_TWINS = [
    ("who should I call", "¿A quién debo llamar?"),
    ("Who should I call first today?", "¿A quién debo llamar primero hoy?"),
    ("who do i call next", "a quien llamo ahora"),
    ("who should I follow up with", "¿Con quién tengo que hacer seguimiento?"),
    ("my call list", "mi lista de llamadas"),
    ("next best action", "siguiente mejor acción"),
    ("show Sarah Chen", "muéstrame a Sarah Chen"),
    ("open Sarah Chen", "abre a Sarah Chen"),
    ("tell me about Marcus", "háblame de Marcus"),
    ("who is Dana?", "¿quién es Dana?"),
    ("pull up Dana", "tráeme a Dana"),
    ("find properties for Sarah Chen", "busca propiedades para Sarah Chen"),
    ("show me houses for Marcus", "muéstrame casas para Marcus"),
    ("get listings for Dana", "dame listados para Dana"),
    ("what's holding 155 Main up", "¿qué está frenando 155 Main?"),
    ("what is blocking the Delaware deal", "¿qué está bloqueando el trato de Delaware?"),
    ("what's stuck on 9 Elm", "¿por qué está atascado 9 Elm?"),
]


class TestFastPathParity:
    @pytest.mark.parametrize("english,spanish", FAST_PATH_TWINS)
    def test_the_spanish_twin_is_handled_like_the_english(self, english, spanish):
        en_name, en_lang, en_groups = _route(english)
        es_name, es_lang, es_groups = _route(spanish)
        assert en_name is not None and en_lang == "en"
        assert es_name == en_name and es_lang == "es"
        if "name" in en_groups:
            assert es_groups["name"] == en_groups["name"]

    def test_the_deal_noun_phrase_is_not_the_deal(self):
        assert _route("¿qué está frenando el trato de Delaware?")[2]["deal"] == "Delaware"
        assert _route("que esta bloqueando la venta de 155 Main")[2]["deal"] == "155 Main"

    def test_every_english_intent_has_a_spanish_pattern(self):
        en = {i.name for i in intents.INTENTS if i.lang == "en"}
        es = {i.name for i in intents.INTENTS if i.lang == "es"}
        assert en == es

    def test_typed_without_accents(self):
        for text in ("muestrame a Sarah", "quien es Dana", "a quien debo llamar", "que esta frenando 155 Main"):
            assert _route(text)[1] == "es", text

    @pytest.mark.parametrize("text", [
        "resume mi pipeline en una línea", "redacta un correo para Sarah",
        "qué le digo a un vendedor preocupado por las tasas", "hola", "No le mandes nada todavía",
    ])
    def test_spanish_without_an_interface_still_reaches_the_model(self, text):
        assert _route(text)[0] is None

    def test_the_english_routing_is_unchanged(self):
        for text, name in (("show Sarah Chen", "show_person"), ("find properties for Sarah", "properties_for"),
                           ("who should I call", "who_to_call"), ("what's holding 155 Main up", "deal_blocker")):
            assert _route(text)[:2] == (name, "en")

    def test_a_spanish_who_to_call_with_a_record_open_goes_to_the_model(self):
        out = asyncio.run(intents.ask(CTX, "¿A quién debo llamar?", has_record=True))
        assert out["fallthrough"]


class TestCommandParity:
    CLASSIFY = [
        ("email John about the offer", "Envíale un correo a John sobre la oferta"),
        ("send an email to Sarah", "Escríbele un correo a Sarah"),
        ("text Dana", "Mándale un texto a Dana"),
        ("send a text to Dana", "Envíale un mensaje de texto a Dana"),
        ("sms Dana", "Textea a Dana"),
        ("call Marcus", "Llama a Marcus"),
        ("phone Marcus", "Llámalo"),
        ("schedule a meeting with Bob", "Agenda una reunión con Bob"),
        ("book a showing with Bob", "Programa una visita con Bob"),
    ]

    @pytest.mark.parametrize("english,spanish", CLASSIFY)
    def test_classify(self, english, spanish):
        from commands_api import _classify_explicit

        assert _classify_explicit(spanish) is _classify_explicit(english)

    @pytest.mark.parametrize("text", ["message her", "mándale un mensaje", "marca como vendido", "hola"])
    def test_classify_still_refuses_what_names_no_channel(self, text):
        from fastapi import HTTPException

        from commands_api import _classify_explicit

        with pytest.raises(HTTPException) as err:
            _classify_explicit(text)
        assert err.value.status_code == 422

    PARSE = [
        ("Email John about the offer", "Envíale un correo a John sobre la oferta", "John"),
        ("Text John about the offer", "Mándale un texto a John sobre la oferta", "John"),
        ("Call Dana about the offer", "Llama a Dana sobre la oferta", "Dana"),
        ("Schedule a call with Sarah", "Programa una llamada con Sarah", "Sarah"),
        ("Draft an assignment contract for Alex", "Prepara un contrato de cesión para Alex", "Alex"),
        ("Calculate the MAO for this property", "Calcula la oferta máxima permitida para esta propiedad", None),
        ("Send Dana a text about our call tomorrow", "Mándale un texto a Dana sobre nuestra llamada de mañana", "Dana"),
        ("Email Sarah the text of the counteroffer", "Envíale un correo a Sarah con el texto de la contraoferta", "Sarah"),
        ("Call him about their last text", "Llámalo por su último texto", None),
    ]

    @pytest.mark.parametrize("english,spanish,name", PARSE)
    def test_parse_intent_and_name(self, english, spanish, name):
        from commands_api import _name_hint, _parse_intent

        intent = _parse_intent(english)
        assert _parse_intent(spanish) is intent
        if name:
            assert _name_hint(spanish, intent) == name

    def test_spanish_names_keep_their_accents(self):
        from commands_api import ParsedIntent, _name_hint

        assert _name_hint("Llama a José Pérez sobre la inspección", ParsedIntent.CALL) == "José Pérez"
        assert _name_hint("Llama a María de la Cruz y pregúntale", ParsedIntent.CALL) == "María de la Cruz"

    def test_de_acuerdo_is_not_an_agreement(self):
        from commands_api import ParsedIntent, _parse_intent

        assert _parse_intent("Llámala, de acuerdo?") is ParsedIntent.CALL


# ── 7. the T05.13 harness scores the languages separately ───────────────────

class TestHarness:
    def test_a_correct_parser_scores_perfectly_in_each_language(self):
        result = asyncio.run(semantic_eval.run(CASES, gateway=semantic_eval.LabelGateway(CASES)))
        assert set(result["by_lang"]) == {"en", "es", "mixed"}
        for group, scores in result["by_lang"].items():
            assert scores["n"] == sum(1 for c in CASES if c.lang == group)
            assert scores["parsed_rate"] == 1.0, group
            assert scores["intent_accuracy"] == 1.0, group
            assert scores["constraint_exact"] == 1.0, group
            assert scores["constraint_preservation"] == 1.0, group
            assert scores["over_restriction_rate"] == 0.0, group
            assert scores["unauthorized_actions"] == 0, group

    def test_a_forgetful_parser_is_scored_as_one_and_the_backstop_separately(self):
        result = asyncio.run(semantic_eval.run(
            CASES, gateway=semantic_eval.LabelGateway(CASES, forget_constraints=True)))
        for group, scores in result["by_lang"].items():
            assert scores["constraint_recall"] == 0.0, group        # the parser missed them
            assert scores["constraint_preservation"] == 1.0, group  # the product still held them
            assert scores["unauthorized_actions"] == 0, group

    def test_a_failure_in_one_language_shows_in_that_language_only(self):
        spanish = {c.text for c in CASES if c.lang == "es"}

        class SpanishBlind(semantic_eval.LabelGateway):
            async def complete(self, prompt, **kw):
                text = prompt.split("<request>", 1)[1].split("</request>", 1)[0]
                if text in spanish:
                    raise RuntimeError("no Spanish model")
                return await super().complete(prompt, **kw)

        result = asyncio.run(semantic_eval.run(CASES, gateway=SpanishBlind(CASES)))
        assert result["by_lang"]["es"]["parsed_rate"] == 0.0
        assert result["by_lang"]["en"]["parsed_rate"] == 1.0
        assert result["by_lang"]["mixed"]["parsed_rate"] == 1.0

    def test_backstop_only_scores_without_a_model(self):
        result = semantic_eval.score_backstop(CASES)
        assert set(result["by_lang"]) == {"en", "es", "mixed"}
        assert result["overall"]["constraint_preservation"] == 1.0
        assert result["overall"]["unauthorized_actions"] == 0

    def test_the_script_refuses_to_spend_without_approval(self, capsys):
        from scripts import eval_semantic_frame as script

        with mock.patch.object(semantic_eval, "run", side_effect=AssertionError("would spend")):
            assert script.main(["--split", "held_out"]) == 2
        assert "--approve-spend" in capsys.readouterr().err

    def test_the_script_backstop_mode_is_free(self, capsys):
        from scripts import eval_semantic_frame as script

        with mock.patch.object(semantic_eval, "run", side_effect=AssertionError("would spend")):
            assert script.main(["--backstop-only", "--lang", "es"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["mode"] == "backstop_only" and set(out["by_lang"]) == {"es"}
