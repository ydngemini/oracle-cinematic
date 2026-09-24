"""Which of this brokerage's buyers should hear about this listing.

Evidence first, never a bare number
-----------------------------------
§24: an unexplained "91% match" is worse than useless — nobody can check it,
and the first time it is wrong the agent stops trusting every number after it.
So a match is a list of reasons a person can read and verify against the
record, plus a tier derived deterministically from how many independent
signals agreed. No model is involved. No score is invented.

Unknown is not "no"
-------------------
§25 is the rule that matters most and is the easiest to get wrong. A buyer
whose budget nobody recorded is not a buyer who cannot afford the house — and
silently dropping them is how a brokerage never calls the person who would
have bought it. Every dimension resolves to one of three answers: it matches,
it conflicts, or we do not know. Only a real CONFLICT can exclude anybody.

Preference data is messy, because it is real
--------------------------------------------
`clients.preferences` is a free JSONB blob written by several code paths over
time, and the live data uses both `target_zips` and `zips`, both `budget_max`
and `budget`. Reading only one spelling silently halves the candidate pool, so
every field below accepts its known aliases.

Deterministic and set-based (§53): matching runs as one query plus pure Python
over the rows. It never calls a model per listing x contact — that is a bill
that grows quadratically and an answer nobody can reproduce.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# Tiers. Deliberately few, and named for what an agent would say out loud.
STRONG = "strong"
POSSIBLE = "possible"
INSUFFICIENT = "insufficient_data"
MISMATCH = "mismatch"

#: Key aliases seen in live data. Order is preference order.
_ALIASES = {
    "zips": ("target_zips", "zips", "zip_codes", "target_zip_codes"),
    "cities": ("target_cities", "cities", "markets", "target_markets"),
    "budget_max": ("budget_max", "budget", "max_price", "price_max"),
    "budget_min": ("budget_min", "min_price", "price_min"),
    "beds_min": ("beds_min", "min_beds", "beds", "bedrooms"),
    "property_types": ("property_types", "property_type", "types"),
}


def _first(mapping: dict, names: Iterable[str]) -> Any:
    for name in names:
        if name in mapping and mapping[name] not in (None, "", [], {}):
            return mapping[name]
    return None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,;/]", value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _as_number(value: Any) -> Optional[float]:
    """Coerce whatever the database or a JSON blob hands us.

    `Decimal` is the one that matters and the one that was missing. Postgres
    returns `numeric` columns — list_price, beds — as Decimal, which is neither
    int nor float, so this used to return None for every real listing. The
    effect was silent and specific: budget and bedroom matching degraded to
    "unknown" against live data while passing every unit test, because the
    tests used Python ints. Worse, the response then told the agent "listing
    has no price" about a listing that plainly had one.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^0-9.]", "", value)
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
    # Decimal, and anything else that knows how to be a float.
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class BuyerPreferences:
    zips: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    budget_max: Optional[float] = None
    budget_min: Optional[float] = None
    beds_min: Optional[int] = None
    property_types: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any((self.zips, self.cities, self.budget_max,
                        self.budget_min, self.beds_min, self.property_types))


def extract_preferences(raw: Any) -> BuyerPreferences:
    """Read a client's preferences blob, tolerating the key drift in live data."""
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    beds = _as_number(_first(raw, _ALIASES["beds_min"]))
    return BuyerPreferences(
        zips=[z[:5] for z in _as_list(_first(raw, _ALIASES["zips"]))],
        cities=[c.lower() for c in _as_list(_first(raw, _ALIASES["cities"]))],
        budget_max=_as_number(_first(raw, _ALIASES["budget_max"])),
        budget_min=_as_number(_first(raw, _ALIASES["budget_min"])),
        beds_min=int(beds) if beds is not None else None,
        property_types=[t.lower() for t in _as_list(_first(raw, _ALIASES["property_types"]))],
    )


@dataclass
class Signal:
    """One dimension's verdict, with the sentence an agent would read."""
    name: str
    verdict: str          # "match" | "conflict" | "unknown"
    note: str


def _location_signal(listing: dict, prefs: BuyerPreferences) -> Signal:
    zip_code = str(listing.get("zip_code") or "")[:5]
    city = str(listing.get("city") or "").lower()
    if not prefs.zips and not prefs.cities:
        return Signal("location", "unknown", "no target area recorded")
    if zip_code and zip_code in prefs.zips:
        return Signal("location", "match", f"looking in {zip_code}")
    if city and city in prefs.cities:
        return Signal("location", "match", f"looking in {listing.get('city')}")
    where = ", ".join(prefs.zips[:3] or prefs.cities[:3])
    return Signal("location", "conflict", f"looking in {where}, not {listing.get('city') or zip_code}")


