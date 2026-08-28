"""Every tracked file must be readable by a group/other user.

The backend container runs as a non-root user against a bind-mounted repo. On
2026-08-28 a batch of edits was written with `tempfile.mkstemp` + `os.replace`
— an atomic-write pattern adopted after a full disk truncated a source file
mid-write — and mkstemp creates with mode 0600. Every file touched that way
became invisible to the container user, and the backend crash-looped on
`PermissionError: [Errno 13] Permission denied: '/app/config.py'`.

Nothing in the test suite noticed, because pytest runs as the owner.
"""

from __future__ import annotations

import pathlib
import stat
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_no_tracked_file_is_owner_only():
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout

    unreadable: list[str] = []
    for name in listing.split("\0"):
        if not name:
            continue
        path = REPO / name
        if not path.is_file():  # submodules, deleted-but-staged
            continue
        mode = path.stat().st_mode
        if not (mode & stat.S_IRGRP and mode & stat.S_IROTH):
            unreadable.append(f"{name} ({stat.filemode(mode)})")

    assert unreadable == [], (
        "files unreadable outside their owner — a container running as another "
        "user cannot import these:\n  " + "\n  ".join(unreadable)
    )
