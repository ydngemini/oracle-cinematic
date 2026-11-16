"""Classify a cycle's outcome and decide retry vs escalate.

Only infrastructure failures are retried (with backoff, no repair budget).
The same failure signature twice escalates to a human instead of buying
another identical attempt — the pre-2026-11-16 loop retried one host-dependent
test failure for four days.
"""
from __future__ import annotations

import hashlib
import re

# autobuild exit codes (see autobuild.EXIT_*)
EXIT_CLASSES = {
    0: "passed", 1: "test_failure", 2: "stopped", 3: "infrastructure", 4: "infrastructure",
    5: "not_reproduced", 6: "baseline_red", 7: "budget", 8: "scope_violation", 124: "timeout",
}
INFRA_TEXT = re.compile(
    r"aclose\(\): asynchronous generator is already running|ECONNREFUSED|Connection refused"
    r"|unable to access 'https://github\.com|Could not resolve host|Temporary failure in name resolution"
    r"|API Error: (?:5\d\d|Connection)|overloaded_error", re.I)
CONFLICT_TEXT = re.compile(r"Main changed|Another auto/neoh branch exists|merge fresh Neoh main", re.I)
SCOPE_TEXT = re.compile(r"unapproved changed path|outside allowed_paths|weakened test|protected path", re.I)
BACKOFF_SECONDS = (600, 1800, 5400)


def classify(exit_code: int, log_text: str) -> str:
    kind = EXIT_CLASSES.get(exit_code, "test_failure" if exit_code else "passed")
    if kind in {"passed", "not_reproduced", "baseline_red", "budget", "scope_violation"}:
        return kind
    tail = log_text[-20000:]
    if INFRA_TEXT.search(tail):
        return "infrastructure"
    if kind == "stopped":
        if SCOPE_TEXT.search(tail):
            return "scope_violation"
        if CONFLICT_TEXT.search(tail):
            return "conflict"
        return "stopped"
    return kind


def signature(kind: str, log_text: str) -> str:
    """Stable across runs: the failure class plus the failing test ids."""
    failing = sorted(set(re.findall(r"^(?:FAILED|ERROR|FAIL) +(\S+)", log_text, re.M)))[:20]
    basis = kind + "|" + "|".join(failing)
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def decide(kind: str, sig: str, previous_signatures: list[str], infra_attempts: int,
           escalate_after: int, retry_max: int) -> tuple[str, int]:
    """('retry', delay) | ('escalate', 0) | ('fail', 0) | ('done', 0)."""
    if kind == "passed":
        return "done", 0
    if kind in {"not_reproduced", "scope_violation"}:
        return "escalate", 0
    if kind == "baseline_red":
        return "halt", 0
    if kind == "infrastructure":
        if infra_attempts < min(retry_max, len(BACKOFF_SECONDS)):
            return "retry", BACKOFF_SECONDS[infra_attempts]
        return "escalate", 0
    if previous_signatures.count(sig) + 1 >= max(1, escalate_after):
        return "escalate", 0
    return "fail", 0
