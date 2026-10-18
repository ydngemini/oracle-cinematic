"""semantic_turn — the semantic-parser tier of a chat turn and of a mission.

Prompt 05, T05.05 (route by task) and T05.04 (short-lived discourse context).
The order is: the deterministic ⌘K fast path (`neoh_intents`) first and
untouched; on a miss the text becomes a chat turn, and the chat turn runs the
semantic parser (`semantic_frame.extract_frame` + `authorize_frame`) before the
model plans or calls a tool. What the frame says must not happen is carried to
the tool chokepoint (`ai_chat_store.execute_safe_tool`) and, for missions, to
every step (`missions.policy.evaluate_action`); `frame_constraints` enforces it.

Switch: `ORACLE_SEMANTIC_FRAME_ENABLED`, ON unless set to 0/false/off/no
(owner, 2026-10-14). Off means no parser call, nothing stored, nothing
enforced — exactly the behaviour before this module.

Cost and failure. One `fast`-tier structured call per chat turn, bounded by
`ORACLE_SEMANTIC_FRAME_TIMEOUT_SECONDS` (wall clock, retries included) and run
concurrently with the turn's other grounding reads. No provider, a timeout, a
rejected answer or any other parser failure leaves the turn exactly as it was
before this module — except that constraints stated in EARLIER turns still
apply: they are stored, so a parser outage cannot forget them.

Discourse store and retention. The turn's frame summary is written to its own
assistant row, `ai_chat_messages.metadata.semantic_frame` (no new table): goal
labels, authorised record ids, constraint kinds and times — never the words,
names or addresses (C5.4). It lives and is erased with the chat message
(privacy_data_map: ai_chat_messages). A chat constraint applies for
`frame_constraints.DISCOURSE_TTL` after the turn that stated it, within the 24
messages a turn already loads; a mission's applies for the mission's life.

Languages (L05-04; owner, 2026-10-14: English and Spanish at launch). The
parser reads English, Spanish and a mix of both. Behind it, a deterministic
bilingual lexicon (`semantic_language.cues`) is a backstop: a restriction the
request plainly states and the parser's frame does not carry — or that a
failed parser never produced — is added anyway (`frame_constraints.
reinforce`). A request in a language neither layer can vouch for (not
English, not Spanish) still goes to the parser; when the parser cannot
answer, the turn takes the strict pair (no sends, approval required) instead
of acting as if nothing had been said. Refusals use the request's language
(en or es; English by default; no user language setting exists to read).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import ai_budget
import frame_constraints as fc
import semantic_frame as sf
import semantic_language as lang_
from llm_gateway import SpendRefused
from tenancy import TenantContext

logger = logging.getLogger(__name__)

SUMMARY_VERSION = 1


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


#: Wall clock for the parser in one turn, retries included. A target that
#: keeps the added delay small, not a measurement of any provider.
FRAME_BUDGET_S = min(_float_env("ORACLE_SEMANTIC_FRAME_TIMEOUT_SECONDS", 3.0),
                     sf.FRAME_TIMEOUT_S)


def default_timezone() -> str:
    """The zone "tomorrow" and "Monday" are read in. No per-agent zone is
    stored yet, so this is a deployment default (documented in .env.example)."""
    name = (os.getenv("ORACLE_SEMANTIC_FRAME_TIMEZONE") or "America/New_York").strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("ORACLE_SEMANTIC_FRAME_TIMEZONE is not a known zone; using UTC")
        return "UTC"
    return name


#: The gateway the parser calls; None means llm_gateway. Tests set a fake
#: (tests/conftest.py installs one that has no model at all).
GATEWAY: Any = None

#: (assistant_id, constraints, reply language, pending question) for the turn
#: running in this task. Keyed by the turn so a tool call from any other turn
#: never reads these.
_TURN: contextvars.ContextVar[
    Optional[tuple[str, tuple[fc.ActiveConstraint, ...], str, Optional[fc.Pending]]]
] = contextvars.ContextVar("semantic_turn", default=None)


#: Record types a choice can be picked as: the chat context a click carries
#: (ai_chat_models.ChatContext). A contact-only person is still named in the
#: question and still held, but cannot be a button yet (L05-11).
CLICKABLE = frozenset({"client", "lead"})


@dataclass(frozen=True)
class Choice:
    """One record the person may have meant, named the way the parser saw it
    (the known-record label) with a detail that tells same-name records apart.
    Lives for the turn; never stored (C5.4)."""
    record_type: str
    id: str
    label: str
    detail: str = ""

    @property
    def clickable(self) -> bool:
        return self.record_type in CLICKABLE


@dataclass(frozen=True)
class Question:
    """The one precise question of a turn (T05.10), built only from what the
    server decided (authorize_frame), never from the parser's own text.

    `choices` are the authorised records the first ambiguous mention could
    mean — none when nobody the caller can open matched, which is still a
    question. `held` is every authorised choice of EVERY ambiguous mention:
    the turn refuses acting on any of them until the person answers. `also`
    names the other ambiguous mentions; `capped` says the lookup stopped at
    its limit, so the count is "at least"."""
    text: str
    choices: tuple[Choice, ...] = ()
    held: frozenset[str] = frozenset()
    also: tuple[str, ...] = ()
    capped: bool = False

    def pending(self) -> Optional[fc.Pending]:
        return fc.Pending(self.held) if self.held else None


def question_from(
    frame: Optional[sf.SemanticFrame], known: Sequence[sf.KnownRecord], *,
    capped: frozenset[int] = frozenset(), lang: Optional[str] = None,
) -> Optional[Question]:
    """The turn's question, from the ambiguities authorize_frame built
    (`server`) on an authorised frame. A parser-written ambiguity (choices
    stripped, free text) never stands in for the server's (L05-11); an
    ambiguity with choices outranks one without."""
    if frame is None or not frame.authorized:
        return None
    named = {(k.record_type, str(k.id)): k for k in known}

    def choice(c: sf.Candidate) -> Choice:
        k = named.get((c.record_type, str(c.id)))
        label = k.label if k and k.label else lang_.t("choices.unnamed", lang)
        return Choice(c.record_type, str(c.id), label, (k.detail if k else "") or "")

    ours = [a for a in frame.ambiguities
            if a.server and a.mention is not None and a.mention < len(frame.mentions)]
    asking = [a for a in ours if len([c for c in a.choices if c.access is not None]) > 1]
    if asking:
        first = asking[0]
        held = frozenset(str(c.id) for a in asking for c in a.choices if c.access is not None)
        return Question(
            text=frame.mentions[first.mention].text,
            choices=tuple(choice(c) for c in first.choices if c.access is not None),
            held=held,
            also=tuple(dict.fromkeys(frame.mentions[a.mention].text for a in asking[1:])),
            capped=first.mention in capped,
        )
    nobody = [a for a in ours if not a.choices]
    if nobody:
        return Question(text=frame.mentions[nobody[0].mention].text)
    return None


def _fenced(choices: Sequence[Choice]) -> str:
    """The choices as data: names and details are written by people, so they
    are JSON inside a fence, '<' escaped so a name cannot close it (the same
    rule as the SELECTED RECORD block)."""
    rows = [{"n": i, "name": c.label[:120], "detail": c.detail[:160], "type": c.record_type}
            for i, c in enumerate(choices, 1)]
    return "<choices>" + json.dumps(rows, ensure_ascii=False).replace("<", "\\u003c") + "</choices>"


@dataclass
class TurnFrame:
    status: str            # parsed | unavailable | timed_out | rejected | no_request
    reason: str = ""
    frame: Optional[sf.SemanticFrame] = None
    own: list[fc.ActiveConstraint] = field(default_factory=list)
    active: list[fc.ActiveConstraint] = field(default_factory=list)
    extract_ms: Optional[int] = None
    #: The language answers are written in (en | es).
    lang: str = lang_.DEFAULT_LANG
    #: What `semantic_language.detect` read: en | es | mixed | other | undetermined.
    detected: str = "undetermined"
    #: Constraint kinds the deterministic backstop added (the frame lacked them).
    backstop: list[str] = field(default_factory=list)
    #: The one precise question this turn must ask before acting (T05.10).
    question: Optional[Question] = None

    def pending(self) -> Optional[fc.Pending]:
        return self.question.pending() if self.question else None

    def question_block(self) -> str:
        """What the model is told to ask. With choices the server enforces it
        (check_pending holds every action); the 'nobody' question binds no
        hold, so its heading claims no enforcement."""
        q = self.question
        if q is None:
            return ""
        if q.choices:
            key = "ask.which_more" if q.capped else "ask.which"
            lines = [lang_.t(key, self.lang, n=len(q.choices), text=q.text),
                     lang_.t("ask.data", self.lang), _fenced(q.choices)]
            if q.also:
                lines.append(lang_.t("ask.also", self.lang, texts=", ".join(f"“{t}”" for t in q.also)))
            lines.append(lang_.t("ask.rules", self.lang))
            heading = "## ASK FIRST — ENFORCED BY THE SERVER"
        else:
            lines = [lang_.t("ask.nobody", self.lang, text=q.text)]
            heading = "## ASK FIRST"
        reply_in = lang_.t("prompt.reply_in", self.lang)
        return heading + "\n" + "\n".join(lines) + (f"\n{reply_in}" if reply_in else "")

    def summary(self) -> dict[str, Any]:
        """What is stored. Ids, labels, kinds and times — no words."""
        f = self.frame
        return {
            "v": SUMMARY_VERSION,
            "schema_version": sf.SCHEMA_VERSION,
            "status": self.status,
            "reason": self.reason[:60],
            "extract_ms": self.extract_ms,
            "authorized": bool(f and f.authorized),
            "goals": [{"label": g.label, "negated": g.negated} for g in (f.goals if f else [])],
            "mentions": [
                {"kind": m.kind, "records": [
                    {"type": c.record_type, "id": str(c.id), "access": c.access}
                    for c in m.candidates if c.access is not None
                ]}
                for m in (f.mentions if f else [])
            ],
            "needs_clarification": bool(f and (f.ambiguities or f.unfilled_slots)),
            "dropped_candidates": f.dropped_candidates if f else 0,
            "own": fc.dump(self.own),
            "active": fc.dump(self.active),
            "lang": self.lang,
            "detected": self.detected,
            "backstop": list(self.backstop),
        }

    def prompt_block(self) -> str:
        return fc.prompt_block(self.active, self.lang)


# ---------------------------------------------------------------------------
# The binding the tool chokepoint reads
# ---------------------------------------------------------------------------

def bind(assistant_id: str, constraints: Sequence[fc.ActiveConstraint],
         lang: str = lang_.DEFAULT_LANG, pending: Optional[fc.Pending] = None) -> None:
    """Bind this turn's constraints (and the language its refusals are
    written in, and the question it is still asking) to the running task.

    `_generate` runs inside its own task (asyncio.wait_for), so the binding
    ends with the turn without a reset.
    """
    _TURN.set((str(assistant_id), tuple(constraints), lang_.lang_or_default(lang), pending))


def constraints_for(message_id: str) -> tuple[fc.ActiveConstraint, ...]:
    """The constraints of the turn `message_id`, or none. Empty when the
    switch is off, so the chokepoint behaves exactly as before."""
    if not fc.enabled():
        return ()
    bound = _TURN.get()
    if bound is None or bound[0] != str(message_id):
        return ()
    return bound[1]


def pending_for(message_id: str) -> Optional[fc.Pending]:
    """The question the turn `message_id` is still asking, or None (also None
    when the switch is off, so the chokepoint behaves exactly as before)."""
    if not fc.enabled():
        return None
    bound = _TURN.get()
    if bound is None or bound[0] != str(message_id):
        return None
    return bound[3]


def language_for(message_id: str) -> str:
    """The language the turn `message_id` answers in (English when unbound)."""
    bound = _TURN.get()
    if bound is None or bound[0] != str(message_id):
        return lang_.DEFAULT_LANG
    return bound[2]


# ---------------------------------------------------------------------------
# Discourse context
# ---------------------------------------------------------------------------

def _metadata(message: dict) -> dict:
    raw = message.get("metadata")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def standing_from_bundle(bundle: dict) -> tuple[list[fc.ActiveConstraint], list[dict]]:
    """The latest stored frame summary among the loaded messages: its active
    constraints (cumulative) and the records it resolved."""
    summary, message = _latest_summary(bundle)
    if summary is None:
        return [], []
    active = fc.load(summary.get("active"), origin=str(message.get("id") or "unreadable"))
    refs = [r for m in (summary.get("mentions") or []) if isinstance(m, dict)
            for r in (m.get("records") or []) if isinstance(r, dict)]
    return active, refs


def _latest_summary(bundle: dict) -> tuple[Optional[dict], dict]:
    for message in reversed(bundle.get("messages") or []):
        if message.get("role") != "assistant":
            continue
        summary = _metadata(message).get("semantic_frame")
        if isinstance(summary, dict):
            return summary, message
    return None, {}


def previous_language(bundle: dict) -> Optional[str]:
    """The language the conversation last answered in, if one was stored."""
    summary, _ = _latest_summary(bundle)
    value = (summary or {}).get("lang")
    return value if value in lang_.LANGS else None


def _last_user_text(bundle: dict) -> str:
    messages = bundle.get("messages") or []
    if messages and messages[-1].get("role") == "user":
        return str(messages[-1].get("content") or "")
    return ""


def record_detail(row: Optional[dict]) -> str:
    """What tells two same-name records apart in a question: a client's type,
    stage and email (what the people search already shows the agent). Never
    stored; used for the turn's question and its buttons only."""
    row = row or {}
    return " · ".join(str(row[k]) for k in ("client_type", "stage", "email") if row.get(k))


