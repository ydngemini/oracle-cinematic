"""The evidence contract: a versioned EvidenceFact and EvidenceBundle
(prompt 06, T06.02, AC06.02; ledger L06-03).

What a model, a dashboard or an answer may rest on, in one typed shape:

* every fact has an immutable id, a subject, a predicate, a typed value, at
  least one source, an observation time and a C6 class (CONTRACT C6: `fact`
  only when source-backed and cited; otherwise `inferred`, `predicted` or
  `provisional`). There is no single undifferentiated "confidence" number;
  a disagreement is the `disputed` marker plus the ids it conflicts with;
* every source the bundle rests on carries its C6 status entry
  (`source_status.py`: checked | timed_out | unavailable | not_applicable,
  with reason, required and elapsed time). A fact may only cite a source
  that was `checked`: a failed source contributes an unknown, never a fact;
* what is not known is said: `unknowns` lists each required source that did
  not answer, and a bundle with one is `degraded`.

Nothing here reads data. Facts come from adapters over the stores that
already hold provenance (T06.04: adapt, do not build a second living graph):
`facts_from_beliefs` reads a `belief_store.beliefs_about` payload as is.

Sensitivity uses retention_policy.RetentionCategory, the vocabulary the
privacy data map already uses, so a fact is classified the way its row is.
Who may read a fact is not decided here: `audience` names the record whose
RLS decided it (the fact was read under the caller's own context), and this
module never filters by duplicating an RLS predicate (CONTRACT C3.3).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import source_status as ss
from retention_policy import RetentionCategory

#: Bumped on any change a reader must know about. `from_dict` refuses a major
#: version it does not know rather than reading it as this one.
SCHEMA_VERSION = "evidence/1"

# C6 classes (CONTRACT C6). `disputed` is a marker on a fact, not a class.
FACT = "fact"
INFERRED = "inferred"
PREDICTED = "predicted"
PROVISIONAL = "provisional"
CLASSES = frozenset({FACT, INFERRED, PREDICTED, PROVISIONAL})

SENSITIVITIES = frozenset(c.value for c in RetentionCategory)


class EvidenceError(ValueError):
    """A fact or bundle that breaks the contract. Raised at construction, so
    a malformed bundle never reaches a prompt."""


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise EvidenceError("timestamps must be timezone-aware")
        return value.isoformat()
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise EvidenceError(f"not an ISO timestamp: {text!r}") from exc
    if parsed.tzinfo is None:
        raise EvidenceError("timestamps must be timezone-aware")
    return text


@dataclass(frozen=True)
class Citation:
    """Where a fact can be checked. `ref` is the source row or message id;
    `quote` is the source's own words, present only when the store kept them
    (and then the fact is communication content)."""

    source: str
    ref: Optional[str] = None
    quote: Optional[str] = None


@dataclass(frozen=True)
class EvidenceFact:
    id: str
    subject: str                    # canonical "<type>:<id>" (04 ids)
    predicate: str
    value: Any
    evidence_class: str             # C6 class
    sources: tuple[str, ...]        # names of bundle sources it rests on
    observed_at: str                # when the evidence happened
    sensitivity: str                # a RetentionCategory value
    audience: str                   # the record whose RLS decided who reads it
    unit: Optional[str] = None
    recorded_at: Optional[str] = None
    valid_until: Optional[str] = None   # None: no known expiry, never "forever"
    refresh: Optional[str] = None       # how the reading ages, in words
    disputed: bool = False
    conflicts: tuple[str, ...] = ()     # ids of the facts it disagrees with
    citations: tuple[Citation, ...] = ()
    basis: Optional[str] = None         # the store's own grade, kept verbatim

    def __post_init__(self) -> None:
        for name in ("id", "subject", "predicate", "audience"):
            if not getattr(self, name):
                raise EvidenceError(f"fact {name} is required")
        if ":" not in self.subject:
            raise EvidenceError(f"subject must be '<type>:<id>', got {self.subject!r}")
        if self.evidence_class not in CLASSES:
            raise EvidenceError(f"unknown C6 class {self.evidence_class!r}")
        if not self.sources:
            raise EvidenceError(f"fact {self.id} has no source")
        if self.sensitivity not in SENSITIVITIES:
            raise EvidenceError(f"unknown sensitivity {self.sensitivity!r}")
        if self.evidence_class == FACT and not any(c.ref for c in self.citations):
            raise EvidenceError(f"fact {self.id}: class 'fact' needs a citation with a ref (C6)")
        if self.disputed and not self.conflicts:
            raise EvidenceError(f"fact {self.id}: disputed without naming what it conflicts with")
        if self.id in self.conflicts:
            raise EvidenceError(f"fact {self.id} conflicts with itself")
        # Normalise timestamps; frozen, so through object.__setattr__.
        object.__setattr__(self, "observed_at", _iso(self.observed_at))
        if self.observed_at is None:
            raise EvidenceError(f"fact {self.id} has no observation time")
        object.__setattr__(self, "recorded_at", _iso(self.recorded_at))
        object.__setattr__(self, "valid_until", _iso(self.valid_until))
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "conflicts", tuple(self.conflicts))
        object.__setattr__(self, "citations", tuple(self.citations))


@dataclass(frozen=True)
class EvidenceBundle:
    sources: Mapping[str, Mapping[str, Any]]   # name -> source_status entry
    facts: tuple[EvidenceFact, ...] = ()
    compiled_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise EvidenceError(f"unsupported schema {self.schema_version!r}")
        for name, entry in self.sources.items():
            if not isinstance(entry, Mapping) or entry.get("status") not in ss.STATUSES:
                raise EvidenceError(f"source {name!r} has no C6 status")
        ids = [f.id for f in self.facts]
        if len(ids) != len(set(ids)):
            raise EvidenceError("fact ids must be unique within a bundle")
        known = set(ids)
        for f in self.facts:
            for name in f.sources:
                status = (self.sources.get(name) or {}).get("status")
                if status != ss.CHECKED:
                    raise EvidenceError(
                        f"fact {f.id} cites source {name!r} that is {status or 'absent'}, not checked")
            missing = [c for c in f.conflicts if c not in known]
            if missing:
                raise EvidenceError(f"fact {f.id} conflicts with facts not in the bundle: {missing}")
        object.__setattr__(self, "facts", tuple(self.facts))
        object.__setattr__(self, "sources", {k: dict(v) for k, v in self.sources.items()})
        object.__setattr__(self, "compiled_at", _iso(self.compiled_at))

    @property
    def unknowns(self) -> list[dict[str, Any]]:
        """Each source that did not answer: what is not known, and why."""
        return [
            {"source": name, "status": e["status"], "reason": e.get("reason"),
             "required": e.get("required", True)}
            for name, e in self.sources.items() if ss.is_failed(e)
        ]

    @property
    def degraded(self) -> bool:
        return ss.degraded(self.sources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "compiled_at": self.compiled_at,
            "degraded": self.degraded,
            "sources": {k: dict(v) for k, v in self.sources.items()},
            "unknowns": self.unknowns,
            "facts": [asdict(f) | {"sources": list(f.sources), "conflicts": list(f.conflicts),
                                   "citations": [asdict(c) for c in f.citations]}
                      for f in self.facts],
        }


def from_dict(data: Mapping[str, Any]) -> EvidenceBundle:
    """Read a serialised bundle back, validating it again."""
    version = str(data.get("schema_version") or "")
    if version != SCHEMA_VERSION:
        raise EvidenceError(f"unsupported schema {version!r}")
    facts = tuple(
        EvidenceFact(**{**f, "sources": tuple(f.get("sources") or ()),
                        "conflicts": tuple(f.get("conflicts") or ()),
                        "citations": tuple(Citation(**c) for c in f.get("citations") or ())})
        for f in data.get("facts") or ()
    )
    return EvidenceBundle(sources=data.get("sources") or {}, facts=facts,
                          compiled_at=data.get("compiled_at"), schema_version=version)


# ---------------------------------------------------------------------------
# Adapter: belief_store (T06.04). The Living Graph already carries provenance,
# decay and disputes; this only reads its payload in the contract's words.
# ---------------------------------------------------------------------------

BELIEFS_SOURCE = "beliefs"

#: The belief's epistemic status (0095) -> C6 class.
#:   confirmed  verified against a document or system of record -> fact, but
#:              only with a citable ref; without one it is provisional
#:   reported   a person said it: true that they said it, not that it holds
#:   inference  derived from evidence
#:   hypothesis worth testing, not acting on
_BELIEF_CLASS = {"confirmed": FACT, "reported": PROVISIONAL,
                 "inference": INFERRED, "hypothesis": PROVISIONAL}


def _belief_class(belief: Mapping[str, Any]) -> str:
    source = belief.get("source") or {}
    cls = _BELIEF_CLASS.get(str(belief.get("status")), PROVISIONAL)
    if cls == FACT and (not source.get("ref") or source.get("kind") == "model"):
        # A model's output is never source-backed, whatever grade it was given.
        return INFERRED if source.get("kind") == "model" else PROVISIONAL
    return cls


def facts_from_beliefs(payload: Mapping[str, Any]) -> list[EvidenceFact]:
    """EvidenceFacts for a `belief_store.beliefs_about` payload. Disputed
    beliefs keep the marker and name each other; retracted and superseded
    ones (history) are not evidence and are left out."""
    from belief_store import DEFAULT_HALF_LIFE_DAYS, HALF_LIFE_DAYS

    subject = f"{payload['subject_type']}:{payload['subject_id']}"
    live: list[Mapping[str, Any]] = [
        b for items in (payload.get("beliefs") or {}).values() for b in items
        if b.get("revision_state") in ("active", "disputed")
    ]
    disputed_by_predicate: dict[str, list[str]] = {}
    for b in live:
        if b.get("revision_state") == "disputed":
            disputed_by_predicate.setdefault(b["predicate"], []).append(f"belief:{b['id']}")

    facts = []
    for b in live:
        fid = f"belief:{b['id']}"
        source = b.get("source") or {}
        disputed = b.get("revision_state") == "disputed"
        conflicts = tuple(x for x in disputed_by_predicate.get(b["predicate"], []) if x != fid) \
            if disputed else ()
        half_life = HALF_LIFE_DAYS.get(b["predicate"], DEFAULT_HALF_LIFE_DAYS)
        facts.append(EvidenceFact(
            id=fid,
            subject=subject,
            predicate=b["predicate"],
            value=b.get("value"),
            evidence_class=_belief_class(b),
            sources=(BELIEFS_SOURCE,),
            observed_at=b["learned_at"],
            recorded_at=b.get("recorded_at"),
            valid_until=b.get("valid_until"),
            refresh="pinned: does not decay" if b.get("pinned")
            else f"reading halves every {half_life:g} days",
            # A lone disputed belief (its partner retracted since) is
            # not shown as disputed with nothing to point at.
            disputed=disputed and bool(conflicts),
            conflicts=conflicts if disputed else (),
            citations=(Citation(source=str(source.get("kind") or "unknown"),
                                ref=source.get("ref"), quote=source.get("quote")),),
            sensitivity=(RetentionCategory.COMMUNICATION_CONTENT if source.get("quote")
                         else RetentionCategory.AI_MEMORY).value,
            audience=subject,
            basis=b.get("status"),
        ))
    return facts


async def beliefs_bundle(ctx: Any, subject_type: str, subject_id: str) -> EvidenceBundle:
    """The beliefs about one record, as a bundle, read under the caller's own
    RLS. A failed read is the `beliefs` source unavailable / timed out — an
    unknown in a degraded bundle, never an empty set of facts."""
    import belief_store

    try:
        payload = await belief_store.beliefs_about(ctx, subject_type, subject_id)
    except Exception as exc:  # noqa: BLE001 — reported as the source's status
        return EvidenceBundle(sources={BELIEFS_SOURCE: ss.failure(exc)})
    facts = facts_from_beliefs(payload)
    return EvidenceBundle(sources={BELIEFS_SOURCE: ss.checked(matched=len(facts))}, facts=tuple(facts))
