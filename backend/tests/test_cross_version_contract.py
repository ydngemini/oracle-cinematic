"""The cross-version contract harness, offline (external audit 35-06).

The `cross_version` CI job asks whether the PREVIOUS release keeps working on
the schema THIS change leaves behind: scripts/cross-version-contract.sh builds
the databases, scripts/cross_version/xv_pytest_plugin.py records why each
previous live test failed, and scripts/cross_version/evaluate.py turns that
into a verdict and an exit code. Only CI can run the whole thing (two
PostgreSQL services, two virtualenvs); this file proves the parts that decide:

* the plugin tells an incompatibility in the previous APP (a schema SQLSTATE
  raised through an app frame, or logged by the app) from a previous TEST that
  pins old behaviour (DRIFT), with real asyncpg exception classes;
* the decision table: every verdict, every exit code, and the canary, which
  passes only when the gate itself fails on a CHECK (false) NOT VALID;
* the migration gap is a SET (git diff --name-status), so a late low-numbered
  file (0144 after 0145, 531c41d) is in it, and so are edits and removals;
* which commit counts as "the previous release" for each GitHub event;
* no password reaches the record;
* nothing (no file, flag or environment variable) can relax an outcome.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "scripts" / "cross_version"
SCRIPT = REPO / "scripts" / "cross-version-contract.sh"
EVALUATE = HARNESS / "evaluate.py"
PY_SCRIPTS = ("evaluate.py", "xv_pytest_plugin.py", "boot_probe.py")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"xv_test_{name}", HARNESS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evaluate = _load("evaluate")
boot_probe = _load("boot_probe")


# ── the plugin, inside a real pytest run (pytester) ──────────────────────────

APP_MOD = '''
import logging

import asyncpg.exceptions as pgexc


def undefined_column():
    raise pgexc.UndefinedColumnError('column "stage" does not exist')


def no_privilege():
    raise pgexc.InsufficientPrivilegeError("permission denied for table leads")


def check_violation():
    raise pgexc.CheckViolationError('new row for relation "leads" violates check constraint "xv_canary"')


def statement_timeout():
    raise pgexc.QueryCanceledError("canceling statement due to statement timeout")


def changed_shape():
    row = {"id": 1}
    return row["tenant_id"]


def value():
    return 1


def swallowed():
    try:
        check_violation()
    except Exception:
        logging.getLogger("app").exception("lead insert failed")
    return None
'''

HELPERS = '''
def raise_it(cls, message):
    raise cls(message)
'''

PREV_TESTS = '''
import os

import asyncpg.exceptions as pgexc
import pytest

import app_mod
from helpers import raise_it


def test_passes():
    assert app_mod.value() == 1


def test_skipped():
    pytest.skip("not on this runner")


def test_app_undefined_column():
    app_mod.undefined_column()


def test_app_no_privilege():
    app_mod.no_privilege()


def test_app_check_violation():
    app_mod.check_violation()


def test_test_undefined_column():
    raise pgexc.UndefinedColumnError('column "stage" does not exist')


def test_test_no_privilege():
    raise_it(pgexc.InsufficientPrivilegeError, "permission denied for table leads")


def test_test_check_violation():
    raise pgexc.CheckViolationError("violates check constraint")


def test_assertion():
    assert app_mod.value() == 2


def test_pytest_fail():
    pytest.fail("the old expectation no longer holds")


def test_did_not_raise():
    with pytest.raises(ValueError):
        app_mod.value()


def test_app_key_error():
    app_mod.changed_shape()


def test_cause_chain():
    try:
        app_mod.undefined_column()
    except Exception as exc:
        raise RuntimeError("wrapped by the test") from exc


def test_context_chain():
    try:
        app_mod.no_privilege()
    except Exception:
        raise RuntimeError("raised while handling")


def test_logged_by_app():
    assert app_mod.swallowed() is not None


def test_timeout():
    app_mod.statement_timeout()


@pytest.fixture
def broken_setup():
    app_mod.undefined_column()


def test_setup_failure(broken_setup):
    pass


def test_secret_in_message():
    pw = os.environ["ORACLE_DB_PASSWORD"]
    raise RuntimeError(f"connect to postgresql://oracle_app_login:{pw}@db/x failed ({pw})")
'''


@pytest.fixture
def plugin_rows(pytester, monkeypatch):
    prev = pytester.path / "prev"
    (prev / "tests").mkdir(parents=True)
    # The real run is `python -m pytest` from the previous backend/, which puts
    # it on sys.path; this conftest does the same for the fake tree.
    (prev / "conftest.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n")
    (prev / "app_mod.py").write_text(APP_MOD)
    (prev / "tests" / "helpers.py").write_text(HELPERS)
    (prev / "tests" / "test_cases.py").write_text(PREV_TESTS)
    (prev / "tests" / "test_module_skipped.py").write_text(
        "import pytest\npytest.skip('needs a provider', allow_module_level=True)\n")
    results = pytester.path / "rows.jsonl"
    monkeypatch.setenv("XV_RESULTS", str(results))
    monkeypatch.setenv("XV_PREVIOUS_BACKEND", str(prev))
    monkeypatch.setenv("ORACLE_DB_PASSWORD", "pw-in-env-4f1c9a7e")
    monkeypatch.syspath_prepend(str(HARNESS))
    outcome = pytester.runpytest("-p", "xv_pytest_plugin", "--rootdir", str(prev),
                                 "-p", "no:cacheprovider", str(prev / "tests"))
    rows = [json.loads(line) for line in results.read_text().splitlines() if line.strip()]
    return outcome, rows


def _failed(rows: list[dict], name: str) -> dict:
    found = [r for r in rows if r.get("nodeid", "").endswith(f"::{name}") and r.get("outcome") == "failed"]
    assert len(found) == 1, (name, found)
    return found[0]


@pytest.mark.parametrize(("name", "sqlstate", "frame"), [
    ("test_app_undefined_column", "42703", "in undefined_column"),
    ("test_app_no_privilege", "42501", "in no_privilege"),
    ("test_app_check_violation", "23514", "in check_violation"),
    ("test_cause_chain", "42703", "in undefined_column"),      # __cause__ followed
    ("test_context_chain", "42501", "in no_privilege"),        # __context__ followed
    ("test_timeout", "57014", "in statement_timeout"),
])
def test_a_schema_error_raised_through_a_previous_app_frame_is_incompatible(plugin_rows, name, sqlstate, frame):
    row = _failed(plugin_rows[1], name)
    assert row["kind"] == "incompatible"
    assert row["sqlstate"] == sqlstate
    assert row["app_frame"].startswith("app_mod.py:") and row["app_frame"].endswith(frame)


@pytest.mark.parametrize(("name", "sqlstate"), [
    ("test_test_undefined_column", "42703"),
    ("test_test_no_privilege", "42501"),       # raised in a tests/ helper: still the test's own SQL
    ("test_test_check_violation", "23514"),
])
def test_the_same_error_raised_only_from_previous_test_frames_is_drift(plugin_rows, name, sqlstate):
    row = _failed(plugin_rows[1], name)
    assert row["kind"] == "drift"
    assert row["sqlstate"] == sqlstate
    assert row["app_frame"] is None


@pytest.mark.parametrize("name", ["test_assertion", "test_pytest_fail", "test_did_not_raise"])
def test_assertions_pytest_fail_and_did_not_raise_are_drift(plugin_rows, name):
    row = _failed(plugin_rows[1], name)
    assert row["kind"] == "drift"
    assert row["sqlstate"] is None


def test_a_non_assertion_error_in_an_app_frame_is_incompatible(plugin_rows):
    row = _failed(plugin_rows[1], "test_app_key_error")
    assert row["kind"] == "incompatible"
    assert row["exc_type"] == "builtins.KeyError"
    assert row["app_frame"].endswith("in changed_shape")


def test_an_asyncpg_error_the_app_only_logged_is_incompatible(plugin_rows):
    row = _failed(plugin_rows[1], "test_logged_by_app")
    assert row["exc_type"] == "builtins.AssertionError"
    assert row["kind"] == "incompatible"
    assert row["sqlstate"] == "23514"
    assert "23514" in row["log_sqlstates"]


def test_a_failing_fixture_is_recorded_in_its_setup_phase(plugin_rows):
    row = _failed(plugin_rows[1], "test_setup_failure")
    assert row["phase"] == "setup"
    assert row["kind"] == "incompatible"
    assert row["sqlstate"] == "42703"


def test_messages_carry_no_credentials(plugin_rows):
    row = _failed(plugin_rows[1], "test_secret_in_message")
    assert "pw-in-env-4f1c9a7e" not in json.dumps(plugin_rows[1])
    assert "oracle_app_login:" not in row["message"]


def test_totals_count_every_outcome_and_a_module_skipped_at_import(plugin_rows):
    outcome, rows = plugin_rows
    totals = [r for r in rows if r.get("type") == "totals"]
    assert len(totals) == 1
    totals = totals[0]
    assert totals["collected"] == 18
    assert (totals["passed"], totals["skipped"], totals["failed"]) == (1, 1, 16)
    assert totals["collect_errors"] == 0 and totals["collect_skipped"] == 1
    assert totals["exitstatus"] == 1
    skips = [r for r in rows if r.get("type") == "collect_skip"]
    assert [r["nodeid"].endswith("test_module_skipped.py") for r in skips] == [True]
    # The plugin records; it never changes an outcome.
    outcome.assert_outcomes(passed=1, skipped=2, failed=15, errors=1)


def test_the_plugin_refuses_to_run_without_a_results_file(pytester, monkeypatch):
    monkeypatch.delenv("XV_RESULTS", raising=False)
    monkeypatch.syspath_prepend(str(HARNESS))
    pytester.makepyfile("def test_a():\n    assert True\n")
    result = pytester.runpytest("-p", "xv_pytest_plugin", "-p", "no:cacheprovider")
    assert result.ret != 0


# ── the decision table ───────────────────────────────────────────────────────

STEPS_OK = {"venv": 0, "build_control": 0, "build_candidate": 0, "deploy_rehearsal": 0}


def _boot(ok: bool = True, *, harness: bool = False, hits: tuple = ()) -> dict:
    return {"ok": ok, "harness": harness, "checks": {"web.health_200": {"ok": ok and not harness}},
            "contract_hits": list(hits), "harness_hits": ["ImportError"] if harness else []}


def _row(node: str, outcome: str, **failure) -> dict:
    row = {"nodeid": f"tests/test_x_live.py::{node}", "phase": "call", "outcome": outcome}
    if outcome == "failed":
        row.update({"kind": "drift", "sqlstate": None, "log_sqlstates": [],
                    "exc_type": "builtins.AssertionError", "app_frame": None, "message": "x"})
        row.update(failure)
    return row


def _suite(*rows: dict, collected: int | None = None, extra: tuple = ()) -> list[dict]:
    status: dict[str, str] = {}
    for r in rows:
        if r["outcome"] == "failed" or r["nodeid"] not in status:
            status[r["nodeid"]] = r["outcome"]
    values = list(status.values())
    totals = {"type": "totals", "collected": len(status) if collected is None else collected,
              "passed": values.count("passed"), "failed": values.count("failed"),
              "skipped": values.count("skipped"), "collect_errors": 0,
              "collect_skipped": len(extra), "exitstatus": 1 if "failed" in values else 0}
    return [*rows, *extra, totals]


def _rc(rows: list[dict] | None) -> int:
    return 1 if any(r.get("outcome") == "failed" for r in rows or []) else 0


def _inputs(run1, *, rerun_c=None, rerun_b=None, steps=None, boots=None, canary=False) -> dict:
    s = dict(STEPS_OK, suite=_rc(run1))
    if rerun_c is not None:
        s.update(build_candidate_r=0, deploy_rehearsal_r=0,
                 rerun_candidate=_rc(rerun_c), rerun_control=_rc(rerun_b))
    s.update(steps or {})
    b = {name: _boot() for name in evaluate.BOOTS}
    b.update(boots or {})
    return {"meta": {"canary": canary}, "gap": [], "steps": s, "boots": b,
            "run1": run1, "rerun_candidate": rerun_c, "rerun_control": rerun_b}


INCOMPATIBLE_ROW = {"kind": "incompatible", "sqlstate": "42703", "app_frame": "crm.py:12 in list_leads",
                    "exc_type": "asyncpg.exceptions.UndefinedColumnError"}
CANARY_ROW = {"kind": "incompatible", "sqlstate": "23514", "app_frame": "crm.py:595 in create_listing",
              "exc_type": "asyncpg.exceptions.CheckViolationError"}


def _attributable(**failure):
    """a fails on C and on a fresh C', and passes on the control B."""
    bad = _row("test_a", "failed", **failure)
    return _inputs(_suite(bad, _row("test_b", "passed")), rerun_c=_suite(bad), rerun_b=_suite(_row("test_a", "passed")))


