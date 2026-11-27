"""A finished cycle is never lost to the supervisor's own bookkeeping
(2026-11-16: NQ-09 and NQ-17 both passed every gate, then vanished).

- NQ-09: main moved during the build. The classifier read the AGENT's prose
  ("rejected it as a protected path") and escalated a scope violation.
- NQ-17: the push failed because the one-hour GitHub token had expired.
- Both: the archiver diffed against HEAD, which already held the commit, saw
  "no changes" and removed the worktree, leaving the commit unreferenced.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import failures  # noqa: E402
import queue_supervisor  # noqa: E402


def test_agent_prose_cannot_turn_a_stop_into_a_scope_violation():
    log = ("I could not write plan.json; the hook rejected it as a protected path.\n"
           "[stop] Neoh main was updated during the build. Stop before pushing.. No main push or deploy.\n")
    assert failures.classify(2, log) == "conflict"
    # The supervisor's own scope stop still counts.
    assert failures.classify(2, "[stop] STOP: unapproved changed path: x. No push") == "scope_violation"


def test_a_main_moved_conflict_retries_instead_of_escalating():
    sig = failures.signature("conflict", "")
    assert failures.decide("conflict", sig, [sig, sig], 0, 2, 2, conflict_attempts=0) == ("retry", 120)
    assert failures.decide("conflict", sig, [sig], 0, 2, 2, conflict_attempts=1) == ("retry", 120)
    assert failures.decide("conflict", sig, [sig], 0, 2, 2, conflict_attempts=2) == ("escalate", 0)


def test_an_expired_token_push_is_infrastructure():
    log = ("[stop] Side-branch push failed (exit 128): fatal: Authentication failed for "
           "'https://github.com/o/r.git/'. No main push or deploy.")
    assert failures.classify(2, log) == "infrastructure"


def test_the_supervisor_asks_for_a_fresh_token_before_it_ages_out(monkeypatch):
    monkeypatch.delenv("NEOH_TOKEN_MINTED_AT", raising=False)
    assert not queue_supervisor.token_needs_refresh(now=10 ** 10)   # no minted token: nothing to refresh
    monkeypatch.setenv("NEOH_TOKEN_MINTED_AT", "1000")
    monkeypatch.setenv("NEOH_TOKEN_REFRESH_SECONDS", "900")
    assert not queue_supervisor.token_needs_refresh(now=1800)
    assert queue_supervisor.token_needs_refresh(now=1901)


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *argv], check=True, capture_output=True,
                          text=True).stdout.strip()


def test_committed_unpushed_work_is_archived_and_pinned(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("x = 1\n")
    _git(root, "add", "a.py")
    _git(root, "commit", "-qm", "base")
    state = root / ".neoh-agent-state"
    worktree = state / "worktrees" / "cycle-1"
    _git(root, "worktree", "add", "-q", "-b", "supervisor/neoh-cycle-1", str(worktree))
    # autobuild commits the verified change beyond the cycle branch (detached).
    _git(worktree, "checkout", "-q", "--detach")
    (worktree / "a.py").write_text("x = 2\n")
    _git(worktree, "commit", "-qam", "noah fix")
    commit = _git(worktree, "rev-parse", "HEAD")
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr(queue_supervisor, "ROOT", root)
    monkeypatch.setattr(queue_supervisor, "STATE_DIR", state)
    monkeypatch.setattr(queue_supervisor, "ARCHIVE", archive)

    result = queue_supervisor.archive_and_remove(worktree, "cycle-1", {}, 0,
                                                 start="supervisor/neoh-cycle-1")

    assert "no changes" not in result
    patch = (archive / "cycle-1" / "cycle-1.patch").read_text()
    assert "+x = 2" in patch
    assert "apply_check_ok" in (archive / "INDEX.tsv").read_text()
    assert _git(root, "rev-parse", "refs/neoh-salvage/cycle-1") == commit
