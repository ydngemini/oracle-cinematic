"""Every real-PostgreSQL test file runs in CI (external audit 35-07).

The CI live job named its files one by one. New `test_*_live.py` files were
never added, so eight of them (privacy, voice and auth regressions) skipped in
every run without anyone noticing: they need ORACLE_LIVE_DB_ADMIN_DSN, which
only that job sets. The job now runs the glob, and this keeps it covering
every file.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
TESTS = ROOT / "backend" / "tests"


def _live_job_targets() -> list[str]:
    text = CI.read_text()
    runs = [line for line in text.splitlines()
            if "python -m pytest" in line and "_live.py" in line]
    assert len(runs) == 1, f"expected one live pytest command in ci.yml, found {len(runs)}"
    return [t.removeprefix("tests/") for t in re.findall(r"tests/\S+\.py", runs[0])]


def test_the_live_job_covers_every_live_test_file():
    targets = _live_job_targets()
    files = sorted(p.name for p in TESTS.glob("test_*_live.py"))
    assert files, "no live test files found"
    missing = [f for f in files if not any(fnmatch.fnmatch(f, t) for t in targets)]
    assert not missing, f"live tests CI never runs: {missing}"


def test_the_live_job_names_no_file_that_does_not_exist():
    for target in _live_job_targets():
        assert list(TESTS.glob(target)), f"ci.yml runs tests/{target}, which matches nothing"
