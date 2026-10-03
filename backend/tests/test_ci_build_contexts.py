"""Every image build in CI and the deploy docs uses the Dockerfile's own directory
as its context.

ci.yml built the backend with `-f backend/Dockerfile .` — the repository root.
backend/Dockerfile COPYs requirements.txt from its context, and the root has
none, so the first release run would have failed at build. Had a root
requirements.txt existed, `COPY . .` would have copied the whole monorepo into
/app, because only backend/.dockerignore exists and it applies only when
backend/ is the context. docker-compose.yml always used ./backend. No Docker is
needed to check this: it is a property of the command line.
"""

from __future__ import annotations

import pathlib
import re
import shlex

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent

FILES = [REPO / ".github" / "workflows" / "ci.yml",
         REPO / "docs" / "deploy-digitalocean.md",
         REPO / "docs" / "staging-setup.md"]


def _docker_builds(text: str) -> list[str]:
    """Each `docker build …` command, with backslash continuations joined."""
    joined = re.sub(r"\\\n\s*", " ", text)
    return [m.group(0) for m in re.finditer(r"docker build\b[^\n]*", joined)
            if " -f " in m.group(0)]


def _file_and_context(cmd: str) -> tuple[str, str]:
    cmd = cmd.split("#", 1)[0]
    words = shlex.split(cmd.replace("${", "").replace("}", ""), posix=True)
    dockerfile = words[words.index("-f") + 1]
    # the context is the last positional word
    return dockerfile, words[-1]


CASES = [(f.name, cmd) for f in FILES if f.exists() for cmd in _docker_builds(f.read_text(encoding="utf-8"))]


def test_there_are_builds_to_check():
    names = {n for n, _ in CASES}
    assert "ci.yml" in names


@pytest.mark.parametrize("where,cmd", CASES)
def test_build_context_is_the_dockerfiles_directory(where, cmd):
    dockerfile, context = _file_and_context(cmd)
    want = str(pathlib.PurePosixPath(dockerfile).parent)
    assert context.rstrip("/") in (want, "./" + want), (
        f"{where}: `{cmd.strip()}` builds {dockerfile} with context {context!r}; "
        f"use {want!r} (its .dockerignore and COPY paths are relative to it)"
    )
