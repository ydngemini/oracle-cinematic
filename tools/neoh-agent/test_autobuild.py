"""Offline tests for Neoh autonomous side-branch delivery. No GitHub push is made."""
import importlib.util
import io
import os
from contextlib import ExitStack, redirect_stdout
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "autobuild", Path(__file__).with_name("autobuild.py")
)
autobuild = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(autobuild)


class SideBranchTests(unittest.TestCase):
    def setUp(self):
        receipt = patch.object(autobuild.intelligence, "save_report")
        receipt.start()
        self.addCleanup(receipt.stop)

    def test_stale_preflight_stops_before_model(self):
        with patch.object(autobuild, "synchronize_latest_main", return_value=("a" * 40, [])), \
                patch.dict(os.environ, {"NEOH_PLANNED_MAIN": "b" * 40}), \
                patch.object(autobuild, "model_round") as model:
            with self.assertRaisesRegex(RuntimeError, "Main changed after preflight"):
                autobuild.autobuild(object())
        model.assert_not_called()

    def failing_builder(self, infrastructure=False):
        args = type("Args", (), dict(
            category="02", task="", max_repairs=4, budget_usd=12,
            max_turns=24, model="sonnet", test_timeout=60,
            agent_timeout=120, skip_categories="", commit=True, push=True,
        ))()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"NEOH_PLANNED_MAIN": ""}))
            stack.enter_context(patch.object(autobuild, "synchronize_latest_main", return_value=("a" * 40, [])))
            stack.enter_context(patch.object(autobuild.work_discovery, "pending_categories", return_value=set()))
            stack.enter_context(patch.object(autobuild.work_discovery, "select_work", return_value={
                "category": "02", "task": "Fix source", "main_sha": "a" * 40, "prompt": "docs/prompt.md",
            }))
            stack.enter_context(patch.object(autobuild, "start_feature_branch", return_value=("auto/test", "b" * 40)))
            model = stack.enter_context(patch.object(autobuild, "model_round", return_value=0))
            stack.enter_context(patch.object(autobuild, "dirty_files", return_value=["backend/source.py"]))
            tests = stack.enter_context(patch.object(autobuild, "execute_tests", return_value=(False, "same assertion")))
            stack.enter_context(patch.object(autobuild.intelligence, "change_fingerprint", return_value="same content"))
            commit = stack.enter_context(patch.object(autobuild, "commit_local"))
            publish = stack.enter_context(patch.object(autobuild, "push_side_branch"))
            if infrastructure:
                tests.side_effect = autobuild.intelligence.InfrastructureFailure("disk full")
            result = autobuild.autobuild(args)
            commit.assert_not_called()
            publish.assert_not_called()
            return result, model.call_count

    def test_unchanged_repairs_stop_early(self):
        self.assertEqual(self.failing_builder(), (1, 2))

    def test_infrastructure_error_does_not_retry_model(self):
        self.assertEqual(self.failing_builder(infrastructure=True), (3, 1))

    def test_failure_report_preserves_diagnostics_and_output_edges(self):
        output = "start marker\n" + "noise\n" * 5000
        output += "FAILED tests/test_seats.py::test_concurrency\n"
        output += "noise\n" * 5000 + "end marker\n"
        report = autobuild.summarize_failure(output, 2500)
        self.assertLessEqual(len(report), 2500)
        self.assertIn("FAILED tests/test_seats.py::test_concurrency", report)
        self.assertIn("start marker", report)
        self.assertIn("end marker", report)

    def test_short_failure_report_is_unchanged(self):
        self.assertEqual(autobuild.summarize_failure("one failure"), "one failure")

    def test_backend_suite_uses_its_measured_timeout(self):
        checks = autobuild.approval_tests(
            ["backend/search_api.py"], timeout=1800, backend_timeout=2700,
        )
        backend = next(check for check in checks if check[0][-2:] == ["tests", "-q"])
        self.assertEqual(backend[2], 2700)

    def test_frontend_suite_keeps_the_standard_timeout(self):
        checks = autobuild.approval_tests(
            ["oracle-app/src/App.jsx"], timeout=1800, backend_timeout=2700,
        )
        frontend = next(check for check in checks if check[0] == ["npm", "test"])
        self.assertEqual(frontend[2], 1800)

    def test_provider_credit_is_not_reported_as_a_code_failure(self):
        args = type("Args", (), dict(
            category="02", budget_usd=2.0, max_turns=4, model="sonnet", agent_timeout=120,
        ))()
        with self._fake_runner("print('Credit balance is too low')\nraise SystemExit(1)"):
            with self.assertRaises(autobuild.ProviderCreditUnavailable):
                autobuild.model_round(args, "Fix one issue", 2.0, allow_dirty=False)

    def _fake_runner(self, script):
        real = subprocess.Popen
        return patch.object(
            autobuild.subprocess, "Popen",
            lambda _argv, **options: real([sys.executable, "-c", script], **options),
        )

    def _round_args(self, timeout=30):
        return type("Args", (), dict(
            category="02", budget_usd=2.0, max_turns=4, model="sonnet", agent_timeout=timeout,
        ))()

    def test_model_output_reaches_the_cycle_log_and_keeps_the_exit_code(self):
        log = io.StringIO()
        with self._fake_runner("print('turn one')\nraise SystemExit(7)"), redirect_stdout(log):
            code = autobuild.model_round(self._round_args(), "Fix", 2.0, allow_dirty=False)
        self.assertEqual(code, 7)
        self.assertIn("turn one", log.getvalue())

    def test_a_timed_out_session_keeps_what_it_already_logged(self):
        log = io.StringIO()
        script = "import time\nprint('turn one', flush=True)\ntime.sleep(60)"
        with self._fake_runner(script), redirect_stdout(log):
            with self.assertRaises(subprocess.TimeoutExpired):
                autobuild.model_round(self._round_args(timeout=2), "Fix", 2.0, allow_dirty=False)
        self.assertIn("turn one", log.getvalue())

    def test_a_grandchild_holding_the_pipe_cannot_hang_a_timeout(self):
        script = ("import subprocess, sys, time\n"
                  "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'])\n"
                  "time.sleep(60)")
        started = time.monotonic()
        with self._fake_runner(script), redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.TimeoutExpired):
                autobuild.model_round(self._round_args(timeout=1), "Fix", 2.0, allow_dirty=False)
        self.assertLess(time.monotonic() - started, 7.5)

    def test_branch_naming(self):
        self.assertIsNotNone(
            autobuild.SIDE_BRANCH.fullmatch("auto/neoh-02-20261019T120000Z-abcdef")
        )
        for name in [
            "main", "master", "feat/other",
            "auto/neoh-99-20261019T120000Z-abcdef",
            "auto/neoh-02-20261019T120000Z-ABCDEF",
            "auto/neoh-02-20261019T120000Z-abcdef:main",
        ]:
            with self.subTest(name=name):
                self.assertIsNone(autobuild.SIDE_BRANCH.fullmatch(name))

    def test_remote_allowlist(self):
        allowed = [
            "https://github.com/ydngemini/oracle-cinematic",
            "https://github.com/ydngemini/oracle-cinematic.git",
            "git@github.com:ydngemini/oracle-cinematic.git",
            "ssh://git@github.com/ydngemini/oracle-cinematic.git",
        ]
        for url in allowed:
            self.assertIsNotNone(autobuild.TRUSTED_ORIGIN.fullmatch(url))
        for url in [
            "https://github.com/elsewhere/oracle-cinematic.git",
            "https://github.com/ydngemini/oracle-cinematic.git@attacker.example",
            "https://attacker.example/ydngemini/oracle-cinematic.git",
            "https://secret@github.com/ydngemini/oracle-cinematic.git",
        ]:
            self.assertIsNone(autobuild.TRUSTED_ORIGIN.fullmatch(url))

    def test_main_is_never_pushed(self):
        with patch.object(autobuild.subprocess, "run") as run:
            for forbidden in ["main", "master", "feature/unsafe"]:
                with self.assertRaises(ValueError):
                    autobuild.push_side_branch(forbidden)
            run.assert_not_called()

    def test_valid_push_only_targets_new_side_branch(self):
        branch = "auto/neoh-02-20261019T120000Z-abcdef"
        origin = "git@github.com:ydngemini/oracle-cinematic.git"
        def git(*args):
            if args == ("branch", "--show-current"):
                return branch
            if args in (
                ("remote", "get-url", "origin"),
                ("remote", "get-url", "--push", "origin"),
            ):
                return origin
            if args == ("status", "--porcelain", "--untracked-files=all"):
                return ""
            if args[0] == "rev-parse":
                return "abc123"
            if args[0] == "diff-tree":
                return "oracle-app/src/App.jsx"
            raise AssertionError(args)

        with patch.object(autobuild.runner, "git", side_effect=git):
            with patch.object(autobuild.subprocess, "run",
                              return_value=subprocess.CompletedProcess(
                                  ["git"], 0, "", "")) as call:
                url = autobuild.push_side_branch(branch)
        self.assertEqual(
            url, "https://github.com/ydngemini/oracle-cinematic/tree/" + branch
        )
        arguments = call.call_args.args[0]
        self.assertEqual(arguments[0], "git")
        self.assertIn("origin", arguments)
        self.assertIn("refs/heads/" + branch + ":refs/heads/" + branch, arguments)
        self.assertNotIn("--force", arguments)
        self.assertNotIn("main", arguments)

    def test_push_destination_mismatch_is_blocked(self):
        branch = "auto/neoh-02-20261019T120000Z-abcdef"
        def git(*args):
            if args == ("branch", "--show-current"):
                return branch
            if args == ("remote", "get-url", "origin"):
                return "https://github.com/ydngemini/oracle-cinematic.git"
            if args == ("remote", "get-url", "--push", "origin"):
                return "https://github.com/attacker/not-neoh.git"
            raise AssertionError(args)
        with patch.object(autobuild.runner, "git", side_effect=git):
            with patch.object(autobuild.subprocess, "run") as call:
                with self.assertRaises(ValueError):
                    autobuild.push_side_branch(branch)
                call.assert_not_called()

    def test_never_push_before_checks_pass(self):
        branch = "auto/neoh-02-20261019T120000Z-abcdef"
        args = type("Args", (), dict(
            category="02", task="Fix one issue", max_repairs=0,
            budget_usd=2.0, max_turns=4, model="sonnet",
            test_timeout=60, agent_timeout=120, skip_categories="",
            commit=True, push=True,
        ))()
        with patch.object(autobuild, "synchronize_latest_main",
                          return_value=("a" * 40, [])):
            with patch.object(autobuild.work_discovery, "pending_categories",
                              return_value=set()):
                with patch.object(autobuild.work_discovery, "select_work",
                                  return_value={"category": "02", "task": "Fix one issue",
                                                "main_sha": "a" * 40, "prompt": "docs/claude-prompts/02-x.md"}):
                    with patch.object(autobuild, "start_feature_branch",
                                      return_value=(branch, "abc123")):
                        with patch.object(autobuild, "model_round", return_value=0):
                            with patch.object(autobuild, "dirty_files", return_value=[
                                "oracle-app/src/App.jsx",
                                "docs/claude-prompts/LEDGER.md",
                            ]):
                                with patch.object(autobuild, "execute_tests",
                                                  return_value=(False, "lint failed")):
                                    with patch.object(autobuild, "push_side_branch") as push:
                                        with patch.object(autobuild, "commit_local") as commit:
                                            result = autobuild.autobuild(args)
        self.assertEqual(result, 1)
        push.assert_not_called()
        commit.assert_not_called()


    def test_changed_main_blocks_automatic_push(self):
        branch = "auto/neoh-02-20261019T120000Z-abcdef"
        args = type("Args", (), dict(
            category="auto", task="", max_repairs=0, budget_usd=2.0,
            max_turns=4, model="sonnet", test_timeout=60,
            agent_timeout=120, skip_categories="", commit=True, push=True,
        ))()
        with patch.object(autobuild, "synchronize_latest_main",
                          return_value=("a" * 40, [])):
            with patch.object(autobuild.work_discovery, "select_work",
                              return_value={"category": "02", "task": "Fix a bug",
                                            "main_sha": "a" * 40,
                                            "prompt": "docs/claude-prompts/02-x.md"}):
                with patch.object(autobuild.work_discovery, "pending_categories",
                                  return_value=set()):
                    with patch.object(autobuild, "start_feature_branch",
                                      return_value=(branch, "abc123")):
                        with patch.object(autobuild, "model_round", return_value=0):
                            with patch.object(autobuild, "dirty_files",
                                              return_value=["oracle-app/src/App.jsx"]):
                                with patch.object(autobuild, "execute_tests",
                                                  return_value=(True, "")):
                                    with patch.object(autobuild, "commit_local"):
                                        with patch.object(autobuild.work_discovery,
                                                          "remote_main_sha", return_value="b" * 40):
                                            with patch.object(autobuild, "push_side_branch") as push:
                                                with self.assertRaises(RuntimeError):
                                                    autobuild.autobuild(args)
                                                push.assert_not_called()

    def test_prompts_must_refresh_before_work_selection(self):
        args = type("Args", (), dict(
            category="auto", task="", max_repairs=0, budget_usd=2,
            max_turns=4, model="sonnet", test_timeout=60,
            agent_timeout=120, skip_categories="", commit=True, push=True
        ))()
        with patch.object(autobuild, "synchronize_latest_main",
                          side_effect=RuntimeError("Cannot refresh main")):
            with patch.object(autobuild.work_discovery, "select_work") as choose:
                with self.assertRaises(RuntimeError):
                    autobuild.autobuild(args)
                choose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
