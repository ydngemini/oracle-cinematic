"""Every API path the frontend calls must resolve to a route the backend mounts.

The failure this exists to catch is silent on both sides. `VideoStudioPanel.jsx`
calls eight `/api/video-studio/*` routes behind `ORACLE_FEATURE_VIDEO_STUDIO`,
which defaults to False — so on a default deployment the panel renders, fires
eight requests, and gets eight 404s. Nothing in the backend knows the frontend
is calling it; nothing in the frontend knows the router is absent. The same
shape appears whenever a route is renamed or deleted without grepping the app.

Routes are collected from a subprocess booted with **every optional router
mounted**, so a path behind a feature flag counts as existing. Whether a flag is
*on* is a deployment question and an honesty question for the UI; whether the
route exists at all is what this test is for.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
FRONTEND_SRC = BACKEND_DIR.parent / "oracle-app" / "src"

# Prefixes the backend owns. Anything else in a string literal (a CDN, a Google
# Maps endpoint, a relative asset) is not ours to resolve.
API_PREFIXES = ("/api/", "/auth/", "/billing/", "/portal/", "/admin/")

# `${...}` interpolation, and `:param` style, both stand for exactly one segment.
_INTERP_RE = re.compile(r"\$\{[^}]*\}")
_PATH_LITERAL_RE = re.compile(r"""['"`](/(?:api|auth|billing|portal|admin)/[^'"`\s]*)['"`]""")

_WILDCARD = "\x00"  # a segment that matches anything


def _normalise(path: str) -> str | None:
    """Strip the query/hash and reduce every dynamic segment to a wildcard.

    Returns None for a literal that is a *prefix expression* rather than a
    request path — `useProtectedMedia.js:16` tests `pathname.startsWith('/api/media/')`
    to decide whether to attach the JWT. Those end in a separator and name no
    endpoint; treating one as a call would report a route that nothing requests.
    """
    path = path.split("?", 1)[0].split("#", 1)[0]
    if path.endswith("/"):
        return None
    return _INTERP_RE.sub(_WILDCARD, path)


def _frontend_paths() -> dict[str, list[str]]:
    """Every backend path referenced in the frontend, mapped to its call sites."""
    found: dict[str, list[str]] = {}
    for source in sorted(FRONTEND_SRC.rglob("*")):
        if source.suffix not in {".js", ".jsx", ".ts", ".tsx"}:
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for raw in _PATH_LITERAL_RE.findall(text):
            if not raw.startswith(API_PREFIXES):
                continue
            path = _normalise(raw)
            if path is None:
                continue
            found.setdefault(path, []).append(
                str(source.relative_to(FRONTEND_SRC.parent))
            )
    return found


def _mounted_paths() -> list[str]:
    """Route templates from a fresh interpreter with all optional routers on."""
    script = (
        "import json, sys; sys.path.insert(0, %r); import server; "
        "print(json.dumps([r.path for r in server.app.routes if getattr(r, 'path', None)]))"
        % str(BACKEND_DIR)
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(BACKEND_DIR),
        "ORACLE_ENV": "dev",
        "ORACLE_SECRET_KEY": "test-only-secret-key-with-at-least-32-bytes",
        # Mount every conditionally-included router so "does this route exist"
        # is answered independently of any deployment's flag settings.
        "ORACLE_FEATURE_VIDEO_STUDIO": "1",
        "AWS_OBSERVABILITY_ENABLED": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, cwd=str(BACKEND_DIR), timeout=180,
    )
    if result.returncode != 0:
        pytest.fail(f"Could not boot the app to collect routes:\n{result.stderr[-2000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def _segments(template: str) -> list[str]:
    out = []
    for segment in template.strip("/").split("/"):
        out.append(_WILDCARD if ("{" in segment or _WILDCARD in segment) else segment)
    return out


def _resolves(path: str, mounted: list[list[str]]) -> bool:
    wanted = _segments(path)
    for candidate in mounted:
        if len(candidate) != len(wanted):
            continue
        if all(
            a == _WILDCARD or b == _WILDCARD or a == b
            for a, b in zip(wanted, candidate)
        ):
            return True
    return False


@pytest.mark.skipif(not FRONTEND_SRC.is_dir(), reason="frontend sources not present")
def test_every_frontend_api_path_resolves_to_a_mounted_route():
    frontend = _frontend_paths()
    assert frontend, "extracted no API paths — the frontend scanner is broken"

    mounted = [_segments(t) for t in _mounted_paths()]
    unresolved = {
        path: sites for path, sites in sorted(frontend.items())
        if not _resolves(path, mounted)
    }

    assert not unresolved, "Frontend calls path(s) the backend does not mount:\n" + "\n".join(
        f"  {path}\n      called from: {', '.join(sorted(set(sites)))}"
        for path, sites in unresolved.items()
    )


def test_the_resolver_actually_rejects_a_missing_route():
    """Guard the guard — otherwise a broken matcher passes everything."""
    mounted = [_segments("/api/crm/leads/{lead_id}/media")]
    assert _resolves("/api/crm/leads/abc/media", mounted)
    assert not _resolves("/api/crm/leads/abc/video", mounted)
    assert not _resolves("/api/crm/leads/abc", mounted)
