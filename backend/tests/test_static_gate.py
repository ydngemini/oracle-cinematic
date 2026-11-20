"""Audit 35-03 phase 1, and the regression it caught: a local
`from agent_profile import load_agent_identity` inside _execute_command_job
(added for call briefings, 34.3) made the name local to the whole function,
so the approved-email branch raised UnboundLocalError before reaching it."""

from __future__ import annotations

import importlib.util
import subprocess
import symtable
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def test_the_command_worker_uses_the_module_level_identity_loader():
    table = symtable.symtable((BACKEND / "commands_api.py").read_text(), "commands_api.py", "exec")
    worker = next(t for t in table.get_children() if t.get_name() == "_execute_command_job")
    assert not worker.lookup("load_agent_identity").is_local(), \
        "a local import shadows the module-level name for the whole function"


@pytest.mark.skipif(importlib.util.find_spec("ruff") is None, reason="ruff not installed (CI installs it)")
def test_the_backend_passes_the_phase_one_static_gate():
    result = subprocess.run([sys.executable, "-m", "ruff", "check", "."], cwd=BACKEND,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-2000:]
