#!/usr/bin/env python3
"""Serve Next's prerendered app output without starting the Next runtime.

This is intentionally a test-only server. It maps the prerendered HTML under
`.next/server/app` and hashed browser assets under `.next/static`, allowing
Playwright smoke tests to run when the production Node server is unavailable.
"""

from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / ".next" / "server" / "app").resolve()
STATIC = (ROOT / ".next" / "static").resolve()


class NextStaticHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        request_path = unquote(urlsplit(path).path)
        if request_path.startswith("/_next/static/"):
            relative = request_path.removeprefix("/_next/static/")
            candidate = (STATIC / relative).resolve()
            base = STATIC
        else:
            routes = {
                "/": APP / "index.html",
                "/tour": APP / "tour.html",
                "/tour/": APP / "tour.html",
                "/tour/generate": APP / "tour" / "generate.html",
                "/tour/generate/": APP / "tour" / "generate.html",
            }
            candidate = routes.get(request_path, APP / "_not-found.html").resolve()
            base = APP

        if candidate != base and base not in candidate.parents:
            return str(APP / "_not-found.html")
        return str(candidate)

    def log_message(self, format: str, *args: object) -> None:
        print(f"static-next: {format % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=3000, type=int)
    args = parser.parse_args()

    for required in (APP / "index.html", APP / "tour.html", STATIC):
        if not required.exists():
            raise SystemExit(f"Missing {required}; run `npm run build` first.")

    server = ThreadingHTTPServer((args.host, args.port), NextStaticHandler)
    print(f"Serving prerendered Next app at http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