def test_every_test_passing_and_every_boot_ok_is_a_pass():
    record, code = evaluate.decide(_inputs(_suite(_row("test_a", "passed"), _row("test_b", "passed"))))
    assert (record["contract_result"], code) == ("pass", 0)
    assert {t["verdict"] for t in record["tests"].values()} == {"PASSED"}


def test_a_failure_that_does_not_reproduce_on_a_fresh_candidate_is_unreproduced():
    bad = _row("test_a", "failed", **INCOMPATIBLE_ROW)
    record, code = evaluate.decide(_inputs(_suite(bad), rerun_c=_suite(_row("test_a", "passed")),
                                           rerun_b=_suite(_row("test_a", "passed"))))
    assert record["tests"]["tests/test_x_live.py::test_a"]["verdict"] == "UNREPRODUCED"
    assert (record["contract_result"], code) == ("not_proven", 0)


def test_a_failure_the_control_shares_is_pre_existing():
    bad = _row("test_a", "failed", **INCOMPATIBLE_ROW)
    record, code = evaluate.decide(_inputs(_suite(bad), rerun_c=_suite(bad), rerun_b=_suite(bad)))
    assert record["tests"]["tests/test_x_live.py::test_a"]["verdict"] == "PRE-EXISTING"
    assert (record["contract_result"], code) == ("not_proven", 0)