async def _known_records(ctx: TenantContext, bundle: dict, refs: list[dict]) -> list[sf.KnownRecord]:
    """What the parser may refer to by number: the selected record, then the
    records the previous turn resolved (re-read as the caller, so a record
    the caller lost access to since is not offered)."""
    known: list[sf.KnownRecord] = []
    assistant = bundle.get("assistant") or {}
    ctype, cid = assistant.get("context_type"), assistant.get("context_id")
    record = bundle.get("record") or {}
    if ctype in ("client", "lead") and cid and record.get("label"):
        known.append(sf.KnownRecord(ctype, str(cid), str(record["label"]), record_detail(record)))
    wanted = []
    for r in refs:
        if r.get("type") in ("client", "lead") and r.get("id") and str(r["id"]) != str(cid):
            wanted.append((r["type"], str(r["id"])))
    wanted = list(dict.fromkeys(wanted))[:5]
    if not wanted:
        return known
    try:
        from ai_chat_store import resolve_record
        from db.connection import tenant_tx

        async with tenant_tx(ctx) as conn:
            for rtype, rid in wanted:
                try:
                    row = await resolve_record(conn, ctx, rtype, rid)
                except Exception:  # noqa: BLE001 — not visible any more: not offered
                    continue
                if row.get("label"):
                    known.append(sf.KnownRecord(rtype, rid, str(row["label"]), record_detail(row)))
    except Exception as exc:  # noqa: BLE001 — discourse is a help, not a dependency
        logger.info("semantic turn: previous records not re-read (%s)", type(exc).__name__)
    return known


