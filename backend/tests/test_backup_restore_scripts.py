"""Contracts for the backup/restore/verify scripts.

These are shell scripts, so most of their behaviour is only provable by running
them — which the drill does. What is pinned here is the set of properties that
a later edit could quietly remove and no test would notice, each of which was a
real defect found while building them.
"""

from __future__ import annotations

import pathlib
import re

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent.parent / "scripts"
BACKUP = SCRIPTS / "backup-postgres.sh"
RESTORE = SCRIPTS / "restore-postgres.sh"
VERIFY = SCRIPTS / "verify-restore.sh"


@pytest.fixture(scope="module")
def backup() -> str:
    return BACKUP.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def restore() -> str:
    return RESTORE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def verify() -> str:
    return VERIFY.read_text(encoding="utf-8")


def test_all_three_scripts_exist_and_are_executable():
    for script in (BACKUP, RESTORE, VERIFY):
        assert script.exists(), f"{script.name} is missing"
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"


# ── Credentials ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["backup-postgres.sh", "restore-postgres.sh", "verify-restore.sh"])
def test_no_script_embeds_a_credential(name):
    """A backup script is the single worst place to hardcode a password: it is
    copied to runbooks, pasted into incident channels and run by people who are
    not the author."""
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    # Assignments of a literal to a password-ish variable.
    offenders = re.findall(
        r'^\s*(?:export\s+)?(PGPASSWORD|[A-Z_]*PASSWORD|[A-Z_]*SECRET|[A-Z_]*_KEY)\s*=\s*["\']?[^"\'\s$#]+',
        text, re.M,
    )
    assert not offenders, f"{name} assigns a literal credential: {offenders}"


@pytest.mark.parametrize("name", ["backup-postgres.sh", "restore-postgres.sh", "verify-restore.sh"])
def test_every_script_requires_an_explicit_target(name):
    """None of these may ever guess a host. The spec's rule, and the reason is
    that the wrong host during an incident is the likeliest mistake there is."""
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    for var in ("ORACLE_DB_HOST", "ORACLE_DB_NAME", "ORACLE_DB_USER", "PGPASSWORD"):
        assert re.search(rf':\s*"\$\{{{var}:\?', text), (
            f"{name} does not hard-require {var}"
        )


# ── The backup ─────────────────────────────────────────────────────────────

def test_backup_requires_tls_for_a_remote_host(backup):
    assert "A remote backup must use TLS" in backup
    assert "IS_LOCAL" in backup, (
        "the TLS DEFAULT must depend on the host, not merely the check — "
        "defaulting everything to require and exempting localhost from the "
        "check still sends sslmode=require to a container that cannot speak it"
    )


def test_backup_deletes_a_partial_dump(backup):
    """A truncated dump sitting in the bucket looking like a backup is worse
    than no backup, because it is the one you reach for."""
    assert 'rm -f "$DUMP_PATH"' in backup
    assert "pg_restore --list" in backup, (
        "exit 0 is not completeness — the archive's own table of contents has "
        "to be readable or the truncation is only discovered during the restore"
    )


def test_backup_reads_the_migration_head_from_the_right_column(backup):
    """The ledger keys on `filename`; there is no `version` column. Getting it
    wrong does not error — the fallback writes "unknown" into the manifest and
    the backup looks fine."""
    assert "max(filename)" in backup
    assert "max(version)" not in backup


def test_backup_refuses_to_record_an_unknown_migration_head(backup):
    assert 'if [ "$MIGRATION_HEAD" = "unknown" ]' in backup


def test_the_authoritative_scope_is_closed_under_foreign_keys(backup):
    """Found by drill: excluding `leads` data while keeping the 13 tables that
    reference it meant 580 child rows were rejected by their foreign keys, and
    pg_restore carried on, so the restore "completed" having dropped them."""
    assert "pg_constraint" in backup and "confrelid" in backup, (
        "the referenced-lead set must be derived from the FK catalog, not a "
        "hand-written list a new child table would fall outside of"
    )


def test_the_draft_predicate_is_not_a_null_check(backup):
    """`dossier_status` is NOT NULL with a 'draft' default, so `IS NOT NULL`
    matches all 10.27M rows. That mistake was made once already."""
    assert "coalesce(dossier_status, 'draft') <> 'draft'" in backup


def test_manifest_separates_source_rows_from_expected_after_restore(backup):
    """Recording the source count for a table whose data the scope excluded
    makes every verification fail — and the tempting fix for that is to loosen
    the check until it stops noticing anything."""
    assert '"source_rows_leads"' in backup
    assert '"expected_leads": "$EXPECTED_LEADS"' in backup


def test_manifest_keys_are_flat(backup):
    """Nested keys defeat the sed fallback: `source_rows.leads` and
    `expected_after_restore.leads` both end in `leads`, and sed returned the
    first one — 10,273,553 where 9 was correct.

    REC-03 added exactly one object, table_counts. It is safe only because its
    keys are schema-qualified (they contain a dot, and no scalar key does), it
    is written one entry per line and last, and the verifier never reads it
    with the scalar reader."""
    manifest = backup[backup.index('cat > "$MANIFEST_PATH"'):backup.index("\nJSON\n")]
    assert manifest.count('": {') == 1, "table_counts must stay the manifest's only object"
    assert '"table_counts": {\n$TABLE_COUNTS_JSON\n  }\n}' in manifest, "table_counts must be last"
    scalar_keys = re.findall(r'^\s*"([^"]+)":', manifest, re.M)
    assert len(scalar_keys) == len(set(scalar_keys)), "every scalar key must be unique"
    assert not [k for k in scalar_keys if "." in k], "no scalar key may contain a dot"
    # Each table_counts entry is the PostgreSQL-encoded "schema.table".
    assert "to_json(t.nspname || '.' || t.relname)" in backup


def test_manifest_carries_no_secret(backup):
    manifest = backup[backup.index('cat > "$MANIFEST_PATH"'):]
    for forbidden in ("PGPASSWORD", "MASTER_KEY\":", "access_key", "token"):
        assert forbidden not in manifest, f"the manifest must not carry {forbidden}"
    # It must, however, say loudly that the key is NOT in the dump.
    assert "requires_master_key" in manifest


# ── The restore ────────────────────────────────────────────────────────────

def test_restore_verifies_the_checksum_before_touching_anything(restore):
    integrity = restore.index("Checking artifact integrity")
    restore_call = restore.index("_pg_restore_section")
    assert integrity < restore_call, (
        "a corrupted dump discovered halfway through has already destroyed the "
        "target"
    )


def test_restore_refuses_a_populated_database_without_an_explicit_flag(restore):
    assert "--i-know-this-database-has-data" in restore
    assert "Refusing to restore over it" in restore


def test_restore_does_not_use_clean_or_if_exists(restore):
    """A flag that drops objects on the way in turns a mis-targeted restore
    into a destroyed database."""
    # The comments explain why these are absent, so only code counts.
    code = "\n".join(
        line for line in restore.splitlines() if not line.lstrip().startswith("#")
    )
    assert "--clean" not in code
    assert "--if-exists" not in code


