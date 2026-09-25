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
    first one — 10,273,553 where 9 was correct."""
    manifest = backup[backup.index('cat > "$MANIFEST_PATH"'):]
    # No nested object literals in the manifest body.
    assert '": {' not in manifest, "the manifest must stay flat and unique-keyed"


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


def test_verifier_will_not_claim_verification_without_a_decrypt_round_trip(verify):
    """26 ciphertext columns come back as NULL, not as an error, when the key
    is absent. Row counts still pass. This is the quietest failure there is."""
    assert "oracle_decrypt(oracle_encrypt(" in verify
    assert "decryptability is UNPROVEN" in verify


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
