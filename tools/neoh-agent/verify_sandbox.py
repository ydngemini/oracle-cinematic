"""Run agent-influenced commands (tests, builds) in a nested bwrap sandbox.

Agent-written tests are arbitrary code. Before 2026-11-16 they ran with the
supervisor's rights: write access to the supervisor checkout, the budget and
claims journals, the owner-context mount point and GH_TOKEN in the
environment. Here they see:
  writable : the cycle worktree and a private /tmp
  read-only: everything else the outer sandbox shows (venv, git objects, /usr)
  hidden   : .neoh-agent-state (except the worktree), owner-context, /home/agent
  env      : cleared, then a minimal allowlist (no tokens, no API keys)
  network  : none (--unshare-all without --share-net)
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

BWRAP = "/usr/bin/bwrap"
ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "TZ", "NODE_OPTIONS", "PYTHONHASHSEED", "TERM")


def enabled() -> bool:
    return os.environ.get("NEOH_VERIFY_SANDBOX") == "1"


def wrap(argv: list[str], *, worktree: Path, state_dir: Path, cwd: Path,
         hidden: tuple[Path, ...] = (), env: dict | None = None) -> list[str]:
    worktree, state_dir, cwd = Path(worktree).resolve(), Path(state_dir).resolve(), Path(cwd).resolve()
    if not cwd.is_relative_to(worktree):
        raise ValueError("verify commands must run inside the cycle worktree")
    if not worktree.is_relative_to(state_dir):
        raise ValueError("the cycle worktree must live under the supervisor state directory")
    if not Path(BWRAP).exists() or not shutil.which(BWRAP):
        raise RuntimeError("bwrap unavailable: refusing to run agent-written code unsandboxed")
    source = os.environ if env is None else env
    command = [
        BWRAP, "--unshare-all", "--die-with-parent", "--new-session",
        "--ro-bind", "/", "/",
        "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
        "--tmpfs", str(state_dir),           # hides budget, claims, logs, other worktrees
        "--bind", str(worktree), str(worktree),
    ]
    for path in hidden:
        command += ["--tmpfs", str(Path(path).resolve())]
    command += ["--clearenv"]
    for name in ENV_ALLOW:
        if name in source:
            command += ["--setenv", name, source[name]]
    command += ["--setenv", "HOME", "/tmp", "--setenv", "CI", "1",
                "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                "--setenv", "ORACLE_SKIP_DOTENV", "1",
                "--chdir", str(cwd), "--", *argv]
    return command