def test_scoped_restore_applies_parent_rows_before_the_data_section(restore):
    """The ordering the drill forced. Applying the extract after the dump
    cannot work: the child tables load during the dump, while `leads` is still
    empty."""
    scoped = restore[restore.index("scoped restore"):]
    pre = scoped.index("_pg_restore_section pre-data")
    extract = scoped.index("applying the authoritative-row extract")
    data = scoped.index('_pg_restore_section data')
    assert pre < extract < data


def test_restore_refuses_when_the_extract_is_missing(restore):
    assert "silently drop the rows that re-harvesting cannot reproduce" in restore


def test_restore_does_not_claim_success_on_its_own(restore):
    assert "A restore is finished when the verifier passes" in restore


# ── The verifier ───────────────────────────────────────────────────────────

def test_verifier_aborts_when_the_manifest_cannot_be_parsed(verify):
    """The failure that would have passed: with no jq and no python3 the first
    parser returned "" for every field, and "" == "" is green."""
    assert "could not read 'migration_head'" in verify
    assert "would compare empty strings" in verify


def test_verifier_tries_jq_then_python_then_sed(verify):
    reader = verify[verify.index("m() {"):]
    assert reader.index("jq") < reader.index("python3") < reader.index("sed")


def test_verifier_checks_the_things_that_fail_silently(verify):
    """Each of these is a way a restore passes `pg_restore; echo $?` and is
    still broken."""
    for guarantee in (
        "migration head",          # right data, wrong schema version
        "relrowsecurity",          # every tenant can read every other tenant
        "relforcerowsecurity",     # the owner role bypasses every policy
        "app_current_tenant",      # policies that cannot evaluate
        "pgcrypto",                # nothing encrypted can be read back
        "has_function_privilege",  # 0003 revokes PUBLIC; a missed re-grant is silent
    ):
        assert guarantee in verify, f"the verifier does not check {guarantee}"


# ── DR-001 / REC-02: the restored ciphertext must open with the real key ───
#
# The verifier used to encrypt and decrypt the literal 'k' and, with no key at
# all, warned and still printed RESTORE VERIFIED. These run the real script
# against a stand-in psql that answers by query shape, and read section 4's
# verdict. (Real PostgreSQL proves the key derivation in the CI DR drill, whose
# fixture holds a note sealed with crypto.derive_tenant_key's output.)

FAKE_PSQL = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG.argv"
SQL="$*"
if [[ " $* " == *" -f - "* ]]; then SQL="$(cat)"; printf '%s\n' "$SQL" >> "$FAKE_LOG.stdin"; fi
case "$SQL" in
  *"column_name LIKE"*) [ "$FAKE_COLUMNS" = yes ] && echo "client_notes body_ciphertext" ;;
  *"oracle_decrypt(s.v"*) echo "$FAKE_DECRYPT" ;;
  *"|| ' 0'"*) echo "$FAKE_PRESENT" ;;
  *) echo "" ;;