_PERSIST_SQL = """
    UPDATE ai_chat_messages
       SET metadata = metadata || jsonb_build_object('semantic_frame', $2::jsonb)
     WHERE id = $1::uuid AND role = 'assistant'
"""


async def _persist(ctx: TenantContext, assistant_id: str, summary: dict) -> None:
    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        await conn.execute(_PERSIST_SQL, assistant_id, json.dumps(summary, default=str))


# ---------------------------------------------------------------------------
# One parse, bounded
# ---------------------------------------------------------------------------

#: Words a mention can be that name nobody to search for.
_PRONOUNS = frozenset({
    "her", "him", "them", "she", "he", "they", "it", "me", "us", "you", "i", "we",
    "ella", "él", "el", "ellos", "ellas", "le", "la", "lo", "les", "los", "las", "nos", "usted", "ustedes", "yo",
})
LOOKUP_LIMIT = 5
#: Wall clock for one people lookup; a slow search leaves the mention as the
#: parser left it rather than holding the turn.
LOOKUP_BUDGET_S = 1.5


def _searchable(text: str) -> bool:
    words = [w for w in text.replace(",", " ").split() if w]
    return (len(text.strip()) >= 2 and any(ch.isalpha() for ch in text)
            and not all(w.lower().strip(".¿?¡!") in _PRONOUNS for w in words))


