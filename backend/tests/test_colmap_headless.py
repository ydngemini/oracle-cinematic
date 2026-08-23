"""COLMAP must be able to start on a headless host.

The defect this pins: COLMAP links Qt and constructs a QGuiApplication even for
its CLI subcommands. On a machine with no display — which every deployed backend
is — that aborts with SIGABRT inside createPlatformIntegration() before it reads
a single image. LocalGpuProvider shelled out to `colmap` with no headless
handling, so the whole reconstruction path was inert on any real server, and the
failure surfaced as an opaque non-zero exit that reads like a corrupt capture
rather than a missing display.

Observed on a headless GPU host 2026-08-23: `colmap feature_extractor` exits
rc=-6 in 0s without a display; with one it extracted features from 43 images in
27s and the subsequent solve registered 43/43 cameras at 0.51px reprojection
error.
"""

import inspect

import reconstruction_providers as rp


def test_colmap_env_requests_an_offscreen_platform():
    assert rp._COLMAP_ENV.get("QT_QPA_PLATFORM") == "offscreen"


def test_every_colmap_invocation_passes_the_headless_env():
    """One unguarded call is enough to abort the pipeline, so this asserts on
    all of them rather than a sample."""
    src = inspect.getsource(rp.LocalGpuProvider.reconstruct)
    colmap_calls = [
        line for line in src.splitlines()
        if "_run([" in line and '"colmap"' in line
    ]
    assert len(colmap_calls) >= 3, colmap_calls
    unguarded = [c.strip() for c in colmap_calls if "env=_COLMAP_ENV" not in c]
    assert not unguarded, f"colmap invoked without the headless env: {unguarded}"


def test_the_trainer_is_not_forced_offscreen():
    """The 3DGS trainer is a different binary that may legitimately want a real
    GL context; inheriting COLMAP's workaround could break it."""
    src = inspect.getsource(rp.LocalGpuProvider.reconstruct)
    trainer_line = [l for l in src.splitlines() if "_run(trainer" in l]
    assert trainer_line, "trainer invocation not found"
    assert "env=_COLMAP_ENV" not in trainer_line[0]


def test_run_merges_rather_than_replaces_the_environment():
    """A bare env= would drop PATH and HOME and break the subprocess in ways
    that look nothing like the original bug."""
    src = inspect.getsource(rp._run)
    assert "os.environ" in src and "**env" in src