esac
"""


def _verify_section_4(tmp_path, *, key, columns="yes", decrypt="3 0", present="3 0"):
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "psql"
    fake.write_text(FAKE_PSQL)
    fake.chmod(0o755)
    manifest = tmp_path / "m.json"
    manifest.write_text('{"migration_head": "0999_x.sql", "ciphertext_columns": "29"}')
    env = {k: v for k, v in os.environ.items() if k != "ORACLE_ENCRYPTION_MASTER_KEY"}
    env.update(PATH=f"{bin_dir}:{env['PATH']}", ORACLE_DB_HOST="127.0.0.1", ORACLE_DB_NAME="r",
               ORACLE_DB_USER="postgres", PGPASSWORD="pw", FAKE_LOG=str(tmp_path / "psql"),
               FAKE_COLUMNS=columns, FAKE_DECRYPT=decrypt, FAKE_PRESENT=present)
    if key:
        env["ORACLE_ENCRYPTION_MASTER_KEY"] = key
    done = subprocess.run(["bash", str(VERIFY), str(manifest)], env=env, capture_output=True,
                          text=True, timeout=60)
    lines = [ln for ln in done.stdout.splitlines() if "decrypt" in ln or "ciphertext value" in ln
             or "MASTER_KEY" in ln]
    return done, "\n".join(lines), tmp_path / "psql"


def test_no_key_with_restored_ciphertext_fails_the_restore(tmp_path):
    done, verdict, _ = _verify_section_4(tmp_path, key=None)
    assert "FAIL" in verdict and "not set" in verdict, verdict
    assert done.returncode != 0 and "RESTORE VERIFIED" not in done.stdout


def test_a_key_that_does_not_open_the_restored_ciphertext_fails(tmp_path):
    _, verdict, _ = _verify_section_4(tmp_path, key="wrong-key", decrypt="3 3")
    assert "FAIL" in verdict and "do NOT decrypt" in verdict, verdict


def test_the_right_key_opening_restored_ciphertext_passes_that_check(tmp_path):
    _, verdict, log = _verify_section_4(tmp_path, key="the-real-master-key", decrypt="3 0")
    assert "PASS" in verdict and "3 restored ciphertext value(s)" in verdict, verdict
    assert "the-real-master-key" not in (log.parent / "psql.argv").read_text(), "the key reached argv"
    sent = (log.parent / "psql.stdin").read_text()
    assert "\\getenv mk ORACLE_ENCRYPTION_MASTER_KEY" in sent and "the-real-master-key" not in sent
    assert "oracle-pii-v1:" in sent, "crypto.derive_tenant_key's HKDF info prefix"


def test_a_dump_with_no_ciphertext_values_needs_no_key(tmp_path):
    _, verdict, _ = _verify_section_4(tmp_path, key=None, present="0 0")
    assert "PASS" in verdict and "nothing encrypted" in verdict, verdict


def test_an_unsampleable_column_is_a_failure_not_a_pass(tmp_path):
    _, verdict, _ = _verify_section_4(tmp_path, key="k1", decrypt="")
    assert "FAIL" in verdict and "could not be sampled" in verdict, verdict


def test_the_literal_key_probe_is_gone(verify):
    assert "oracle_encrypt('neoh-restore-probe', 'k')" not in verify


def test_the_drill_fixture_seals_a_note_with_the_apps_own_key_derivation():
    """The CI drill decrypts this with the SQL derivation; the literal must be
    what crypto.derive_tenant_key produces, or the drill proves nothing."""
    import sys

    sys.path.insert(0, str(SCRIPTS.parent / "backend"))
    from crypto import derive_tenant_key

    fixture = (SCRIPTS / "dr-drill-fixture.sql").read_text()
    key = derive_tenant_key("aaaaaaaa-0000-4000-8000-00000000000a", "drill-probe-key")
    assert f"oracle_encrypt('drill sealed note', '{key}')" in fixture


def test_verifier_exits_nonzero_when_anything_failed(verify):
    assert "DO NOT TRUST THIS RESTORE" in verify
    assert verify.rstrip().endswith("exit 1")


# ── Roles: the gap the DR drill found ──────────────────────────────────────
#
# pg_dump does not dump roles — they are cluster-level. The first restore into
# a fresh cluster produced a database with every row, every RLS policy and all
# 146 policies intact, and NONE of the three roles that 566 table grants point
# at. Every existing check passed. The application could not open a connection.

def test_backup_captures_roles(backup):
    assert "roles_filename" in backup, "the manifest must name a roles file"
    assert "pg_auth_members" in backup, "role memberships must travel too"
    assert "rolcanlogin" in backup


def test_backup_refuses_to_record_a_backup_with_no_roles(backup):
    """Zero roles means every GRANT in the dump fails on restore. Better to
    fail the backup than to produce one that restores into an unusable
    database."""
    assert "captured zero roles" in backup


def test_the_roles_file_carries_no_passwords(backup):
    """`pg_dumpall --roles-only` embeds the SCRAM verifier for every login
    role. A backup artifact gets copied into buckets and incident channels."""
    assert "NO PASSWORDS" in backup
    # The comment explains why pg_dumpall is not used, so only code counts.
    code = "\n".join(l for l in backup.splitlines() if not l.lstrip().startswith("#"))
    assert "rolpassword" not in code
    assert "pg_dumpall" not in code


def test_neither_script_discards_privileges(backup, restore):
    """The 566 grants live in the dump's ACL entries. --no-privileges on EITHER
    side drops them all — and the drill found exactly that asymmetry, where the
    backup had been fixed and the restore had not."""
    for name, text in (("backup", backup), ("restore", restore)):
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        assert "--no-privileges" not in code, (
            f"{name} discards privileges; the application role would hold no grants"
        )


def test_restore_creates_roles_before_restoring_the_dump(restore):
    """A GRANT to a role that does not exist is an error, 566 times over."""
    # The first `_pg_restore_section` in the file is the function DEFINITION,
    # which necessarily precedes everything. Compare against the first CALL.
    roles_at = restore.index("creating roles")
    first_call = restore.index("_pg_restore_section pre-data")
    assert roles_at < first_call


def test_restore_refuses_when_the_roles_file_is_missing(restore):
    assert "no role the application can connect as" in restore


def test_verifier_checks_the_application_can_actually_use_the_database(verify):
    for guarantee in ("roles_count", "role_table_grants", "rolpassword IS NULL"):
        assert guarantee in verify, f"the verifier does not check {guarantee}"


# ── The drill itself ───────────────────────────────────────────────────────

DRILL = SCRIPTS / "dr-drill.sh"
FIXTURE = SCRIPTS / "dr-drill-fixture.sql"


def test_the_drill_exists_and_is_executable():
    assert DRILL.exists() and DRILL.stat().st_mode & 0o111


def test_the_drill_removes_its_volumes():
    """`docker rm -f` leaves a container's anonymous volume behind, and the
    postgres image declares its data directory as one — about 300 MB per run.
    Twenty runs took the host from 2.2 GB free to 35 MB, at which point no
    container could start and every assertion came back empty. A drill that
    cannot be run repeatedly is not a drill."""
    text = DRILL.read_text(encoding="utf-8")
    assert "docker rm -f " not in text.replace("docker rm -fv ", ""), (
        "every container removal in the drill must pass -v"
    )


def test_the_fixture_contacts_nobody():
    """No real phone number, no real Stripe id, no real provider account."""
    text = FIXTURE.read_text(encoding="utf-8")
    import re

    for number in re.findall(r"\+1\d{10}", text):
        assert number.startswith("+1555"), (
            f"{number} is outside the reserved fictional +1555 range"
        )
    for sid in re.findall(r"AC[0-9A-Fa-f]{32}", text):
        assert "d411" in sid or set(sid[2:]) <= {"0"}, f"{sid} looks like a real SID"
    assert "@drill.invalid" in text, "fixture emails must use a reserved TLD"
    assert "DRILLFIXTURE" in text, "Stripe ids must be obviously synthetic"


def test_the_drill_proves_isolation_with_the_real_application_role():
    """An invented role with blanket SELECT proves less and misleads more: it
    cannot execute app_current_tenant(), because 0003 revokes EXECUTE from
    PUBLIC, so the policy errors and the failure reads as a leak."""
    text = DRILL.read_text(encoding="utf-8")
    assert "oracle_app_login" in text
    assert "CREATE ROLE drill_app" not in text


# ── REC-03: an unexplained pg_restore error fails the restore; the log is kept ─

FAKE_PG = r"""#!/usr/bin/env bash
case "$(basename "$0")" in
  psql) echo "0" ;;                                   # an empty target
  pg_restore) [ -n "$FAKE_RESTORE_STDERR" ] && printf '%s\n' "$FAKE_RESTORE_STDERR" >&2; exit "${FAKE_RC:-0}" ;;
esac
"""


def _restore(tmp_path, *, stderr="", rc=0, expected=None):
    import hashlib
    import json
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("psql", "pg_restore"):
        (bin_dir / tool).write_text(FAKE_PG)
        (bin_dir / tool).chmod(0o755)
    dump = tmp_path / "neoh.dump"
    dump.write_bytes(b"PGDMP fake dump")
    manifest = tmp_path / "neoh.manifest.json"
    manifest.write_text(json.dumps({"dump_filename": dump.name,
                                    "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
                                    "migration_head": "0999_x.sql", "scope": "full"}))
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "ORACLE_DB_HOST": "127.0.0.1",
           "ORACLE_DB_NAME": "r", "ORACLE_DB_USER": "postgres", "PGPASSWORD": "pw",
           "FAKE_RESTORE_STDERR": stderr, "FAKE_RC": str(rc)}
    env.pop("ORACLE_RESTORE_EXPECTED_ERRORS", None)
    if expected:
        env["ORACLE_RESTORE_EXPECTED_ERRORS"] = expected
    done = subprocess.run(["bash", str(RESTORE), str(manifest)], env=env, capture_output=True,
                          text=True, timeout=60)
    return done, done.stdout + done.stderr, sorted(tmp_path.glob("restore-*.log"))


DROPPED = "pg_restore: error: could not execute query: ERROR:  insert or update on table \"property_media\" violates foreign key"


def test_an_unexplained_restore_error_fails_the_restore_and_keeps_the_log(tmp_path):
    done, out, logs = _restore(tmp_path, stderr=DROPPED, rc=1)
    assert done.returncode != 0 and "nobody has explained" in out, out
    assert "NOT DONE YET" not in out, "it must stop, not hand a damaged restore to the verifier"
    assert len(logs) == 1 and "violates foreign key" in logs[0].read_text()


def test_an_operator_explained_error_lets_the_restore_continue(tmp_path):
    done, out, logs = _restore(tmp_path, stderr="pg_restore: error: must be owner of extension plpgsql",
                               rc=1, expected="must be owner of extension")
    assert done.returncode == 0 and "every error line was expected" in out, out
    assert len(logs) == 1


def test_a_clean_restore_still_points_at_the_verifier_and_keeps_its_log(tmp_path):
    done, out, logs = _restore(tmp_path)
    assert done.returncode == 0 and "NOT DONE YET" in out and "log kept" in out, out
    assert len(logs) == 1


# ── REC-04: tenant isolation must come back in full, not "at least one table" ─

FAKE_PSQL_RLS = r"""#!/usr/bin/env bash
case "$*" in
  *relforcerowsecurity*) echo "$FAKE_FORCED" ;;
  *relrowsecurity*) echo "$FAKE_RLS" ;;
  *pg_policies*) echo "$FAKE_POLICIES" ;;
  *) echo "" ;;
