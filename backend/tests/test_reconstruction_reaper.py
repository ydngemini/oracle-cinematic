"""Leaked GPU pods get swept, and running ones do not.

`PodProvider.reconstruct` has a `finally` that terminates the pod on every
failure path. What it cannot cover is this process being killed between creating
a pod and reaching that block — and in that window the pod bills by the hour
with nothing pointing at it. An idle 24 GB card left overnight is about $5, and
nothing in the product would show it.

The reaper closes that window. The danger it introduces is the mirror image: it
cannot tell a leaked pod from one a *different backend replica* is actively
using, since it sees only a name and an age. So the age threshold has to stay
above the job ceiling, and that relationship is what most of this file defends.
"""

from __future__ import annotations

import asyncio

import pytest

import reconstruction_worker as worker


# ---------------------------------------------------------------------------
# The safety property
# ---------------------------------------------------------------------------

def test_the_sweep_threshold_stays_above_the_job_ceiling(monkeypatch):
    """Below it, the sweep terminates reconstructions that are still training —
    which surfaces as a mysterious GPU failure, not as a misconfiguration."""
    monkeypatch.setenv("RECON_POD_TIMEOUT", "5400")
    monkeypatch.delenv("RECON_REAP_MAX_AGE", raising=False)

    assert worker._reap_max_age_seconds() > 5400


def test_a_threshold_set_below_the_ceiling_is_raised_to_the_floor(monkeypatch):
    monkeypatch.setenv("RECON_POD_TIMEOUT", "5400")
    monkeypatch.setenv("RECON_REAP_MAX_AGE", "600")   # 10 min: would kill live jobs

    assert worker._reap_max_age_seconds() > 5400


def test_raising_the_job_ceiling_raises_the_threshold_with_it(monkeypatch):
    """Derived rather than set independently, so the two cannot silently
    invert when someone lengthens the timeout."""
    monkeypatch.delenv("RECON_REAP_MAX_AGE", raising=False)
    monkeypatch.setenv("RECON_POD_TIMEOUT", "5400")
    short = worker._reap_max_age_seconds()
    monkeypatch.setenv("RECON_POD_TIMEOUT", "7200")
    long = worker._reap_max_age_seconds()

    assert long > short > 7200 - 1800


def test_a_junk_threshold_does_not_crash_the_sweep(monkeypatch):
    monkeypatch.setenv("RECON_POD_TIMEOUT", "not-a-number")
    monkeypatch.setenv("RECON_REAP_MAX_AGE", "also-not-a-number")

    assert worker._reap_max_age_seconds() > 0


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def _run_one_sweep(monkeypatch, provider, *, sleep_raises=asyncio.CancelledError):
    """Drive exactly one iteration: the loop sweeps, then hits sleep, which we
    use to stop it. That also proves the FIRST sweep runs immediately rather
    than after an interval — the leak most worth catching is the one left by the
    crash that caused this restart."""
    monkeypatch.setattr(worker, "get_provider", lambda: provider)

    async def _stop(_seconds):
        raise sleep_raises()

    monkeypatch.setattr(worker.asyncio, "sleep", _stop)

    async def _drive():
        with pytest.raises(sleep_raises):
            await worker._reaper_loop()

    asyncio.run(_drive())


def test_the_first_sweep_runs_before_any_waiting(monkeypatch):
    swept: list = []

    class _Pods:
        def reap_stale_pods(self, max_age):
            swept.append(max_age)
            return ["pod-leaked"]

    _run_one_sweep(monkeypatch, _Pods())

    assert len(swept) == 1
    assert swept[0] > 5400, "the sweep must be handed a safe threshold"


def test_a_provider_that_rents_nothing_is_a_no_op(monkeypatch):
    """stub, local and the S3-staged providers have no pods to leak, so the
    sweep must cost them nothing rather than erroring every cycle."""
    class _NoPods:
        name = "stub"

    _run_one_sweep(monkeypatch, _NoPods())   # must not raise


def test_a_failing_sweep_does_not_take_down_the_worker_pool(monkeypatch):
    """An unfunded or misconfigured account raises here every cycle. That is a
    reason to log, not to stop accepting captures."""
    class _Broken:
        def reap_stale_pods(self, max_age):
            raise RuntimeError("RunPod GET /pods failed (401)")

    _run_one_sweep(monkeypatch, _Broken())   # swallowed, loop continues to sleep


def test_cancellation_is_not_swallowed(monkeypatch):
    """The reaper is cancelled at shutdown. A broad except that ate
    CancelledError would hang the server's shutdown instead."""
    class _Slow:
        def reap_stale_pods(self, max_age):
            raise asyncio.CancelledError()

    monkeypatch.setattr(worker, "get_provider", lambda: _Slow())

    # Patched even though this test is about the sweep: an unpatched sleep here
    # means a real 30-minute wait the moment anything reorders the loop, which
    # turns a failing assertion into a hung test run.
    async def _no_wait(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(worker.asyncio, "sleep", _no_wait)

    async def _drive():
        with pytest.raises(asyncio.CancelledError):
            await worker._reaper_loop()

    asyncio.run(_drive())


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_the_reaper_starts_with_the_pool_and_stops_with_it(monkeypatch):
    """Wiring is the whole point: before this, reap_stale_pods existed, was
    tested, and was called by nothing — so the leak it describes was still open."""
    monkeypatch.setattr(worker, "WORKER_COUNT", 1)

    async def _idle(*_a, **_k):
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "_worker_loop", _idle)
    monkeypatch.setattr(worker, "_reaper_loop", _idle)

    async def _drive():
        await worker.start_reconstruction_workers()
        started = len(worker._workers)
        await worker.stop_reconstruction_workers()
        return started, len(worker._workers)

    started, after = asyncio.run(_drive())

    assert started == 2, "one worker plus the reaper"
    assert after == 0, "shutdown must cancel the reaper too, not leave it running"