def test_an_attributable_incompatible_failure_exits_1():
    record, code = evaluate.decide(_attributable(**INCOMPATIBLE_ROW))
    test = record["tests"]["tests/test_x_live.py::test_a"]
    assert (test["verdict"], test["sqlstate"], test["app_frame"]) == ("INCOMPATIBLE", "42703", "crm.py:12 in list_leads")
    assert (record["contract_result"], code) == ("incompatible", 1)


def test_an_attributable_drift_is_not_proven_and_exits_0():
    record, code = evaluate.decide(_attributable())
    assert record["tests"]["tests/test_x_live.py::test_a"]["verdict"] == "DRIFT"
    assert (record["contract_result"], code, record["has_drift"]) == ("not_proven", 0, True)
    assert any("DRIFT" in w for w in record["warnings"])


def test_a_skip_is_not_proven():
    record, code = evaluate.decide(_inputs(_suite(_row("test_a", "passed"), _row("test_b", "skipped"))))
    assert (record["contract_result"], code) == ("not_proven", 0)


def test_a_module_skipped_at_collection_is_not_proven():
    skip = ({"type": "collect_skip", "nodeid": "tests/test_y_live.py", "message": "skip"},)
    record, code = evaluate.decide(_inputs(_suite(_row("test_a", "passed"), extra=skip)))
    assert (record["contract_result"], code) == ("not_proven", 0)


