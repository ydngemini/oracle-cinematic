"""Audit 35-03 phase 1, and the regression it caught: a local
`from agent_profile import load_agent_identity` inside _execute_command_job
(added for call briefings, 34.3) made the name local to the whole function,
so the approved-email branch raised UnboundLocalError before reaching it.

Phase 2 widened the gate to F841 and F811 (unused locals, redefinitions of
unused names); the families are pinned below so the gate cannot pass by
checking less."""

from __future__ import annotations

import importlib.util
import subprocess
import symtable
import sys
import tomllib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def test_the_command_worker_uses_the_module_level_identity_loader():
    table = symtable.symtable((BACKEND / "commands_api.py").read_text(), "commands_api.py", "exec")
    worker = next(t for t in table.get_children() if t.get_name() == "_execute_command_job")
    assert not worker.lookup("load_agent_identity").is_local(), \
        "a local import shadows the module-level name for the whole function"


@pytest.mark.skipif(importlib.util.find_spec("ruff") is None, reason="ruff not installed (CI installs it)")
def test_the_backend_passes_the_static_gate():
    result = subprocess.run([sys.executable, "-m", "ruff", "check", "."], cwd=BACKEND,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-2000:]


def test_the_gate_keeps_its_phase_one_and_two_families():
    """Removing a family from the selection would make every later run pass
    while checking less. An intended finding is silenced on its own line with
    a reason (`# noqa: F811 - ...`), never by ignoring the rule for a file."""
    lint = tomllib.loads((BACKEND / "ruff.toml").read_text())["lint"]
    assert {"E9", "F63", "F7", "F82", "F841", "F811"} <= set(lint["select"])
    ignored = set(lint.get("ignore", [])) | {
        code for codes in lint.get("per-file-ignores", {}).values() for code in codes
    }
    # Any ignore that covers a phase-2 rule, prefixes ("F8", "F84") included.
    assert not [c for c in ignored for rule in ("F841", "F811") if rule.startswith(c)], ignored
