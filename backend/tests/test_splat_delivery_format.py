"""The delivery format must be one splat-transform can actually write.

This is a regression suite for a defect that shipped and went unnoticed for
months: `reconstruction_worker` asked PlayCanvas splat-transform to *write* a
`.splat`, which no released version of that tool can do. `.splat` (antimatter15)
is listed input-only in v2.7.1, v3.0.0 and 3.3.0, and the pinned binary's own
`--help` agrees:

    SUPPORTED OUTPUTS
      .ply .compressed.ply .sog .spz meta.json lod-meta.json .glb
      .csv .html .voxel.json .webp null

So every provider that emits PLY from splatfacto — local, cloud, aws_batch,
runpod, oncompute, i.e. all the real ones — failed at the conversion step. Only
StubProvider survived, because `write_demo_splat` writes `.splat` bytes itself
and never invokes the converter. That is why the only splat ever recorded was
the synthetic one.

No test caught it because none of them ever exercised the conversion command.
These do, by asserting the command's shape rather than running the binary.
"""

import asyncio
import shutil
from pathlib import Path

import pytest

from reconstruction_providers import (
    DELIVERY_SUFFIX,
    ProviderError,
    _download_first_available,
    _validate_artifact,
)
import reconstruction_worker
from reconstruction_worker import _convert_to_delivery

#: Verified from `splat-transform --help` at the pinned version. The point of
#: pinning this list here is that it is the tool's capability, not ours: if a
#: future change picks a delivery format outside it, the pipeline silently stops
#: producing output again, exactly as it did before.
SPLAT_TRANSFORM_WRITABLE = {
    ".ply", ".compressed.ply", ".sog", ".spz",
    "meta.json", "lod-meta.json", ".glb", ".csv", ".html", ".voxel.json", ".webp",
}
#: Readable but NOT writable. The whole defect in one line.
SPLAT_TRANSFORM_READ_ONLY = {".splat", ".ksplat", ".lcc", ".lcc2", ".mjs"}


class _FakeProc:
    """Stands in for splat-transform: records nothing, just succeeds and
    creates the output file the caller checks for."""

    returncode = 0

    def __init__(self, out: Path):
        self._out = out

    async def communicate(self):
        self._out.parent.mkdir(parents=True, exist_ok=True)
        self._out.write_bytes(b"SOG\x00fake-compressed-payload")
        return (b"", b"")


def _capture_command(monkeypatch):
    """Run the converter without running the binary; return the argv it built."""
    seen = {}
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/splat-transform")

    async def fake_exec(*cmd, **kwargs):
        seen["cmd"] = list(cmd)
        return _FakeProc(Path(cmd[-1]))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


def test_converter_requests_a_format_splat_transform_can_write(monkeypatch, tmp_path):
    """The regression test. Against the pre-fix code this fails: the output
    path ended in `.splat`, which is read-only, so the real binary always
    errored and every genuine reconstruction died here."""
    seen = _capture_command(monkeypatch)
    src = tmp_path / "model.ply"
    src.write_bytes(b"ply\nformat binary_little_endian 1.0\n")

    out = asyncio.run(_convert_to_delivery(src, tmp_path, "media-1"))

    requested = "".join(Path(seen["cmd"][-1]).suffixes[-1:])
    assert requested in SPLAT_TRANSFORM_WRITABLE, (
        f"pipeline asked splat-transform to write {requested!r}, which it cannot do"
    )
    assert requested not in SPLAT_TRANSFORM_READ_ONLY
    assert out.name == f"media-1{DELIVERY_SUFFIX}"


def test_converter_pins_the_tool_version_when_falling_back_to_npx(monkeypatch, tmp_path):
    """An unpinned `npx -y` resolves to whatever is latest at run time, which is
    how format support changed underneath this pipeline with no diff to review."""
    seen = {}
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "splat-transform" else "/usr/bin/npx")

    async def fake_exec(*cmd, **kwargs):
        seen["cmd"] = list(cmd)
        return _FakeProc(Path(cmd[-1]))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    src = tmp_path / "model.ply"
    src.write_bytes(b"ply\n")

    asyncio.run(_convert_to_delivery(src, tmp_path, "media-2"))

    pkg = next(a for a in seen["cmd"] if a.startswith("@playcanvas/splat-transform"))
    assert "@" in pkg.removeprefix("@playcanvas/"), f"{pkg} is unpinned"


