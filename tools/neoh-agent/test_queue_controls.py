"""Offline tests for Noah's queue controls (plan v2 §5, scenarios 1-18).

No network, no model, no provider: git is faked where it would be called.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import audit_queue  # noqa: E402
import budget as budget_mod  # noqa: E402
import claims as claims_mod  # noqa: E402
import completion  # noqa: E402
import eligibility  # noqa: E402
import failures  # noqa: E402
import resources  # noqa: E402
import verify_sandbox  # noqa: E402

SHA = "a" * 40
MAIN = "b" * 40


def item(**overrides) -> dict:
    base = {
        "id": "NQ-01", "category": "02", "finding_ids": ["33.4"], "source": "audit chunk 33",
        "verified_sha": SHA, "priority": "P2", "severity": "low", "status": "confirmed_open",
        "root_cause": "stale search responses overwrite newer ones",
        "allowed_paths": ["oracle-app/src/components/SalesAgentPage.jsx",
                          "oracle-app/src/components/SalesAgentPage.test.jsx"],
        "restricted_paths": [], "depends_on": [], "conflicts_with": [], "requires_decisions": [],
        "failing_test": {"file": "oracle-app/src/components/SalesAgentPage.test.jsx",
                         "name": "ignores stale search results", "intent": "older reply never wins"},
        "expected_behaviour": "only the newest query's results are shown",
        "acceptance": ["newest query wins"],
        "verify_commands": [{"kind": "vitest", "args": ["src/components/SalesAgentPage.test.jsx"]}],
        "budget": {"usd": 3.0, "minutes": 30, "max_turns": 24, "disk_gib": 1},
        "retry": {"max": 2}, "escalate_after": 2, "live_provider": False, "spends_money": False,
        "evidence": {"paths": ["oracle-app/src/components/SalesAgentPage.jsx"]},
        "review_class": "p2_claude_ok",
    }
    base.update(overrides)
    return base


def policy(**overrides) -> dict:
    base = {"mode": "canary_single_cycle", "legacy_fallback": False, "per_task_usd": 3.0,
            "daily_usd": 3.0, "weekly_usd": 3.0, "contingency_ratio": 0.2, "contingency_hours": 24,
            "max_concurrent": 1, "max_retained_failed_worktrees": 1, "canary_item": "NQ-01",
            "disk_floors": {"core2_free_gib": 3.0, "root_free_gib": 1.0, "inode_free_ratio": 0.1,
                            "mem_available_gib": 2.0}}
    base.update(overrides)
    return base


def ctx(**overrides) -> eligibility.Context:
    base = dict(main_sha=MAIN, budget_headroom_usd=3.0, disk_ok=True, disk_detail="ok", now=1000.0)
    base.update(overrides)
    return eligibility.Context(**base)


FRESH = (True, "fresh")


def write(tmp_path: Path, name: str, data) -> Path:
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


# 1 + 3 + 11 ---------------------------------------------------------------
def test_1_a_valid_eligible_item_is_chosen():
    chosen, report = eligibility.choose([item()], ctx(), lambda _i: FRESH)
    assert chosen["id"] == "NQ-01" and report["NQ-01"] == []


def test_priority_orders_before_queue_order():
    low, high = item(id="NQ-01"), item(id="NQ-02", priority="P1")
    chosen, _ = eligibility.choose([low, high], ctx(), lambda _i: FRESH)
    assert chosen["id"] == "NQ-02"


def test_3_legacy_fallback_cannot_be_enabled(tmp_path):
    with pytest.raises(audit_queue.QueueInvalid, match="decision 3"):
        audit_queue.load_policy(write(tmp_path, "p.json", policy(legacy_fallback=True)))


def test_11_an_outstanding_decision_blocks_only_its_own_item():
    gated = item(id="NQ-02", requires_decisions=["privacy-review-NQ-02"])
    chosen, report = eligibility.choose([gated, item()], ctx(), lambda _i: FRESH)
    assert chosen["id"] == "NQ-01"
    assert "privacy-review-NQ-02" in report["NQ-02"][0]
    chosen, _ = eligibility.choose([gated], ctx(answered_decisions={"privacy-review-NQ-02"}),
                                   lambda _i: FRESH)
    assert chosen["id"] == "NQ-02"


# 2: fail closed ------------------------------------------------------------
@pytest.mark.parametrize("content, message", [
    (None, "missing"),
    ("{not json", "not valid JSON"),
    ({"version": 2, "items": []}, "version"),
    ({"version": 1, "items": [{"id": "NQ-01"}]}, "missing"),
])
def test_2_bad_queues_halt(tmp_path, content, message):
    path = tmp_path / "q.json"
    if content is not None:
        write(tmp_path, "q.json", content)
    with pytest.raises(audit_queue.QueueInvalid, match=message):
        audit_queue.load_queue(path)


def test_2_duplicate_ids_and_dependency_cycles_halt(tmp_path):
    with pytest.raises(audit_queue.QueueInvalid, match="duplicate"):
        audit_queue.load_queue(write(tmp_path, "q.json", {"version": 1, "items": [item(), item()]}))
    a = item(id="NQ-01", depends_on=["NQ-02"])
    b = item(id="NQ-02", depends_on=["NQ-01"])
    with pytest.raises(audit_queue.QueueInvalid, match="cycle"):
        audit_queue.load_queue(write(tmp_path, "q.json", {"version": 1, "items": [a, b]}))


def test_2_unreadable_queue_halts(tmp_path):
    path = write(tmp_path, "q.json", {"version": 1, "items": [item()]})
    path.chmod(0)
    try:
        if os.geteuid() == 0:
            pytest.skip("root reads mode-0 files")
        with pytest.raises(audit_queue.QueueInvalid, match="unreadable"):
            audit_queue.load_queue(path)
    finally:
        path.chmod(0o600)


def test_2_a_valid_empty_queue_is_empty_not_an_error(tmp_path):
    assert audit_queue.load_queue(write(tmp_path, "q.json", {"version": 1, "items": []})) == []


@pytest.mark.parametrize("change", [
    {"live_provider": True}, {"spends_money": True}, {"status": "suspected"},
    {"allowed_paths": ["../escape.py"]}, {"allowed_paths": ["/etc/passwd"]},
    {"verify_commands": [{"kind": "sh", "args": ["-c", "rm -rf /"]}]},
    {"verify_commands": [{"kind": "vitest", "args": ["a b; rm -rf /"]}]},
    {"verified_sha": "abc"},
])
def test_unsafe_items_are_invalid(change):
    assert audit_queue.validate_item(item(**change))


# 6 + 7: reservations, protected paths, open branches (legacy included) -----
def test_6_reserved_paths_skip_the_item():
    reservation = {"holder": "interactive PR #80", "paths": ["oracle-app/src/components/"], "expires": 0}
    reasons = eligibility.check(item(), ctx(reservations=[reservation]), FRESH)
    assert any("reserved by interactive PR #80" in r for r in reasons)


def test_6_expired_reservations_do_not_block():
    reservation = {"paths": ["oracle-app/src/components/"], "expires": 10}
    assert eligibility.check(item(), ctx(reservations=[reservation]), FRESH) == []


def test_6_protected_paths_cannot_be_assigned_even_by_the_queue():
    for path in ["tools/neoh-agent/runner.py", ".github/workflows/ci.yml",
                 "backend/db/migrations/0200_x.sql", "docs/claude-prompts/LEDGER.md"]:
        bad = item(allowed_paths=[path, "oracle-app/src/components/SalesAgentPage.test.jsx"])
        assert any("protected" in r for r in eligibility.check(bad, ctx(), FRESH)), path


def test_7_an_open_branch_on_the_same_file_skips_the_item():
    branches = {"auto/neoh-02-x": ["oracle-app/src/components/SalesAgentPage.jsx"]}
    reasons = eligibility.check(item(), ctx(open_branch_paths=branches), FRESH)
    assert any("open branch auto/neoh-02-x" in r for r in reasons)


# 13: freshness -------------------------------------------------------------
def fake_git(ancestor=0, changed="", blob=None):
    def run(*args):
        if args[0] == "merge-base":
            return SimpleNamespace(returncode=ancestor, stdout="")
        if args[0] == "diff":
            return SimpleNamespace(returncode=0, stdout=changed)
        if args[0] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout=(blob or "c" * 40) + "\n")
        raise AssertionError(args)
    return run


def test_13_ancestry_is_necessary_but_not_sufficient():
    assert audit_queue.freshness(item(), MAIN, fake_git(ancestor=1))[0] is False
    fresh, why = audit_queue.freshness(item(), MAIN, fake_git(changed="oracle-app/src/components/SalesAgentPage.jsx\n"))
    assert fresh is False and why.startswith("STALE")
    assert audit_queue.freshness(item(), MAIN, fake_git())[0] is True


def test_13_a_pinned_blob_that_changed_is_stale():
    pinned = item(evidence={"paths": ["oracle-app/src/components/SalesAgentPage.jsx"],
                            "blobs": {"oracle-app/src/components/SalesAgentPage.jsx": "d" * 40}})
    assert audit_queue.freshness(pinned, MAIN, fake_git(blob="e" * 40))[0] is False
    assert audit_queue.freshness(pinned, MAIN, fake_git(blob="d" * 40))[0] is True


def test_13_a_stale_item_is_not_eligible():
    reasons = eligibility.check(item(), ctx(), (False, "STALE: evidence changed"))
    assert reasons == ["STALE: evidence changed"]


# 4, 5, 12: claims ----------------------------------------------------------
def test_4_two_claimers_race_and_exactly_one_wins(tmp_path):
    store = claims_mod.ClaimStore(tmp_path)
    results = []
    barrier = threading.Barrier(8)

    def worker(n):
        barrier.wait()
        results.append(store.claim("NQ-01", f"exec-{n}", 3600))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_5_an_expired_lease_is_recovered_and_the_old_exec_cannot_continue(tmp_path):
    now = [1000.0]
    store = claims_mod.ClaimStore(tmp_path, clock=lambda: now[0])
    assert store.claim("NQ-01", "old", 60)
    now[0] += 61
    assert store.recover_stale() == ["NQ-01"]
    assert store.replay()["NQ-01"]["state"] == "RETRYABLE"
    assert store.claim("NQ-01", "new", 60)
    with pytest.raises(claims_mod.ClaimError, match="another execution"):
        store.transition("NQ-01", "old", "TESTING")
    assert not store.heartbeat("NQ-01", "old", 60)


def test_12_a_torn_journal_line_replays_consistently(tmp_path):
    store = claims_mod.ClaimStore(tmp_path)
    assert store.claim("NQ-01", "e1", 3600)
    store.transition("NQ-01", "e1", "TESTING")
    with open(store.journal, "a") as handle:
        handle.write('{"item": "NQ-01", "to": "IMPLEM')  # crash mid-append
    assert store.replay()["NQ-01"]["state"] == "TESTING"
    store.transition("NQ-01", "e1", "IMPLEMENTING")
    assert store.replay()["NQ-01"]["state"] == "IMPLEMENTING"


def test_illegal_transitions_are_refused(tmp_path):
    store = claims_mod.ClaimStore(tmp_path)
    assert store.claim("NQ-01", "e1", 3600)
    with pytest.raises(claims_mod.ClaimError):
        store.transition("NQ-01", "e1", "REVIEW_REQUIRED")  # skipped TESTING/IMPLEMENTING/VERIFYING


# 15: only the supervisor closes; acceptance + integration ------------------
def test_15_noah_cannot_set_integrated_or_closed(tmp_path):
    store = claims_mod.ClaimStore(tmp_path)
    assert store.claim("NQ-01", "e1", 3600)
    for state in ("TESTING", "IMPLEMENTING", "VERIFYING", "REVIEW_REQUIRED"):
        store.transition("NQ-01", "e1", state)
    with pytest.raises(claims_mod.ClaimError, match="only by the supervisor"):
        store.transition("NQ-01", "e1", "INTEGRATED")
    store.transition("NQ-01", "e1", "INTEGRATED", by_supervisor=True)
    assert store.replay()["NQ-01"]["state"] == "INTEGRATED"


def accept(**overrides):
    record = {"item": "NQ-01", "exec_id": "e1", "finding_ids": ["33.4"], "reviewer": "claude-interactive",
              "reviewer_role": "engineer", "revision": "f" * 40, "evidence": "PR #80 CI + review",
              "decision": "accepted", "pr": 80}
    record.update(overrides)
    return record


def test_15_acceptance_rules():
    assert completion.acceptance_for(item(), "e1", [accept()])[0]
    assert completion.acceptance_for(item(), "e1", [accept(reviewer="noah")])[0] is None
    assert completion.acceptance_for(item(), "e1", [accept(implementer="x", reviewer="x")])[0] is None
    assert completion.acceptance_for(item(), "e1", [accept(decision="rejected")])[0] is None
    assert completion.acceptance_for(item(), "e1", [accept(finding_ids=[])])[0] is None
    assert completion.acceptance_for(item(), "other", [accept()])[0] is None
    owner_item = item(review_class="owner_required")
    assert completion.acceptance_for(owner_item, "e1", [accept()])[0] is None
    assert completion.acceptance_for(owner_item, "e1", [accept(reviewer="ydngemini", reviewer_role="owner")])[0]


@pytest.mark.parametrize("pr, ancestor, ok", [
    ({"state": "MERGED", "mergeCommit": {"oid": "1" * 40}, "headRefOid": "f" * 40}, 0, True),   # merge
    ({"state": "MERGED", "mergeCommit": {"oid": "2" * 40}, "headRefOid": "f" * 40}, 0, True),   # squash/rebase
    ({"state": "OPEN", "mergeCommit": None, "headRefOid": "f" * 40}, 0, False),
    ({"state": "MERGED", "mergeCommit": {"oid": "1" * 40}, "headRefOid": "9" * 40}, 0, False),  # other revision
    ({"state": "MERGED", "mergeCommit": {"oid": "1" * 40}, "headRefOid": "f" * 40}, 1, False),  # not on main
])
def test_15_integration_handles_every_merge_style(pr, ancestor, ok):
    def run(argv):
        if argv[0] == "gh":
            return SimpleNamespace(returncode=0, stdout=json.dumps(pr))
        return SimpleNamespace(returncode=ancestor, stdout="")
    assert completion.integrated(accept(), MAIN, run)[0] is ok


# 9: budget -----------------------------------------------------------------
NONCE = "0123456789abcdef0123456789abcdef"


def test_9_cost_markers_charge_unreported_sessions_in_full():
    log = "\n".join([
        f"[noah-session:{NONCE}] start 1 alloc=0.4500",
        f"[noah-cost:{NONCE}] session=1 reported=0.3100",
        f"[noah-session:{NONCE}] start 2 alloc=0.7000",   # crashed: no cost line
        "[noah-cost:ffffffffffffffffffffffffffffffff] session=2 reported=0.0001",  # forged, wrong nonce
        f"[noah-session:{NONCE}] start 3 alloc=0.7000",
        f"[noah-cost:{NONCE}] session=3 reported=none",
    ])
    assert budget_mod.cost_markers(log, NONCE) == (0.31, 1.4, 3)


def test_9_reserve_settle_contingency_and_hard_stops(tmp_path):
    now = [10_000.0]
    spend = budget_mod.Budget(tmp_path, policy(daily_usd=5.0, weekly_usd=6.0, per_task_usd=3.0),
                              clock=lambda: now[0])
    assert spend.headroom() == 3.0
    assert spend.reserve("e1", 3.0)
    assert spend.headroom() == 2.0                      # open reservation counts in full
    spend.settle("e1", 1.0, 0.5)
    assert spend.committed(budget_mod.DAY) == pytest.approx(1.8)   # 1.5 spent + 20% contingency
    assert not spend.reserve("e2", 3.5)                 # above the per-task cap
    assert spend.reserve("e2", 3.0)
    spend.settle("e2", 3.0, 0.0)
    assert spend.headroom() == 0.0                      # 4.5 + 0.9 contingency + nothing open
    assert not spend.reserve("e3", 0.5)
    now[0] += budget_mod.DAY + 1
    assert spend.headroom() == pytest.approx(min(3.0, 6.0 - 4.5))  # day rolled; week still counts
    with pytest.raises(ValueError):
        spend.settle("never-reserved", 1.0, 0.0)


# 10: disk floors on every filesystem ---------------------------------------
def statvfs_factory(free_gib: dict, inode_ratio: dict):
    def statvfs(path):
        key = "root" if path == resources.ROOT_PROBE else "core2"
        return SimpleNamespace(f_bavail=int(free_gib[key] * resources.GIB), f_frsize=1,
                               f_files=1000, f_favail=int(1000 * inode_ratio[key]))
    return statvfs


MEM_OK = "MemAvailable:   4194304 kB\n"


@pytest.mark.parametrize("free, inodes, mem, ok", [
    ({"core2": 4, "root": 1.5}, {"core2": 0.9, "root": 0.6}, MEM_OK, True),
    ({"core2": 2.9, "root": 1.5}, {"core2": 0.9, "root": 0.6}, MEM_OK, False),
    ({"core2": 4, "root": 0.9}, {"core2": 0.9, "root": 0.6}, MEM_OK, False),
    ({"core2": 4, "root": 1.5}, {"core2": 0.05, "root": 0.6}, MEM_OK, False),
    ({"core2": 4, "root": 1.5}, {"core2": 0.9, "root": 0.05}, MEM_OK, False),
    ({"core2": 4, "root": 1.5}, {"core2": 0.9, "root": 0.6}, "MemAvailable: 1048576 kB\n", False),
])
def test_10_each_floor_on_each_filesystem_stops(free, inodes, mem, ok):
    passed, problems = resources.check_floors(policy()["disk_floors"], "/core2",
                                              statvfs=statvfs_factory(free, inodes), meminfo=mem)
    assert passed is ok, problems


# 16: verify sandbox ---------------------------------------------------------
def test_16_verify_sandbox_hides_state_and_secrets(tmp_path):
    state = tmp_path / "state"
    worktree = state / "worktrees" / "cycle-1"
    worktree.mkdir(parents=True)
    env = {"PATH": "/usr/bin", "GH_TOKEN": "ghp_x", "ANTHROPIC_API_KEY": "sk-ant-x", "LANG": "C.UTF-8"}
    argv = verify_sandbox.wrap(["npm", "test"], worktree=worktree, state_dir=state,
                               cwd=worktree / "oracle-app", hidden=(tmp_path / "owner",), env=env)
    joined = " ".join(argv)
    assert "--clearenv" in argv and "--share-net" not in argv
    assert "ghp_x" not in joined and "sk-ant-x" not in joined
    assert argv[argv.index("--tmpfs", argv.index("/tmp")) + 1] == str(state.resolve())
    assert ["--bind", str(worktree.resolve()), str(worktree.resolve())] == \
        argv[argv.index("--bind"):argv.index("--bind") + 3]
    assert str((tmp_path / "owner").resolve()) in argv
    with pytest.raises(ValueError):
        verify_sandbox.wrap(["npm", "test"], worktree=worktree, state_dir=state, cwd=tmp_path, env=env)


@pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="bwrap not installed")
def test_16_verify_sandbox_really_blocks_writes_secrets_and_network(tmp_path):
    state = tmp_path / "state"
    worktree = state / "worktrees" / "cycle-1"
    worktree.mkdir(parents=True)
    (state / "budget.jsonl").write_text("ledger\n")
    owner = tmp_path / "owner"
    owner.mkdir()
    (owner / "audit-queue.json").write_text("{}")
    script = (
        f"echo x > {worktree}/ok.txt && echo wrote-worktree; "
        f"(cat {state}/budget.jsonl >/dev/null 2>&1 && echo LEAK-state) || echo no-state; "
        f"echo forged >> {state}/budget.jsonl 2>/dev/null; "  # lands on a private tmpfs, if anywhere
        f"(ls {owner}/audit-queue.json >/dev/null 2>&1 && echo LEAK-owner) || echo no-owner; "
        "[ -z \"$GH_TOKEN\" ] && echo no-token; "
        "(getent hosts github.com >/dev/null 2>&1 && echo LEAK-net) || echo no-net"
    )
    argv = verify_sandbox.wrap(["/bin/sh", "-c", script], worktree=worktree, state_dir=state, cwd=worktree,
                               hidden=(owner,), env={"PATH": "/usr/bin:/bin", "GH_TOKEN": "ghp_x"})
    out = subprocess.run(argv, capture_output=True, text=True, timeout=30).stdout
    assert "wrote-worktree" in out and "LEAK" not in out, out
    assert {"no-state", "no-owner", "no-token", "no-net"} <= set(out.split())
    assert (state / "budget.jsonl").read_text() == "ledger\n"


# 17: scope and weakening gates (autobuild) ---------------------------------
def test_17_scope_gate():
    import autobuild

    it = item()
    assert autobuild.scope_problems(["oracle-app/src/components/SalesAgentPage.jsx"], it) == []
    assert autobuild.scope_problems(["oracle-app/src/App.jsx"], it)
    assert autobuild.scope_problems(["tools/neoh-agent/runner.py"], it)
    assert autobuild.scope_problems(["docs/claude-prompts/LEDGER.md"], it)


@pytest.mark.parametrize("line, weak", [
    ("+  it.skip('x', () => {})", True), ("+  it.only('x', () => {})", True),
    ("+@pytest.mark.skip(reason='flaky')", True), ("+    pytest.skip('later')", True),
    ("+  it('x', () => {})", False),
])
def test_17_added_skips_are_weakening(line, weak):
    import autobuild

    assert bool(autobuild.WEAKENING_ADDED.search(line)) is weak


def test_17_removed_assertions_are_weakening():
    import autobuild

    assert autobuild.WEAKENING_REMOVED.search("-    assert x == 1")
    assert autobuild.WEAKENING_REMOVED.search("-  expect(a).toBe(b)")
    assert not autobuild.WEAKENING_REMOVED.search("-  const a = 1")


def test_runner_write_guard_honours_the_phase_scope(monkeypatch):
    import runner

    monkeypatch.setenv("NEOH_ALLOWED_PATHS", json.dumps(["oracle-app/src/components/SalesAgentPage.test.jsx"]))
    assert runner.write_path_allowed("oracle-app/src/components/SalesAgentPage.test.jsx")
    assert not runner.write_path_allowed("oracle-app/src/components/SalesAgentPage.jsx")
    monkeypatch.setenv("NEOH_ALLOWED_PATHS", "not json")
    assert not runner.write_path_allowed("oracle-app/src/components/SalesAgentPage.test.jsx")
    monkeypatch.delenv("NEOH_ALLOWED_PATHS")
    assert not runner.write_path_allowed("backend/db/migrations/0200_x.sql")
    assert not runner.write_path_allowed("tools/neoh-agent/runner.py")


# 8 + 18: classification, signatures, retry vs escalate ---------------------
def test_18_infrastructure_retries_with_backoff_then_escalates():
    log = "API Error: Connection refused\nRuntimeError: aclose(): asynchronous generator is already running"
    assert failures.classify(1, log) == "infrastructure"
    assert failures.decide("infrastructure", "s", [], 0, 2, 2) == ("retry", 600)
    assert failures.decide("infrastructure", "s", [], 1, 2, 2) == ("retry", 1800)
    assert failures.decide("infrastructure", "s", [], 2, 2, 2) == ("escalate", 0)


def test_18_the_same_signature_twice_escalates():
    log = "FAILED tests/test_object_storage.py::test_x[env0-False] - AssertionError"
    sig = failures.signature("test_failure", log)
    assert sig == failures.signature("test_failure", log + "\nnoise 12.3s")
    assert failures.decide("test_failure", sig, [], 0, 2, 2) == ("fail", 0)
    assert failures.decide("test_failure", sig, [sig], 0, 2, 2) == ("escalate", 0)


def test_18_baseline_red_halts_and_not_reproduced_escalates():
    assert failures.classify(6, "") == "baseline_red"
    assert failures.decide("baseline_red", "s", [], 0, 2, 2) == ("halt", 0)
    assert failures.decide("not_reproduced", "s", [], 0, 2, 2) == ("escalate", 0)
    assert failures.classify(2, "[stop] outside allowed_paths: x") == "scope_violation"


def test_8_red_evidence_is_kept_in_the_record():
    import queue_supervisor

    log = f"[noah-phase:{NONCE}] TESTING\n[red] x.test.jsx fails on unchanged code (exit 1):\nFAIL x\n[noah-phase:{NONCE}] IMPLEMENTING\n"
    assert queue_supervisor.phases_seen(log, NONCE) == ["TESTING", "IMPLEMENTING"]


def test_policy_canary_requires_an_item_and_sane_caps(tmp_path):
    with pytest.raises(audit_queue.QueueInvalid, match="canary_item"):
        audit_queue.load_policy(write(tmp_path, "p.json", policy(canary_item=None)))
    with pytest.raises(audit_queue.QueueInvalid, match="per_task <= daily"):
        audit_queue.load_policy(write(tmp_path, "p.json", policy(per_task_usd=5.0)))
    assert audit_queue.load_policy(write(tmp_path, "p.json", policy()))["mode"] == "canary_single_cycle"


def test_10_root_floor_probes_the_host_root_not_the_sandbox_tmpfs():
    """Inside the sandbox "/" is a tmpfs; /usr is the host root partition."""
    assert resources.ROOT_PROBE == os.environ.get("NEOH_ROOT_PROBE", "/usr")
    seen = []

    def statvfs(path):
        seen.append(path)
        return SimpleNamespace(f_bavail=10 * resources.GIB, f_frsize=1, f_files=10, f_favail=9)

    resources.check_floors(policy()["disk_floors"], "/core2", statvfs=statvfs, meminfo=MEM_OK)
    assert seen == ["/core2", resources.ROOT_PROBE] and "/" not in seen


def test_15_the_latest_review_record_decides():
    """Found by the first real completion check: an earlier changes_requested
    record (no PR yet) hid the later acceptance."""
    requested = accept(decision="changes_requested", pr=None)
    assert completion.acceptance_for(item(), "e1", [requested, accept()])[0]
    assert completion.acceptance_for(item(), "e1", [accept(), accept(decision="rejected")])[0] is None