esac
"""


def _verify_rls(tmp_path, *, rls, forced, policies, manifest_counts):
    import json
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "psql").write_text(FAKE_PSQL_RLS)
    (bin_dir / "psql").chmod(0o755)
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"migration_head": "0999_x.sql", **manifest_counts}))
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "ORACLE_DB_HOST": "127.0.0.1",
           "ORACLE_DB_NAME": "r", "ORACLE_DB_USER": "postgres", "PGPASSWORD": "pw",
           "FAKE_RLS": rls, "FAKE_FORCED": forced, "FAKE_POLICIES": policies}
    out = subprocess.run(["bash", str(VERIFY), str(manifest)], env=env, capture_output=True,
                         text=True, timeout=60).stdout
    return "\n".join(line for line in out.splitlines() if "row-level security" in line or "RLS policies" in line)


def test_a_restore_that_lost_rls_on_most_tables_fails(tmp_path):
    lines = _verify_rls(tmp_path, rls="3", forced="3", policies="5",
                        manifest_counts={"rls_tables": "120", "rls_forced_tables": "118", "rls_policies": "123"})
    assert lines.count("FAIL") == 3 and "the backup had 120" in lines, lines


def test_a_full_rls_restore_matches_the_backup(tmp_path):
    lines = _verify_rls(tmp_path, rls="120", forced="118", policies="123",
                        manifest_counts={"rls_tables": "120", "rls_forced_tables": "118", "rls_policies": "123"})
    assert lines.count("PASS") == 3 and "FAIL" not in lines, lines


def test_an_older_manifest_falls_back_to_the_weaker_check_and_says_so(tmp_path):
    lines = _verify_rls(tmp_path, rls="3", forced="3", policies="5", manifest_counts={})
    assert lines.count("PASS") == 3 and "predates exact RLS counts" in lines, lines


def test_the_backup_manifest_records_the_rls_counts(backup):
    for key in ('"rls_tables"', '"rls_forced_tables"', '"rls_policies"'):
        assert key in backup


# ── REC-03: exhaustive, snapshot-consistent reconciliation ─────────────────
#
# The manifest counted four tables, in separate sessions BEFORE pg_dump began,
# and the verifier checked only those four (skipping any it could not read).
# A restore that lost rows anywhere else passed; on a live database even the
# four could disagree with the dump. The backup now counts every table whose
# data is in the dump, inside the dump's own exported snapshot, and the
# verifier requires every one back exactly. The CI DR drill proves it on real
# PostgreSQL (a writer committing during the backup; a deleted row; an
# unvalidated FK; a dropped index). These run the real scripts against
# stand-in psql/pg_dump/pg_restore.

import json as _json
import os as _os
import shutil as _shutil
import subprocess as _subprocess
import time as _time

SNAPSHOT = "00000003-0000001B-1"


def _shared_definition(text: str, name: str) -> str:
    match = re.search(rf'^{name}="([^\n]*)"$', text, re.M)
    assert match, f"{name} is not defined on one line"
    return match.group(1)


@pytest.mark.parametrize("name", ["DATA_TABLES_SQL", "CONSTRAINTS_SQL", "INDEXES_SQL"])
def test_backup_and_verifier_count_exactly_the_same_things(backup, verify, name):
    """If the two predicates drifted, a perfect restore would fail, and the
    tempting fix would be to loosen the check until it noticed nothing."""
    assert _shared_definition(backup, name) == _shared_definition(verify, name)


def test_the_data_table_predicate_mirrors_what_pg_dump_writes(backup):
    sql = _shared_definition(backup, "DATA_TABLES_SQL")
    assert "c.relkind = 'r'" in sql, "ordinary tables and leaf partitions; never a partitioned parent"
    assert "relpersistence <> 't'" in sql, "pg_dump writes no temporary table"
    assert "deptype = 'e'" in sql, "an extension's tables (spatial_ref_sys) are not table data"
    assert "count(*) FROM ONLY %I.%I" in backup, "COPY reads a table's own rows, not its children's"


def test_the_backup_counts_inside_the_dumps_own_snapshot(backup):
    code = "\n".join(l for l in backup.splitlines() if not l.lstrip().startswith("#"))
    assert "BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY;" in code
    assert "pg_export_snapshot()" in code
    assert '--snapshot="$SNAPSHOT_ID"' in code
    assert "SET row_security = off;" in code, "a count under RLS must error, not undercount"
    assert "SET TRANSACTION SNAPSHOT" in code, "the extract imports the same snapshot"
    # The old per-table counts outside the snapshot are gone.
    assert "count_of()" not in code


def test_excluded_tables_are_built_with_the_dump_exclusions(backup):
    """The tables the manifest leaves uncounted must be exactly the tables
    whose data pg_dump leaves out — one loop builds both."""
    loop = backup[backup.index('for t in $REBUILDABLE_TABLES; do'):]
    loop = loop[:loop.index("done")]
    assert '--exclude-table-data="public.$t"' in loop and "public.$t" in loop.split("EXCLUDED_TABLES=")[1]


# ── the backup, run against stand-ins ──────────────────────────────────────

FAKE_BACKUP_PSQL = r"""#!/usr/bin/env bash
L="$FAKE_LOG"
printf '%s\n' "$*" >> "$L.argv"
printf '%s\n' "${PGAPPNAME:-}" >> "$L.appnames"
case " $* " in
  *" -c "*)
    SQL="${@: -1}"
    case "$SQL" in
      *"SHOW server_version"*) echo "16.14" ;;
      *"CREATE ROLE"*) echo 'DO $$BEGIN CREATE ROLE oracle_app; END$$;' ;;
      *pg_auth_members*) : ;;
      *pg_terminate_backend*) : > "$L.terminated"; echo t ;;
      *) echo "unexpected -c: $SQL" >&2; exit 1 ;;
    esac ;;
  *" -f "*)
    for ((i = 1; i <= $#; i++)); do [ "${!i}" = "-f" ] && { j=$((i + 1)); F="${!j}"; }; done
    SQL="$(cat "$F")"; printf '%s\n' "$SQL" >> "$L.import"
    [ -e "$L.open" ] && echo import:session-open >> "$L.events" || echo import:session-closed >> "$L.events"
    case "$SQL" in *"COPY (SELECT"*) printf '%b' "$FAKE_TSV" ;; esac ;;
  *)
    while IFS= read -r line; do
      printf '%s\n' "$line" >> "$L.session"
      case "$line" in
        *"pg_export_snapshot()"*) echo "snapshot=$FAKE_SNAPSHOT"; : > "$L.open" ;;
        *"'backend='"*) echo "backend=4242:20261120140540123456" ;;
        *"'migration_head='"*) echo "migration_head=0999_x.sql" ;;
        *"'migration_count='"*) echo "migration_count=172" ;;
        *"'ciphertext_columns='"*) echo "ciphertext_columns=42" ;;
        *"'rls_tables='"*) echo "rls_tables=150" ;;
        *"'rls_forced_tables='"*) echo "rls_forced_tables=150" ;;
        *"'rls_policies='"*) echo "rls_policies=378" ;;
        *"'constraints_public='"*) echo "constraints_public=1054" ;;
        *"'foreign_keys_public='"*) echo "foreign_keys_public=300" ;;
        *"'foreign_keys_not_validated='"*) echo "foreign_keys_not_validated=lead_response_events.lead_response_events_command_fk" ;;
        *"'indexes_public='"*) echo "indexes_public=566" ;;
        *"'referenced_sql='"*) echo "referenced_sql=SELECT lead_id FROM property_media WHERE lead_id IS NOT NULL" ;;
        *"'extract_rows='"*) echo "source_leads=10273553"; echo "extract_rows=$FAKE_EXTRACT_ROWS" ;;
        '\gexec')
          if [ -n "$FAKE_COUNT_FAIL" ]; then echo "ERROR:  permission denied for table leads" >&2; exit 3; fi
          if [ -n "$FAKE_COUNT_BLOCK" ]; then
            # A count still running on the server: only terminating the
            # backend ends it, exactly as with real PostgreSQL.
            : > "$L.counting"
            while [ ! -e "$L.terminated" ]; do sleep 0.1; done
            echo terminated-by-backend-kill >> "$L.events"
            echo "FATAL:  terminating connection due to administrator command" >&2; exit 2
          fi
          printf '%b' "$FAKE_COUNTS" ;;
        'COMMIT;') rm -f "$L.open"; echo committed >> "$L.events" ;;
        'ROLLBACK;') rm -f "$L.open"; echo rolled-back >> "$L.events" ;;
        '\echo '*) echo "${line#\\echo }" ;;
        '\q') exit 0 ;;
      esac
    done ;;
