"""The Agent Twin is advice only (prompt 07 T07.11, AC07.09; external audit AI-004).

T07.11: the twin learns how an agent decides and REPORTS it; it never
releases, ranks or reprioritises an action. AC07.09 asks for a test that a
twin preference cannot release or reorder anything. That holds by
construction while no ordering or release path can reach the twin's policy,
so this pins the construction: the only importers of the module are its own
API and the package facade, and anything else may take only the statistics
helper (wilson_interval), never a policy or a recorded decision. Wiring the
twin into a ranking or a release must fail here and be decided on purpose.
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
SKIP = {"tests", "venv", "ml_forge", "node_modules", "__pycache__"}

# Whole-module importers allowed: the twin's own HTTP API and the facade that
# lists every intelligence module (it re-exports, it does not call).
MODULE_IMPORTERS = {"intelligence_os_api.py", "intelligence/__init__.py"}
# What anything else may take from it.
SAFE_NAMES = {"wilson_interval"}
# The paths that order, release or permit actions.
DECISION_PATHS = ("command_center.py", "autonomy.py", "expected_value.py", "opportunity_engine.py",
                  "missions/policy.py", "missions/executor.py", "missions/planner.py")


def _sources():
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND)
        if SKIP & set(rel.parts):
            continue
        yield rel.as_posix(), path.read_text(encoding="utf-8")


def test_only_the_twin_api_and_the_facade_import_the_whole_module():
    offenders = []
    for rel, src in _sources():
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import) and any(a.name == "agent_twin" for a in node.names):
                if rel not in MODULE_IMPORTERS:
                    offenders.append(rel)
            if isinstance(node, ast.ImportFrom) and node.module == "intelligence" \
                    and any(a.name == "agent_twin" for a in node.names):
                offenders.append(rel)
    assert not offenders, f"the twin's policy is reachable from {offenders} (AC07.09)"


def test_everything_else_takes_only_the_statistics_helper():
    offenders = []
    for rel, src in _sources():
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom) and node.module == "agent_twin":
                extra = {a.name for a in node.names} - SAFE_NAMES
                if extra:
                    offenders.append(f"{rel}: {sorted(extra)}")
    assert not offenders, offenders


def test_no_ordering_or_release_path_reads_the_twin():
    for rel in DECISION_PATHS:
        path = BACKEND / rel
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id in {"agent_twin", "intelligence"} and node.attr not in SAFE_NAMES:
                raise AssertionError(f"{rel} reads agent_twin.{node.attr}: a twin preference would reach ordering or release")
