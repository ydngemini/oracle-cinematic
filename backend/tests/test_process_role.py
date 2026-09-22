"""ORACLE_PROCESS_ROLE — the switch that makes horizontal web scaling safe.

voice_intel.py and reconstruction_worker.py hold their job queues in an
in-process asyncio.Queue with no cross-replica coordination: a job submitted
to one horizontally-scaled web replica is invisible to any other. Before this
variable existed, every replica unconditionally started every worker and
scheduler, so scaling the web tier on DigitalOcean App Platform (or anywhere
else) would have silently dropped voice/reconstruction work depending on
which replica happened to receive it.

Run in a subprocess, matching test_config_weak_secrets.py: reloading `config`
in-process cannot undo its module-level env side effects.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _process_role(role: str | None) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_BACKEND_DIR)
    environment["ORACLE_ENV"] = "dev"
    if role is None:
        environment.pop("ORACLE_PROCESS_ROLE", None)
    else:
        environment["ORACLE_PROCESS_ROLE"] = role
    return subprocess.run(
        [
            sys.executable, "-c",
            "import config; print(config.PROCESS_ROLE); print(config.RUNS_BACKGROUND_WORK)",
        ],
        cwd=_BACKEND_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestResolution:
    def test_unset_defaults_to_all_and_runs_background_work(self):
        """No deployment that predates this variable changes behaviour."""
        result = _process_role(None)
        assert result.stdout.splitlines() == ["all", "True"]

    def test_web_excludes_background_work(self):
        result = _process_role("web")
        assert result.stdout.splitlines() == ["web", "False"]

    def test_worker_runs_background_work(self):
        result = _process_role("worker")
        assert result.stdout.splitlines() == ["worker", "True"]

    def test_an_unrecognised_value_fails_safe_to_all(self):
        """Fails toward "runs everything", not toward "silently drops work" —
        a typo in this variable must not quietly turn off the scheduler."""
        result = _process_role("gcp-cloud-run")
        assert result.stdout.splitlines() == ["all", "True"]

    def test_case_insensitive(self):
        result = _process_role("WEB")
        assert result.stdout.splitlines() == ["web", "False"]


class TestServerGating:
    """The lifespan itself must actually consult the flag, for every
    replica-unsafe (or simply pointless-to-duplicate) subsystem."""

    def test_every_background_starter_is_gated(self):
        import server

        source = inspect.getsource(server.lifespan)
        for starter in (
            "start_voice_workers()",
            "start_reconstruction_workers()",
            "start_disposition_enforcer()",
            "start_job_workers()",
            "start_periodic_scheduler()",
        ):
            idx = source.index(starter)
            guard_window = source[max(0, idx - 200):idx]
            assert "config.RUNS_BACKGROUND_WORK" in guard_window, (
                f"{starter} is not gated by config.RUNS_BACKGROUND_WORK"
            )

    def test_every_background_stopper_is_symmetrically_gated(self):
        import server

        source = inspect.getsource(server.lifespan)
        finally_block = source[source.index("finally:"):]
        for stopper in (
            "stop_periodic_scheduler()",
            "stop_job_workers()",
            "stop_disposition_enforcer()",
            "stop_voice_workers()",
            "stop_reconstruction_workers()",
        ):
            idx = finally_block.index(stopper)
            guard_window = finally_block[max(0, idx - 400):idx]
            assert "config.RUNS_BACKGROUND_WORK" in guard_window, (
                f"{stopper} is not gated the same way its start call is"
            )