@pytest.mark.parametrize("name", [f"already{DELIVERY_SUFFIX}", "legacy.splat"])
def test_deliverable_formats_pass_through_untouched(monkeypatch, tmp_path, name):
    """`.sog` is current and `.splat` is legacy-but-renderable. Neither needs a
    conversion pass, and converting `.splat` would make the stub path — which
    runs on hosts with no Node at all — depend on Node for nothing."""
    def explode(*a, **k):  # noqa: ANN002
        raise AssertionError("a deliverable artifact must not be re-converted")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", explode)
    src = tmp_path / name
    src.write_bytes(b"\x00" * 32)

    assert asyncio.run(_convert_to_delivery(src, tmp_path, "media-3")) == src


def test_unknown_format_is_refused_by_name(monkeypatch, tmp_path):
    """Naming the format matters: an SPZ v4 file hitting a splat-transform too
    old to read it fails at conversion, and a bare 'conversion failed' sends the
    reader to the GPU job instead of the version pin."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/splat-transform")
    src = tmp_path / "capture.obj"
    src.write_bytes(b"v 0 0 0\n")

    with pytest.raises(ProviderError) as exc:
        asyncio.run(_convert_to_delivery(src, tmp_path, "media-4"))
    assert "capture.obj" in str(exc.value)


def test_row_size_invariant_applies_to_splat_but_never_to_sog(tmp_path):
    """`.splat` is a headerless array of fixed 32-byte records, so a size that
    is not a multiple of 32 proves truncation. `.sog` is a compressed container
    with no such invariant — applying the check there would reject 31 of every
    32 valid files, turning a working reconstruction into 'invalid artifact'."""
    sog = tmp_path / f"model{DELIVERY_SUFFIX}"
    sog.write_bytes(b"\x00" * 33)          # deliberately not a multiple of 32
    _validate_artifact(sog, provider="test")   # must not raise

    truncated = tmp_path / "model.splat"
    truncated.write_bytes(b"\x00" * 33)
    with pytest.raises(ProviderError, match="truncated"):
        _validate_artifact(truncated, provider="test")

    empty = tmp_path / f"empty{DELIVERY_SUFFIX}"
    empty.write_bytes(b"")
    with pytest.raises(ProviderError, match="empty"):
        _validate_artifact(empty, provider="test")


def test_download_prefers_sog_but_still_accepts_an_older_image(tmp_path):
    """A backend deploy must not require rebuilding every worker image the same
    day, so an image still writing model.splat keeps working."""
    class _OnlyLegacy:
        def download_file(self, bucket, key, dest):
            if not key.endswith(".splat"):
                raise FileNotFoundError(key)
            Path(dest).write_bytes(b"\x00" * 64)

    out = _download_first_available(
        _OnlyLegacy(), "bucket", "recon-outputs/job/model.splat", tmp_path, provider="test"
    )
    assert out.name == "model.splat"

    class _Current:
        def download_file(self, bucket, key, dest):
            if not key.endswith(DELIVERY_SUFFIX):
                raise FileNotFoundError(key)
            Path(dest).write_bytes(b"SOG\x00")

    out = _download_first_available(
        _Current(), "bucket", "recon-outputs/job/model.splat", tmp_path, provider="test"
    )
    assert out.name == f"model{DELIVERY_SUFFIX}"


def test_container_pipeline_does_not_ask_for_an_unwritable_format():
    """The same defect lived in the image, not just the backend: run.sh ran
    `splat-transform "$PLY" model.splat`. The backend fix alone would leave every
    containerised provider broken."""
    run_sh = Path(__file__).resolve().parents[2] / "infra" / "reconstruction" / "run.sh"
    body = run_sh.read_text()
    convert = [l for l in body.splitlines() if l.strip().startswith("splat-transform ")]
    assert convert, "run.sh no longer converts — did the pipeline move?"
    for line in convert:
        target = line.split()[-1].strip('"')
        assert not target.endswith(".splat"), f"run.sh asks for an unwritable format: {line}"
