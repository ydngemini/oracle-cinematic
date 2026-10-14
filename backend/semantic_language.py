"""semantic_language — which language a request is in, and the words of a refusal.

Prompt 05, slice L05-04. Owner decision 2026-10-14: the semantic layer
supports English AND Spanish at launch. This module holds the three pieces
that are about language rather than meaning:

1. **Detection** (`detect`, `reply_language`). Deterministic and cheap: word
   lists, Spanish-only punctuation and script, no model. It answers two
   questions — which language to answer a refusal or a failed lookup in (en
   or es; English by default), and whether the request is in a language the
   deterministic layers below can read at all (`Detection.lang == "other"`).
   No per-user language setting exists (there is no locale field anywhere in
   the schema), so the request itself is the only signal.

2. **Constraint cues** (`cues`). A small bilingual lexicon of the phrases that
   state a restriction — "don't message her yet" / "no le mandes nada
   todavía", "just drafts" / "solo borradores", "wait for my approval" /
   "espera mi aprobación", "not before Friday" / "no antes del viernes",
   "don't contact …" / "no contactes a …". It is a BACKSTOP to the model
   parser, never a replacement: `frame_constraints.reinforce` adds a cue's
   restriction only when the parser's frame does not already carry it (or
   the parser failed). A cue can only ever add a restriction; nothing here
   lifts one or authorises anything. Each cue carries its verbatim span of
   the request, so it is as checkable as a frame's evidence.

3. **The catalogue** (`t`). The product sentences for refusals and failure
   answers, in English and Spanish. One table, so a new message cannot exist
   in one language only (tests/test_semantic_language.py checks it).

Nothing here reads or writes the database, calls a model, or logs the words
of a request (C5.4).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

#: The languages the semantic layer answers in. English is the default.
LANGS: tuple[str, ...] = ("en", "es")
DEFAULT_LANG = "en"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

#: Typographic marks phone keyboards insert. Mapped one character to one, so
#: positions in the folded text are positions in the original.
_PUNCT = str.maketrans({
    "‘": "'", "’": "'", "ʼ": "'", "“": '"', "”": '"',
    "«": '"', "»": '"', " ": " ",
})


def nfc(text: str) -> str:
    """Composed Unicode with typographic quotes made plain.

    "todavía" typed on one keyboard is one code point for the í, on another
    two; a verbatim quote must match either way.
    """
    return unicodedata.normalize("NFC", text or "").translate(_PUNCT)


def fold(text: str) -> str:
    """Lower-case, accents removed, SAME LENGTH as `nfc(text)`.

    Index i of the result is character i of `nfc(text)`, so a match found in
    the folded text is quoted verbatim from the original.
    """
    out = []
    for ch in nfc(text):
        base = unicodedata.normalize("NFD", ch)[:1] or ch
        low = base.lower()
        out.append(low if len(low) == 1 else base)
    return "".join(out)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")

#: Words that are common in English requests and NOT Spanish words. Words
#: both languages share ("a", "no", "me", "solo") are deliberately absent.
_EN = frozenset("""
the an and or of to for with about from at on in into is are was be been it its this that these those
i you he she we they my your his her our their them him us what who whom which when where why how
should could would can will do does did don't doesn't didn't isn't won't can't not yet just only please
show find get pull open tell call text email send message draft wait until after before approval approve
manager broker signs sign off houses house homes home properties property listings deal deals client clients
anything nothing anyone everyone today tomorrow next week monday tuesday wednesday thursday friday saturday sunday
let's hold stuck blocking holding up list queue best action follow reach out contact some any
""".split())

#: Spanish words that are not English words (folded: no accents).
_ES = frozenset("""
el la los las lo le les un una unos unas del al de y o u que quien quienes cual cuales cuando donde como por
para con sin sobre hasta desde antes despues mi mis tu tus su sus nuestro nuestra nuestros nuestras ella ellas
ellos el usted ustedes yo nosotros esto esta este estos estas eso esa ese esos esas aquel aquella
es son esta estan fue ser estar hay tiene tengo tienes puedo puedes debo deberia debe tengo hace
nada nadie todavia aun ya tambien muy mas menos solo solamente unicamente si porque pero
manda mandes mande mandale mandar envia envie envies enviale enviar escribe escribas escribele redacta redactale
llama llames llame llamala llamalo llamale llamar contacta contactes contactar contacto habla hables
busca buscame encuentra encuentrame muestra muestrame ensename abre abreme dame trae traeme dime cuentame hablame
espera esperes esperar aprobacion apruebe apruebo aprobar autorizacion autorice gerente jefe jefa
borrador borradores casa casas propiedades propiedad vivienda viviendas inmueble inmuebles cliente clientes
trato tratos operacion negocio cierre venta compra llamada llamadas lista cola mensaje correo texto cita reunion
hoy manana semana proxima lunes martes miercoles jueves viernes sabado domingo
gracias favor oye vale bueno entonces luego ahora necesito quiero
""".split())

#: A handful of words for the languages people are likeliest to try next.
#: Only used to tell "another language" from "too short to tell".
_OTHER = {
    "pt": frozenset("nao voce ainda ela ele dele dela com uma isso obrigado tambem envie mande nenhum "
                    "nunca mensagem rascunho aprovacao antes depois sexta segunda casas para".split()),
    "fr": frozenset("le les des une est pas ne lui elle il avec pour sur dans ce cette encore rien "
                    "envoie envoyer brouillon approbation avant vendredi lundi maisons merci".split()),
    "it": frozenset("il gli della non ancora niente nulla lei lui con per sono questo questa "
                    "invia mandare bozza approvazione prima venerdi lunedi case grazie".split()),
    "de": frozenset("der die das und nicht noch nichts sie er mit fur ist ein eine schick senden "
                    "entwurf genehmigung vor freitag montag hauser danke bitte".split()),
}
#: Words shared by Spanish and one of the others count for Spanish only.
_OTHER = {k: v - _ES - _EN for k, v in _OTHER.items()}

_ES_MARKS = frozenset("¿¡ñ")
#: Latin letters Spanish never uses ("não", "ça", "très", "für").
_OTHER_MARKS = frozenset("ãõçèêëâîôûàìòùäöß")


@dataclass(frozen=True)
class Detection:
    """`lang`: en | es | mixed | other | undetermined. Scores are word hits."""
    lang: str
    en: int = 0
    es: int = 0
    other: int = 0

    @property
    def reply(self) -> str:
        """The language to answer in, with no history: en or es."""
        if self.lang == "es":
            return "es"
        if self.lang == "mixed" and self.es > self.en:
            return "es"
        return DEFAULT_LANG


def _non_latin_share(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    other = sum(1 for ch in letters if not unicodedata.name(ch, "").startswith("LATIN"))
    return other / len(letters)


def detect(text: str) -> Detection:
    """Which language a request is written in. Deterministic; no model.

    * ``en`` / ``es``: one language clearly dominates.
    * ``mixed``: both appear, the smaller at least a third of the larger.
    * ``other``: another script, or another language's words outnumber both.
    * ``undetermined``: too little signal ("ok", a bare name) — treated as
      English for answers and as a recognised language for the backstop.
    """
    raw = nfc(text)
    if _non_latin_share(raw) > 0.5:
        return Detection("other", other=1)
    folded = fold(raw)
    words = _WORD.findall(folded)
    en = sum(1 for w in words if w in _EN or w.replace("'", "") in _EN)
    es = sum(1 for w in words if w in _ES)
    es += sum(1 for ch in raw.lower() if ch in _ES_MARKS)
    other = max((sum(1 for w in words if w in vocab) for vocab in _OTHER.values()), default=0)
    other += sum(1 for ch in raw.lower() if ch in _OTHER_MARKS)
    if other >= 2 and other > max(en, es):
        return Detection("other", en, es, other)
    if en == 0 and es == 0:
        return Detection("undetermined", 0, 0, other)
    if en and es and min(en, es) * 3 >= max(en, es):
        return Detection("mixed", en, es, other)
    return Detection("en" if en >= es else "es", en, es, other)


def reply_language(text: str, previous: Optional[str] = None) -> str:
    """en or es. A request too short to tell keeps the conversation's
    previous language; with no history it is English."""
    found = detect(text)
    if found.lang in ("undetermined", "other") and previous in LANGS:
        return previous
    return found.reply


def recognised(text: str) -> bool:
    """True when the deterministic backstop can read this request (English,
    Spanish, a mix, or too short to be anything else)."""
    return detect(text).lang != "other"


# ---------------------------------------------------------------------------
# Constraint cues (the backstop lexicon)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Cue:
    kind: str     # one of frame_constraints.KINDS
    quote: str    # the verbatim span of the request
    lang: str     # which lexicon matched: en | es


def _alt(*words: str) -> str:
    return "(?:" + "|".join(words) + ")"


# Times a "not before" can point at. Folded text: no accents, lower case.
_EN_TIME = _alt(
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)day", "tomorrow", "tonight", r"next\s+week",
    r"the\s+weekend", "noon", r"\d{1,2}(?::\d{2})?\s*(?:am|pm)", r"the\s+\d{1,2}(?:st|nd|rd|th)?",
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}",
)
_ES_TIME = _alt(
    "lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo",
    r"pasado\s+manana", "manana", r"la\s+(?:proxima\s+semana|semana\s+que\s+viene)",
    r"(?:el\s+)?fin\s+de\s+semana", "mediodia", r"las?\s+\d{1,2}(?::\d{2})?",
    r"(?:el\s+)?\d{1,2}(?:\s+de\s+[a-z]+)?",
)
#: People mix languages inside one phrase ("no antes del Friday").
_ANY_TIME = _alt(_ES_TIME, _EN_TIME)
# Spanish clitics between "no" and the verb: "no le", "no se lo", "no me".
_CL = r"(?:(?:le|les|la|las|lo|los|me|nos|te|se)\s+){0,2}"
# "por qué no le mandas …" is a suggestion, not a restriction.
_NOT_WHY_ES = r"(?<!por que )(?<!porque )"
_NOT_WHY_EN = r"(?<!why )"

_DONT = r"(?:don'?t|do\s+not|dont|never)"

_LEXICON: tuple[tuple[str, str, str], ...] = (
    # ── no_send ────────────────────────────────────────────────────────────
    ("en", "no_send", _NOT_WHY_EN + rf"\b{_DONT}\s+(?:send|message|text|email|e-mail|mail|forward)\b"),
    ("en", "no_send", r"\bhold\s+off\s+on\s+(?:sending|messaging|texting|emailing)\b"),
    ("en", "no_send", r"\b(?:nothing|no\s+(?:message|text|email)s?)\s+(?:goes|go|gets\s+sent|is\s+sent)\s+out\b"),
    ("en", "no_send", r"\bno\s+(?:sends|sending)\b"),
    ("en", "no_send", r"\bnot\s+(?:send|message|text|email)\s+(?:her|him|them|anyone|anything)\s+yet\b"),
    ("es", "no_send", _NOT_WHY_ES + rf"\bno\s+{_CL}(?:mandes|mande|manden|mandar|envies|envie|envien|enviar|"
                      r"escribas|escriba|escriban|reenvies|textees|mensajees)\b"),
    ("es", "no_send", r"\bsin\s+(?:enviar|mandar)(?:le|les|lo|la)?\b"),
    ("es", "no_send", r"\bnada\s+de\s+(?:enviar|mandar|mensajes|correos|textos)\b"),
    ("es", "no_send", r"\b(?:aun|todavia)\s+no\s+(?:se\s+)?(?:envia|manda|sale)\b"),
    ("es", "no_send", r"\bque\s+no\s+salga\s+nada\b"),
    # ── draft_only ─────────────────────────────────────────────────────────
    ("en", "draft_only", r"\b(?:just|only)\s+(?:a\s+|the\s+)?drafts?\b"),
    ("en", "draft_only", r"\bdrafts?\s+only\b"),
    ("en", "draft_only", r"\bleave\s+(?:it|them)\s+(?:as\s+a\s+|in\s+)?drafts?\b"),
    ("es", "draft_only", r"\b(?:solo|solamente|unicamente|nada\s+mas)\s+(?:como\s+|en\s+)?(?:un\s+|el\s+|los\s+|unos\s+)?borradore?s?\b"),
    ("es", "draft_only", r"\b(?:solo|solamente|unicamente)\s+(?:redacta|redactalo|redactala|redactalos|prepara|preparalo|preparala)\b"),
    ("es", "draft_only", r"\b(?:como|en)\s+borrador(?:es)?\s+(?:nada\s+mas|solamente|solo|y\s+ya)\b"),
    ("es", "draft_only", r"\bdeja(?:lo|la|los|las)?\s+(?:como|en)\s+borrador(?:es)?\b"),
    # ── approval_required ─────────────────────────────────────────────────
    ("en", "approval_required", r"\b(?:wait|hold)\s+(?:for|on)\s+(?:my|the\s+manager'?s?|the\s+broker'?s?|his|her|their)\s+"
                                r"(?:approval|sign[- ]?off|ok|okay|go[- ]ahead)\b"),
    ("en", "approval_required", r"\b(?:after|once|when|until)\s+(?:the|my)\s+(?:manager|broker|boss|team\s+lead)\s+"
                                r"(?:signs?\s+off|approves?|okays?|oks?)\b"),
    ("en", "approval_required", r"\b(?:after|once|until|unless)\s+i\s+(?:approve|sign\s+off|say\s+so|ok\s+it|okay\s+it)\b"),
    ("en", "approval_required", r"\b(?:needs?|requires?|without|pending)\s+(?:my\s+|the\s+manager'?s?\s+|the\s+broker'?s?\s+)?approval\b"),
    ("es", "approval_required", r"\bespera(?:r|s)?\s+(?:a\s+)?(?:mi|la|su|tu)\s+(?:aprobacion|autorizacion|visto\s+bueno|ok|approval|sign[- ]?off)\b"),
    ("es", "approval_required", r"\b(?:con|sin)\s+(?:mi|la|su|tu)\s+(?:aprobacion|autorizacion|visto\s+bueno)\b"),
    ("es", "approval_required", r"\bhasta\s+que\s+(?:yo\s+)?(?:lo\s+|la\s+|los\s+|las\s+)?(?:apruebe|autorice|revise)\b"),
    ("es", "approval_required", r"\b(?:cuando|despues\s+de\s+que|hasta\s+que)\s+(?:el|la|mi)\s+(?:gerente|manager|broker|jefe|jefa|"
                                r"supervisora?|director|directora)\s+(?:lo\s+|la\s+)?(?:apruebe|autorice|firme|de\s+el\s+visto\s+bueno)\b"),
    ("es", "approval_required", r"\b(?:necesita|requiere|pendiente\s+de)\s+(?:mi\s+|la\s+|su\s+|tu\s+)?(?:aprobacion|autorizacion)\b"),
    ("es", "approval_required", r"\bpara\s+(?:mi|su)\s+aprobacion\b"),
    ("es", "approval_required", r"\bque\s+yo\s+(?:lo\s+|la\s+)?apruebe\b"),
    # ── not_before ─────────────────────────────────────────────────────────
    ("en", "not_before", rf"\bnot\s+(?:before|until|till)\s+(?:the\s+)?{_EN_TIME}\b"),
    ("en", "not_before", rf"\b(?:wait|hold\s+off)\s+(?:until|till)\s+(?:the\s+)?{_EN_TIME}\b"),
    ("en", "not_before", rf"\bno\s+earlier\s+than\s+{_EN_TIME}\b"),
    ("en", "not_before", rf"\b{_DONT}\s+\w+(?:\s+\w+){{0,3}}?\s+(?:before|until|till)\s+{_EN_TIME}\b"),
    ("en", "not_before", rf"\b(?:after|starting)\s+(?:the\s+)?{_EN_TIME}\b"),
    ("es", "not_before", rf"\bno\s+(?:\w+\s+){{0,4}}?antes\s+de(?:l|\s+el|\s+la|\s+las)?\s+{_ANY_TIME}\b"),
    ("es", "not_before", rf"\bno\s+(?:\w+\s+){{0,4}}?hasta\s+(?:el\s+)?{_ANY_TIME}\b"),
    ("es", "not_before", rf"\bespera(?:r|te)?\s+(?:hasta|al)\s+(?:el\s+)?{_ANY_TIME}\b"),
    ("es", "not_before", rf"\b(?:a\s+partir|despues)\s+de(?:l|\s+el|\s+la|\s+las)?\s+{_ANY_TIME}\b"),
    # ── do_not_contact ─────────────────────────────────────────────────────
    ("en", "do_not_contact", _NOT_WHY_EN + rf"\b{_DONT}\s+(?:contact|call|phone|ring|reach\s+out\s+to|reach|bother|"
                             r"get\s+in\s+touch\s+with)\b"),
    ("en", "do_not_contact", r"\bno\s+(?:contact|outreach)\s+(?:with|to)\b"),
    ("es", "do_not_contact", _NOT_WHY_ES + rf"\bno\s+{_CL}(?:contactes|contacte|contacten|contactar|llames|llame|llamen|llamar|"
                             r"molestes|moleste|molestar|hables|hable|hablar|busques|telefonees)\b"),
    ("es", "do_not_contact", r"\bno\s+(?:te\s+)?(?:comuniques|comunicar|pongas\s+en\s+contacto)\b"),
    ("es", "do_not_contact", r"\bni\s+(?:le\s+|la\s+|lo\s+)?(?:llames|contactes|escribas)\b"),
    ("es", "do_not_contact", r"\bsin\s+contactar(?:la|lo|le|los|las)?\b"),
    ("es", "do_not_contact", r"\bdeja(?:la|lo|los|las)?\s+en\s+paz\b"),
)

_COMPILED = tuple((lang, kind, re.compile(p)) for lang, kind, p in _LEXICON)


#: Someone asked for a draft: "prepara el correo", "draft the email". Not
#: when the verb is itself negated ("no le prepares nada").
_DRAFT_REQUEST = re.compile(
    r"(?<!no )(?<!no le )(?<!no lo )(?<!don't )(?<!do not )"
    r"\b(?:draft|write\s+up|prepare|redacta(?:le|lo|la|los|las)?|prepara(?:le|lo|la|los|las)?|"
    r"(?:haz|hazme)\s+un\s+borrador)\b"
)
_CLAUSE_BREAK = re.compile(r"[.;!?]|,\s*(?:y|pero|and|but)?\s")
_BLOCKING_CUES = frozenset({"no_send", "do_not_contact"})
_QUALIFIER_CUES = frozenset({"not_before", "approval_required"})


def cues(text: str) -> tuple[Cue, ...]:
    """Every restriction the lexicon reads in `text`, at most one per kind.

    Quotes are spans of `nfc(text)` — verbatim, accents and all.

    Two readings keep the backstop from over-reaching:

    * A "don't send / don't call" QUALIFIED in the same clause by a time or
      an approval ("no le mandes nada hasta el lunes", "don't send anything
      until I approve") means "not before" / "approval required", not "never"
      — the qualifier's constraint is kept and the blanket one dropped.
    * "Prepara el correo pero no lo mandes" asks for a draft and forbids the
      send: that is draft_only, not a no_send that would refuse the draft.
    """
    raw = nfc(text)
    folded = fold(raw)
    found: dict[str, tuple[Cue, int, int]] = {}
    for lang, kind, pattern in _COMPILED:
        if kind in found:
            continue
        m = pattern.search(folded)
        if m:
            found[kind] = (Cue(kind, raw[m.start():m.end()], lang), m.start(), m.end())

    for blocking in _BLOCKING_CUES & set(found):
        _, b_start, b_end = found[blocking]
        for qualifier in _QUALIFIER_CUES & set(found):
            _, q_start, q_end = found[qualifier]
            inside = q_start <= b_start and b_end <= q_end
            gap = folded[b_end:q_start] if q_start >= b_end else None
            same_clause = gap is not None and len(gap) <= 40 and not _CLAUSE_BREAK.search(gap)
            if inside or same_clause:
                found.pop(blocking)
                break

    if "no_send" in found and _DRAFT_REQUEST.search(folded):
        cue, start, end = found.pop("no_send")
        found.setdefault("draft_only", (Cue("draft_only", cue.quote, cue.lang), start, end))

    return tuple(entry[0] for entry in sorted(found.values(), key=lambda e: e[1]))


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

MESSAGES: dict[str, dict[str, str]] = {
    # frame_constraints.check_tool
    "refuse.do_not_contact": {
        "en": "You asked me not to contact {who}, so I did not prepare this. Nothing was drafted or sent.",
        "es": "Me pediste que no contactara a {who}, así que no preparé esto. No se redactó ni se envió nada.",
    },
    "refuse.no_send": {
        "en": ("You asked me not to send anything yet, so I did not prepare this message or request. "
               "Tell me when it should go and I will draft it for your approval."),
        "es": ("Me pediste que todavía no enviara nada, así que no preparé este mensaje ni esta solicitud. "
               "Dime cuándo debe salir y lo redacto para que lo apruebes."),
    },
    "refuse.draft_only": {
        "en": "You asked for drafts only. This would request an action rather than a draft, so I did not stage it.",
        "es": "Pediste solo borradores. Esto solicitaría una acción en lugar de un borrador, así que no lo preparé.",
    },
    "refuse.approval_required": {
        "en": "You asked for this to wait for approval. This change would apply immediately, so I did not make it.",
        "es": "Pediste que esto esperara una aprobación. Este cambio se aplicaría de inmediato, así que no lo hice.",
    },
    "refuse.not_before": {
        "en": "You asked me not to do this before {t}, so nothing was changed or staged. Ask me again then.",
        "es": "Me pediste que no hiciera esto antes de {t}, así que no se cambió ni se preparó nada. Pídemelo de nuevo entonces.",
    },
    "who.unresolved": {
        "en": "the person you meant (I could not tell who, so all contact is held)",
        "es": "la persona a la que te referías (no pude saber quién, así que todo contacto queda en espera)",
    },
    "who.anyone": {"en": "anyone", "es": "nadie"},
    "who.this": {"en": "this person", "es": "esta persona"},
    # frame_constraints.check_mission_step (a step's blocked_reason)
    "step.do_not_contact": {"en": "stated constraint: do not contact {who}",
                            "es": "restricción indicada: no contactar a {who}"},
    "step.no_send": {"en": "stated constraint: no sends", "es": "restricción indicada: no enviar nada"},
    "step.draft_only_call": {"en": "stated constraint: drafts only (a call is not a draft)",
                             "es": "restricción indicada: solo borradores (una llamada no es un borrador)"},
    "step.draft_only": {"en": "stated constraint: drafts only", "es": "restricción indicada: solo borradores"},
    "step.approval_required": {"en": "stated constraint: approval required",
                               "es": "restricción indicada: requiere aprobación"},
    # neoh_render: a search leg that did not answer (T05.11)
    "source.people": {"en": "people", "es": "personas"},
    "source.properties": {"en": "properties", "es": "propiedades"},
    "source.deals": {"en": "deals", "es": "tratos"},
    "source.conversations": {"en": "conversations", "es": "conversaciones"},
    "source.records": {"en": "public records", "es": "registros públicos"},
    "failed.how.timed_out": {"en": "timed out", "es": "agotó el tiempo de espera"},
    "failed.how.unavailable": {"en": "is unavailable", "es": "no está disponible"},
    "failed.one": {"en": "I couldn't search {source} for “{query}”: the search {how}.",
                   "es": "No pude buscar {source} para “{query}”: la búsqueda {how}."},
    "failed.many": {"en": "I couldn't search {sources} for “{query}”: those searches failed.",
                    "es": "No pude buscar {sources} para “{query}”: esas búsquedas fallaron."},
    "failed.tail": {"en": " This is not a “no match” — nothing was checked. Try again in a moment.",
                    "es": " Esto no es un “sin coincidencias”: no se comprobó nada. Inténtalo de nuevo en un momento."},
    "failed.label": {"en": "{Source} search {how}", "es": "Búsqueda de {source}: {how}"},
    "failed.detail": {"en": "Not checked, so an empty result here is not a verified miss.",
                      "es": "No se comprobó, así que un resultado vacío aquí no es una ausencia verificada."},
    # neoh_render: verified misses and questions
    "miss.show_person": {"en": "Nothing matches “{query}”.", "es": "Nada coincide con “{query}”."},
    "miss.properties_for": {"en": "No one matches “{query}”.", "es": "Nadie coincide con “{query}”."},
    "miss.deal_blocker": {"en": "No deal matches “{query}”.", "es": "Ningún trato coincide con “{query}”."},
    "choices.people": {"en": "{n} people match “{query}”.{more}", "es": "{n} personas coinciden con “{query}”.{more}"},
    "choices.more": {"en": " Showing the first {shown}.", "es": " Se muestran las primeras {shown}."},
    "choices.properties": {"en": "{n} properties match “{query}”.", "es": "{n} propiedades coinciden con “{query}”."},
    "choices.deals": {"en": "{n} deals match “{query}”.", "es": "{n} tratos coinciden con “{query}”."},
    "choices.which": {"en": "Which one?", "es": "¿Cuál?"},
    "choices.which_deal": {"en": "Which deal?", "es": "¿Qué trato?"},
    "deal.not_loaded": {"en": "That deal could not be loaded.", "es": "No se pudo cargar ese trato."},
    "person.contact_only": {
        "en": "{label} is in the contact book but has no client record yet, so there is nothing to read from.",
        "es": "{label} está en la agenda de contactos pero aún no tiene ficha de cliente, así que no hay nada que leer.",
    },
    # neoh_render.properties_for: the public-record lookup behind a shortlist
    "shortlist.failed": {
        "en": ("I couldn't check public records for {name}: the last lookup {how}, so this is not "
               "a 'no matches'. It retries on the next reconciliation."),
        "es": ("No pude consultar los registros públicos de {name}: la última consulta {how}, así que esto "
               "no es un “sin coincidencias”. Se reintenta en la próxima conciliación."),
    },
    "shortlist.how.timed_out": {"en": "timed out", "es": "agotó el tiempo de espera"},
    "shortlist.how.failed": {"en": "failed", "es": "falló"},
    "shortlist.last": {"en": " The last successful check was {date}.",
                       "es": " La última consulta correcta fue el {date}."},
    "shortlist.unchecked": {
        "en": ("{name}'s record has not been checked against public records yet, "
               "so an empty shortlist here is not a 'no matches'."),
        "es": ("El registro de {name} aún no se ha comparado con los registros públicos, "
               "así que una lista vacía aquí no es un “sin coincidencias”."),
    },
    "shortlist.empty": {
        "en": ("Nothing is on {name}'s shortlist yet. It fills in as they browse "
               "and as the criteria on their record get specific enough to match on."),
        "es": ("Todavía no hay nada en la lista de {name}. Se va llenando a medida que navega "
               "y cuando los criterios de su registro son lo bastante específicos para buscar coincidencias."),
    },
    # neoh_render.who_to_call: an empty queue
    "nobody.failed_one": {
        "en": "No one to call — but 1 detector failed this pass, so this is an incomplete answer rather than a quiet day.",
        "es": "No hay nadie a quien llamar, pero 1 detector falló en esta pasada, así que es una respuesta incompleta y no un día tranquilo.",
    },
    "nobody.failed_many": {
        "en": "No one to call — but {n} detectors failed this pass, so this is an incomplete answer rather than a quiet day.",
        "es": "No hay nadie a quien llamar, pero {n} detectores fallaron en esta pasada, así que es una respuesta incompleta y no un día tranquilo.",
    },
    "nobody.suppressed_one": {
        "en": "No one worth calling yet. 1 possibility was found but held back as too uncertain to put a name to.",
        "es": "Todavía no hay nadie a quien valga la pena llamar. Se encontró 1 posibilidad, pero se retuvo por ser demasiado incierta para ponerle nombre.",
    },
    "nobody.suppressed_many": {
        "en": "No one worth calling yet. {n} possibilities were found but held back as too uncertain to put a name to.",
        "es": "Todavía no hay nadie a quien valga la pena llamar. Se encontraron {n} posibilidades, pero se retuvieron por ser demasiado inciertas para ponerles nombre.",
    },
    "nobody.quiet": {
        "en": "No one to call right now. Nothing in the book is asking for you today.",
        "es": "No hay nadie a quien llamar ahora mismo. Nada en tu cartera te necesita hoy.",
    },
    # frame_constraints.prompt_block: tells the model which language to explain in
    "prompt.reply_in": {"en": "", "es": "The person is writing in Spanish: explain what you did not do, and why, in Spanish."},
}


def lang_or_default(lang: Optional[str]) -> str:
    return lang if lang in LANGS else DEFAULT_LANG


def t(key: str, lang: Optional[str] = None, **values: object) -> str:
    """The sentence `key` in `lang` (English when the language is unknown)."""
    entry = MESSAGES[key]
    return entry[lang_or_default(lang)].format(**values)
