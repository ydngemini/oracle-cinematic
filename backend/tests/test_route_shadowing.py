"""A parameterised route must never be declared before a literal it would swallow.

Starlette matches in declaration order, first match wins. So `/api/commands/{id}`
declared before `/api/commands/approvals` makes the literal route dead — every
request for it is handed to the parameterised handler with `id="approvals"`, which
then 404s or 422s on a lookup that was never going to succeed.

Nothing about this is visible at import time and nothing fails loudly. The eight
pairs in this repo are currently ordered correctly; that is a property of the
order somebody happened to write the decorators in, not of anything enforcing it.
Moving a route, or adding a literal below an existing wildcard, silently breaks it.

This test is that enforcement.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import *  # noqa: F401,F403 — env bootstrap must run before `server`


_PARAM_RE = re.compile(r"\{[^}]+\}")


def _routes():
    import server

    out = []
    for index, route in enumerate(server.app.routes):
        path = getattr(route, "path", None)
        if not path:
            continue
        methods = frozenset(getattr(route, "methods", None) or {"WEBSOCKET"})
        out.append((index, path, methods))
    return out


def _to_regex(template: str) -> re.Pattern:
    """A path template as a matcher, with `{param}` standing for one segment.

    Starlette's converters allow `{p:path}` to span separators; treat those as
    greedy so the shadowing check stays conservative (it would rather flag a
    pair than miss one).
    """
    parts = []
    for chunk in re.split(r"(\{[^}]+\})", template):
        if chunk.startswith("{") and chunk.endswith("}"):
            parts.append(".+" if ":path" in chunk else "[^/]+")
        else:
            parts.append(re.escape(chunk))
    return re.compile("^" + "".join(parts) + "$")


def test_no_parameterised_route_shadows_a_literal_declared_after_it():
    routes = _routes()
    literals = [(i, p, m) for i, p, m in routes if not _PARAM_RE.search(p)]
    templated = [(i, p, m) for i, p, m in routes if _PARAM_RE.search(p)]

    shadowed = []
    for t_index, t_path, t_methods in templated:
        matcher = _to_regex(t_path)
        for l_index, l_path, l_methods in literals:
            if l_index < t_index:
                continue  # literal wins on declaration order — correct
            if not (t_methods & l_methods):
                continue  # different verbs never collide
            if matcher.fullmatch(l_path):
                shadowed.append(
                    f"{sorted(t_methods & l_methods)} {t_path} (declared #{t_index}) "
                    f"swallows {l_path} (declared #{l_index})"
                )

    assert not shadowed, (
        "Parameterised route(s) declared before a literal they match — the literal "
        "is unreachable. Move the literal's declaration above the wildcard:\n  "
        + "\n  ".join(shadowed)
    )


def test_the_shadowing_detector_actually_detects():
    """Guard the guard: a deliberately-inverted pair must be caught.

    Without this, a bug in `_to_regex` would make the test above vacuously pass
    and the protection would be imaginary.
    """
    matcher = _to_regex("/api/commands/{command_id}")
    assert matcher.fullmatch("/api/commands/approvals")
    assert not matcher.fullmatch("/api/commands/a/b")
    assert _to_regex("/files/{rest:path}").fullmatch("/files/a/b/c")