#: A hit becomes a candidate only for a strong name match: exact, prefix or a
#: word that starts with what was said (search_api.score_match >= 0.7). A
#: substring inside another name ("Bob" in "Bobby"), an email or a company is
#: not the person the request named (L05-11).
STRONG_MATCH = 0.7


@dataclass
class Lookup:
    """What the people lookup added to a frame."""
    frame: Optional[sf.SemanticFrame]
    found: list[sf.KnownRecord] = field(default_factory=list)
    #: Mention indices whose candidates came from the lookup, not the parser.
    #: They never scope or lift a constraint (from_frame / lifts).
    looked_up: frozenset[int] = frozenset()
    #: Mention indices whose search stopped at its limit.
    capped: frozenset[int] = frozenset()


async def _lookup_people(
    ctx: TenantContext, frame: Optional[sf.SemanticFrame], *, lang: Optional[str] = None,
) -> Lookup:
    """A person the parser named but could not place — the name is not among
    the records it was shown — is looked up as the caller, with the fast
    path's own people search (T05.10, L05-10). Strong matches become the
    mention's candidates for authorize_frame to keep or drop, so the turn
    resolves one match or asks about several instead of leaving the model to
    guess.

    Pronouns and texts with no letters are never searched. A search that
    fails, times out or did not run its people leg changes nothing (unknown
    is not "nobody", C6)."""
    if frame is None:
        return Lookup(frame)
    wanted = [i for i, m in enumerate(frame.mentions)
              if m.kind == "person" and not m.candidates and _searchable(m.text)]
    if not wanted:
        return Lookup(frame)
    import search_api
    import source_status as ss

    found: list[sf.KnownRecord] = []
    looked_up: set[int] = set()
    capped: set[int] = set()
    mentions = list(frame.mentions)
    for index in wanted:
        text = mentions[index].text
        try:
            out = await asyncio.wait_for(
                search_api.search(ctx, text, ["people"], LOOKUP_LIMIT), timeout=LOOKUP_BUDGET_S)
        except Exception as exc:  # noqa: BLE001 — unknown is not nobody
            logger.info("semantic turn: people lookup unavailable (%s)", type(exc).__name__)
            continue
        if ((out.get("source_status") or {}).get("people") or {}).get("status") != ss.CHECKED:
            continue
        hits = out.get("results") or []
        candidates: list[sf.Candidate] = []
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            record = hit.get("record") or {}
            rtype, rid = record.get("type"), str(record.get("id") or "")
            if rtype not in ("client", "contact") or (rtype, rid) in seen:
                continue
            if float(hit.get("score") or 0) < STRONG_MATCH:
                continue
            try:
                as_uuid = uuid.UUID(rid)
            except ValueError:
                continue
            seen.add((rtype, rid))
            candidates.append(sf.Candidate(record_type=rtype, id=as_uuid))
            label = hit.get("label") or ""
            if label in ("", "Unnamed"):
                label = lang_.t("choices.unnamed", lang)
            elif label == "Contact":
                label = lang_.t("choices.contact", lang)
            found.append(sf.KnownRecord(rtype, rid, str(label), str(hit.get("sublabel") or "")))
        if candidates:
            mentions[index] = mentions[index].model_copy(update={"candidates": candidates[:sf.MAX_CANDIDATES]})
            looked_up.add(index)
            if len(hits) >= LOOKUP_LIMIT or len(candidates) > sf.MAX_CANDIDATES:
                capped.add(index)
    return Lookup(frame.model_copy(update={"mentions": mentions}), found,
                  frozenset(looked_up), frozenset(capped))


