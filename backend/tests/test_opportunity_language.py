"""L05-06: the opportunity cards (the ⌘K call queue) in English and Spanish.

L05-05 put the call queue's spoken line, metric label and caveat in the
request's language but left each card's headline, why and recommended action
English, because opportunity_engine wrote them. These tests hold the cards to
the same rule as the rest of the fast path:

* the English cards are byte-identical to what 26e06e1 produced
  (`opportunity_en_golden.json`, see opportunity_language_cases.py);
* a Spanish card says every product sentence in Spanish;
* record content (names, addresses, a model's own suggestion and summary,
  stated needs, showing outcomes, unrecognised producer text) is never
  translated;
* the language changes sentences only: the same cards, in the same order,
  with the same confidence, ids, deadlines and evidence rows.
"""

from __future__ import annotations

import asyncio
import json
import re
from unittest import mock

import pytest

import command_center
import neoh_intents as intents
import opportunity_engine as oe
import semantic_language as lang_
from tests import opportunity_language_cases as cases

# Words from the English card sentences. A Spanish card containing one leaked.
_ENGLISH = re.compile(
    r"\b(?:Contract|expires|day|days|status|unset|Confirm|closing|parties|today|"
    r"Unnamed|record|This|Recommended|Follow|maintained|recommends|step|Lead score|"
    r"Model summary|Not known|unscored|Verify|seller|parcel|Review|candidates|"
    r"verified|recorded|lookup|May fit|criteria|agree|other buyer|buyers|Ask Neoh|"
    r"reach out|looking|budget|wants|bedrooms|Fits|Wants|Was shown|Motivation|"
    r"untouched|scores|marked|Nobody|worked|dossier|source it|Last updated|unknown|"
    r"draft|new|under_contract)\b"
)

#: Record content in the fixtures that must come through as stored.
_RECORD = ("Book a second showing at 9 Lake Rd", "Call her back",
           "Asked twice about schools near the lake.",
           "Pre-approved; wants to see the lake house again.",
           "a yard for the dog", "near good schools", "12 Oak St — liked", "unlabelled",
           "Sarah Johnson", "Luis Ortega", "12 Oak St", "40 Elm Ave", "5 Pine St, NJ", "77 Bay Rd, MD",
           "9 Lake Rd, Dover", "1 Bay Ct, Lewes", "unparseable", "condo")


def _sentences(card: dict) -> str:
    text = " ".join([card["subject"], card["headline"], card["why"], card["recommended_action"],
                     *(e["label"] for e in card["evidence"]),
                     *(e["value"] for e in card["evidence"])])
    for content in _RECORD:
        text = text.replace(content, " ")
    return text


@pytest.fixture(scope="module")
def spanish():
    return cases.outputs("es")


# ── English is unchanged ────────────────────────────────────────────────────

@pytest.mark.parametrize("lang", [None, "en", "fr", "zz"])
def test_english_cards_are_byte_identical_to_the_frozen_output(lang):
    golden = json.loads(cases.GOLDEN.read_text())
    assert json.loads(json.dumps(cases.outputs(lang), ensure_ascii=False)) == golden


# ── Spanish ─────────────────────────────────────────────────────────────────

def test_every_detector_has_cards_in_the_fixture(spanish):
    assert all(spanish[name] for name in cases.DETECTORS)


def test_spanish_cards_say_no_english_product_sentence(spanish):
    for name, cards in spanish.items():
        for card in cards:
            leaked = _ENGLISH.findall(_sentences(card))
            assert not leaked, (name, card["subject"], leaked)


def test_spanish_changes_sentences_only(spanish):
    english = cases.outputs(None)
    shape = ("kind", "subject_id", "subject_type", "confidence", "score", "value_signal",
             "safe_to_automate", "deadline", "action_type")
    for name in cases.DETECTORS:
        assert len(spanish[name]) == len(english[name])
        for es, en in zip(spanish[name], english[name]):
            assert {k: es[k] for k in shape} == {k: en[k] for k in shape}
            assert [(e["source"], e["as_of"]) for e in es["evidence"]] == \
                   [(e["source"], e["as_of"]) for e in en["evidence"]]


