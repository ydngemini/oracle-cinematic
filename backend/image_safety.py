"""Process-wide ceilings on how large an image this service will decode.

Uploads are checked for size in BYTES, but a decoder allocates by PIXELS: a
140 KB PNG of 12000x12000 decoded to 411 MiB in OpenCV (865 MiB peak RSS), and
Pillow's default only warns until twice its 89 MP limit (security review DOS-1,
2026-10-01). Floor-plan extraction, video stills and frame selection all decode
untrusted images, some from the public upload link.

Imported first thing by server.py, so it applies to the web and worker roles
before any decoder is touched. 50 MP is ~8,660 x 5,770 — larger than any
phone or DSLR still a property capture produces.
"""
from __future__ import annotations

import os
import warnings

MAX_IMAGE_PIXELS = int(os.getenv("ORACLE_MAX_IMAGE_PIXELS", str(50_000_000)))

# OpenCV reads this when its image codecs are first used; set it before then.
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", str(MAX_IMAGE_PIXELS))

try:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    # Pillow only WARNS between 1x and 2x the limit; a bomb in that band would
    # still be decoded. Make the warning the refusal.
    warnings.simplefilter("error", Image.DecompressionBombWarning)
except ImportError:  # pragma: no cover — Pillow is a hard dependency in production
    pass
