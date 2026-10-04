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

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

log = logging.getLogger("oracle.buyer_matching")

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


async def buyers_for_listing(conn, listing: dict, *, limit: int = 25,
                            ctx=None) -> list[BuyerMatch]:
    """Rank this brokerage's buyer contacts against one listing.

    One query, then pure Python. RLS on `clients` scopes it to the caller's
    tenant, so there is no tenant predicate here to drift out of step with the
    policy — 0109's lesson about duplicating half a policy by hand.

    With one exception, which is the other half of that same lesson. The policy
    is `app_is_platform_admin() OR tenant_id = app_current_tenant()`, so under
    a platform-admin session RLS WIDENS instead of narrowing: an admin opening
    any listing would get up to 500 contacts drawn from every brokerage, with
    names and budgets, in an array with no tenant label. 0109 warned about
    hiding rows from an admin; this is the mirror image, and matching a
    listing against other people's clients is not a thing an admin should get
    by accident. So that case returns nothing and says why.
    """
    if ctx is not None and getattr(ctx, "is_platform_admin", False):
        log.info("Buyer matching skipped for a platform-admin session — RLS "
                 "would widen this query across every tenant.")
        return []

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


# ---------------------------------------------------------------------------
# A brokerage's OWN property (listings / leads), not an MLS row
# ---------------------------------------------------------------------------
#
# "Who should I call about this?" asked with the brokerage's own listing open
# had no answer: buyers_for_listing was reachable only from the MLS detail
# page, keyed on the shared MLS cache's columns. A brokerage's own listing keeps
# its address on `listings`/`leads` and its specs on the companion lead, with
# no city/zip columns, so those are read from the address the agent typed.

_US_ADDRESS = re.compile(
    r"^\s*(?P<street>[^,]+),\s*(?P<city>[^,]+?)\s*,\s*(?P<state>[A-Za-z]{2})\s*(?P<zip>\d{5})?"
)

#: Preference keys that hold free-text needs ("home office", "updated
#: kitchen"). They are never scored — there is no honest way to score free text
#: against free text without a model — but they are evidence an agent (and the
#: assistant) should see next to the match.
_NEEDS_KEYS = ("must_haves", "needs", "wants")


def parse_us_address(address: str) -> dict:
    """'123 Main Street, Wilmington, DE 19801' -> street/city/state/zip, or {}."""
    m = _US_ADDRESS.match(str(address or ""))
    if not m:
        return {}
    return {
        "street": m.group("street").strip(),
        "city": m.group("city").strip(),
        "state": m.group("state").upper(),
        "zip_code": m.group("zip") or "",
    }


def _as_dict(raw: Any) -> dict:
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def stated_needs(raw: Any) -> list[str]:
    prefs = _as_dict(raw)
    out: list[str] = []
    for key in _NEEDS_KEYS:
        out.extend(_as_list(prefs.get(key)))
    return out[:10]


async def load_owned_property(conn, ctx, *, lead_id: Optional[str] = None,
                              listing_id: Optional[str] = None) -> Optional[dict]:
    """The brokerage's own property, in the matcher's listing shape."""
    row = await conn.fetchrow(
        """
        SELECT ld.id::text AS lead_id, l.id::text AS listing_id,
               COALESCE(l.address, ld.address) AS address,
               COALESCE(l.price, ld.asking_price) AS price,
               l.status, ld.beds, ld.baths, ld.sqft, ld.state, ld.payload
          FROM leads ld
          FULL OUTER JOIN listings l ON l.lead_id = ld.id AND l.tenant_id = ld.tenant_id
         WHERE ($1::uuid IS NOT NULL AND ld.id = $1::uuid AND ld.tenant_id = $3::uuid)
            OR ($2::uuid IS NOT NULL AND l.id = $2::uuid AND l.tenant_id = $3::uuid)
         LIMIT 1
        """,
        lead_id, listing_id, ctx.tenant_id,
    )
    if row is None:
        return None
    payload = _as_dict(row["payload"])
    place = parse_us_address(row["address"] or payload.get("address") or "")
    return {
        "lead_id": row["lead_id"],
        "listing_id": row["listing_id"],
        "address": row["address"] or payload.get("address"),
        "status": row["status"],
        "list_price": row["price"],
        "beds": row["beds"],
        "baths": row["baths"],
        "sqft": row["sqft"],
        "city": payload.get("city") or place.get("city"),
        "zip_code": payload.get("zip_code") or place.get("zip_code"),
        "state": row["state"] if row["state"] not in (None, "", "NA") else place.get("state"),
        "property_type": payload.get("property_type"),
        "features": [str(f) for f in (payload.get("features") or []) if str(f).strip()][:20],
    }


async def property_buyer_matches(conn, ctx, *, lead_id: Optional[str] = None,
                                 listing_id: Optional[str] = None,
                                 limit: int = 5) -> Optional[dict]:
    """Rank this brokerage's buyers for one of its own properties, with the
    evidence behind each: the criteria that agree, what is unknown, their
    stated needs, and the properties they were actually shown recently."""
    prop = await load_owned_property(conn, ctx, lead_id=lead_id, listing_id=listing_id)
    if prop is None:
        return None
    matches = [m for m in await buyers_for_listing(conn, prop, ctx=ctx, limit=50)
               if m.verdict in (STRONG, POSSIBLE)][:limit]
    ids = [m.client_id for m in matches]
    showings: dict[str, list[dict]] = {}
    needs: dict[str, list[str]] = {}
    if ids:
        rows = await conn.fetch(
            """
            SELECT s.client_id::text AS client_id, s.shown_at, s.outcome, s.feedback,
                   COALESCE(l.address, ld.address) AS address
              FROM showings s
              LEFT JOIN listings l ON l.id = s.listing_id
              LEFT JOIN leads ld ON ld.id = COALESCE(s.lead_id, l.lead_id)
             WHERE s.client_id = ANY($1::uuid[])
               AND s.shown_at > now() - interval '120 days'
             ORDER BY s.shown_at DESC
            """,
            ids,
        )
        for r in rows:
            showings.setdefault(r["client_id"], []).append({
                "address": r["address"],
                "shown_at": r["shown_at"].date().isoformat() if r["shown_at"] else None,
                "outcome": r["outcome"],
                "feedback": (r["feedback"] or "")[:240] or None,
            })
        pref_rows = await conn.fetch(
            "SELECT id::text AS id, preferences FROM clients WHERE id = ANY($1::uuid[])", ids,
        )
        needs = {r["id"]: stated_needs(r["preferences"]) for r in pref_rows}
    return {
        "property": prop,
        "matches": [
            {
                "client_id": m.client_id,
                "name": m.name,
                "verdict": m.verdict,
                "matched_criteria": m.evidence,
                "unknown": m.unknowns,
                "matched_signals": m.matched_signals,
                "stated_needs": needs.get(m.client_id, []),
                "recent_showings": showings.get(m.client_id, [])[:5],
            }
            for m in matches
        ],
        "method": (
            "Rule-based: each buyer's recorded location, budget, bedroom and "
            "property-type preferences compared with this property. 'strong' "
            "means two or more criteria agree and none conflict. There is no "
            "model score or probability behind it."
        ),
    }
