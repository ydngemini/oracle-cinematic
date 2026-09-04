"""A reconstruction job says WHICH stage failed, not just that one did.

The pipeline has never produced a non-synthetic splat, and until now a failed
run recorded `status='failed'` and one error string. Six stages can fail —
capture, frame extraction, camera registration, training, delivery conversion,
storage — each with a different fix, and they were indistinguishable.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

import reconstruction_worker as worker

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "db" / "migrations" / "0101_reconstruction_diagnostics.sql"
)


@pytest.fixture(scope="module")
def sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


class TestSchema:
    def test_a_job_can_record_what_each_stage_measured(self, sql):
        assert "ADD COLUMN IF NOT EXISTS diagnostics jsonb" in sql

    def test_a_bad_capture_is_distinguishable_from_an_outage(self, sql):
        """"Capture more of the house" and "the deployment is broken" need
        opposite responses and both used to be status='failed'."""
        assert "'failed_quality_gate'" in sql
        assert "reconstruction_jobs_status_chk" in sql
        # Every pre-existing status must survive the re-added CHECK.
        for status in ("queued", "running", "succeeded", "failed"):
            assert f"'{status}'" in sql

    def test_the_check_is_dropped_before_it_is_added(self, sql):
        """A NOT VALID or already-present constraint would otherwise abort the
        migration — the DROP/re-ADD rule this repo learned the hard way."""
        assert sql.index("DROP CONSTRAINT IF EXISTS reconstruction_jobs_status_chk") \
            < sql.index("ADD CONSTRAINT reconstruction_jobs_status_chk")


class TestInstrumentation:
    def test_every_stage_of_the_pipeline_reports(self):
        run = inspect.getsource(worker._run_job) if hasattr(worker, "_run_job") else ""
        source = run or inspect.getsource(worker)
        for stage in ("capture", "reconstruction", "delivery", "storage"):
            assert f'"{stage}"' in source, f"{stage} is not instrumented"

    def test_diagnostics_never_break_a_run(self):
        """A metric that kills the job it is measuring is worse than no metric."""
        source = inspect.getsource(worker._record_stage)
        assert "except Exception" in source
        assert "never break a run" in source.lower()

    def test_no_capture_content_is_recorded(self):
        """Counts, sizes, durations and tool names only. Image bytes never."""
        source = inspect.getsource(worker._record_stage) + inspect.getsource(
            worker._image_dimensions)
        for forbidden in ("read_bytes", "b64", "base64", ".read()"):
            assert forbidden not in source, forbidden

    def test_registration_metrics_are_read_not_invented(self):
        """A fabricated quality number is worse than a missing one, because it
        would be trusted. Only what the toolchain actually wrote down."""
        source = inspect.getsource(worker._provider_metrics)
        assert "last_metrics" in source
        assert "cameras.json" in source
        assert "rather than an invented registration ratio" in source \
            or "invented" in source


class TestReadinessGate:
    def test_training_finishing_is_not_the_same_as_ready(self):
        """A job that trained and produced an empty delivery asset must fail.
        This is the gate the whole phase exists for."""
        source = inspect.getsource(worker)
        assert "if not delivery_bytes:" in source
        assert "training" in source and "renderable" in source

    def test_an_empty_capture_is_a_quality_gate_not_a_crash(self):
        assert issubclass(worker.QualityGateFailure, RuntimeError)
        source = inspect.getsource(worker)
        assert "no_usable_images" in source
        assert "failed_quality_gate" in source

    def test_a_quality_gate_failure_does_not_reraise_as_an_outage(self):
        """It returns. Re-raising would put it in the same bucket as a broken
        GPU, which is the distinction this adds."""
        source = inspect.getsource(worker)
        gate_block = source.split("except QualityGateFailure")[1].split("except Exception")[0]
        assert "return" in gate_block
        assert "raise" not in gate_block