def test_record_content_is_shown_as_stored(spanish):
    nba = {c["subject_id"]: c for c in spanish["_intent_model_opportunities"]}
    # A model's own suggestion and summary are not looked up or rewritten.
    assert nba["c-4"]["headline"] == "Book a second showing at 9 Lake Rd"
    assert nba["c-4"]["why"] == "Pre-approved; wants to see the lake house again."
    assert nba["c-6"]["recommended_action"] == "Call her back"
    # A gap with no label is shown by its code, as in English.
    assert any(e["value"] == "unlabelled" for e in nba["c-5"]["evidence"])
    match = spanish["_listing_buyer_opportunities"][0]
    values = [e["value"] for e in match["evidence"]]
    assert "a yard for the dog" in values and "12 Oak St — liked" in values
    assert match["subject"] == "9 Lake Rd, Dover"


def test_fixed_producer_text_is_translated_by_code(spanish):
    nba = {c["subject_id"]: c for c in spanish["_intent_model_opportunities"]}
    assert nba["c-1"]["headline"] == lang_.t("nba.verify-contact", "es")
    assert nba["c-2"]["recommended_action"] == lang_.t("nba.confirm-property", "es")
    values = [e["value"] for e in nba["c-2"]["evidence"]]
    assert lang_.t("gap.property-lookup.timed_out", "es") in values
    assert lang_.t("gap.contact-history", "es") in values
    assert nba["c-2"]["evidence"][0]["value"] == lang_.t("opp.nba.unscored", "es")


def test_a_title_under_a_fixed_code_but_different_words_stays_as_written():
    """Translation keys on the exact English the producer writes. A title
    someone edited is their text, not the catalogue's."""
    assert oe._known("Verify the phone number first", ("nba.verify-contact",), "es") == \
        "Verify the phone number first"
    assert oe._known("Verify contact information", ("nba.verify-contact",), "es") == \
        "Verifica los datos de contacto"
    assert oe._known("Verify contact information", ("nba.verify-contact",), "en") == \
        "Verify contact information"


def test_matched_criteria_are_said_from_their_values(spanish):
    sarah, luis, _blank = spanish["_listing_buyer_opportunities"]
    assert sarah["why"] == ("Los criterios registrados de Sarah Johnson coinciden en 3 de 4: "
                            "busca en 19901; presupuesto de hasta $450,000; "
                            "quiere 3+ dormitorios, esta tiene 3.")
    assert luis["why"].endswith(". 1 comprador más también encaja.")
    assert _blank["why"].endswith(". Otros 2 compradores también encajan.")
    assert _blank["recommended_action"] == "Pregunta a Neoh por el comprador y esta casa, y luego contacta."


def test_a_note_without_its_key_stays_as_written():
    """An older producer's note (no matched_detail) is not guessed at."""
    match = {"matched_criteria": ["looking in 19901", "garden facing south"],
             "matched_detail": [{"key": "location", "params": {"where": "19901"}}]}
    assert oe._fit_notes(match, "es") == ["busca en 19901", "garden facing south"]
    assert oe._fit_notes(match, "en") == ["looking in 19901", "garden facing south"]


def test_buyer_matching_fallback_name_reads_in_spanish():
    unnamed = {**cases.BUYER_MATCHES["li-3"]}
    unnamed["matches"] = [{**unnamed["matches"][0], "name": "Unnamed contact"}]
    with mock.patch.dict(cases.BUYER_MATCHES, {"li-3": unnamed}):
        es = cases._run("_listing_buyer_opportunities", "es")[2]
        en = cases._run("_listing_buyer_opportunities", None)[2]
    assert es["headline"] == "Puede encajar con Contacto sin nombre"
    assert es["recommended_action"].startswith("Pregunta a Neoh por el comprador ")
    assert en["headline"] == "May fit Unnamed contact"  # English as before


