#!/usr/bin/env python3
"""Run the spatial Playwright matrix against Next's prerendered build."""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def wait_for_server(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"static server did not bind port {port} within {timeout:g}s")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3021)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    output = Path(args.output_dir or tempfile.mkdtemp(prefix="oracle-spatial-"))

    server = subprocess.Popen(
        [sys.executable, "-u", "scripts/serve-next-static.py", "--port", str(args.port)],
        cwd=ROOT,
    )
    try:
        wait_for_server(args.port)
        for viewport in ("desktop", "mobile"):
            for scene in ("home", "tour"):
                subprocess.run(
                    [
                        sys.executable,
                        "-u",
                        "scripts/test-spatial-playwright.py",
                        "--base-url",
                        f"http://127.0.0.1:{args.port}",
                        "--viewport",
                        viewport,
                        "--scene",
                        scene,
                        "--output-dir",
                        str(output),
                    ],
                    cwd=ROOT,
                    check=True,
                )
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()

    print(f"Spatial Playwright matrix passed; pixels: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