esac
"""

FAKE_PG_DUMP = r"""#!/usr/bin/env bash
printf '%s\n' "${PGAPPNAME:-}" >> "$FAKE_LOG.appnames"
exec >/dev/null 8>&- 9<&-
printf '%s\n' "$*" >> "$FAKE_LOG.pg_dump"
[ -e "$FAKE_LOG.open" ] && echo dump-start:session-open >> "$FAKE_LOG.events" || echo dump-start:session-closed >> "$FAKE_LOG.events"
sleep "$FAKE_DUMP_SLEEP"
[ -e "$FAKE_LOG.open" ] && echo dump-end:session-open >> "$FAKE_LOG.events" || echo dump-end:session-closed >> "$FAKE_LOG.events"
for a in "$@"; do case "$a" in --file=*) printf 'PGDMP fake dump' > "${a#--file=}" ;; esac; done
exit "${FAKE_DUMP_RC:-0}"
"""

FAKE_PG_RESTORE_LIST = r"""#!/usr/bin/env bash
echo toc-listed >> "$FAKE_LOG.events"
for i in $(seq 1 60); do echo "$i; 0 0 TABLE DATA public t$i postgres"; done
"""

COUNTS = '"public.clients"\\t5\\n"public.leads"\\t1\\n"public.property_media"\\t1\\n"public.tenants"\\t2\\n"public.users"\\t4\\n'


def _backup_env(tmp_path, *, scope="full", counts=COUNTS, count_fail="", count_block="", dump_rc=0,
                dump_sleep="0.3", tsv="", extract_rows="0"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("psql", FAKE_BACKUP_PSQL), ("pg_dump", FAKE_PG_DUMP),
                       ("pg_restore", FAKE_PG_RESTORE_LIST)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    out = tmp_path / "out"
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    log = tmp_path / "fake"
    env = {k: v for k, v in _os.environ.items() if not k.startswith(("ORACLE_", "PG"))}
    env.update(PATH=f"{bin_dir}:{env['PATH']}", TMPDIR=str(tmp), ORACLE_DB_HOST="localhost",
               ORACLE_DB_NAME="neoh_src", ORACLE_DB_USER="postgres", PGPASSWORD="pw-9f8e7d6c",
               ORACLE_ENV="test", ORACLE_GIT_SHA="0123456789abcdef", ORACLE_BACKUP_SCOPE=scope,
               FAKE_LOG=str(log), FAKE_SNAPSHOT=SNAPSHOT, FAKE_COUNTS=counts, FAKE_COUNT_FAIL=count_fail,
               FAKE_DUMP_RC=str(dump_rc), FAKE_DUMP_SLEEP=dump_sleep, FAKE_TSV=tsv,
               FAKE_EXTRACT_ROWS=extract_rows, FAKE_COUNT_BLOCK=count_block)

    def read(suffix):
        path = pathlib.Path(f"{log}.{suffix}")
        return path.read_text() if path.exists() else ""

    return env, out, tmp, log, read


def _backup(tmp_path, **kwargs):
    env, out, tmp, _, read = _backup_env(tmp_path, **kwargs)
    started = _time.monotonic()
    done = _subprocess.run(["bash", str(BACKUP), str(out)], env=env, capture_output=True, text=True,
                           timeout=60)
    elapsed = _time.monotonic() - started
    manifests = sorted(out.glob("*.manifest.json"))
    manifest = _json.loads(manifests[0].read_text()) if manifests else None
    return done, manifest, read, out, tmp, elapsed


def test_the_dump_and_the_counts_share_one_snapshot_held_open_until_the_dump_ends(tmp_path):
    done, manifest, read, out, tmp, _ = _backup(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"--snapshot={SNAPSHOT}" in read("pg_dump")
    for flag in ("--format=custom", "--compress=6", "--no-owner"):
        assert flag in read("pg_dump")
    events = read("events").split()
    assert events == ["dump-start:session-open", "dump-end:session-open", "committed", "toc-listed"], (
        "held until pg_dump finished, and released before anything that does not read the "
        f"database (the table-of-contents check, the checksums): {events}")
    session = read("session")
    assert "BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY;" in session
    assert session.index("SET row_security = off;") < session.index("pg_export_snapshot()")
    assert list(tmp.iterdir()) == [], "the FIFOs' temp directory must be removed"
    assert manifest["manifest_version"] == 2 and manifest["snapshot_id"] == SNAPSHOT


def test_the_manifest_carries_every_tables_exact_count_and_the_schema_inventory(tmp_path):
    done, manifest, _, out, _, _ = _backup(tmp_path)
    assert done.returncode == 0, done.stderr
    assert manifest["table_counts"] == {"public.clients": 5, "public.leads": 1,
                                        "public.property_media": 1, "public.tenants": 2,
                                        "public.users": 4}
    assert manifest["table_counts_tables"] == 5 and manifest["table_counts_rows"] == 13
    assert manifest["excluded_tables"] == ""
    assert (manifest["constraints_public"], manifest["foreign_keys_public"],
            manifest["indexes_public"]) == ("1054", "300", "566")
    assert manifest["foreign_keys_not_validated"] == "lead_response_events.lead_response_events_command_fk"
    # The version-1 fields keep their meaning, now from the same snapshot.
    assert (manifest["expected_tenants"], manifest["expected_users"], manifest["expected_clients"],
            manifest["expected_leads"], manifest["source_rows_leads"]) == ("2", "4", "5", "1", "1")
    assert manifest["migration_head"] == "0999_x.sql" and manifest["rls_policies"] == "378"
    text = next(out.glob("*.manifest.json")).read_text()
    assert "pw-9f8e7d6c" not in text, "no credential in the manifest"


def test_an_authoritative_backup_leaves_excluded_tables_uncounted_and_extracts_in_the_snapshot(tmp_path):
    counts = '"public.clients"\\t5\\n"public.property_media"\\t1\\n"public.tenants"\\t2\\n"public.users"\\t4\\n'
    done, manifest, read, out, _, _ = _backup(tmp_path, scope="authoritative", counts=counts,
                                              tsv="row-1\\nrow-2\\n", extract_rows="2")
    assert done.returncode == 0, done.stdout + done.stderr
    assert manifest["excluded_tables"] == "public.public_property_records public.di_cache public.leads"
    assert not set(manifest["table_counts"]) & set(manifest["excluded_tables"].split())
    session = read("session")
    assert "string_to_array('public.public_property_records public.di_cache public.leads', ' ')" in session
    for t in ("public_property_records", "di_cache", "leads"):
        assert f"--exclude-table-data=public.{t}" in read("pg_dump")
    imported = read("import")
    assert f"SET TRANSACTION SNAPSHOT '{SNAPSHOT}';" in imported and "COPY (SELECT * FROM leads" in imported
    events = read("events").split()
    assert "import:session-open" in events, "the extract must import while the exporter is open"
    assert events.index("committed") > max(events.index("import:session-open"),
                                           events.index("dump-end:session-open"))
    assert events[-1] == "toc-listed", f"nothing after the release reads the database: {events}"
    assert "extract_rows=" in session and "source_leads=" in session
    assert session.index("referenced_sql=") < session.index("\\gexec"), (
        "the extract's predicate is read before the counts, so the extract runs alongside the dump")
    assert manifest["expected_leads"] == "2" and manifest["authoritative_extract_rows"] == "2"
    assert manifest["source_rows_leads"] == "10273553", "source_rows_leads keeps the source's count"
    extract = next(out.glob("*.authoritative-rows.sql")).read_text()
    assert "row-1\nrow-2\n\\.\nCOMMIT;" in extract


def test_an_extract_that_disagrees_with_the_snapshot_count_is_refused(tmp_path):
    done, manifest, _, out, _, _ = _backup(tmp_path, scope="authoritative", tsv="row-1\\n",
                                           extract_rows="2")
    assert done.returncode != 0 and "holds 1 row(s), but the snapshot" in done.stderr
    assert manifest is None and not list(out.glob("neoh-*"))


def test_a_failed_dump_releases_the_snapshot_and_leaves_no_artifact(tmp_path):
    done, manifest, read, out, tmp, _ = _backup(tmp_path, dump_rc=1)
    assert done.returncode != 0 and "pg_dump failed" in done.stderr
    assert manifest is None and list(out.iterdir()) == [], list(out.iterdir())
    # The snapshot is released as soon as pg_dump has finished, before its exit
    # status is even looked at, so a failed dump does not hold it either.
    assert read("events").split()[-1] == "committed"
    assert list(tmp.iterdir()) == []


def test_a_count_that_fails_stops_the_dump_and_leaves_no_artifact(tmp_path):
    done, manifest, read, out, tmp, elapsed = _backup(tmp_path, count_fail="yes", dump_sleep="20")
    assert done.returncode != 0 and "could not count every table" in done.stderr
    assert "permission denied for table leads" in done.stderr, "the session's own error is shown"
    assert elapsed < 15, "the running pg_dump must be stopped, not waited for"
    assert "dump-end" not in read("events")
    assert manifest is None and list(out.iterdir()) == [], list(out.iterdir())
    assert list(tmp.iterdir()) == []


def test_a_signal_during_a_running_count_terminates_the_server_backend(tmp_path):
    """Killing the psql client does not stop a count already running on the
    server: PostgreSQL notices a vanished client only when it next writes to
    it, and count(*) writes nothing until it is done — holding back VACUUM the
    whole time. The backup must end the BACKEND, by pid and start time, from a
    separate connection, and do it promptly rather than after the count."""
    import signal

    env, out, tmp, log, read = _backup_env(tmp_path, count_block="yes", dump_sleep="20")
    proc = _subprocess.Popen(["bash", str(BACKUP), str(out)], env=env, stdout=_subprocess.PIPE,
                             stderr=_subprocess.PIPE, text=True)
    try:
        deadline = _time.monotonic() + 20
        while not pathlib.Path(f"{log}.counting").exists():
            assert _time.monotonic() < deadline and proc.poll() is None, "the count never started"
            _time.sleep(0.05)
        started = _time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=20)
        elapsed = _time.monotonic() - started
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 143
    assert elapsed < 10, "SIGTERM must not wait for the count to finish"
    argv = read("argv")
    assert argv.count("pg_terminate_backend") == 1, argv
    terminate = argv[argv.index("pg_terminate_backend"):]
    # Every backend of THIS run (counting session, extract, pg_dump), matched by
    # the run's unique application_name, never the cleanup connection itself.
    names = [n for n in read("appnames").split() if n]
    run = {n for n in names if not n.endswith("-cleanup")}
    assert len(run) == 1 and next(iter(run)).startswith("neoh-backup-"), names
    assert re.fullmatch(r"neoh-backup-\d{8}T\d{6}Z-\d+-[0-9a-f]{8}", next(iter(run))), names
    assert f"{next(iter(run))}-cleanup" in names, names
    assert f"application_name = '{next(iter(run))}'" in terminate, terminate
    assert "pid <> pg_backend_pid()" in terminate, terminate
    assert "terminated-by-backend-kill" in read("events").split()
    assert "dump-end" not in read("events"), "the running pg_dump is stopped too"
    assert list(out.iterdir()) == [], list(out.iterdir())
    assert list(tmp.iterdir()) == []


# ── the verifier, run against a stand-in psql ──────────────────────────────

FAKE_VERIFY_PSQL = r"""#!/usr/bin/env bash
SQL="$*"
if [[ " $* " == *" -f - "* ]]; then
  SQL="$(cat)"; printf '%s\n' "$SQL" >> "$FAKE_LOG.stdin"
  case "$SQL" in
    *'\gexec'*) printf '%b' "$FAKE_GOT"; exit "${FAKE_GOT_RC:-0}" ;;
  esac
  exit 0