def _without_lookup(frame: Optional[sf.SemanticFrame], looked_up: frozenset[int]) -> Optional[sf.SemanticFrame]:
    """The frame as the parser placed it: lookup-supplied candidates removed,
    so a constraint on that mention stays unscoped (applies to everyone) and a
    lift names nobody — a search hit never narrows or lifts a restriction."""
    if frame is None or not looked_up:
        return frame
    mentions = [m.model_copy(update={"candidates": []}) if i in looked_up else m
                for i, m in enumerate(frame.mentions)]
    return frame.model_copy(update={"mentions": mentions})


@dataclass
class Parsed:
    frame: Optional[sf.SemanticFrame]
    status: str
    reason: str
    elapsed: int
    lookup: Lookup


async def _parse(
    ctx: TenantContext, text: str, *, known: Sequence[sf.KnownRecord], now: datetime,
    lookup: bool = True, lang: Optional[str] = None,
) -> Parsed:
    """The authorised frame (or None), how the parse ended and what the people
    lookup added. Never raises. `lookup=False` for a mission objective: it has
    no question step, so a search there could only narrow its constraints."""
    zone = default_timezone()
    started = time.perf_counter()
    frame: Optional[sf.SemanticFrame] = None
    status, reason = "parsed", ""
    try:
        # A deadline that fires while the AI budget is being read is the
        # budget's (SpendUnavailable), not the parser's timeout (T18.07).
        frame = await ai_budget.wait_for(
            sf.extract_frame(
                text, now=now.astimezone(ZoneInfo(zone)), timezone=zone,
                known=known, gateway=GATEWAY,
            ),
            FRAME_BUDGET_S,
        )
    except SpendRefused as refused:
        status, reason = "unavailable", refused.code
    except asyncio.TimeoutError:
        status = "timed_out"
    except sf.FrameUnavailable as exc:
        status = "unavailable"
        # A brokerage AI budget refusal (T18.07) is named by its code, so the
        # record says why the parse did not run rather than only that it did not.
        refused = exc.__cause__
        if isinstance(refused, SpendRefused):
            reason = refused.code
    except sf.FrameRejected as exc:
        status, reason = "rejected", exc.reason
    except Exception as exc:  # noqa: BLE001 — the parser never breaks the turn
        status, reason = "unavailable", type(exc).__name__
    elapsed = int((time.perf_counter() - started) * 1000)
    looked = Lookup(frame)
    if frame is not None:
        if lookup:
            looked = await _lookup_people(ctx, frame, lang=lang)
            frame = looked.frame
        try:
            frame = await sf.authorize_frame(ctx, frame)
        except Exception as exc:  # noqa: BLE001
            # Keep the unauthorised frame: its constraints still count, but
            # unscoped (from_frame never scopes to an unchecked identity).
            reason = f"authorization_failed:{type(exc).__name__}"
    if status != "parsed":
        logger.info("semantic turn: parser %s (%s) in %d ms", status, reason or "-", elapsed)
    looked.frame = frame
    return Parsed(frame, status, reason, elapsed, looked)


