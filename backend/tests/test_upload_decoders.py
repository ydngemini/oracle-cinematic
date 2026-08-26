"""The two places a dependency parses bytes a user supplied.

Both were untested when Pillow was found carrying 22 known CVEs and pypdf 2.
That combination is the reason this file exists: the decoders on the upload path
were the CVE-exposed surface *and* the surface with no coverage, so a version
bump had nothing to prove itself against.

These are contract tests, not vulnerability tests. They pin the behaviour each
decoder is relied on for — what it accepts, what it refuses, and that it refuses
by raising the documented 422 rather than by crashing or, worse, by letting a
malformed file through as valid. A future upgrade that changes any of those
answers should fail here rather than in production.
"""

from __future__ import annotations

import io

import pytest
from fastapi import HTTPException

Image = pytest.importorskip("PIL.Image")

import ai_chat_api
import property_view_api


def _jpeg(width: int, height: int) -> bytes:
    """A real encoded JPEG, not a stub — the point is to exercise the decoder."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(90, 110, 130)).save(buffer, format="JPEG")
    return buffer.getvalue()


# ── _require_equirectangular ────────────────────────────────────────────────
# A 360° scene is wrapped onto a sphere by the viewer. A flat photo accepted as
# one renders as a smeared room rather than an error, so this check exists to
# fail loudly at upload instead of degrading silently at render time.


@pytest.mark.parametrize("size", [(1024, 512), (4096, 2048), (5760, 2880)])
def test_equirectangular_photos_are_accepted(size):
    """Exactly 2:1 is the projection's defining ratio at any resolution."""
    property_view_api._require_equirectangular(_jpeg(*size), "pano.jpg")


def test_a_slightly_cropped_rig_export_is_still_accepted():
    """Real rigs drift a little; the tolerance is deliberate, so hold it."""
    within = int(1024 / (property_view_api._EQUIRECT_RATIO
                         - property_view_api._EQUIRECT_TOLERANCE / 2))
    property_view_api._require_equirectangular(_jpeg(1024, within), "drifted.jpg")


def test_a_flat_photo_labelled_360_is_refused():
    with pytest.raises(HTTPException) as caught:
        property_view_api._require_equirectangular(_jpeg(1600, 1200), "kitchen.jpg")
    assert caught.value.status_code == 422
    # The message must name the file and the measured shape: an agent who mislabels
    # an upload needs to know which one and why, not that "an error occurred".
    assert "kitchen.jpg" in caught.value.detail
    assert "1600" in caught.value.detail and "1200" in caught.value.detail


def test_a_panorama_that_is_too_wide_is_also_refused():
    """Wider than 2:1 is not equirectangular either — the check is two-sided."""
    with pytest.raises(HTTPException) as caught:
        property_view_api._require_equirectangular(_jpeg(3000, 500), "strip.jpg")
    assert caught.value.status_code == 422


def test_bytes_that_are_not_an_image_are_refused_not_crashed():
    """The decoder is the CVE surface; undecodable input must land on 422."""
    with pytest.raises(HTTPException) as caught:
        property_view_api._require_equirectangular(b"\xff\xd8\xff" + b"garbage" * 40,
                                                   "truncated.jpg")
    assert caught.value.status_code == 422
    assert "could not be read as an image" in caught.value.detail


def test_an_empty_upload_is_refused_not_crashed():
    with pytest.raises(HTTPException) as caught:
        property_view_api._require_equirectangular(b"", "empty.jpg")
    assert caught.value.status_code == 422


# ── _extract_pdf_text_sync ──────────────────────────────────────────────────
# Chat attachments are read for text so the assistant can reason over them. The
# extraction is best-effort by design: a malformed PDF must degrade to None, not
# raise, because the attachment itself is still stored and inspectable.


def test_text_is_extracted_from_a_pdf_this_repo_wrote(tmp_path):
    """Round-trip through our own stdlib PDF writer — the realistic case.

    `write_contract_pdf` is hand-rolled on the standard library, so this pins
    both halves at once: that our writer emits something a reader accepts, and
    that the reader still finds the words in it.
    """
    synthetic_lawyer = pytest.importorskip("ml_forge.synthetic_lawyer")

    target = tmp_path / "assignment.pdf"
    synthetic_lawyer.write_contract_pdf(
        "ASSIGNMENT OF CONTRACT\n\nThe assignor conveys all rights for "
        "the sum of one hundred thousand dollars.",
        target,
    )

    extracted = ai_chat_api._extract_pdf_text_sync(target.read_bytes())

    assert extracted is not None
    assert "ASSIGNMENT" in extracted
    assert "assignor" in extracted


def test_a_pdf_shaped_file_that_is_not_a_pdf_returns_none():
    """Magic bytes are not a parse. This must degrade, never raise."""
    assert ai_chat_api._extract_pdf_text_sync(b"%PDF-1.4\nnot really a pdf") is None


def test_arbitrary_bytes_return_none():
    assert ai_chat_api._extract_pdf_text_sync(b"\x00\x01\x02\x03" * 64) is None


def test_empty_bytes_return_none():
    assert ai_chat_api._extract_pdf_text_sync(b"") is None


def test_extraction_is_bounded(monkeypatch):
    """The 300k cap is what keeps one attachment from filling a prompt window."""

    class _Page:
        @staticmethod
        def extract_text():
            return "x" * 10_000

    class _Reader:
        def __init__(self, *_args, **_kwargs):
            self.pages = [_Page()] * 200

    import pypdf

    monkeypatch.setattr(pypdf, "PdfReader", _Reader)
    extracted = ai_chat_api._extract_pdf_text_sync(b"%PDF-1.4")

    assert extracted is not None
    assert len(extracted) == 300_000