fi
case "$SQL" in
  *"max(filename)"*) echo "0999_x.sql" ;;
  *"FROM schema_migrations"*) echo "172" ;;
  *relforcerowsecurity*) echo "150" ;;
  *relrowsecurity*) echo "150" ;;
  *pg_policies*) echo "378" ;;
  *has_function_privilege*) echo "0" ;;
  *"FROM pg_proc WHERE proname"*) echo "1" ;;
  *"column_name LIKE"*) echo "" ;;
  *"data_type='bytea'"*) echo "42" ;;
  *pgcrypto*) echo "1" ;;
  *"FROM pg_authid"*) echo "0" ;;
  *"FROM pg_roles"*) echo "5" ;;
  *role_table_grants*) echo "628" ;;
  *"c.relkind='r'"*) echo "178" ;;
  *to_regclass*) echo "true" ;;
  *"FROM ONLY public."*) echo "$FAKE_EXCLUDED_ROWS" ;;
  *"NOT k.convalidated"*) echo "$FAKE_NV" ;;
  *"k.contype = 'f'"*) echo "${FAKE_FKS:-300}" ;;
  *"FROM pg_constraint k"*) echo "${FAKE_CONSTRAINTS:-1054}" ;;
  *"FROM pg_index i"*) echo "${FAKE_INDEXES:-566}" ;;
  *"FROM tenants"*) echo "2" ;;
  *"FROM users"*) echo "4" ;;
  *"FROM clients"*) echo "5" ;;
  *"FROM leads"*) echo "1" ;;
  *) echo "" ;;