async def prepare_turn(
    ctx: TenantContext, bundle: dict, assistant_id: str, *, now: Optional[datetime] = None,
) -> Optional[TurnFrame]:
    """Parse this turn, amend the standing constraints, store the result.

    Returns None when the switch is off. Raises only when constraints are in
    force and cannot be stored: a turn must not act under restrictions the
    next turn would not see (fail closed — the turn fails honestly instead).
    """
    if not fc.enabled():
        return None
    now = now or datetime.now(timezone.utc)
    standing, refs = standing_from_bundle(bundle)
    text = _last_user_text(bundle)

    lang = lang_.reply_language(text, previous_language(bundle), bundle.get("lang_hint"))
    # The frame constraints are read from: as the parser placed it, without
    # candidates the people lookup supplied (L05-11).
    constraint_frame: Optional[sf.SemanticFrame] = None
    if not text.strip():
        turn = TurnFrame(status="no_request")
    else:
        known = await _known_records(ctx, bundle, refs)
        parsed = await _parse(ctx, text, known=known, now=now, lang=lang)
        found = parsed.lookup
        turn = TurnFrame(status=parsed.status, reason=parsed.reason, frame=parsed.frame,
                         extract_ms=parsed.elapsed,
                         question=question_from(parsed.frame, [*known, *found.found],
                                                capped=found.capped, lang=lang))
        turn.detected = lang_.detect(text).lang
        constraint_frame = _without_lookup(parsed.frame, found.looked_up)
    turn.lang = lang

    if constraint_frame is not None:
        turn.own = fc.from_frame(constraint_frame, origin=str(assistant_id), stated_at=now)
    turn.own, turn.backstop = apply_backstop(
        text, turn.frame, turn.own, detected=turn.detected,
        origin=str(assistant_id), now=now,
    )
    # A turn that states a restriction is never also a lift of one, and a turn
    # still asking who the person meant lifts nothing: "OK, text Sarah now"
    # with two Sarahs must not undo a "don't contact" on either (L05-11).
    lifted = (fc.lifts(constraint_frame)
              if not turn.backstop and turn.question is None else None)
    turn.active = fc.merge(standing, turn.own, lifted_for=lifted, now=now)

    if turn.frame is not None or turn.active or standing:
        try:
            await _persist(ctx, assistant_id, turn.summary())
        except Exception as exc:  # noqa: BLE001
            if turn.active:
                raise RuntimeError("stated constraints could not be stored") from exc
            logger.warning("semantic turn: frame summary not stored (%s)", type(exc).__name__)
    return turn


