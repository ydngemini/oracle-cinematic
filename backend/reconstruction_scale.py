"""Putting a reconstruction into metres, from what the record already knows.

A photogrammetric reconstruction has no scale. COLMAP solves geometry up to a
similarity transform, so the cloud is the right SHAPE and an arbitrary SIZE, and
`floorplan_pipeline` refuses to produce a plan without an anchor — deliberately,
because a guessed scale multiplies every length and area by a constant and looks
entirely correct while doing it.

This finds that anchor for a subject the CRM already holds.

**Which anchor, and why the order.** Two are available and they are not equally
safe:

  1. **The building footprint** (`parcel_footprint_m2`), resolved from the
     address through licensed parcel data or OpenStreetMap. The pipeline
     compares it against the convex hull of the capture's own slice band — an
     outline against an outline — and that comparison does not care how many
     storeys the building has, because both are ground-plan areas. Preferred.
  2. **Recorded living area** (`known_total_sqft`, `leads.sqft`). The pipeline
     solves this against the interior area it detected, which is right for a
     single-storey home and wrong by roughly the storey count for anything
     taller: living area counts every floor, a plan is one floor. Usable, and
     carries that caveat with it.

**Both fail the same way on a partial capture.** A sweep of one room compared
against a whole building's footprint or living area yields a scale that is too
large by whatever fraction of the home was skipped. Nothing here can detect that
from the anchor alone, so `cross_check` compares the finished plan against a
second independent figure when one exists, and the disagreement is recorded
rather than smoothed over.

Refusing is a normal outcome. A subject with no address match and no recorded
square footage gets no plan, which is the honest result — the alternative is a
document full of confident measurements that are all wrong by the same factor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("oracle.reconstruction_scale")

SQFT_PER_M2 = 10.763910416709722

#: Below this a recorded square footage is a data-entry artefact rather than a
#: building, and anchoring to it would produce a plan scaled to a cupboard.
MIN_PLAUSIBLE_SQFT = 120.0

#: Above this it is a warehouse or a typo, and either way not something a
#: walkthrough capture measured.
MAX_PLAUSIBLE_SQFT = 25_000.0

#: A building footprint outside this range is not a dwelling.
MIN_PLAUSIBLE_FOOTPRINT_M2 = 15.0
MAX_PLAUSIBLE_FOOTPRINT_M2 = 2_000.0

#: How far the finished plan may sit from an independent figure before the
#: disagreement is worth saying out loud. Generous on purpose: the two numbers
#: measure genuinely different things — living area counts every storey and
#: includes wall thickness by most standards — so a modest gap is expected and
#: only a large one indicates the capture covered a different building than the
#: record describes.
CROSS_CHECK_TOLERANCE = 0.35


@dataclass(slots=True)
class ScaleAnchor:
    """One way to put this capture into metres, and how much to trust it."""

    kind: str                      # 'parcel_footprint_m2' | 'known_total_sqft'
    value: float
    source: str                    # where the number came from, for provenance
    caveat: str = ""               # what could still be wrong, in plain words

    def as_kwargs(self) -> dict[str, float]:
        """The keyword `extract_from_reconstruction` expects."""
        return {self.kind: self.value}

    def describe(self) -> str:
        note = f"Scale anchored to {self.source}."
        return f"{note} {self.caveat}".strip()


async def _subject_row(conn, *, lead_id: Optional[str], listing_id: Optional[str]):
    """Address and any recorded square footage for the capture's subject.

    `listings` carries no square footage at all, so a listing-backed capture has
    only the footprint route. That is a schema fact rather than an oversight
    here, and the caller should not be surprised by a None.
    """
    if lead_id:
        return await conn.fetchrow(
            "SELECT address, sqft FROM leads WHERE id = $1", lead_id
        )
    if listing_id:
        row = await conn.fetchrow(
            "SELECT address FROM listings WHERE id = $1", listing_id
        )
        return {"address": row["address"], "sqft": None} if row else None
    return None


async def resolve_anchor(conn, *, lead_id=None, listing_id=None) -> Optional[ScaleAnchor]:
    """The best anchor available for this subject, or None.

    None means no plan can be derived, and that is a legitimate answer — see the
    module docstring on why guessing is worse than refusing.
    """
    row = await _subject_row(conn, lead_id=lead_id, listing_id=listing_id)
    if row is None:
        return None

    address = (row["address"] or "").strip()
    recorded_sqft = row["sqft"]

    # 1. The building footprint: an outline measured against an outline.
    if address:
        try:
            from data_integrations import building_footprint

            candidates = await building_footprint.resolve_footprints(address=address)
        except Exception:  # noqa: BLE001 - an outage here must not fail the job
            logger.info("Footprint lookup failed for %r; falling back", address[:80])
            candidates = []
        for candidate in candidates:
            area = float(getattr(candidate, "area_sqm", 0) or 0)
            if MIN_PLAUSIBLE_FOOTPRINT_M2 <= area <= MAX_PLAUSIBLE_FOOTPRINT_M2:
                return ScaleAnchor(
                    kind="parcel_footprint_m2",
                    value=area,
                    source=(
                        f"a {area:.0f} m² building footprint from "
                        f"{candidate.source} ({candidate.attribution})"
                    ),
                    caveat=(
                        "It assumes the capture covers the whole building; a "
                        "partial sweep anchored this way reads too large."
                    ),
                )
            logger.info(
                "Ignoring %s footprint of %.0f m² for %r — outside %g-%g m²",
                getattr(candidate, "source", "?"), area, address[:60],
                MIN_PLAUSIBLE_FOOTPRINT_M2, MAX_PLAUSIBLE_FOOTPRINT_M2,
            )

    # 2. Recorded living area: right for one storey, wrong by the storey count
    #    for anything taller.
    if recorded_sqft and MIN_PLAUSIBLE_SQFT <= float(recorded_sqft) <= MAX_PLAUSIBLE_SQFT:
        return ScaleAnchor(
            kind="known_total_sqft",
            value=float(recorded_sqft),
            source=f"{float(recorded_sqft):.0f} sq ft recorded on the lead",
            caveat=(
                "Living area counts every storey while a plan is one floor, so "
                "on a multi-storey home this scale reads large."
            ),
        )

    logger.info(
        "No metric anchor for lead=%s listing=%s (address=%s, sqft=%s); "
        "no plan can be derived without one.",
        lead_id, listing_id, bool(address), recorded_sqft,
    )
    return None


def cross_check(document, anchor: ScaleAnchor, recorded_sqft) -> Optional[str]:
    """Compare the finished plan against an INDEPENDENT figure, if there is one.

    The anchor cannot validate itself: a plan solved against a footprint will
    always match that footprint. What catches a partial capture — the failure
    neither anchor can see — is a second number the plan was not fitted to.

    Returns a sentence for the provenance when they disagree, else None.
    """
    if not recorded_sqft or anchor.kind == "known_total_sqft":
        return None                      # nothing independent to compare against
    try:
        expected = float(recorded_sqft)
        produced = float(document.total_sqft)
    except (TypeError, ValueError):
        return None
    if expected <= 0 or produced <= 0:
        return None

    drift = abs(produced - expected) / expected
    if drift <= CROSS_CHECK_TOLERANCE:
        return None
    return (
        f"This plan measures {produced:,.0f} sq ft against {expected:,.0f} "
        f"recorded on the lead — {drift:.0%} apart. Either the capture covered "
        f"part of the property, or the record is wrong; the dimensions here are "
        f"only as good as whichever is right."
    )
