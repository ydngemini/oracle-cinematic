"""Offline tests for Neoh autonomous side-branch delivery. No GitHub push is made."""
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "autobuild", Path(__file__).with_name("autobuild.py")
)
autobuild = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(autobuild)


class SideBranchTests(unittest.TestCase):
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
            test_timeout=60, agent_timeout=120,
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


if __name__ == "__main__":
    unittest.main()
