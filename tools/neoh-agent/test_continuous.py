"""Offline tests for persistent Neoh development supervision."""
import importlib.util
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "continuous", Path(__file__).with_name("continuous.py")
)
continuous = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(continuous)


class PersistentWorkerTests(unittest.TestCase):
    def test_interruptions_are_not_failures(self):
        self.assertEqual(continuous.cycle_status(-15), "interrupted")
        self.assertEqual(continuous.cycle_status(-2), "interrupted")
        self.assertEqual(continuous.cycle_status(1, stopped=True), "interrupted")
        self.assertEqual(continuous.cycle_status(1), "failed")
        self.assertEqual(continuous.cycle_status(-11), "failed")
        self.assertEqual(continuous.cycle_status(0, stopped=True), "passed")

    def test_storage_threshold(self):
        with patch.object(continuous.shutil, "disk_usage",
                          return_value=SimpleNamespace(free=2 * 1024 ** 3)):
            self.assertTrue(continuous.storage_ready(2))
            self.assertFalse(continuous.storage_ready(3))

    def test_low_disk_does_not_reserve_budget_or_start_cycle(self):
        state = {"blocked": {}, "history": [], "budget_day": continuous.utc_now()[:10],
                 "daily_reserved_usd": 0.0}
        args = SimpleNamespace(max_cycles=1, budget_usd=12, daily_budget_usd=48,
                               min_free_gib=2, error_poll_seconds=60)
        def stop(_seconds):
            continuous.STOP = True
        with patch.object(continuous, "STOP", False), \
                patch.object(continuous, "ensure_supervisor_checkout"), \
                patch.object(continuous, "acquire_lock"), \
                patch.object(continuous, "read_state", return_value=state), \
                patch.object(continuous, "write_state") as write, \
                patch.object(continuous, "storage_ready", return_value=False), \
                patch.object(continuous, "sleep_interruptible", side_effect=stop), \
                patch.object(continuous, "run_cycle") as cycle:
            self.assertEqual(continuous.supervise(args), 0)
            cycle.assert_not_called()
            write.assert_called_once()
            self.assertEqual(state["idle_reason"], "low_disk")
            self.assertEqual(state["daily_reserved_usd"], 0)

    def test_no_work_does_not_create_checkout_or_reserve_budget(self):
        state = {"blocked": {}, "history": [], "budget_day": continuous.utc_now()[:10],
                 "daily_reserved_usd": 0.0}
        args = SimpleNamespace(max_cycles=1, budget_usd=12, daily_budget_usd=48,
                               min_free_gib=0.5, error_poll_seconds=60)
        def stop(_seconds):
            continuous.STOP = True
        with patch.object(continuous, "STOP", False), \
                patch.object(continuous, "ensure_supervisor_checkout"), \
                patch.object(continuous, "acquire_lock"), \
                patch.object(continuous, "read_state", return_value=state), \
                patch.object(continuous, "write_state"), \
                patch.object(continuous, "storage_ready", return_value=True), \
                patch.object(continuous, "preflight_work",
                             side_effect=continuous.work_discovery.NoActionableWork("blocked")), \
                patch.object(continuous, "sleep_interruptible", side_effect=stop), \
                patch.object(continuous, "run_cycle") as cycle:
            self.assertEqual(continuous.supervise(args), 0)
            cycle.assert_not_called()
            self.assertEqual(state["daily_reserved_usd"], 0)
            self.assertEqual(state["idle_reason"], "no_eligible_work")
            self.assertNotIn("failures", state)

    def test_preflight_attaches_task_identity_and_risk(self):
        args = SimpleNamespace(category="auto", task="")
        with patch.object(continuous.work_discovery, "fetch_latest_main", return_value=("a" * 40, [])), \
                patch.object(continuous.work_discovery, "pending_categories", return_value={"03"}), \
                patch.object(continuous.work_discovery, "select_work", return_value={
                    "category": "02", "task": "Fix routes", "main_sha": "a" * 40,
                }) as choose:
            selection = continuous.preflight_work(args, {"05"})
        self.assertEqual(choose.call_args.kwargs["pending"], {"03", "05"})
        self.assertEqual(len(selection["task_id"]), 20)
        self.assertEqual(selection["risk"]["level"], "normal")

    def test_idle_reason_is_persisted_once(self):
        state = {"active_task": {"category": "02"}}
        with patch.object(continuous, "write_state") as write, \
                patch.object(continuous, "sleep_interruptible") as sleep:
            continuous.idle(state, "daily_budget", 120)
            continuous.idle(state, "daily_budget", 120)
        write.assert_called_once()
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn("active_task", state)

    def test_state_survives_restart_and_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with patch.object(continuous, "STATE_DIR", base):
                with patch.object(continuous, "STATE_FILE", base / "continuous.json"):
                    current = continuous.read_state()
                    current["cycles"] = 4
                    current["blocked"]["02"] = 2000000000
                    current["daily_reserved_usd"] = 12.0
                    continuous.write_state(current)
                    again = continuous.read_state()
                    self.assertEqual(again["cycles"], 4)
                    self.assertEqual(again["blocked"]["02"], 2000000000)
                    self.assertEqual(again["daily_reserved_usd"], 12.0)
                    self.assertEqual((base / "continuous.json").stat().st_mode & 0o777, 0o600)

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continuous.json"
            path.write_text("{broken")
            with patch.object(continuous, "STATE_FILE", path):
                with self.assertRaises(RuntimeError):
                    continuous.read_state()

    def test_one_supervisor_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(continuous, "STATE_DIR", root):
                with patch.object(continuous, "LOCK_FILE", root / "worker.lock"):
                    first = continuous.acquire_lock()
                    try:
                        with self.assertRaises(RuntimeError):
                            continuous.acquire_lock()
                    finally:
                        first.close()

    def test_refuse_auto_branch_as_supervisor_home(self):
        with patch.object(continuous, "git", return_value="auto/neoh-02-20261019T120000Z-abcdef"):
            with self.assertRaises(RuntimeError):
                continuous.ensure_supervisor_checkout()

    def test_stop_flag_interrupts_sleep(self):
        with patch.object(continuous, "STOP", True):
            with patch.object(continuous.time, "sleep") as mock_sleep:
                continuous.sleep_interruptible(1000)
                mock_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