@pytest.mark.parametrize(("label", "inputs"), [
    ("zero collected", _inputs(_suite(collected=0), steps={"suite": 5})),
    ("every test skipped", _inputs(_suite(_row("test_a", "skipped"), _row("test_b", "skipped")))),
    ("no results at all", _inputs(None, steps={"suite": 1})),
    ("results without totals", _inputs([_row("test_a", "passed")])),
    ("an empty results file", _inputs([], steps={"suite": 0})),
    ("the suite never ran", _inputs(_suite(_row("test_a", "passed")), steps={"suite": None})),
    ("pytest interrupted", _inputs(_suite(_row("test_a", "passed")), steps={"suite": 2})),
    ("killed", _inputs(_suite(_row("test_a", "passed")), steps={"suite": 137})),
    ("pytest failed, the plugin saw nothing", _inputs(_suite(_row("test_a", "passed")), steps={"suite": 1})),
    ("the plugin missed tests", _inputs(_suite(_row("test_a", "passed"), collected=5))),
    ("a collection error", _inputs([_row("test_a", "passed"), {"type": "totals", "collected": 1, "passed": 1,
                                                                "failed": 0, "skipped": 0, "collect_errors": 1}])),
    ("the venv failed", _inputs(_suite(_row("test_a", "passed")), steps={"venv": 1})),
    ("the control was not built", _inputs(_suite(_row("test_a", "passed")), steps={"build_control": 1})),
    ("P fails to boot on B", _inputs(_suite(_row("test_a", "passed")), boots={"P@B": _boot(False)})),
    ("a boot probe broke", _inputs(_suite(_row("test_a", "passed")), boots={"P@C": _boot(False, harness=True)})),
    ("a boot left no result", _inputs(_suite(_row("test_a", "passed")), boots={"N@C": None})),
    ("a rerun gave no answer", _inputs(_suite(_row("test_a", "failed", **INCOMPATIBLE_ROW)),
                                       rerun_c=_suite(_row("test_a", "skipped")),
                                       rerun_b=_suite(_row("test_a", "passed")))),
    ("reruns never ran", _inputs(_suite(_row("test_a", "failed", **INCOMPATIBLE_ROW)))),
])
def test_harness_problems_fail_closed_with_exit_2(label, inputs):
    record, code = evaluate.decide(inputs)
    assert (record["contract_result"], code) == ("harness", 2), label
    assert record["harness_reasons"], label


def test_the_previous_app_failing_to_boot_on_the_new_schema_exits_1():
    hits = ({"exception": "UndefinedColumnError", "sqlstate": "42703", "line": "..."},)
    record, code = evaluate.decide(_inputs(_suite(_row("test_a", "passed")),
                                           boots={"P@C": _boot(False, hits=hits)}))
    assert (record["contract_result"], code) == ("incompatible", 1)


def test_the_new_app_failing_to_boot_on_the_upgraded_schema_exits_1():
    record, code = evaluate.decide(_inputs(_suite(_row("test_a", "passed")), boots={"N@C": _boot(False)}))
    assert (record["contract_result"], code) == ("new_boot_failed", 1)


@pytest.mark.parametrize("precheck_exit", [3, 4, 1])
def test_a_failed_pre_deploy_rehearsal_exits_3(precheck_exit):
    record, code = evaluate.decide(_inputs(None, steps={"deploy_rehearsal": precheck_exit, "suite": None}))
    assert (record["contract_result"], code) == ("deploy_would_fail", 3)
    assert record["deploy_rehearsal"]["exit"] == precheck_exit


def test_canary_detected_only_when_the_gate_fails_on_23514():
    inputs = _attributable(**CANARY_ROW)
    inputs["meta"]["canary"] = True
    record, code = evaluate.decide(inputs)
    assert record["contract_result"] == "incompatible"
    assert (code, record["canary_result"]["detected"]) == (0, True)
    assert record["canary_result"]["evidence"] == ["tests/test_x_live.py::test_a"]


def test_canary_caught_by_the_p_at_c_boot_counts():
    hits = ({"exception": "CheckViolationError", "sqlstate": "23514", "line": "..."},)
    inputs = _inputs(_suite(_row("test_a", "passed")), boots={"P@C": _boot(False, hits=hits)}, canary=True)
    record, code = evaluate.decide(inputs)
    assert (code, record["canary_result"]["evidence"]) == (0, ["boot P@C"])


@pytest.mark.parametrize(("label", "inputs"), [
    ("nothing failed", _inputs(_suite(_row("test_a", "passed")), canary=True)),
    ("only another sqlstate", _attributable(**INCOMPATIBLE_ROW)),
    # A 23514 from the previous TEST's own SQL is DRIFT: a warning, exit 0 in a
    # normal run. The gate did not fail, so the canary is not detected.
    ("23514 only as drift", _attributable(sqlstate="23514", exc_type="asyncpg.exceptions.CheckViolationError")),
    ("23514 but unreproduced", _inputs(_suite(_row("test_a", "failed", **CANARY_ROW)),
                                       rerun_c=_suite(_row("test_a", "passed")),
                                       rerun_b=_suite(_row("test_a", "passed")), canary=True)),
])
def test_canary_without_an_attributable_23514_incompatibility_means_the_gate_cannot_fail(label, inputs):
    inputs["meta"]["canary"] = True
    record, code = evaluate.decide(inputs)
    assert (code, record["canary_result"]["detected"]) == (1, False), label
    assert "the gate cannot fail" in record["canary_result"]["verdict"]


def test_a_canary_that_could_not_run_exits_2():
    inputs = _inputs(None, steps={"build_candidate": 1}, canary=True)
    record, code = evaluate.decide(inputs)
    assert (code, record["canary_result"]["detected"]) == (2, False)


def _additive_gap(tmp_path: Path) -> list[dict]:
    sql = tmp_path / "9998_xv_probe.sql"
    sql.write_text("ALTER TABLE leads ADD COLUMN xv_probe integer;\n")
    return [{"name": "backend/db/migrations/9998_xv_probe.sql", "status": "A", "path": str(sql)}]


def test_classifier_miss_heads_the_summary_when_an_additive_gap_breaks_the_previous_app(tmp_path):
    inputs = _attributable(**INCOMPATIBLE_ROW)
    inputs["gap"] = _additive_gap(tmp_path)
    record, code = evaluate.build_record(inputs, REPO)
    assert record["classifier"]["whole_gap_additive"] is True
    assert (record["classifier_miss"], code, record["rollback_app_only_supported"]) == (True, 1, False)
    assert "### CLASSIFIER MISS" in evaluate.summary_markdown(record)

    drift = _attributable()
    drift["gap"] = _additive_gap(tmp_path)
    record, _ = evaluate.build_record(drift, REPO)
    assert record["classifier_miss"] is True       # not_proven because of DRIFT


