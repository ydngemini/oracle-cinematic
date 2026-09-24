"""Structural rules for the MLS path that must not regress.

These read source rather than behaviour, because each one is a rule about what
the code is *allowed to do* — and the failure modes are silent. A DELETE that
should not exist removes history nobody notices is gone; a logged token is not
visible until someone reads the log.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
MLS_SOURCES = [
    BACKEND / "data_integrations" / "bridge_listings_feed.py",
    BACKEND / "data_integrations" / "listings_feed.py",
    BACKEND / "data_integrations" / "mls_sink.py",
    BACKEND / "mls_portal.py",
    BACKEND / "mls_health.py",
]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------------------
# §9 — a listing that stops appearing is not a listing that was deleted
# ---------------------------------------------------------------------------

def test_nothing_in_the_mls_path_deletes_listings():
    """Bridge does not expose a deletion feed on the endpoints in use, so a
    record absent from a delta means "not modified recently", not "withdrawn".
    Deleting on that inference destroys sale history that cannot be recovered
    and that the provider never asked us to remove.

    Removal is expressed as a status transition instead — expired, withdrawn,
    closed — which is reversible and auditable.
    """
    for path in MLS_SOURCES:
        body = _text(path)
        assert "DELETE FROM oracle_mls_listings" not in body, (
            f"{path.name} deletes listings. A record missing from a delta is not "
            f"a deletion; use a status transition."
        )


def test_no_invented_deletion_semantics():
    """If a provider gains a real deletion contract, it should be handled
    explicitly and this test updated — not inferred from absence."""
    for path in MLS_SOURCES:
        body = _text(path).lower()
        assert "if not seen" not in body
        assert "missing_from_delta" not in body


# ---------------------------------------------------------------------------
# §37 — what must never reach a log line
# ---------------------------------------------------------------------------

_FORBIDDEN_IN_LOGS = ("access_token", "self.token", "authorization", "api_key")


def test_sync_logging_never_interpolates_a_credential():
    for path in MLS_SOURCES:
        for line in _text(path).splitlines():
            stripped = line.strip()
            if not stripped.startswith(("logger.", "log.")):
                continue
            lowered = stripped.lower()
            for needle in _FORBIDDEN_IN_LOGS:
                assert needle not in lowered, f"{path.name}: log line may carry a credential: {stripped[:90]}"


def test_sync_logging_never_dumps_a_whole_provider_payload():
    for path in MLS_SOURCES:
        for line in _text(path).splitlines():
            stripped = line.strip().lower()
            if stripped.startswith(("logger.", "log.")):
                assert "payload)" not in stripped and "%s\", raw" not in stripped


def test_the_backfill_emits_one_structured_line_per_run():
    body = _text(BACKEND / "data_integrations" / "bridge_listings_feed.py")
    assert "mls_sync feed=" in body
    for field in ("mode=", "state=", "pages=", "received=", "rejected=",
                  "upserted=", "licence="):
        assert field in body, f"structured sync log is missing {field}"


# ---------------------------------------------------------------------------
# Fail-closed licensing must have exactly one authority
# ---------------------------------------------------------------------------

def test_no_module_reimplements_the_licence_decision():
    """The original bug was a second, fail-open copy of this decision living
    in the feed adapter. One authority, or it drifts again."""
    for path in MLS_SOURCES:
        body = _text(path)
        if path.name == "mls_licensing.py":
            continue
        # A literal set of the developer dataset names outside mls_licensing is
        # how the denylist got duplicated the first time.
        assert not re.search(r'"test_sd"\s*,\s*"test_sf"', body), (
            f"{path.name} appears to re-declare the developer dataset list"
        )


def test_licensed_classification_is_never_a_literal_default():
    """`return "licensed_property_listing"` as a fallback is the fail-open
    shape that mislabelled 52,622 rows."""
    for path in MLS_SOURCES:
        tree = ast.parse(_text(path) or "pass")
        for node in ast.walk(tree):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant):
                assert node.value.value != "licensed_property_listing", (
                    f"{path.name} returns a hardcoded licensed classification"
                )
