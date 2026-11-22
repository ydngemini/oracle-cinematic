"""The launch backlog is read from the ledger and kept current (T20.02,
AC20.02, L20-02). It also keeps every ledger row parseable: a row whose YAML
does not parse, or whose fence is left open, fails here."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("launch_backlog", REPO / "scripts/build-launch-backlog.py")
backlog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backlog)


def test_every_ledger_row_parses_and_appears_once():
    text = backlog.LEDGER.read_text()
    heads = re.findall(r"^#### (L\d{2}-\d{2})", text, re.M)
    assert len(heads) == len(set(heads)), "duplicate ledger ids"
    rows = backlog.rows(text)
    assert [r["id"] for r in rows] == heads
    built = backlog.build(text)
    for rid in heads:
        assert built.count(f"**{rid}**") == 1, rid
    assert len(re.findall(r"^\| A\d{2} \|", built, re.M)) == 10


def test_nothing_short_of_production_verified_is_closed():
    text = backlog.LEDGER.read_text()
    open_ids = {r["id"] for r in backlog.rows(text) if r["status"] != "production_verified"}
    built = backlog.build(text)
    assert all(f"**{rid}**" in built for rid in open_ids)


def test_the_committed_backlog_is_current():
    assert backlog.OUT.read_text() == backlog.build(backlog.LEDGER.read_text()), \
        "run: python3 scripts/build-launch-backlog.py"


def test_every_evidence_sha_is_read_as_text():
    """A commit hash of digits only (or digits and one 'e') is read by YAML as
    a number: 0650144 became 217188 (octal) and 1e21191 would become infinity.
    Quote such a hash in evidence_shas so the ledger records the real commit."""
    text = backlog.LEDGER.read_text()
    numeric = []
    for row in backlog.rows(text):
        for sha in row.get("evidence_shas") or []:
            if not isinstance(sha, str):
                numeric.append((row["id"], sha))
    assert not numeric, f"quote these evidence_shas: {numeric}"
