"""Offline guardrail tests. No Anthropic key or SDK import needed."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "runner", Path(__file__).with_name("runner.py")
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class NeohAgentGuardTests(unittest.TestCase):
    def test_category(self):
        self.assertTrue(runner.prompt_path("02").name.startswith("02-"))
        with self.assertRaises(ValueError):
            runner.prompt_path("21")

    def test_source_boundary(self):
        self.assertTrue(runner.source_path_allowed("oracle-app/src/App.jsx"))
        self.assertTrue(runner.source_path_allowed("backend/server.py"))
        for path in [".env", ".env.prod", ".git/config",
                     ".github/workflows/ci.yml", "infra/digitalocean/app.yaml",
                     "../outside.py", "/tmp/private.txt", ""]:
            with self.subTest(path=path):
                self.assertFalse(runner.source_path_allowed(path))

    def test_mode_prompt(self):
        from argparse import Namespace
        category = runner.prompt_path("02")
        audit = runner.engineering_prompt(
            Namespace(mode="audit", category="02", task=""), "abc", category
        )
        build = runner.engineering_prompt(
            Namespace(mode="build", category="02", task="fix one bug"), "abc", category
        )
        self.assertIn("READ-ONLY", audit)
        self.assertIn("implemented_untested", build)


if __name__ == "__main__":
    unittest.main()
