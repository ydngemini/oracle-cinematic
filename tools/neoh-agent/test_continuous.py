"""Offline tests for persistent Neoh development supervision."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "continuous", Path(__file__).with_name("continuous.py")
)
continuous = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(continuous)


class PersistentWorkerTests(unittest.TestCase):
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