def test_a_clean_additive_gap_is_no_miss_and_supports_app_only_rollback(tmp_path):
    inputs = _inputs(_suite(_row("test_a", "passed")))
    inputs["gap"] = _additive_gap(tmp_path)
    record, code = evaluate.build_record(inputs, REPO)
    assert (record["contract_result"], code, record["classifier_miss"]) == ("pass", 0, False)
    assert record["rollback_app_only_supported"] is True
    assert "CLASSIFIER MISS" not in evaluate.summary_markdown(record)
    assert record["migrations_applied_as"] == "nosuperuser-bypassrls-owner"
    assert record["app_role"] == "oracle_app_login"


def test_a_removed_migration_is_never_rollback_safe():
    inputs = _inputs(_suite(_row("test_a", "passed")))
    inputs["gap"] = [{"name": "backend/db/migrations/0145_x.sql", "status": "D"}]
    record, _ = evaluate.build_record(inputs, REPO)
    assert record["gap"][0]["classification"] == "removed"
    assert record["classifier"]["rollback_safe"] is False
    assert record["rollback_app_only_supported"] is False


def _write_work(work: Path, inputs: dict) -> None:
    work.mkdir(parents=True, exist_ok=True)
    (work / "meta.json").write_text(json.dumps({**inputs["meta"], "previous_sha": "a" * 40,
                                                "current_sha": "b" * 40}))
    (work / "gap.json").write_text(json.dumps(inputs["gap"]))
    (work / "steps.jsonl").write_text("".join(json.dumps({"step": k, "exit": v}) + "\n"
                                              for k, v in inputs["steps"].items() if v is not None))
    for name, boot in inputs["boots"].items():
        if boot is not None:
            (work / f"boot-{name}.json").write_text(json.dumps(boot))
    for key, file in (("run1", "run1"), ("rerun_candidate", "rerun-candidate"), ("rerun_control", "rerun-control")):
        if inputs[key] is not None:
            (work / f"{file}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in inputs[key]))


def _canary(inputs: dict) -> dict:
    inputs["meta"]["canary"] = True
    return inputs


@pytest.mark.parametrize(("label", "inputs", "exit_code", "result"), [
    ("pass", _inputs(_suite(_row("test_a", "passed"))), 0, "pass"),
    ("incompatible", _attributable(**INCOMPATIBLE_ROW), 1, "incompatible"),
    ("harness", _inputs(None, steps={"suite": 1}), 2, "harness"),
    ("deploy would fail", _inputs(None, steps={"deploy_rehearsal": 3}), 3, "deploy_would_fail"),
    ("canary detected", _canary(_attributable(**CANARY_ROW)), 0, "incompatible"),
    ("canary missed", _canary(_inputs(_suite(_row("test_a", "passed")))), 1, "pass"),
])
def test_the_process_exit_code_is_the_verdict(tmp_path, label, inputs, exit_code, result):
    work, out = tmp_path / "work", tmp_path / "out" / "record.json"
    _write_work(work, inputs)
    done = subprocess.run([sys.executable, str(EVALUATE), "record", "--work", str(work), "--repo", str(REPO),
                           "--out", str(out)], capture_output=True, text=True, env=_clean_env(tmp_path))
    assert done.returncode == exit_code, (label, done.stdout, done.stderr)
    record = json.loads(out.read_text())
    assert (record["exit_code"], record["contract_result"], record["schema"]) == (exit_code, result, 1)


def test_an_evaluate_crash_is_a_harness_failure(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _write_work(work, _inputs(_suite(_row("test_a", "passed"))))
    (work / "run1.jsonl").write_text("{not json\n")
    args = ["record", "--work", str(work), "--repo", str(REPO), "--out", str(tmp_path / "r.json")]
    assert evaluate.main(args) == 2

    _write_work(work, _inputs(_suite(_row("test_a", "passed"))))

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(evaluate, "_emit", boom)
    assert evaluate.main(args) == 2


# ── the gap and the previous release, on a real git repository ───────────────

def _clean_env(home: Path, **extra: str) -> dict[str, str]:
    """Nothing from this process: in CI the backend job's own GITHUB_* values
    would otherwise point the script at that job's outputs and summary."""
    env = {"PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
           "HOME": str(home), "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "xv", "GIT_AUTHOR_EMAIL": "xv@example.invalid",
           "GIT_COMMITTER_NAME": "xv", "GIT_COMMITTER_EMAIL": "xv@example.invalid"}
    env.update(extra)
    return env


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.env = _clean_env(root.parent)
        root.mkdir()
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        done = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=self.root, env=self.env,
                              capture_output=True, text=True, check=True)
        return done.stdout.strip()

    def commit(self, message: str, write: dict[str, str] | None = None, remove: tuple[str, ...] = ()) -> str:
        for name, text in (write or {}).items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            self.git("add", name)
        for name in remove:
            self.git("rm", "-q", name)
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def identify(self, work: Path, *args: str, **env: str) -> tuple[dict, dict]:
        output = work.parent / f"{work.name}.github_output"
        done = subprocess.run(["bash", str(SCRIPT), "identify", "--work", str(work), *args], cwd=self.root,
                              env=_clean_env(self.root.parent, GITHUB_OUTPUT=str(output), **env),
                              capture_output=True, text=True)
        assert done.returncode == 0, done.stdout + done.stderr
        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
        return json.loads((work / "identify.json").read_text()), outputs


MIG = "backend/db/migrations"


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    repo = Repo(tmp_path_factory.mktemp("xvgit") / "repo")
    c = {"bare": repo.commit("no migrations yet", {"README.md": "x\n"})}
    c["head_0145"] = repo.commit("0143 and 0145", {f"{MIG}/0143_a.sql": "SELECT 1;\n",
                                                  f"{MIG}/0145_c.sql": "SELECT 3;\n",
                                                  "backend/run_migrations.py": "# runner\n"})
    c["late_0144"] = repo.commit("0144 lands after 0145", {f"{MIG}/0144_b.sql": "SELECT 2;\n"})
    c["edit_0143"] = repo.commit("edit an applied migration", {f"{MIG}/0143_a.sql": "SELECT 11;\n"})
    c["drop_0145"] = repo.commit("remove a migration", remove=(f"{MIG}/0145_c.sql",))
    c["runner"] = repo.commit("runner change", {"backend/run_migrations.py": "# runner v2\n"})
    c["app_only"] = repo.commit("backend code only", {"backend/server.py": "app = None\n",
                                                     f"{MIG}/README.md": "notes\n"})
    c["harness"] = repo.commit("harness change", {"scripts/cross_version/evaluate.py": "# v2\n"})
    c["harness_gap"] = repo.commit("harness and a migration", {"scripts/cross-version-contract.sh": "#\n",
                                                              f"{MIG}/0146_d.sql": "SELECT 4;\n"})
    return repo, c


def test_a_late_lower_numbered_migration_is_in_the_gap(history, tmp_path):
    repo, c = history
    ident, out = repo.identify(tmp_path / "w", "--previous", c["head_0145"], "--current", c["late_0144"])
    assert ident["gap"] == [{"name": f"{MIG}/0144_b.sql", "status": "A"}]
    assert (ident["mode"], out["mode"], out["gap_count"]) == ("gap", "gap", "1")
    # What a filename range (rollback.sh before 35-06 F1) saw: nothing.
    assert [n for n in ("0143_a.sql", "0144_b.sql", "0145_c.sql") if "0145_c.sql" < n <= "0145_c.sql"] == []


def test_edited_and_removed_migrations_and_runner_changes_are_in_the_gap(history, tmp_path):
    repo, c = history
    ident, _ = repo.identify(tmp_path / "w1", "--previous", c["late_0144"], "--current", c["edit_0143"])
    assert ident["gap"] == [{"name": f"{MIG}/0143_a.sql", "status": "M"}]
    ident, _ = repo.identify(tmp_path / "w2", "--previous", c["edit_0143"], "--current", c["drop_0145"])
    assert ident["gap"] == [{"name": f"{MIG}/0145_c.sql", "status": "D"}]
    ident, _ = repo.identify(tmp_path / "w3", "--previous", c["drop_0145"], "--current", c["runner"])
    assert ident["gap"] == [{"name": "backend/run_migrations.py", "status": "M"}]


def test_a_backend_code_only_change_is_no_gap_and_writes_a_record(history, tmp_path):
    repo, c = history
    work = tmp_path / "w"
    ident, out = repo.identify(work, "--previous", c["runner"], "--current", c["app_only"])
    assert (ident["mode"], ident["gap"], ident["canary"], out["mode"]) == ("no_gap", [], False, "no_gap")
    record = json.loads((work / "out" / "cross-version-report.json").read_text())
    assert (record["contract_result"], record["previous_sha"], record["current_sha"]) == \
        ("no_gap", c["runner"], c["app_only"])


def test_no_previous_release_with_migrations_is_recorded(history, tmp_path):
    repo, c = history
    work = tmp_path / "w"
    ident, _ = repo.identify(work, "--previous", c["bare"], "--current", c["head_0145"])
    assert ident["mode"] == "no_previous"
    assert json.loads((work / "out" / "cross-version-report.json").read_text())["contract_result"] == "no_previous"


def test_a_harness_change_forces_the_canary(history, tmp_path):
    repo, c = history
    ident, out = repo.identify(tmp_path / "w1", "--previous", c["app_only"], "--current", c["harness"])
    assert (ident["mode"], ident["canary"], out["canary"]) == ("canary", True, "true")
    ident, out = repo.identify(tmp_path / "w2", "--previous", c["harness"], "--current", c["harness_gap"])
    assert (ident["mode"], ident["canary"]) == ("gap", True)
    assert ident["gap"] == [{"name": f"{MIG}/0146_d.sql", "status": "A"}]


@pytest.fixture(scope="module")
def merge_ref(tmp_path_factory):
    """refs/pull/N/merge: HEAD is a merge of the main tip (first parent) and
    the PR head (second parent)."""
    repo = Repo(tmp_path_factory.mktemp("xvpr") / "repo")
    base = repo.commit("base", {f"{MIG}/0001_a.sql": "SELECT 1;\n"})
    repo.git("checkout", "-q", "-b", "pr")
    pr_head = repo.commit("the PR", {f"{MIG}/0003_c.sql": "SELECT 3;\n"})
    repo.git("checkout", "-q", "main")
    main_tip = repo.commit("main moved on", {f"{MIG}/0002_b.sql": "SELECT 2;\n"})
    repo.git("checkout", "-q", "--detach", main_tip)
    repo.git("merge", "-q", "--no-ff", "-m", "merge ref", "pr")
    return repo, {"base": base, "pr_head": pr_head, "main_tip": main_tip, "merge": repo.git("rev-parse", "HEAD")}


def _event(tmp_path: Path, name: str, payload: dict) -> dict[str, str]:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload))
    return {"GITHUB_EVENT_NAME": name, "GITHUB_EVENT_PATH": str(path)}