def _budget_signal(listing: dict, prefs: BuyerPreferences) -> Signal:
    price = _as_number(listing.get("list_price"))
    if price is None:
        return Signal("budget", "unknown", "listing has no price")
    if prefs.budget_max is None and prefs.budget_min is None:
        return Signal("budget", "unknown", "no budget recorded")
    if prefs.budget_max is not None and price > prefs.budget_max:
        # A near miss is worth saying out loud rather than hiding: agents
        # routinely stretch a buyer by a few percent.
        over = (price - prefs.budget_max) / prefs.budget_max
        if over <= 0.10:
            return Signal("budget", "match",
                          f"${price:,.0f} is {over:.0%} over their ${prefs.budget_max:,.0f} budget")
        return Signal("budget", "conflict",
                      f"budget up to ${prefs.budget_max:,.0f}, this is ${price:,.0f}")
    if prefs.budget_min is not None and price < prefs.budget_min:
        return Signal("budget", "conflict",
                      f"looking above ${prefs.budget_min:,.0f}, this is ${price:,.0f}")
    if prefs.budget_max is not None:
        return Signal("budget", "match", f"budget up to ${prefs.budget_max:,.0f}")
    return Signal("budget", "match", f"looking above ${prefs.budget_min:,.0f}")


def _beds_signal(listing: dict, prefs: BuyerPreferences) -> Signal:
    if prefs.beds_min is None:
        return Signal("beds", "unknown", "no bedroom requirement recorded")
    beds = _as_number(listing.get("beds"))
    if beds is None:
        return Signal("beds", "unknown", "listing does not state bedrooms")
    if beds >= prefs.beds_min:
        return Signal("beds", "match", f"wants {prefs.beds_min}+ bedrooms, this has {int(beds)}")
    return Signal("beds", "conflict", f"wants {prefs.beds_min}+ bedrooms, this has {int(beds)}")


def _type_signal(listing: dict, prefs: BuyerPreferences) -> Signal:
    if not prefs.property_types:
        return Signal("property_type", "unknown", "no property type recorded")
    listing_type = str(listing.get("property_type") or "").lower()
    if not listing_type:
        return Signal("property_type", "unknown", "listing has no property type")
    if any(t in listing_type or listing_type in t for t in prefs.property_types):
        return Signal("property_type", "match", f"wants {prefs.property_types[0]}")
    return Signal("property_type", "conflict",
                  f"wants {', '.join(prefs.property_types[:2])}, this is {listing_type}")


@dataclass
class BuyerMatch:
    client_id: str
    name: str
    verdict: str
    evidence: list[str]
    unknowns: list[str]
    conflicts: list[str]
    #: How many independent dimensions agreed. NOT a probability, and never
    #: rendered as a percentage — see the module docstring.
    matched_signals: int


def match_listing_to_buyer(listing: dict, client: dict) -> BuyerMatch:
    prefs = extract_preferences(client.get("preferences"))
    signals = [
        _location_signal(listing, prefs),
        _budget_signal(listing, prefs),
        _beds_signal(listing, prefs),
        _type_signal(listing, prefs),
    ]
    matched = [s for s in signals if s.verdict == "match"]
    conflicts = [s for s in signals if s.verdict == "conflict"]
    unknown = [s for s in signals if s.verdict == "unknown"]

    if conflicts:
        verdict = MISMATCH
    elif not matched:
        # Nothing agreed and nothing disagreed: we simply do not know this
        # person's preferences. That is not a rejection.
        verdict = INSUFFICIENT
    elif len(matched) >= 2:
        verdict = STRONG
    else:
        verdict = POSSIBLE

    return BuyerMatch(
        client_id=str(client.get("id") or ""),
        name=str(client.get("full_name") or "").strip() or "Unnamed contact",
        verdict=verdict,
        evidence=[s.note for s in matched],
        unknowns=[s.note for s in unknown],
        conflicts=[s.note for s in conflicts],
        matched_signals=len(matched),
    )


#: Sort order. Strong first, then possible, then the people we know too little
#: about — who stay in the list, because §25.
_RANK = {STRONG: 0, POSSIBLE: 1, INSUFFICIENT: 2, MISMATCH: 3}


def rank_matches(matches: list[BuyerMatch], *, include_mismatches: bool = False) -> list[BuyerMatch]:
    kept = [m for m in matches if include_mismatches or m.verdict != MISMATCH]
    return sorted(kept, key=lambda m: (_RANK[m.verdict], -m.matched_signals, m.name))


async def buyers_for_listing(conn, listing: dict, *, limit: int = 25) -> list[BuyerMatch]:
    """Rank this brokerage's buyer contacts against one listing.

    One query, then pure Python. RLS on `clients` scopes it to the caller's
    tenant, so there is no tenant predicate here to drift out of step with the
    policy — 0109's lesson about duplicating half a policy by hand.
    """
    rows = await conn.fetch(
        """
        SELECT id, full_name, preferences, stage, lead_score, last_contacted_at
          FROM clients
         WHERE archived_at IS NULL
           AND (client_type IS NULL OR client_type <> 'seller')
         ORDER BY COALESCE(lead_score, 0) DESC, updated_at DESC
         LIMIT 500
        """
    )
    matches = [match_listing_to_buyer(listing, dict(r)) for r in rows]
    return rank_matches(matches)[:limit]
