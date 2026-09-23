"""Is this MLS feed licensed? One place decides, and it fails closed.

Why this module exists
----------------------
Classification used to be a denylist of three names that defaulted to
"licensed":

    _DEV_DATASETS = {"test", "test_sd", "test_sf"}
    if dataset in _DEV_DATASETS: developer
    else:                        licensed          # <- fail OPEN

So any dataset slug that was not one of those three became
`licensed_property_listing` automatically. A typo, a new Bridge sample set, or
a *reference* dataset all became "real licensed inventory" with no human ever
saying so — and the live configuration was `actris_ref`, a Bridge REFERENCE
dataset whose own adapter docstring notes has every ModificationTimestamp
frozen. 52,622 rows are sitting in oracle_mls_listings today stamped
`licensed_property_listing` on the strength of a naming convention.

That matters because licensed classification is what a brokerage's MLS
readiness is allowed to key on. A feed that is not really licensed must never
be able to make a customer's setup screen say "MLS: Ready", and must never be
presented to a customer as live inventory.

The rule here is the inverse: a feed is developer data unless an operator has
explicitly declared it licensed AND named the agreement. Naming conventions do
not grant rights; contracts do.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

# The two values that may ever reach a stored record. Anything downstream —
# search, detail, readiness, buyer matching — branches on these.
DEVELOPER = "developer_listing_dataset"
LICENSED = "licensed_property_listing"

# Bridge's own synthetic sets. Kept for clarity, but they are no longer what
# makes the decision — they are simply datasets that can never be licensed.
KNOWN_DEVELOPER_DATASETS = frozenset({"test", "test_sd", "test_sf"})

# Bridge publishes per-board *reference* datasets (frozen sample inventory for
# integration work) under a `_ref` suffix. They look exactly like a board feed
# and are not one.
_REFERENCE_SUFFIXES = ("_ref", "-ref", "_reference", "_sample", "_demo")


@dataclass(frozen=True)
class FeedLicense:
    classification: str
    source_kind: str
    #: Why this feed is or is not licensed — shown to operators, never to
    #: customers, and recorded on the feed's health row.
    reason: str
    #: The operator-supplied agreement/board reference. Absent means unlicensed.
    agreement_ref: Optional[str] = None

    @property
    def is_licensed(self) -> bool:
        return self.classification == LICENSED

    @property
    def may_report_ready(self) -> bool:
        """Only a licensed feed may make a brokerage's MLS capability READY."""
        return self.is_licensed


def looks_like_reference_dataset(dataset: str) -> bool:
    slug = (dataset or "").strip().lower()
    return any(slug.endswith(suffix) for suffix in _REFERENCE_SUFFIXES)


def classify_dataset(
    dataset: str,
    *,
    declared_licensed: bool = False,
    agreement_ref: str = "",
) -> FeedLicense:
    """Decide what a dataset's records are, failing closed.

    `declared_licensed` and `agreement_ref` come from operator configuration,
    never from the provider's response and never from a request.
    """
    slug = (dataset or "").strip().lower()
    ref = (agreement_ref or "").strip()

    if not slug:
        return FeedLicense(DEVELOPER, DEVELOPER, "no dataset configured")

    if slug in KNOWN_DEVELOPER_DATASETS:
        return FeedLicense(
            DEVELOPER, DEVELOPER,
            f"{slug} is one of Bridge's synthetic developer datasets",
        )

    if looks_like_reference_dataset(slug):
        # Even an explicit declaration does not override this: a reference
        # dataset is frozen sample inventory, so calling it licensed live data
        # would be wrong regardless of what any agreement says.
        return FeedLicense(
            DEVELOPER, DEVELOPER,
            f"{slug} is a provider reference/sample dataset, not live inventory",
        )

    if not declared_licensed:
        return FeedLicense(
            DEVELOPER, DEVELOPER,
            f"{slug} has not been declared licensed by an operator "
            f"(set ORACLE_MLS_LICENSED=1 and ORACLE_MLS_AGREEMENT_REF)",
        )

    if not ref:
        return FeedLicense(
            DEVELOPER, DEVELOPER,
            f"{slug} was declared licensed but no agreement reference was given",
        )

    return FeedLicense(
        LICENSED, "licensed_mls",
        f"{slug} declared licensed under {ref}",
        agreement_ref=ref,
    )


def classify_from_env(dataset: str, *, prefix: str = "ORACLE_MLS") -> FeedLicense:
    """Read the operator's declaration from the environment.

    Deliberately two variables rather than one: flipping a boolean by accident
    is easy, and having to also name the agreement makes the declaration
    deliberate and auditable.
    """
    declared = (os.getenv(f"{prefix}_LICENSED", "") or "").strip().lower() in ("1", "true", "yes")
    ref = os.getenv(f"{prefix}_AGREEMENT_REF", "") or ""
    return classify_dataset(dataset, declared_licensed=declared, agreement_ref=ref)


_SAFE_REF = re.compile(r"^[A-Za-z0-9 ._/-]{3,120}$")


def valid_agreement_ref(value: str) -> bool:
    """Agreement references end up in logs and operator screens."""
    return bool(_SAFE_REF.match((value or "").strip()))