@pytest.mark.parametrize(("event", "payload", "expected", "how"), [
    ("pull_request", {"number": 7}, "main_tip", "first parent of HEAD"),
    ("push", {"before": "<base>"}, "base", "github.event.before"),
    ("push", {"before": "0" * 40}, "main_tip", "first parent of HEAD"),
    ("push", {"before": "1234567" * 5 + "89abc"}, "main_tip", "first parent of HEAD"),   # unfetchable
    ("workflow_dispatch", {"inputs": {"confirm": ""}}, "main_tip", "first parent of HEAD"),
])
def test_which_commit_is_the_previous_release(merge_ref, tmp_path, event, payload, expected, how):
    repo, c = merge_ref
    payload = {k: (c["base"] if v == "<base>" else v) for k, v in payload.items()}
    ident, out = repo.identify(tmp_path / "w", **_event(tmp_path, event, payload))
    assert (ident["previous_sha"], ident["how"], out["prev"]) == (c[expected], how, c[expected])
    assert ident["current_sha"] == c["merge"]
    assert ident["previous_sha"] != c["pr_head"]


# ── no secrets in the record ─────────────────────────────────────────────────

SECRETS = {"XV_SU_PW": "su-0f3a9c1d2e4b", "XV_DOADMIN_PW": "do-7b2e4f6a8c0d", "XV_APP_PW": "app-1c3e5a7b9d2f",
           "XV_PLATFORM_PW": "plat-2d4f6b8a0c1e", "XV_PG_SUPERUSER_PASSWORD": "xv-ci-superuser"}


