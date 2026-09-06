"""A blurry frame is worse than no frame, so selection must see focus.

Nobody photographs their own house 350 times; a walkthrough video is the real
capture. These tests build sharp and blurred frames and check the selector
keeps the sharp ones WITHOUT collapsing coverage onto whichever wall happened
to be well lit.
"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageFilter

import frame_selection
from frame_selection import select_sharpest, sharpness

RNG = np.random.default_rng(11)


def _write(path, blur=0.0):
    """A frame of random texture, optionally defocused."""
    arr = RNG.integers(0, 255, size=(160, 240), dtype=np.uint8)
    im = Image.fromarray(arr, mode="L")
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    im.save(path, format="JPEG", quality=95)
    return path


def test_sharpness_ranks_focus_not_brightness(tmp_path):
    sharp = _write(tmp_path / "sharp.jpg")
    blurred = _write(tmp_path / "blurred.jpg", blur=3.0)
    assert sharpness(sharp) > sharpness(blurred) * 2

    # A flat grey frame has no edges at all: the floor of the measure.
    flat = tmp_path / "flat.jpg"
    Image.new("L", (240, 160), color=128).save(flat, format="JPEG")
    assert sharpness(flat) < sharpness(blurred)


def test_an_unreadable_frame_scores_none_not_zero(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not an image")
    # None, so it is never ranked as "very blurry" and silently dropped in
    # favour of something genuinely worse.
    assert sharpness(broken) is None
    assert sharpness(tmp_path / "absent.jpg") is None


def test_it_keeps_the_sharp_frame_out_of_each_blurry_run(tmp_path):
    # Every third frame is sharp; the rest are motion-blurred.
    frames = []
    for i in range(12):
        frames.append(_write(tmp_path / f"f{i:02d}.jpg", blur=0.0 if i % 3 == 0 else 4.0))
    chosen = select_sharpest(frames, 4)
    assert len(chosen) == 4
    assert all(int(p.stem[1:]) % 3 == 0 for p in chosen), [p.name for p in chosen]


def test_coverage_is_preserved_rather_than_chasing_the_sharpest_wall(tmp_path):
    """The globally sharpest N would return one corner of the house."""
    frames = []
    for i in range(20):
        # The first five are much sharper than everything after them.
        frames.append(_write(tmp_path / f"f{i:02d}.jpg", blur=0.0 if i < 5 else 2.5))
    chosen = select_sharpest(frames, 4)
    picked = [int(p.stem[1:]) for p in chosen]
    assert len(chosen) == 4
    # One per contiguous run: spread across the sequence, not clustered at the start.
    assert max(picked) >= 12, picked
    assert picked == sorted(picked), "order must survive; sequential matching depends on it"


def test_it_returns_everything_when_the_capture_already_fits(tmp_path):
    frames = [_write(tmp_path / f"f{i}.jpg") for i in range(3)]
    assert select_sharpest(frames, 10) == frames
    assert select_sharpest(frames, 3) == frames


def test_it_falls_back_to_even_spacing_when_nothing_can_be_scored(tmp_path, monkeypatch):
    frames = [_write(tmp_path / f"f{i:02d}.jpg") for i in range(10)]
    monkeypatch.setattr(frame_selection, "sharpness", lambda _p: None)
    chosen = select_sharpest(frames, 5)
    # Degraded to the old behaviour, not broken: still five, still in order.
    assert len(chosen) == 5
    assert chosen == sorted(chosen)


def test_a_bucket_of_only_unreadable_frames_still_contributes_one(tmp_path):
    frames = []
    for i in range(6):
        p = tmp_path / f"f{i}.jpg"
        if i < 3:
            p.write_bytes(b"broken")
        else:
            _write(p)
        frames.append(p)
    chosen = select_sharpest(frames, 2)
    assert len(chosen) == 2


@pytest.mark.parametrize("target", [1, 2, 7, 11])
def test_it_always_returns_exactly_what_was_asked_for(tmp_path, target):
    frames = [_write(tmp_path / f"f{i:02d}.jpg", blur=(i % 4)) for i in range(11)]
    chosen = select_sharpest(frames, target)
    assert len(chosen) == min(target, len(frames))
    assert len(set(chosen)) == len(chosen), "no frame may be selected twice"
