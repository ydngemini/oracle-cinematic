"""35-06 F1: rollback.sh judges the SET of migrations the live release has and
the target lacks. A filename range missed a migration merged late with a lower
number: 0144 landed while the head was 0145, so a rollback across it saw an
empty gap even though the classifier calls 0144 UNKNOWN. Runs the script's
dry run (no doctl, no network, nothing changed)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = sorted(p.name for p in (REPO / "backend" / "db" / "migrations").glob("*.sql"))
LATE = "0144_drop_duplicate_tenant_policies.sql"


def _manifest(tmp_path, name, migrations, *, with_list=True):
    data = {"release_id": name, "backend_digest": "sha256:" + "a" * 64, "frontend_digest": "sha256:" + "b" * 64,
            "migration_head": migrations[-1], "migration_count": len(migrations)}
    if with_list:
        data["migrations"] = migrations
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(data))
    return path


def _rollback(to, frm):
    return subprocess.run(["bash", str(REPO / "scripts" / "rollback.sh"), "--to", str(to), "--from", str(frm)],
                          capture_output=True, text=True, cwd=REPO, timeout=120)


@pytest.mark.skipif(LATE not in MIGRATIONS, reason="the late migration is part of this tree's history")
def test_a_late_lower_numbered_migration_is_in_the_gap(tmp_path):
    target = [m for m in MIGRATIONS if m != LATE]           # same head, one older file missing
    result = _rollback(_manifest(tmp_path, "target", target), _manifest(tmp_path, "live", MIGRATIONS))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "1 migration(s) applied since the target release" in result.stdout
    assert "APPLICATION ROLLBACK UNSAFE" in result.stdout


def test_an_older_manifest_without_the_list_is_refused_when_the_range_hides_a_file(tmp_path):
    target = [m for m in MIGRATIONS if m != LATE]
    result = _rollback(_manifest(tmp_path, "target", target, with_list=False),
                       _manifest(tmp_path, "live", MIGRATIONS, with_list=False))
    assert result.returncode == 3
    assert "cannot be proven" in result.stdout


def test_identical_sets_have_nothing_to_be_incompatible_with(tmp_path):
    result = _rollback(_manifest(tmp_path, "target", MIGRATIONS), _manifest(tmp_path, "live", MIGRATIONS))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no migrations were applied after" in result.stdout


def test_the_manifest_builder_records_the_migration_list():
    source = (REPO / "scripts" / "build-release-manifest.sh").read_text()
    assert '"migrations": $MIGRATIONS_JSON' in source