esac
"""

NV = "lead_response_events.lead_response_events_command_fk"
V2 = {"manifest_version": 2, "migration_head": "0999_x.sql", "migration_count": "172",
      "ciphertext_columns": "42", "roles_count": "5", "rls_tables": "150", "rls_forced_tables": "150",
      "rls_policies": "378", "expected_tenants": "2", "expected_users": "4", "expected_clients": "5",
      "expected_leads": "1", "constraints_public": "1054", "foreign_keys_public": "300",
      "foreign_keys_not_validated": NV, "indexes_public": "566", "excluded_tables": "",
      "table_counts_tables": 3, "table_counts_rows": 7,
      "table_counts": {"public.clients": 5, "public.property_media": 1, "public.tenants": 1}}
GOT_OK = '"public.clients"\\t5\\n"public.property_media"\\t1\\n"public.tenants"\\t1\\n'


def _restricted_path(tmp_path) -> str:
    """A PATH with the tools a stock postgres image has — and no jq, no python3."""
    rbin = tmp_path / "rbin"
    rbin.mkdir()
    for tool in ("bash", "sed", "grep", "head", "tail", "seq", "basename", "awk", "cat", "env",
                 "tr", "wc", "cut"):
        found = _shutil.which(tool)
        assert found, f"{tool} not on this host"
        (rbin / tool).symlink_to(found)
    return str(rbin)


def _verify(tmp_path, manifest, *, got=GOT_OK, got_rc=0, nv=NV, excluded_rows="0", restricted=False,
            raw_manifest=None, **counts):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "psql").write_text(FAKE_VERIFY_PSQL)
    (bin_dir / "psql").chmod(0o755)
    path = tmp_path / "m.json"
    path.write_text(raw_manifest if raw_manifest is not None else _json.dumps(manifest, indent=2))
    base = _restricted_path(tmp_path) if restricted else _os.environ["PATH"]
    env = {k: v for k, v in _os.environ.items() if not k.startswith(("ORACLE_", "PG"))}
    env.update(PATH=f"{bin_dir}:{base}", ORACLE_DB_HOST="127.0.0.1", ORACLE_DB_NAME="r",
               ORACLE_DB_USER="postgres", PGPASSWORD="pw", FAKE_LOG=str(tmp_path / "psql"),
               FAKE_GOT=got, FAKE_GOT_RC=str(got_rc), FAKE_NV=nv, FAKE_EXCLUDED_ROWS=excluded_rows,
               **{f"FAKE_{k.upper()}": v for k, v in counts.items()})
    done = _subprocess.run(["bash", str(VERIFY), str(path)], env=env, capture_output=True, text=True,
                           timeout=60)
    fails = [ln.strip() for ln in done.stdout.splitlines() if ln.startswith("  FAIL  ")]
    return done, done.stdout, fails


@pytest.mark.parametrize("restricted", [False, True], ids=["jq-or-python", "stock-postgres-image"])
def test_a_complete_restore_is_verified_exhaustively(tmp_path, restricted):
    done, out, fails = _verify(tmp_path, V2, restricted=restricted)
    assert done.returncode == 0 and not fails, out
    assert "every one of the 3 tables in the backup's snapshot came back with exactly its row count" in out
    assert "Row reconciliation: EXHAUSTIVE" in out and "RESTORE VERIFIED." in out


@pytest.mark.parametrize("restricted", [False, True], ids=["jq-or-python", "stock-postgres-image"])
def test_one_row_lost_from_a_table_outside_the_old_sample_fails_and_names_it(tmp_path, restricted):
    got = GOT_OK.replace('"public.property_media"\\t1', '"public.property_media"\\t0')
    done, out, fails = _verify(tmp_path, V2, got=got, restricted=restricted)
    assert done.returncode == 1 and "DO NOT TRUST THIS RESTORE" in out
    assert fails == ["FAIL  public.property_media: 0 row(s) after the restore, the backup's snapshot had 1"], fails


def test_a_table_missing_after_restore_fails(tmp_path):
    got = GOT_OK.replace('"public.property_media"\\t1\\n', "")
    _, _, fails = _verify(tmp_path, V2, got=got)
    assert fails == ["FAIL  public.property_media: the backup had 1 row(s); the table is MISSING after the restore"]


def test_a_table_the_manifest_does_not_list_fails(tmp_path):
    _, _, fails = _verify(tmp_path, V2, got=GOT_OK + '"public.surprise"\\t9\\n')
    assert len(fails) == 1 and fails[0].startswith("FAIL  public.surprise: present after the restore (9 rows)")


def test_excluded_tables_are_not_counted_and_leads_is_held_to_the_extract(tmp_path):
    manifest = {**V2, "excluded_tables": "public.di_cache public.leads", "expected_leads": "1"}
    done, out, fails = _verify(tmp_path, manifest, excluded_rows="1")
    assert done.returncode == 0 and not fails, out
    sent = (tmp_path / "psql.stdin").read_text()
    assert "string_to_array('public.di_cache public.leads', ' ')" in sent, "excluded tables are not counted"
    assert "NOTE  public.di_cache: 1 row(s); data excluded" in out
    assert "PASS  public.leads: 1 row(s), the authoritative extract's exact count" in out


def test_a_restored_count_that_could_not_be_taken_fails(tmp_path):
    _, _, fails = _verify(tmp_path, V2, got="ERROR:  permission denied for table clients\\n", got_rc=3)
    assert fails == ["FAIL  could not count every restored table: ERROR:  permission denied for table clients"]


def test_a_manifest_the_fallback_reader_cannot_read_in_full_is_not_called_exhaustive(tmp_path):
    """No jq, no python3, and table_counts on one line: the bash reader sees
    nothing. That must FAIL — never compare zero entries and call it a match."""
    pretty = _json.dumps({k: v for k, v in V2.items() if k != "table_counts"}, indent=2)
    raw = pretty[:-2] + ',\n  "table_counts": ' + _json.dumps(V2["table_counts"]) + "\n}"
    done, out, fails = _verify(tmp_path, V2, restricted=True, raw_manifest=raw)
    assert done.returncode == 1
    assert any("read 0 of the 3 table counts" in f for f in fails), fails
    assert "Row reconciliation: EXHAUSTIVE — all" not in out


def test_a_version_2_manifest_without_counts_is_damaged_not_old(tmp_path):
    manifest = {k: v for k, v in V2.items() if not k.startswith("table_counts")}
    _, _, fails = _verify(tmp_path, manifest)
    assert any("manifest_version is '2' but there is no table_counts_tables" in f for f in fails), fails


def test_an_older_manifest_keeps_the_four_table_check_and_says_it_is_not_exhaustive(tmp_path):
    old = {k: v for k, v in V2.items()
           if k not in ("manifest_version", "table_counts", "table_counts_tables", "table_counts_rows",
                        "excluded_tables", "constraints_public", "foreign_keys_public",
                        "foreign_keys_not_validated", "indexes_public")}
    done, out, fails = _verify(tmp_path, old)
    assert done.returncode == 0 and not fails, out
    assert "Row reconciliation is NOT\n        EXHAUSTIVE" in out
    assert "Row reconciliation: NOT EXHAUSTIVE" in out
    assert "RESTORE VERIFIED, BUT NOT EXHAUSTIVELY" in out
    for table in ("tenants: 2", "users: 4", "clients: 5", "leads: 1"):
        assert f"PASS  {table}" in out


def test_an_unvalidated_foreign_key_the_source_had_validated_fails(tmp_path):
    _, _, fails = _verify(tmp_path, V2, nv=f"{NV} property_media.property_media_lead_id_fkey")
    assert fails == ["FAIL  foreign key property_media.property_media_lead_id_fkey is NOT VALIDATED after "
                     "the restore, but the source had validated it — referential integrity of its rows "
                     "is unproven"], fails


def test_the_sources_own_not_valid_foreign_keys_are_allowed_and_named(tmp_path):
    done, out, fails = _verify(tmp_path, V2)
    assert not fails and f"NOT VALID exactly as in the source: {NV}" in out


def test_a_source_with_no_unvalidated_foreign_key_passes_with_zero(tmp_path):
    _, out, fails = _verify(tmp_path, {**V2, "foreign_keys_not_validated": ""}, nv="")
    assert not fails and "every foreign key is validated" in out


def test_a_source_with_no_unvalidated_foreign_key_fails_on_any(tmp_path):
    _, _, fails = _verify(tmp_path, {**V2, "foreign_keys_not_validated": ""}, nv=NV)
    assert any(f"foreign key {NV} is NOT VALIDATED" in f for f in fails), fails


@pytest.mark.parametrize("field,label", [("constraints", "constraints"), ("fks", "foreign keys"),
                                         ("indexes", "indexes")])
def test_a_lost_constraint_or_index_fails_on_its_count(tmp_path, field, label):
    want = {"constraints": "1054", "fks": "300", "indexes": "566"}[field]
    _, _, fails = _verify(tmp_path, V2, **{field: str(int(want) - 1)})
    assert fails == [f"FAIL  {int(want) - 1} {label} in public, but the backup had {want} — the restore "
                     "did not bring the schema back whole"], fails


# Fail closed: a comparison that did not complete is a FAIL, never a gap
# between PASS and FAIL that lets an otherwise clean run print VERIFIED.

BROKEN = {
    "awk-fails": "#!/usr/bin/env bash\nexit 2\n",
    "awk-prints-nothing": "#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n",
}


@pytest.mark.parametrize("kind", sorted(BROKEN))
def test_a_comparison_that_does_not_complete_fails_the_restore(tmp_path, kind):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "awk").write_text(BROKEN[kind])
    (broken / "awk").chmod(0o755)
    real_path = _os.environ["PATH"]
    try:
        _os.environ["PATH"] = f"{broken}:{real_path}"
        done, out, fails = _verify(tmp_path, V2)
    finally:
        _os.environ["PATH"] = real_path
    assert done.returncode == 1 and "RESTORE VERIFIED" not in out, out
    assert len(fails) == 1, fails
    assert ("awk exited 2" in fails[0]) if kind == "awk-fails" else ("only none of the 3 tables" in fails[0])
    assert "Row reconciliation: EXHAUSTIVE — all" not in out


def test_a_reader_that_returns_nothing_fails_the_restore(tmp_path):
    """jq present but returning nothing (and exiting 0) must not read as an
    empty match."""
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "jq").write_text("#!/usr/bin/env bash\n"
                               "case \"$*\" in *to_entries*) exit 0 ;; esac\n"
                               f"exec {_shutil.which('jq') or 'false'} \"$@\"\n")
    (broken / "jq").chmod(0o755)
    real_path = _os.environ["PATH"]
    try:
        _os.environ["PATH"] = f"{broken}:{real_path}"
        done, out, fails = _verify(tmp_path, V2)
    finally:
        _os.environ["PATH"] = real_path
    assert done.returncode == 1 and any("read 0 of the 3 table counts" in f for f in fails), out


# ── the drill proves it on real PostgreSQL ─────────────────────────────────

def test_the_drill_backs_up_under_a_concurrent_writer_and_checks_the_snapshot():
    text = DRILL.read_text(encoding="utf-8")
    writer = text.index("WRITER_PID=$!")
    backup_run = text.index('bash -s /backups < "$HERE/backup-postgres.sh"')
    stop = text.index("VALUES (-1);")
    assert writer < backup_run < stop, "the writer must run across the whole backup"
    assert "COMMIT;" in text[text.index("DO \\$\\$ BEGIN"):writer], "each insert commits on its own"
    assert '[ "$M_WRITES" -lt "${W_AFTER:-0}" ]' in text, "rows after the snapshot must be absent"
    assert 'check "rows written during the backup" "${M_WRITES:-?}"' in text


def test_the_drill_breaks_the_restore_and_requires_the_validator_to_name_it():
    text = DRILL.read_text(encoding="utf-8")
    negative = text[text.index('phase "10. A damaged restore must fail validation"'):]
    assert "DELETE FROM property_media" in negative
    assert 'grep -q "  FAIL  public.property_media: "' in negative
    assert "NOT VALID" in negative and "DROP INDEX" in negative
    assert "Row reconciliation: EXHAUSTIVE — all" in text, "the clean restore must reconcile every table"


def test_the_drill_also_restores_and_verifies_the_other_scope():
    """CI runs the drill in `full` scope; the nightly backup runs
    `authoritative`, whose in-snapshot extract must be drilled too."""
    text = DRILL.read_text(encoding="utf-8")
    assert 'OTHER_SCOPE=authoritative' in text and '-e ORACLE_BACKUP_SCOPE="$OTHER_SCOPE"' in text
    second = text.index('"$SRC" bash -s /backups < "$HERE/backup-postgres.sh" > "$WORK/backup-other.log"')
    assert text.index("WRITER_PID=$!") < second < text.index("VALUES (-1);"), "under the same writer"
    other = text[text.index('phase "11. The $OTHER_SCOPE-scope backup: restore and validate"'):]
    assert "CREATE DATABASE neoh_restore_other" in other
    assert 'grep -q "Row reconciliation: EXHAUSTIVE — all" "$WORK/verify-other.log"' in other
    assert "the authoritative extract's exact count" in other
    assert 'check "rows written during the $OTHER_SCOPE backup"' in other
