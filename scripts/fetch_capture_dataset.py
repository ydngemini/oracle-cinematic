#!/usr/bin/env python3
"""Pull ONE scene's images out of a huge remote zip, without downloading it.

    python3 scripts/fetch_capture_dataset.py --scene room --res 4 --out ~/neoh-capture

Why this exists: proving the reconstruction pipeline needs a real multi-view
capture of a real interior. The obvious source is the mip-NeRF 360 dataset,
whose indoor scenes (room, kitchen, counter, bonsai) are genuine photographs of
genuine rooms — but the archive is 12.5 GB and this machine has ~5 GB free.

A zip's central directory lives at the END of the file and lists every member
with its byte offset, so two range requests find the index and one more fetches
each file we actually want. We move about 60 MB instead of 12.5 GB.

This is NOT a synthetic scene and NOT a substitute for capturing a property. It
is real photons off real walls, used to prove the pipeline works before anyone
spends an afternoon photographing a house — so that when a real capture fails,
we already know the pipeline itself is sound.
"""
from __future__ import annotations

import argparse
import io
import struct
import sys
import urllib.request
import zipfile
from pathlib import Path

URL = "https://storage.googleapis.com/gresearch/refraw360/360_v2.zip"

# The indoor scenes. Outdoor ones exist in the same archive but a house tour is
# an interior problem: enclosed geometry, short baselines, windows blowing out.
INDOOR = ("room", "kitchen", "counter", "bonsai")


def _get(url: str, start: int, end: int) -> bytes:
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _size(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return int(resp.headers["Content-Length"])


def central_directory(url: str, total: int) -> zipfile.ZipFile:
    """Read the zip index with two range requests.

    Zip64 is required here: the archive is over 4 GB, so the classic
    end-of-central-directory record cannot hold the real offsets and the
    locator has to be followed to the zip64 record.
    """
    tail = _get(url, max(0, total - 200_000), total - 1)
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise SystemExit("no end-of-central-directory found")

    locator = tail.rfind(b"PK\x06\x07")
    if locator >= 0:
        zip64_eocd_offset = struct.unpack("<Q", tail[locator + 8:locator + 16])[0]
        rec = _get(url, zip64_eocd_offset, zip64_eocd_offset + 55)
        cd_size, cd_offset = struct.unpack("<QQ", rec[40:56])
    else:
        cd_size, cd_offset = struct.unpack("<II", tail[eocd + 12:eocd + 20])

    print(f"  central directory: {cd_size / 1e6:.1f} MB at offset {cd_offset}")
    cd = _get(url, cd_offset, cd_offset + cd_size + 100_000)
    # Rebuild a minimal zip the stdlib can parse: the directory plus a synthetic
    # EOCD pointing at it.
    buf = io.BytesIO()
    buf.write(b"\x00" * cd_offset if cd_offset < 1 else b"")
    return _index_from(cd, cd_offset)


def _index_from(cd: bytes, cd_offset: int) -> list[dict]:
    """Parse central-directory records directly — simpler than faking a zip."""
    entries: list[dict] = []
    i = 0
    while True:
        i = cd.find(b"PK\x01\x02", i)
        if i < 0:
            break
        (compress_type,) = struct.unpack("<H", cd[i + 10:i + 12])
        csize, usize = struct.unpack("<II", cd[i + 20:i + 28])
        name_len, extra_len, comment_len = struct.unpack("<HHH", cd[i + 28:i + 34])
        (offset,) = struct.unpack("<I", cd[i + 42:i + 46])
        name = cd[i + 46:i + 46 + name_len].decode("utf-8", "replace")
        extra = cd[i + 46 + name_len:i + 46 + name_len + extra_len]

        # Zip64 extra field carries the real sizes/offset when the 32-bit
        # fields are saturated with 0xFFFFFFFF.
        if 0xFFFFFFFF in (csize, usize, offset):
            j = 0
            while j + 4 <= len(extra):
                tag, size = struct.unpack("<HH", extra[j:j + 4])
                if tag == 0x0001:
                    vals = struct.unpack(f"<{size // 8}Q", extra[j + 4:j + 4 + (size // 8) * 8])
                    it = iter(vals)
                    if usize == 0xFFFFFFFF:
                        usize = next(it)
                    if csize == 0xFFFFFFFF:
                        csize = next(it)
                    if offset == 0xFFFFFFFF:
                        offset = next(it)
                    break
                j += 4 + size
        entries.append({
            "name": name, "offset": offset, "csize": csize,
            "usize": usize, "compress_type": compress_type,
        })
        i += 46 + name_len + extra_len + comment_len
    return entries


def fetch_member(url: str, entry: dict) -> bytes:
    """One range request per file, plus its local header."""
    head = _get(url, entry["offset"], entry["offset"] + 29)
    name_len, extra_len = struct.unpack("<HH", head[26:30])
    start = entry["offset"] + 30 + name_len + extra_len
    raw = _get(url, start, start + entry["csize"] - 1)
    if entry["compress_type"] == zipfile.ZIP_STORED:
        return raw
    import zlib
    return zlib.decompress(raw, -15)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scene", default="room", choices=INDOOR)
    ap.add_argument("--res", default="4", choices=["1", "2", "4", "8"],
                    help="1 = full res (large), 4 = quarter (recommended)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=280,
                    help="stay under the provider's 300-image cap")
    ap.add_argument("--url", default=URL)
    args = ap.parse_args()

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    print(f"\nindexing {args.url}")
    total = _size(args.url)
    print(f"  archive: {total / 1e9:.1f} GB (not downloading it)")
    entries = central_directory(args.url, total)
    print(f"  members: {len(entries)}")

    prefix = f"{args.scene}/images" + ("" if args.res == "1" else f"_{args.res}") + "/"
    wanted = sorted(
        (e for e in entries
         if e["name"].startswith(prefix) and e["name"].lower().endswith((".jpg", ".jpeg", ".png"))),
        key=lambda e: e["name"],
    )
    if not wanted:
        sample = [e["name"] for e in entries[:8]]
        raise SystemExit(f"no images under {prefix!r}. First members: {sample}")

    print(f"  {prefix}: {len(wanted)} images")
    if len(wanted) > args.limit:
        # Take a CONTIGUOUS run, not every Nth: the images are a walk around
        # the room, and skipping frames halves the overlap the solver needs.
        wanted = wanted[:args.limit]
        print(f"  taking the first {args.limit} (contiguous — skipping frames "
              f"would halve the overlap)")

    got = 0
    for entry in wanted:
        dest = out / Path(entry["name"]).name
        if dest.exists() and dest.stat().st_size == entry["usize"]:
            got += 1
            continue
        dest.write_bytes(fetch_member(args.url, entry))
        got += 1
        if got % 25 == 0 or got == len(wanted):
            print(f"    {got}/{len(wanted)}")

    size_mb = sum(p.stat().st_size for p in out.iterdir()) / 1e6
    print(f"\n{got} images in {out}  ({size_mb:.0f} MB)")
    print("This is a real photographic capture of a real interior — not synthetic,")
    print("and not a property in your CRM. It proves the pipeline, nothing more.\n")


if __name__ == "__main__":
    main()
