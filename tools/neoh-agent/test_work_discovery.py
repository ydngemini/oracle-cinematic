"""Offline tests for reading fresh Neoh prompts and choosing safe work."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "work_discovery", Path(__file__).with_name("work_discovery.py")
)
work = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(work)

ROOT = "docs/claude-prompts"
SHA = "a" * 40


def sample_ledger(overrides=None):
    overrides = overrides or {}
    rows = []
    for n in range(1, 21):
        category = f"{n:02d}"
        row = overrides.get(category, {})
        rows.append(
            f"| {category} | Category {category} | [prompt] | "
            f"{row.get('status', 'source_verified')} | local | abc | "
            f"{row.get('blockers', '—')} | {row.get('next', 'Fix one source-backed issue')} |"
        )
    return "## Category summary\n\n| Cat | Name | Prompt | Rollup | Env | Evidence | Blockers | Next task |\n" + "\n".join(rows) + "\n\n## Categories and deliverables\n"


def sample_readme():
    rows = []
    for n in range(1, 21):
        c = f"{n:02d}"
        depends = "—" if c in ("01", "02") else "01, 02"
        if c == "20":
            depends = "01–19"
        rows.append(f"| {c} | Category {c} | [prompt] | {depends} |")
    return "## The 20 categories\n" + "\n".join(rows) + "\n\n## Build order\n"


def paths():
    return [f"{ROOT}/{n:02d}-category.md" for n in range(1, 21)] + [
        f"{ROOT}/{name}" for name in ("README.md", "CONTRACT.md", "LEDGER.md")
    ]


class WorkDiscoveryTests(unittest.TestCase):
    def test_parse_full_ledger(self):
        parsed = work.summary_rows(sample_ledger({"02": {
            "status": "tests_pass", "next": "Fix page X"
        }}))
        self.assertEqual(len(parsed), 20)
        self.assertEqual(parsed["02"]["status"], "tests_pass")
        self.assertEqual(parsed["02"]["next"], "Fix page X")

    def test_parse_dependencies(self):
        deps = work.category_dependencies(sample_readme())
        self.assertEqual(len(deps), 20)
        self.assertEqual(deps["03"], ["01", "02"])
        self.assertEqual(len(deps["20"]), 19)

    def test_remote_guard(self):
        self.assertIsNotNone(work.ORIGIN_RE.fullmatch(
            "git@github.com:ydngemini/oracle-cinematic.git"
        ))
        self.assertIsNone(work.ORIGIN_RE.fullmatch(
            "https://github.com/attacker/oracle-cinematic"
        ))

    def run_selector(self, ledger, category="auto", task="", pending=None):
        snapshot = {
            f"{ROOT}/README.md": sample_readme(),
            f"{ROOT}/LEDGER.md": ledger,
            f"{ROOT}/CONTRACT.md": "# Contract\nC4. External approvals\nC10. Ledger\n",
        }
        def fake_git(*args, **kwargs):
            if args[:4] == ("ls-tree", "-r", "--name-only", SHA):
                return "\n".join(paths())
            raise AssertionError(args)
        def fake_read(sha, path):
            self.assertEqual(sha, SHA)
            return snapshot.get(path, "Contract: binding\n")
        with patch.object(work, "git", side_effect=fake_git), patch.object(
            work, "read_pinned", side_effect=fake_read
        ):
            return work.select_work(
                SHA, category=category, task=task, pending=pending or set()
            )

    def test_unassigned_starts_category_02(self):
        selection = self.run_selector(sample_ledger())
        self.assertEqual(selection["category"], "02")
        self.assertEqual(selection["task"], "Fix one source-backed issue")

    def test_occupied_category_is_not_duplicated(self):
        selection = self.run_selector(sample_ledger(), pending={"02"})
        self.assertEqual(selection["category"], "01")

    def test_explicit_category_not_invented(self):
        row = sample_ledger({"02": {"status": "blocked"}})
        with self.assertRaises(ValueError):
            self.run_selector(row, category="02")

    def test_owner_gate_skipped(self):
        row = sample_ledger({"02": {"next": "Owner decision: choose provider"}})
        selection = self.run_selector(row)
        self.assertEqual(selection["category"], "01")

    def test_no_task_without_category(self):
        with self.assertRaises(ValueError):
            self.run_selector(sample_ledger(), task="Custom work")

    def test_requires_latest_contract_and_all_prompts(self):
        with patch.object(work, "git", return_value="docs/claude-prompts/README.md"):
            with self.assertRaises(ValueError):
                work.select_work(SHA, pending=set())


if __name__ == "__main__":
    unittest.main()
