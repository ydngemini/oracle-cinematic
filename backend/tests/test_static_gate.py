"""Audit 35-03 phase 1, and the regression it caught: a local
`from agent_profile import load_agent_identity` inside _execute_command_job
(added for call briefings, 34.3) made the name local to the whole function,
so the approved-email branch raised UnboundLocalError before reaching it.

Phase 2 widened the gate to F841 and F811 (unused locals, redefinitions of
unused names), phase 3 to F401 (unused imports), phase 4 to B023/B904 (loop
closures, exception causes) and phase 5 to B017/B011/B007/B905 (blind raises,
`assert False`, unused loop variables, zip without strict=). The families are
pinned below so the gate cannot pass by checking less."""

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


PINNED = ("E9", "F63", "F7", "F82", "F841", "F811", "F401", "PGH004", "B023", "B904",
          "B017", "B011", "B007", "B905")


def test_the_gate_keeps_its_pinned_families():
    """Removing a family from the selection would make every later run pass
    while checking less. An intended finding is silenced on its own line with
    a reason (`# noqa: F811 - ...`), never by ignoring the rule for a file."""
    lint = tomllib.loads((BACKEND / "ruff.toml").read_text())["lint"]
    assert set(PINNED) <= set(lint["select"])
    ignored = set(lint.get("ignore", [])) | set(lint.get("extend-ignore", [])) | {
        code
        for key in ("per-file-ignores", "extend-per-file-ignores")
        for codes in lint.get(key, {}).values() for code in codes
    }
    # Any ignore that covers a pinned rule: prefixes ("F8", "F84") and ALL included.
    assert not [c for c in ignored for rule in PINNED if c == "ALL" or rule.startswith(c)], ignored


def test_no_file_silences_the_gate_wholesale_and_import_noqa_names_its_reason():
    """A file-level `# ruff: noqa` / `# flake8: noqa` (indented or not, any
    case) turns the gate off for that file. Any noqa on the opening line of a
    multi-line import silences EVERY name in the statement, reason or not
    (178 dead names hid behind "# noqa: F401  (re-exported for route
    handlers)" in state_compliance), so it is refused outright: a genuine
    re-export or fixture import goes on its own line. Every other F401 noqa
    states its reason."""
    import re

    bad_file_level, bare_import_noqa, block_noqa = [], [], []
    for path in BACKEND.rglob("*.py"):
        if any(part in ("venv", "node_modules", "pascal-editor") for part in path.parts):
            continue
        text = path.read_text(errors="ignore")
        rel = path.relative_to(BACKEND)
        if re.search(r"^\s*#\s*(ruff|flake8)\s*:\s*noqa\b", text, re.M | re.I):
            bad_file_level.append(str(rel))
        for n, line in enumerate(text.splitlines(), 1):
            # a bare block noqa, or one that names F401 (an E402-only one hides nothing here)
            # (names before the noqa on the opening line hide the rest too)
            if re.search(r"\bimport\s*\([^)#]*#\s*noqa(?!\s*:)|\bimport\s*\([^)#]*#\s*noqa\s*:[^#\n]*F401", line, re.I):
                block_noqa.append(f"{rel}:{n}")
            m = re.search(r"#\s*noqa:\s*([A-Z0-9, ]*F401[A-Z0-9, ]*)(.*)$", line, re.I)
            if m and not re.search(r"[A-Za-z]{3}", m.group(2)):
                bare_import_noqa.append(f"{rel}:{n}")
    assert not bad_file_level, bad_file_level
    assert not block_noqa, block_noqa
    assert not bare_import_noqa, bare_import_noqa


def _zip_strict_false_lines(text: str) -> list[int]:
    """Lines where zip() (or builtins.zip) is called with a falsy constant
    strict= (False, 0, None), however it is spaced."""
    import ast

    lines = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_zip = (isinstance(func, ast.Name) and func.id == "zip") or (
            isinstance(func, ast.Attribute) and func.attr == "zip"
            and isinstance(func.value, ast.Name) and func.value.id == "builtins")
        if not is_zip:
            continue
        for kw in node.keywords:
            if kw.arg == "strict" and isinstance(kw.value, ast.Constant) and not kw.value.value:
                lines.append(kw.value.lineno)
    return lines


def _comments_by_line(text: str) -> dict[int, str]:
    """Real comments only: a `#` inside a string is not one."""
    import io
    import tokenize

    return {tok.start[0]: tok.string.lstrip("#").strip()
            for tok in tokenize.generate_tokens(io.StringIO(text).readline)
            if tok.type == tokenize.COMMENT}


def test_a_deliberately_uneven_zip_says_why():
    """B905 (phase 5) makes every zip() choose. strict=False is right only when
    the sides differ ON PURPOSE, and the reader needs the purpose: the line
    carries a comment. Sliding pairs are itertools.pairwise and a cut is
    itertools.islice, so neither needs strict=False at all."""
    probe = ("zip(a, b, strict=False)\nzip(a, b, strict = False)\nzip(a, b, strict=0)\n"
             "builtins.zip(a, b, strict=False)\nzip(a, b, strict=True)\n")
    assert _zip_strict_false_lines(probe) == [1, 2, 3, 4]
    assert _comments_by_line('x = "# not a comment"\ny = 1  # a reason\n') == {2: "a reason"}
    unexplained = []
    for path in BACKEND.rglob("*.py"):
        if any(part in ("venv", "node_modules", "pascal-editor") for part in path.parts):
            continue
        text = path.read_text(errors="ignore")
        if "strict" not in text:
            continue
        lines = _zip_strict_false_lines(text)
        comments = _comments_by_line(text) if lines else {}
        unexplained += [f"{path.relative_to(BACKEND)}:{n}" for n in lines if len(comments.get(n, "")) < 8]
    assert not unexplained, unexplained