def test_the_record_holds_no_password(monkeypatch, tmp_path):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    leak = (f"connect postgresql://postgres:{SECRETS['XV_PG_SUPERUSER_PASSWORD']}@localhost:5432/xv "
            f"password={SECRETS['XV_SU_PW']} {SECRETS['XV_APP_PW']} {SECRETS['XV_PLATFORM_PW']} "
            f"{SECRETS['XV_DOADMIN_PW']}")
    inputs = _attributable(**INCOMPATIBLE_ROW, message=leak)
    inputs["boots"]["P@C"] = _boot(False, hits=({"exception": None, "sqlstate": None, "line": leak},))
    inputs["gap"] = _additive_gap(tmp_path)
    record, _ = evaluate.build_record(inputs, REPO)
    text = json.dumps(record) + evaluate.summary_markdown(record)
    for value in SECRETS.values():
        assert value not in text
    assert "postgres:" not in text


def test_a_boot_probe_crash_writes_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_DB_PASSWORD", SECRETS["XV_APP_PW"])
    monkeypatch.setenv("ORACLE_DB_PLATFORM_PASSWORD", SECRETS["XV_PLATFORM_PW"])

    def broken(_args):
        raise RuntimeError(f"password={SECRETS['XV_APP_PW']}")

    monkeypatch.setattr(boot_probe, "probe", broken)
    out = tmp_path / "boot.json"
    assert boot_probe.main(["--name", "P@C", "--app-dir", str(tmp_path), "--port", "5432", "--db", "x",
                            "--expect-head", "0001_a.sql", "--out", str(out), "--log-dir", str(tmp_path)]) == 0
    result = json.loads(out.read_text())
    assert (result["ok"], result["harness"]) == (False, True)
    assert SECRETS["XV_APP_PW"] not in out.read_text()


def test_every_artifact_file_is_scrubbed_of_every_run_password():
    text = SCRIPT.read_text()
    scrub = re.search(r'XV_SCRUB="([^"]*)"', text).group(1)
    for name in ("XV_SU_PW", "XV_DOADMIN_PW", "XV_APP_PW", "XV_PLATFORM_PW", "XV_PG_SUPERUSER_PASSWORD"):
        assert "${" + name in scrub
    assert 'echo "::add-mask::$v"' in text
    assert re.search(r"^cleanup\(\) \{\n(?:.*\n)*?\s+scrub_outputs$", text, re.M)
    assert "trap cleanup EXIT" in text


# ── nothing can relax an outcome ─────────────────────────────────────────────

RELAXING = re.compile(r"overrid|exempt|waive|declar|allow[-_ ]?list|ignore[-_ ]?list|baseline|accept"
                      r"|tolera|known[-_ ](?:fail|issue)|relax|permit", re.I)

# The only environment the harness reads from outside. None of it can turn a
# failure into a pass: inputs (paths, the CI event), the throwaway clusters'
# password, local port overrides, and the run's passwords read to redact them.
SHELL_ENV_ALLOWED = {
    "PATH", "HOME", "TMPDIR", "BASH", "BASH_SOURCE",
    "GITHUB_ACTIONS", "GITHUB_EVENT_NAME", "GITHUB_EVENT_PATH", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY",
    "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_TEMP",
    "XV_PG_SUPERUSER_PASSWORD", "XV_CANDIDATE_PORT", "XV_CONTROL_PORT",
}
PY_ENV_ALLOWED = {
    "evaluate.py": {"XV_SU_PW", "XV_DOADMIN_PW", "XV_APP_PW", "XV_PLATFORM_PW", "XV_PG_SUPERUSER_PASSWORD"},
    "xv_pytest_plugin.py": {"XV_RESULTS", "XV_PREVIOUS_BACKEND", "ORACLE_DB_PASSWORD",
                            "ORACLE_DB_PLATFORM_PASSWORD", "ORACLE_LIVE_DB_ADMIN_DSN"},
    "boot_probe.py": {"PATH", "HOME", "LANG", "TMPDIR", "ORACLE_DB_PASSWORD", "ORACLE_DB_PLATFORM_PASSWORD"},
}
CLI_ALLOWED = {
    "evaluate.py": {"--work", "--repo", "--out", "--summary", "--result", "--previous", "--current",
                    "--reason", "--run-id", "--run-attempt"},
    "boot_probe.py": {"--name", "--app-dir", "--python", "--port", "--db", "--expect-head", "--out",
                      "--log-dir", "--timeout", "--hold"},
}