def apply_backstop(
    text: str, frame: Optional[sf.SemanticFrame], own: list[fc.ActiveConstraint], *,
    detected: str, origin: str, now: datetime,
) -> tuple[list[fc.ActiveConstraint], list[str]]:
    """The deterministic layer behind the parser (L05-04). Only ever adds.

    * English / Spanish / mixed / too short to tell: the bilingual cue
      lexicon adds a restriction the request states and the frame lacks (or
      that a failed parser never produced).
    * Any other language: nothing deterministic can read it. With a frame,
      the parser's reading stands, as in English. With NO frame (parser
      unavailable, timed out, rejected) the turn is in doubt and takes the
      strict pair rather than acting as if nothing was said.
    """
    if not (text or "").strip():
        return own, []
    if detected == "other":
        if frame is not None:
            return own, []
        have = {c.kind for c in own}
        strict = [c for c in fc.strict(origin=origin, stated_at=now) if c.kind not in have]
        return own + strict, [c.kind for c in strict]
    return fc.reinforce(own, lang_.cues(text), origin=origin, stated_at=now)


# ---------------------------------------------------------------------------
# Missions (08)
# ---------------------------------------------------------------------------

async def mission_constraints(
    ctx: TenantContext, objective_text: str, *, now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The `missions.constraints` value for a new mission ({} when off).

    The objective is parsed once, as the person creating it, and what it says
    must not happen is stored on the mission and checked at every step. A
    parser failure is recorded, not hidden: the mission keeps every gate it
    had before (approval, shadow mode, compliance).
    """
    if not fc.enabled():
        return {}
    now = now or datetime.now(timezone.utc)
    parsed = await _parse(ctx, objective_text, known=(), now=now, lookup=False)
    frame, status, reason, elapsed = parsed.frame, parsed.status, parsed.reason, parsed.elapsed
    own = fc.from_frame(frame, origin="mission", stated_at=now) if frame else []
    detected = lang_.detect(objective_text).lang
    own, backstop = apply_backstop(objective_text, frame, own, detected=detected,
                              origin="mission", now=now)
    return {"semantic_frame": {
        "v": SUMMARY_VERSION,
        "schema_version": sf.SCHEMA_VERSION,
        "status": status,
        "reason": reason[:60],
        "extract_ms": elapsed,
        "goals": [{"label": g.label, "negated": g.negated} for g in (frame.goals if frame else [])],
        "active": fc.dump(own),
        "lang": lang_.reply_language(objective_text),
        "detected": detected,
        "backstop": backstop,
    }}


def mission_active(mission: dict[str, Any]) -> list[fc.ActiveConstraint]:
    """The stated constraints a mission row carries (fail closed on garbage)."""
    raw = mission.get("constraints")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return fc.load("unreadable", origin="mission")
    if not isinstance(raw, dict):
        return []
    summary = raw.get("semantic_frame")
    if summary is None:
        return []
    if not isinstance(summary, dict):
        return fc.load("unreadable", origin="mission")
    return fc.load(summary.get("active"), origin="mission")


def mission_language(mission: dict[str, Any]) -> str:
    """The language a mission's stated-constraint reasons are written in."""
    raw = mission.get("constraints")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return lang_.DEFAULT_LANG
    summary = raw.get("semantic_frame") if isinstance(raw, dict) else None
    value = summary.get("lang") if isinstance(summary, dict) else None
    return lang_.lang_or_default(value)
