"""Disk, inode and memory floors on EVERY filesystem a cycle touches.

SYPHER_CORE2 holds the worktrees, state and archive; the root filesystem
holds the host /tmp, journald and home; the sandbox /tmp is tmpfs (RAM).
Any floor breached means: do not start, do not archive, report why.
"""
from __future__ import annotations

import os
from pathlib import Path

GIB = 1024 ** 3


def mem_available_bytes(meminfo: str | None = None) -> int:
    text = meminfo if meminfo is not None else Path("/proc/meminfo").read_text()
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return 0


def check_floors(floors: dict, core2: str, root: str = "/", *, statvfs=os.statvfs,
                 meminfo: str | None = None, extra_core2_gib: float = 0.0) -> tuple[bool, list[str]]:
    problems: list[str] = []
    for label, path, free_floor in (("SYPHER_CORE2", core2, floors["core2_free_gib"] + extra_core2_gib),
                                    ("root", root, floors["root_free_gib"])):
        try:
            stats = statvfs(path)
        except OSError as error:
            problems.append(f"{label}: cannot stat {path} ({type(error).__name__})")
            continue
        free = stats.f_bavail * stats.f_frsize
        if free < free_floor * GIB:
            problems.append(f"{label}: {free / GIB:.2f} GiB free < {free_floor:.2f} GiB floor")
        if stats.f_files:
            ratio = stats.f_favail / stats.f_files
            if ratio < floors["inode_free_ratio"]:
                problems.append(f"{label}: {ratio:.1%} inodes free < {floors['inode_free_ratio']:.0%}")
    try:
        available = mem_available_bytes(meminfo)
    except OSError:
        available = 0
    if available < floors["mem_available_gib"] * GIB:
        problems.append(f"memory: {available / GIB:.2f} GiB available < {floors['mem_available_gib']:.2f} GiB "
                        "(sandbox /tmp is RAM-backed)")
    return not problems, problems
