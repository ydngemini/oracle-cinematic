import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import autobuild
import intelligence


class IntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "backend/tests").mkdir(parents=True)
        (self.root / "backend/source.py").write_text("value = 1\n")
        (self.root / "backend/tests/test_source.py").write_text("def test_value():\n    assert True\n")
        (self.root / ".neoh-agent-state").mkdir()
        self.paths = ["backend/source.py", "backend/tests/test_source.py", "docs/claude-prompts/LEDGER.md"]
        self.plan = {
            "category": "02", "goal": "Fix a bounded source regression",
            "acceptance": ["The existing input produces the expected output"],
            "evidence": [{"path": "backend/source.py", "line": 1}],
            "files": self.paths, "rollback": "Revert this isolated side-branch change",
            "self_review": "Inspected empty input and invalid input edge cases",
        }
        self.save_plan()

    def save_plan(self):
        (self.root / intelligence.PLAN).write_text(json.dumps(self.plan))

    def test_task_identity_is_pinned_and_deterministic(self):
        first = intelligence.task_identity("02", "Fix routes", "a" * 40)
        self.assertEqual(first, intelligence.task_identity("02", "Fix routes ", "a" * 40))
        self.assertNotEqual(first, intelligence.task_identity("02", "Fix routes", "b" * 40))

    def test_sensitive_risk_profile(self):
        self.assertEqual(intelligence.risk_profile("18", "Seats")["level"], "high")
        self.assertEqual(intelligence.risk_profile("02", "Fix permission leak")["level"], "high")
        self.assertEqual(intelligence.risk_profile("02", "Fix routes")["level"], "normal")

    def test_plan_requires_real_evidence(self):
        self.assertEqual(intelligence.read_plan(self.root, "02", self.paths)["category"], "02")
        self.plan["evidence"][0]["line"] = 999
        self.save_plan()
        with self.assertRaisesRegex(ValueError, "line"):
            intelligence.read_plan(self.root, "02", self.paths)

    def test_plan_requires_acceptance_and_rollback(self):
        for field, value in (("acceptance", []), ("rollback", ""), ("self_review", "")):
            with self.subTest(field=field):
                original = self.plan[field]
                self.plan[field] = value
                self.save_plan()
                with self.assertRaises(ValueError):
                    intelligence.read_plan(self.root, "02", self.paths)
                self.plan[field] = original

    def test_scope_and_category_must_match(self):
        with self.assertRaises(ValueError):
            intelligence.read_plan(self.root, "18", self.paths)
        with self.assertRaises(ValueError):
            intelligence.read_plan(self.root, "02", self.paths + ["unplanned.py"])

    def test_plan_cannot_read_credentials_or_escape(self):
        for name in ("../secret", "/etc/passwd", ".env", ".git/config"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                intelligence.local_file(self.root, name)
        (self.root / "escape").symlink_to("/etc")
        with self.assertRaises(ValueError):
            intelligence.local_file(self.root, "escape/passwd")

    def test_churn_limits_and_binaries(self):
        intelligence.check_churn(self.root, self.paths, "2\t1\tbackend/source.py\n", [])
        for stats in ("1501\t0\tbackend/source.py\n", "-\t-\timage.png\n"):
            with self.assertRaises(ValueError):
                intelligence.check_churn(self.root, self.paths, stats, [])
        (self.root / "new.py").write_text("new\n" * 1501)
        with self.assertRaises(ValueError):
            intelligence.check_churn(self.root, ["new.py"], "", ["new.py"])

    def test_syntax_fast_fail_does_not_execute_source(self):
        (self.root / "backend/source.py").write_text("def broken(\n")
        self.assertEqual(len(intelligence.syntax_errors(self.root, self.paths)), 1)

    def test_focused_test_selection(self):
        result = intelligence.focused_tests(self.root, ["backend/source.py"])
        self.assertEqual(result["backend"], ["tests/test_source.py"])
        (self.root / "oracle-app/src").mkdir(parents=True)
        (self.root / "oracle-app/src/routes.test.js").write_text("test('routes', () => {})")
        result = intelligence.focused_tests(self.root, ["oracle-app/src/routes.js"])
        self.assertEqual(result["frontend"], ["src/routes.test.js"])

    def test_failure_classification(self):
        self.assertEqual(intelligence.failure_kind("ENOSPC", 1), "disk")
        self.assertEqual(intelligence.failure_kind("test_no_tracked_file_is_owner_only unreadable", 1), "permissions")
        self.assertEqual(intelligence.failure_kind("", 124), "timeout")
        self.assertEqual(intelligence.failure_kind("AssertionError", 1), "test")

    def test_sensitive_source_requires_changed_regression_test(self):
        self.plan["category"] = "18"
        self.save_plan()
        with patch.object(autobuild, "ROOT", self.root), \
                patch.object(autobuild, "changed_paths_are_safe"), \
                patch.object(autobuild, "command") as command:
            passed, reason = autobuild.execute_tests(
                ["backend/source.py", "docs/claude-prompts/LEDGER.md"], 60, "18",
            )
        self.assertFalse(passed)
        self.assertIn("regression tests", reason)
        command.assert_not_called()

    def test_failure_fingerprint_ignores_runtime_noise(self):
        self.assertEqual(intelligence.failure_fingerprint("failed in 1.2s cycle-123-2"),
                         intelligence.failure_fingerprint("failed in 9.9s cycle-999-8"))

    def test_change_fingerprint_includes_untracked_content(self):
        first = intelligence.change_fingerprint(self.root, ["backend/source.py"])
        (self.root / "backend/source.py").write_text("value = 2\n")
        self.assertNotEqual(first, intelligence.change_fingerprint(self.root, ["backend/source.py"]))

    def test_recent_failures_are_category_scoped_and_bounded(self):
        history = [{"category": "02", "status": "failed", "task_id": str(index), "secret": "excluded"}
                   for index in range(8)]
        history.append({"category": "18", "status": "failed"})
        result = intelligence.recent_failures(history, "02")
        self.assertEqual(len(result), 3)
        self.assertEqual(result[-1]["task_id"], "7")
        self.assertNotIn("secret", result[-1])

    def test_receipts_are_versioned_private_and_atomic(self):
        intelligence.save_report(self.root, "checks", {"passed": True})
        target = self.root / ".neoh-agent-state/checks.json"
        self.assertEqual(json.loads(target.read_text()), {"schema": 1, "passed": True})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertFalse(target.with_suffix(".tmp").exists())

    def test_secret_redaction(self):
        secret = "sk-ant-" + "x" * 30
        result = intelligence.redact(f"{secret}\nAuthorization: Bearer token-value\npassword=hidden")
        self.assertNotIn(secret, result)
        self.assertNotIn("token-value", result)
        self.assertNotIn("hidden", result)

    def test_answer_freshness_and_category(self):
        now = datetime.now(timezone.utc)
        entries = [{"category": "02", "answer": "Current", "answered_at": now.isoformat()},
                   {"category": "02", "answer": "Old", "answered_at": (now - timedelta(days=31)).isoformat()},
                   {"category": "02", "answer": "Future", "answered_at": (now + timedelta(days=1)).isoformat()},
                   {"category": "18", "answer": "Other", "answered_at": now.isoformat()},
                   {"category": "02", "answer": "No timestamp"}]
        result = intelligence.fresh_answers(entries, "02", now)
        self.assertEqual([item["answer"] for item in result], ["Current"])

    def test_timeout_terminates_grouped_descendants(self):
        code = ("import subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "print(child.pid,flush=True); time.sleep(60)")
        result = autobuild.command([sys.executable, "-c", code], cwd=self.root, timeout=3)
        self.assertEqual(result.returncode, 124)
        pid = int(result.stdout.strip())
        status = Path(f"/proc/{pid}/stat")
        if status.exists():
            self.assertEqual(status.read_text().split()[2], "Z")

    def test_focused_checks_do_not_replace_full_suite(self):
        executed = []
        def command(argv, **kwargs):
            executed.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        with patch.object(autobuild, "ROOT", self.root), \
                patch.object(autobuild, "changed_paths_are_safe"), \
                patch.object(autobuild, "command", side_effect=command):
            passed, _ = autobuild.execute_tests(self.paths, 60, "02")
        self.assertTrue(passed)
        focused = [sys.executable, "-m", "pytest", "tests/test_source.py", "-q"]
        broad = [sys.executable, "-m", "pytest", "tests", "-q"]
        self.assertLess(executed.index(focused), executed.index(broad))
        report = json.loads((self.root / ".neoh-agent-state/checks.json").read_text())
        self.assertTrue(report["complete"])

    def test_focused_failure_stops_before_full_suite(self):
        executed = []
        def command(argv, **kwargs):
            executed.append(argv)
            failed = "tests/test_source.py" in argv
            return subprocess.CompletedProcess(argv, int(failed), "AssertionError" if failed else "", "")
        with patch.object(autobuild, "ROOT", self.root), \
                patch.object(autobuild, "changed_paths_are_safe"), \
                patch.object(autobuild, "command", side_effect=command):
            passed, _ = autobuild.execute_tests(self.paths, 60, "02")
        self.assertFalse(passed)
        self.assertNotIn([sys.executable, "-m", "pytest", "tests", "-q"], executed)

    def test_infrastructure_failure_bypasses_model_repair(self):
        def command(argv, **kwargs):
            failed = "tests/test_source.py" in argv
            return subprocess.CompletedProcess(argv, int(failed), "No space left on device" if failed else "", "")
        with patch.object(autobuild, "ROOT", self.root), \
                patch.object(autobuild, "changed_paths_are_safe"), \
                patch.object(autobuild, "command", side_effect=command):
            with self.assertRaises(intelligence.InfrastructureFailure):
                autobuild.execute_tests(self.paths, 60, "02")


if __name__ == "__main__":
    unittest.main()
