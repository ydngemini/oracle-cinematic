"""35-06 F1: rollback.sh classifies every migration that may be live and the
target lacks — the set difference between the two manifests' migration lists,
UNION the old filename range. The range alone missed a migration merged late
with a lower number (0144 landed while the head was 0145); the set alone would
miss a file applied independently of the image. Runs the script's dry run
(no doctl, no network, nothing changed)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = sorted(p.name for p in (REPO / "backend" / "db" / "migrations").glob("*.sql"))
LATE = "0144_drop_duplicate_tenant_policies.sql"
DESTRUCTIVE = "0173"   # the classifier calls it non-additive (see test below)


def _manifest(tmp_path, name, names, *, with_list=True, **extra):
    migrations = names
    data = {"release_id": name, "backend_digest": "sha256:" + "a" * 64, "frontend_digest": "sha256:" + "b" * 64,
            "migration_head": migrations[-1], "migration_count": len(migrations)}
    if with_list:
        data["migrations"] = migrations
    data.update(extra)
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(data))
    return path


def _rollback(tmp_path, to, frm=None):
    argv = ["bash", str(REPO / "scripts" / "rollback.sh"), "--to", str(to)]
    if frm is not None:
        argv += ["--from", str(frm)]
    env = {**os.environ, "ORACLE_ROLLBACK_SPEC": str(tmp_path / "rollback.yaml")}
    return subprocess.run(argv, capture_output=True, text=True, cwd=REPO, env=env, timeout=120)


def _through(prefix):
    return [m for m in MIGRATIONS if m[:4] <= prefix]


needs_late = pytest.mark.skipif(LATE not in MIGRATIONS, reason="needs the late 0144 migration in this tree")


@needs_late
def test_a_late_lower_numbered_migration_is_in_the_gap(tmp_path):
    target = [m for m in MIGRATIONS if m != LATE]           # same head, one older file missing
    result = _rollback(tmp_path, _manifest(tmp_path, "target", target), _manifest(tmp_path, "live", MIGRATIONS))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "1 migration(s) applied since the target release" in result.stdout
    assert "APPLICATION ROLLBACK UNSAFE" in result.stdout


@needs_late
def test_an_older_manifest_without_the_list_is_refused_when_the_range_hides_a_file(tmp_path):
    target = [m for m in MIGRATIONS if m != LATE]
    result = _rollback(tmp_path, _manifest(tmp_path, "target", target, with_list=False),
                       _manifest(tmp_path, "live", MIGRATIONS, with_list=False))
    assert result.returncode == 3
    assert "cannot be established" in result.stdout
    assert "no migrations were applied" not in result.stdout


def test_a_file_in_the_range_counts_even_when_the_live_list_omits_it(tmp_path):
    """Migrations can be applied independently of the image: the union keeps
    the old range's strictness (review r1: the set alone allowed this)."""
    destructive = [m for m in MIGRATIONS if m.startswith(DESTRUCTIVE)]
    assert destructive, "fixture migration missing"
    target = _through("0172")
    live = [m for m in _through("0179") if not m.startswith(DESTRUCTIVE)]
    result = _rollback(tmp_path, _manifest(tmp_path, "target", target), _manifest(tmp_path, "live", live))
    assert result.returncode == 3, result.stdout + result.stderr
    assert destructive[0].split(".")[0] in result.stdout


def test_a_migration_only_the_target_has_is_refused(tmp_path):
    live = [m for m in MIGRATIONS if m != MIGRATIONS[-5]]
    result = _rollback(tmp_path, _manifest(tmp_path, "target", MIGRATIONS[:-1]), _manifest(tmp_path, "live", live))
    assert result.returncode == 3
    assert "in the target release but not the live one" in result.stdout
    assert "applied since the target release" not in result.stdout


def test_a_gap_file_missing_from_this_checkout_is_refused(tmp_path):
    live = MIGRATIONS + ["9998_from_a_newer_branch.sql"]
    result = _rollback(tmp_path, _manifest(tmp_path, "target", MIGRATIONS), _manifest(tmp_path, "live", live))
    assert result.returncode == 3
    assert "not in this checkout" in result.stdout


def test_a_malformed_migration_list_is_refused(tmp_path):
    to = _manifest(tmp_path, "target", MIGRATIONS[:-1])
    frm = _manifest(tmp_path, "live", MIGRATIONS, with_list=False, **{"migrations": MIGRATIONS[-1]})   # a string
    result = _rollback(tmp_path, to, frm)
    assert result.returncode != 0
    assert "could not classify" in result.stderr


def test_identical_sets_have_nothing_to_be_incompatible_with(tmp_path):
    result = _rollback(tmp_path, _manifest(tmp_path, "target", MIGRATIONS), _manifest(tmp_path, "live", MIGRATIONS))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no migrations were applied after" in result.stdout


def test_without_from_the_dry_run_compares_with_the_working_tree(tmp_path):
    result = _rollback(tmp_path, _manifest(tmp_path, "target", MIGRATIONS))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "current head taken from the working tree" in result.stdout
    assert "no migrations were applied after" in result.stdout


def test_the_manifest_builder_records_the_sorted_migration_list(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text('#!/bin/sh\necho "sha256:' + "c" * 64 + '"\n')
    (bin_dir / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "REGISTRY": "registry.example/neoh",
           "GIT_SHA": "abc1234"}
    proc = subprocess.run(["bash", str(REPO / "scripts" / "build-release-manifest.sh")],
                          capture_output=True, text=True, cwd=REPO, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads(proc.stdout)
    assert manifest["migrations"] == MIGRATIONS
    assert manifest["migration_count"] == len(MIGRATIONS)
    assert manifest["migration_head"] == MIGRATIONS[-1]