def test_buyer_matching_records_each_matched_notes_key_and_values():
    import buyer_matching as bm

    listing = {"zip_code": "19901", "city": "Dover", "list_price": 310_000, "beds": 3,
               "property_type": "Condo"}
    client = {"id": "c", "full_name": "Ana", "preferences": {
        "target_zips": ["19901"], "budget_max": 300_000, "beds_min": 2, "property_types": ["condo"]}}
    match = bm.match_listing_to_buyer(listing, client)
    assert match.evidence == ["looking in 19901", "$310,000 is 3% over their $300,000 budget",
                              "wants 2+ bedrooms, this has 3", "wants condo"]
    said = oe._fit_notes({"matched_criteria": match.evidence, "matched_detail": match.matched_detail}, "en")
    assert said == match.evidence
    # Each key's English template reproduces buyer_matching's own note.
    for note, detail in zip(match.evidence, match.matched_detail):
        assert lang_.t(f"fit.{detail['key']}", "en", **detail["params"]) == note
    assert oe._fit_notes({"matched_criteria": match.evidence,
                          "matched_detail": match.matched_detail}, "es") == [
        "busca en 19901", "$310,000 supera en un 3% su presupuesto de $300,000",
        "quiere 2+ dormitorios, esta tiene 3", "busca condo"]


def test_every_card_key_has_english_and_spanish():
    prefixes = ("opp.", "fit.", "nba.", "gap.", "dossier.")
    keys = [k for k in lang_.MESSAGES if k.startswith(prefixes)]
    assert keys
    for key in keys:
        assert set(lang_.MESSAGES[key]) >= {"en", "es"}, key
        assert lang_.MESSAGES[key]["es"] != lang_.MESSAGES[key]["en"] or key == "fit.property_type", key


def test_the_fixed_english_matches_what_client_ai_automation_writes():
    """The by-code lookup only works while the catalogue's English is the
    producer's English. Checked against the producer's source."""
    import inspect

    import client_ai_automation

    src = inspect.getsource(client_ai_automation)
    for key in [k for k in lang_.MESSAGES if k.startswith(("nba.", "gap."))]:
        assert f'"{lang_.MESSAGES[key]["en"]}"' in src, key


# ── the language reaches the detectors ──────────────────────────────────────

def test_scan_hands_its_language_to_every_detector(monkeypatch):
    from contextlib import asynccontextmanager

    seen = []

    async def detector(conn, ctx, lang=None):
        seen.append(lang)
        return []

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield object()

    async def coverage(_ctx):
        return {}

    monkeypatch.setattr(oe, "_DETECTORS", (detector, detector))
    monkeypatch.setattr(oe, "tenant_tx", fake_tx)
    monkeypatch.setattr(oe, "perception_coverage", coverage)
    asyncio.run(oe.scan(cases.CTX, lang="es"))
    asyncio.run(oe.scan(cases.CTX))
    assert seen == ["es", "es", None, None]


def test_briefing_passes_its_language_to_the_scan(monkeypatch):
    seen = {}

    async def scan(_ctx, lang=None):
        seen["lang"] = lang
        return {"opportunities": []}

    async def nothing(*_a, **_k):
        return {}

    monkeypatch.setattr(command_center.opportunity_engine, "scan", scan)
    monkeypatch.setattr(command_center, "_changed_since", nothing)

    async def no_median(_ctx):
        return None

    monkeypatch.setattr(command_center, "_market_median", no_median)
    asyncio.run(command_center.briefing(cases.CTX, lang="es"))
    assert seen["lang"] == "es"
    asyncio.run(command_center.briefing(cases.CTX))
    assert seen["lang"] is None  # the Command Center screen stays English


@pytest.mark.parametrize("text, lang", [("who should I call", "en"), ("¿A quién debo llamar?", "es")])
def test_the_call_queue_asks_for_cards_in_the_request_language(text, lang):
    seen = {}
    card = cases.outputs(lang)["_listing_buyer_opportunities"][0]

    async def briefing(_ctx, **kw):
        seen.update(kw)
        return {"attention": {"opportunities": [card], "portfolio": None}}

    with mock.patch("command_center.briefing", briefing):
        out = asyncio.run(intents.ask(cases.CTX, text))
    assert seen.get("lang") == lang
    item = next(b for b in out["blocks"] if b["primitive"] == "call_queue")["props"]["items"][0]
    assert item["headline"] == card["headline"]
    if lang == "es":
        assert item["headline"].startswith("Puede encajar con")
        assert item["action"].startswith("Pregunta a Neoh")
