"""docs/launch-state.md must not pass for the current state (audit 35-05).

It called itself "the one authoritative view of launch state" while saying
"State as of 2026-10-03", migration head 0124, `STAGING_ENABLED` unset and a
release pipeline that "has never run end to end" — all overtaken (the first CI
release to staging was 2026-10-04). Status lives only in the ledger
(CONTRACT C10.1). The page is now a dated snapshot under a banner; these tests
keep it from claiming authority again, or naming a stale migration head.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DOC = REPO / "docs" / "launch-state.md"
MIGRATIONS = REPO / "backend" / "db" / "migrations"

BANNER = re.compile(r"Historical snapshot, \d{4}-\d{2}-\d{2}, not maintained", re.I)
_HEAD_PHRASE = re.compile(r"migration[ _]head", re.I)
# A migration file name (0124_job_claim_aging.sql), or a bare four-digit number
# that is neither a year (19xx/20xx: "moved often during 2026") nor the start
# of a date (2026-10-03).
_MIGRATION_NO = re.compile(r"\b(\d{4})_[a-z0-9_]+(?:\.sql)?\b|\b(?!19|20)(\d{4})\b(?!-\d)")


def newest_migration() -> str:
    nums = [p.name[:4] for p in MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")]
    assert nums, f"no migrations under {MIGRATIONS}"
    return max(nums)


def problems(text: str, newest: str) -> list[str]:
    out = []
    head = "\n".join(text.splitlines()[:12])
    has_banner = bool(BANNER.search(head))
    if not has_banner:
        out.append("no 'Historical snapshot, <date>, not maintained' banner at the top")
    for n, line in enumerate(text.splitlines(), 1):
        if re.search(r"\bauthoritative\b", line, re.I):
            if not has_banner:
                out.append(f":{n} says 'authoritative' without the snapshot banner")
            elif not re.search(r"\b(not|no longer|never)\b", line, re.I):
                out.append(f":{n} claims authority: {line.strip()[:100]}")
        for m in _HEAD_PHRASE.finditer(line):
            for named, bare in _MIGRATION_NO.findall(line[m.end(): m.end() + 80]):
                num = named or bare
                if num != newest:
                    out.append(f":{n} names migration head {num}, but the newest migration file is {newest}")
    return out


def test_the_page_is_a_dated_snapshot_and_names_no_stale_migration_head():
    assert problems(DOC.read_text(encoding="utf-8"), newest_migration()) == []


def test_the_banner_points_to_where_status_actually_lives():
    head = "\n".join(DOC.read_text(encoding="utf-8").splitlines()[:12])
    assert "claude-prompts/LEDGER.md" in head
    assert "neoh-launch-readiness.py" in head and "generated_at" in head


@pytest.mark.parametrize("index", ["README.md", "docs/README.md"])
def test_the_indexes_do_not_call_it_the_current_state(index):
    readme = (REPO / index).read_text(encoding="utf-8")
    pointers = 0
    for para in re.split(r"\n\s*\n|\n(?=\|)", readme):
        if "launch-state.md" in para:
            pointers += 1
            flat = " ".join(para.split())
            assert not re.search(r"\bcurrent state\b", flat, re.I), flat
            assert re.search(r"historical|snapshot", flat, re.I), flat
    assert pointers, f"{index} no longer links launch-state.md; drop it from this test"


# The checker itself, on the text the audit quoted and on its corrections.

_STALE = """# Neoh launch state

**Could Brokerage #1 use Neoh tomorrow? No.**

This page is the one authoritative view of launch state.
**State as of 2026-10-03 ~20:00 UTC.** Migration head is `0124_job_claim_aging.sql`.
"""


@pytest.mark.parametrize("text, expect", [
    (_STALE, ["banner", "authoritative", "migration head 0124"]),
    ("# T\n\n> **Historical snapshot, 2026-10-03, not maintained.** It is not authoritative.\n", []),
    ("# T\n\n> **Historical snapshot, 2026-10-03, not maintained.**\n\nThe one authoritative view.\n",
     ["claims authority"]),
    ("# T\n\n> Historical snapshot, 2026-10-03, not maintained.\n\nMigration head is 0124 on 2026-10-03.\n",
     ["migration head 0124"]),
    ("# T\n\n> Historical snapshot, 2026-10-03, not maintained.\n\nMigration head: `0999_last.sql`.\n", []),
    ("# T\n\n> Historical snapshot, 2026-10-03, not maintained.\n\nThe live migration_head is in GET /version.\n",
     []),
    ("# T\n\n> Historical snapshot, 2026-10-03, not maintained.\n\nThe migration head moved often during 2026.\n",
     []),
    ("# T\n\n> Historical snapshot, 2026-10-03, not maintained.\n\nMigration head `0124_job_claim_aging.sql` in 2026.\n",
     ["migration head 0124"]),
])
def test_the_checker(text, expect):
    got = problems(text, "0999")
    assert len(got) == len(expect), got
    for want, line in zip(expect, got, strict=True):
        assert want in line, (want, line)