def _is_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _constants(node: ast.AST, module_consts: dict[str, set[str]]) -> set[str] | None:
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return {e.value for e in node.elts}
    if isinstance(node, ast.Name):
        return module_consts.get(node.id)
    return None


def _env_read_nodes(tree: ast.Module):
    """(node, key, the os.environ node or None) for every environment read."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            if node.func.attr == "get" and _is_environ(node.func.value):
                yield node, node.args[0], node.func.value
            elif node.func.attr == "getenv" and isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
                yield node, node.args[0], None
        elif isinstance(node, ast.Subscript) and _is_environ(node.value):
            yield node, node.slice, node.value


def _env_reads(tree: ast.Module) -> tuple[set[str], list[str]]:
    """Every environment variable a module reads, and every read that cannot
    be resolved to a fixed name (or any other use of os.environ). A name read
    through a loop variable resolves only within the comprehension that binds
    it, over a literal tuple or a module-level tuple of literals."""
    module_consts: dict[str, set[str]] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            found = _constants(stmt.value, {})
            if found is not None:
                module_consts[stmt.targets[0].id] = found
    reads = list(_env_read_nodes(tree))
    resolved: dict[int, set[str]] = {}
    for comp in ast.walk(tree):        # breadth-first: an inner binding wins
        if not isinstance(comp, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            continue
        bound = {gen.target.id: _constants(gen.iter, module_consts) for gen in comp.generators
                 if isinstance(gen.target, ast.Name) and _constants(gen.iter, module_consts) is not None}
        inside = {id(n) for n in ast.walk(comp)}
        for node, key, _environ in reads:
            if id(node) in inside and isinstance(key, ast.Name) and key.id in bound:
                resolved[id(node)] = bound[key.id]
    names: set[str] = set()
    bad: list[str] = []
    allowed: set[int] = set()
    for node, key, environ in reads:
        if environ is not None:
            allowed.add(id(environ))
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            names.add(key.value)
        elif id(node) in resolved:
            names |= resolved[id(node)]
        else:
            bad.append(f"line {node.lineno}: unresolvable environment read")
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(_is_environ(c) for c in node.comparators):
            allowed.update(id(c) for c in node.comparators)
    bad += [f"line {n.lineno}: os.environ used as a whole" for n in ast.walk(tree)
            if _is_environ(n) and id(n) not in allowed]
    return names, bad


def _code_strings(tree: ast.Module) -> list[str]:
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                  and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


@pytest.mark.parametrize("name", PY_SCRIPTS)
def test_the_python_harness_reads_only_allowlisted_environment(name):
    names, bad = _env_reads(ast.parse((HARNESS / name).read_text()))
    assert not bad, bad
    assert names <= PY_ENV_ALLOWED[name], names - PY_ENV_ALLOWED[name]


@pytest.mark.parametrize("name", PY_SCRIPTS)
def test_the_python_harness_has_no_exemption_file_or_relaxing_option(name):
    tree = ast.parse((HARNESS / name).read_text())
    strings = _code_strings(tree)
    assert not [s for s in strings if RELAXING.search(s)], [s for s in strings if RELAXING.search(s)]
    options = {n.args[0].value for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_argument"
               and n.args and isinstance(n.args[0], ast.Constant)}
    assert options <= CLI_ALLOWED.get(name, set()), options - CLI_ALLOWED.get(name, set())


def _shell_code() -> str:
    return "\n".join(line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))


def test_the_shell_harness_reads_only_allowlisted_environment():
    code = _shell_code()
    referenced = set(re.findall(r"\$\{?([A-Z_][A-Z0-9_]*)", code))
    # and what its embedded Python snippets read
    referenced |= set(re.findall(r'os\.environ(?:\.get\(|\[)"([A-Z_][A-Z0-9_]*)"', code))
    assigned = set(re.findall(r"(?:^|[\s;(])([A-Z_][A-Z0-9_]*)=", code, re.M))
    for exported in re.findall(r"^\s*export\s+([A-Z_][A-Z0-9_ ]*)$", code, re.M):
        assigned.update(exported.split())
    external = referenced - assigned
    assert external <= SHELL_ENV_ALLOWED, external - SHELL_ENV_ALLOWED
    assert not [n for n in referenced | assigned if RELAXING.search(n)]


def test_the_shell_harness_has_no_exemption_file_or_relaxing_flag():
    code = _shell_code()
    assert not RELAXING.search(code), RELAXING.search(code)
    flags = set(re.findall(r"^\s*(--[a-z-]+)\)", code, re.M))
    # --canary only ADDS a check; the others pick commits and a work directory.
    assert flags == {"--previous", "--current", "--canary", "--work"}


def test_no_environment_variable_changes_a_verdict(monkeypatch):
    baseline = evaluate.decide(_attributable(**INCOMPATIBLE_ROW))
    for name in ("XV_OVERRIDE", "XV_ALLOW_INCOMPATIBLE", "XV_EXEMPT", "XV_ACCEPT", "CROSS_VERSION_SKIP",
                 "ORACLE_CROSS_VERSION_OVERRIDE", "XV_FORCE_PASS"):
        monkeypatch.setenv(name, "1")
    assert evaluate.decide(_attributable(**INCOMPATIBLE_ROW)) == baseline
    assert baseline[1] == 1
