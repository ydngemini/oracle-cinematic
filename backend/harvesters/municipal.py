"""Normalization and aggregation helpers for municipal violation feeds."""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import replace
from typing import Iterable

from .property_adapter import PropertyRecord

_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^A-Z0-9# -]")
_SUFFIXES = {
    "STREET": "ST",
    "AVENUE": "AVE",
    "BOULEVARD": "BLVD",
    "ROAD": "RD",
    "DRIVE": "DR",
    "LANE": "LN",
    "COURT": "CT",
    "PLACE": "PL",
    "PARKWAY": "PKWY",
    "HIGHWAY": "HWY",
}


def normalize_street_address(value: str) -> str:
    """Conservative canonical address for grouping, not mailing delivery."""
    text = _PUNCT_RE.sub(" ", str(value or "").upper())
    parts = [part for part in _SPACE_RE.sub(" ", text).strip().split(" ") if part]
    return " ".join(_SUFFIXES.get(part, part) for part in parts)


def normalize_bbl(value: str) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits.zfill(10) if digits else ""


def normalize_pin(value: str) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    # Cook County PIN is 14 digits.  Preserve a non-standard source key rather
    # than padding it into a potentially different parcel.
    return digits if len(digits) == 14 else str(value or "").strip().upper()


def aggregate_property_records(
    records: Iterable[PropertyRecord],
) -> list[PropertyRecord]:
    """Collapse many violation rows into one record per reconciled parcel."""
    groups: "OrderedDict[str, list[PropertyRecord]]" = OrderedDict()
    for record in records:
        key = record.parcel_id or f"ADDR:{normalize_street_address(record.address)}"
        groups.setdefault(key, []).append(record)

    output: list[PropertyRecord] = []
    for rows in groups.values():
        first = rows[0]
        flags = sorted({flag for row in rows for flag in row.distress_flags})
        metadata = dict(first.source_metadata)
        metadata.update(
            {
                "violation_count": len(rows),
                "aggregated_by_property": True,
                "normalized_address": normalize_street_address(first.address),
            }
        )
        output.append(
            replace(
                first,
                address=normalize_street_address(first.address),
                distress_flags=flags,
                source_metadata=metadata,
            )
        )
    return output
