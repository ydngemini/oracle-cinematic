#!/usr/bin/env bash
# Cross-version contract (external audit 35-06): can the PREVIOUS release run
# on the schema THIS change leaves behind?
#
# That is the question backend/migration_safety.py answers by reading SQL and
# cannot settle. It matters during every deploy, not only on rollback: the
# PRE_DEPLOY job migrates before traffic switches (infra/digitalocean/app.yaml
# jobs[migrate]), so the old release serves on the new schema for a while.
#
# The classifier stays the only authority over application-only rollback
# (DEP-05). This job is an extra check that can only make things stricter.
# There is no declaration file, flag or environment variable that changes an
# outcome; docs/cross-version-contract.md explains how to read a result.
#
#   scripts/cross-version-contract.sh identify   # CI step 1: previous SHA, gap, mode
#   scripts/cross-version-contract.sh run        # CI step 2 (mode gap or canary)
#
# Local (two PostgreSQL 16 clusters reachable on localhost; never /tmp):
#   XV_PG_SUPERUSER_PASSWORD=... scripts/cross-version-contract.sh run \
#       --previous <sha> [--current <sha>] [--canary] --work <dir on SYPHER_CORE2>
#
# Modes: no_previous | no_gap (record written, exit 0) | gap | canary.
# Canary is forced whenever this script or scripts/cross_version/ changed:
# a CHECK (false) NOT VALID constraint on leads, which the classifier calls
# ADDITIVE, must come out as an attributable 23514, or the job fails with
# "the gate cannot fail".
#
# Exit codes of `run`: 3 DEPLOY_WOULD_FAIL, 2 HARNESS, 1 INCOMPATIBLE (or the
# canary was not detected), 0 otherwise (see scripts/cross_version/evaluate.py).
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
HARNESS_DIR="$(dirname "$SELF")/cross_version"
CANARY_FILE="9999_xv_canary.sql"
CANARY_SQL="ALTER TABLE leads ADD CONSTRAINT xv_canary CHECK (false) NOT VALID;"
CANDIDATE_PORT="${XV_CANDIDATE_PORT:-5432}"
CONTROL_PORT="${XV_CONTROL_PORT:-5433}"
ZERO_SHA="0000000000000000000000000000000000000000"

die() { local code=$1; shift; echo "::error::cross-version: $*" >&2; exit "$code"; }
note() { echo "cross-version: $*"; }

# ── identify ─────────────────────────────────────────────────────────────

event_field() { # key -> value of a top-level string field of the event payload
  local path="${GITHUB_EVENT_PATH:-}"
  [ -n "$path" ] && [ -f "$path" ] || return 0
  python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
v=d.get(sys.argv[2])
print(v if isinstance(v,str) else "")' "$path" "$1"
}

resolve_previous() { # cur -> prints "<sha>\t<how>" or nothing
  local cur=$1 before prev=""
  if [ -n "$PREV_ARG" ]; then
    prev="$(git rev-parse --verify --quiet "${PREV_ARG}^{commit}")" \
      || die 2 "--previous $PREV_ARG is not a commit"
    printf '%s\t%s\n' "$prev" "--previous"; return 0
  fi
  if [ "${GITHUB_EVENT_NAME:-}" = "push" ]; then
    before="$(event_field before)"
    if [[ "$before" =~ ^[0-9a-f]{40}$ ]] && [ "$before" != "$ZERO_SHA" ]; then
      if git cat-file -e "${before}^{commit}" 2>/dev/null \
         || git fetch --no-tags --depth=1 origin "$before" >/dev/null 2>&1; then
        if git cat-file -e "${before}^{commit}" 2>/dev/null; then
          printf '%s\t%s\n' "$before" "github.event.before"; return 0
        fi
      fi
    fi
  fi
  # pull_request (refs/pull/N/merge: the main tip this PR lands on),
  # workflow_dispatch, a push whose `before` is unusable, and local runs.
  prev="$(git rev-parse --verify --quiet "${cur}^1" || true)"
  [ -n "$prev" ] && printf '%s\t%s\n' "$prev" "first parent of HEAD"
  return 0
}

identify() {
  local cur prevline prev how mode canary=0 gap_count harness_changed
  cur="$(git rev-parse --verify "${CUR_ARG:-HEAD}^{commit}")"
  prevline="$(resolve_previous "$cur")"
  prev="${prevline%%$'\t'*}"; how="${prevline#*$'\t'}"
  mkdir -p "$XV_WORK" "$XV_OUT"
  if [ -z "$prev" ] || ! git cat-file -e "${prev}:backend/db/migrations" 2>/dev/null; then
    mode=no_previous; gap_count=0; harness_changed=""
    : > "$XV_WORK/gap.tsv"
  else
    # Set-based, not a filename range: added, edited and removed files all
    # count, so a late low-numbered file (0144 after 0145, 531c41d) is in it.
    git diff --no-renames --name-status "$prev" "$cur" -- \
      'backend/db/migrations/*.sql' backend/run_migrations.py > "$XV_WORK/gap.tsv"
    gap_count="$(grep -c . "$XV_WORK/gap.tsv" || true)"
    harness_changed="$(git diff --name-only "$prev" "$cur" -- \
      scripts/cross-version-contract.sh scripts/cross_version)"
    if [ -n "$harness_changed" ] || [ "$FORCE_CANARY" = 1 ]; then canary=1; fi
    if [ "$gap_count" -gt 0 ]; then mode=gap
    elif [ "$canary" = 1 ]; then mode=canary
    else mode=no_gap; fi
  fi
  python3 - "$XV_WORK/identify.json" "$mode" "$canary" "$prev" "$cur" "$how" \
    "$XV_WORK/gap.tsv" <<'PY'
import json, sys
out, mode, canary, prev, cur, how, gap_tsv = sys.argv[1:]
gap = []
for line in open(gap_tsv, encoding="utf-8"):
    if line.strip():
        status, name = line.rstrip("\n").split("\t", 1)
        gap.append({"name": name, "status": status[0]})
json.dump({"mode": mode, "canary": canary == "1", "previous_sha": prev or None,
           "current_sha": cur, "how": how if prev else None, "gap": gap},
          open(out, "w"), indent=2)
PY
  local prev7="${prev:0:7}"; [ -n "$prev7" ] || prev7=none
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
      echo "mode=$mode"; echo "canary=$([ "$canary" = 1 ] && echo true || echo false)"
      echo "prev=$prev"; echo "prev7=$prev7"; echo "sha7=${cur:0:7}"; echo "gap_count=$gap_count"
    } >> "$GITHUB_OUTPUT"
  fi
  {
    echo "### Cross-version contract: identify"
    echo ""
    echo "- mode: \`$mode\`$([ "$canary" = 1 ] && echo ' (+ canary self-test: the harness changed)')"
    echo "- previous: \`${prev:-none}\` (${how:-no previous release})"
    echo "- current: \`$cur\`"
    echo "- migration gap: $gap_count file(s)"
    sed 's/^/    /' "$XV_WORK/gap.tsv"
  } | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
  case "$mode" in
    no_previous)
      echo "::notice::cross-version: no previous release with migrations; nothing to be compatible with"
      python3 "$HARNESS_DIR/evaluate.py" trivial --result no_previous --previous "$prev" \
        --current "$cur" --reason "no previous commit with backend/db/migrations" \
        --run-id "${GITHUB_RUN_ID:-local}" --run-attempt "${GITHUB_RUN_ATTEMPT:-0}" \
        --out "$XV_OUT/cross-version-report.json" >/dev/null ;;
    no_gap)
      echo "::notice::cross-version: no migration or runner change since ${prev:0:7}; the schema is unchanged"
      python3 "$HARNESS_DIR/evaluate.py" trivial --result no_gap --previous "$prev" \
        --current "$cur" --reason "no change under backend/db/migrations/*.sql or backend/run_migrations.py" \
        --run-id "${GITHUB_RUN_ID:-local}" --run-attempt "${GITHUB_RUN_ATTEMPT:-0}" \
        --out "$XV_OUT/cross-version-report.json" >/dev/null ;;
  esac
}

# ── internal steps (each runs as its own bash process, so set -e holds) ──

psql_admin() { # port db [psql args...] — as the cluster superuser, SQL on stdin or -c
  local port=$1 db=$2; shift 2
  PGPASSWORD="$XV_PG_SUPERUSER_PASSWORD" psql -X -q -v ON_ERROR_STOP=1 \
    -h localhost -p "$port" -U postgres -d "$db" "$@"
}

head_of() { (cd "$1" && printf '%s\n' *.sql | LC_ALL=C sort | tail -n 1); }

_venvs() { # install the previous and current requirements (one venv when identical)
  local venv dir f
  install_one() {
    venv=$1; dir=$2
    python3 -m venv "$venv"
    for f in requirements.txt requirements-test.txt; do
      [ -f "$dir/$f" ] || continue
      local flags=()
      if grep -q -- '--hash=' "$dir/$f"; then flags=(--require-hashes); fi
      "$venv/bin/python" -m pip install --quiet --disable-pip-version-check "${flags[@]}" -r "$dir/$f" \
        || { note "retrying pip install -r $f once"; \
             "$venv/bin/python" -m pip install --quiet --disable-pip-version-check "${flags[@]}" -r "$dir/$f"; }
    done
  }
  install_one "$(dirname "$(dirname "$XV_PREV_PY")")" "$XV_PREV_ROOT/backend"
  if [ "$XV_NEW_PY" != "$XV_PREV_PY" ]; then
    install_one "$(dirname "$(dirname "$XV_NEW_PY")")" "$XV_CUR_ROOT/backend"
  fi
}

_build_db() { # port db — the previous schema, migrated as a superuser, then handed to the doadmin stand-in
  local port=$1 db=$2 log="$XV_LDIR/build-$2.log" i
  for i in $(seq 1 30); do pg_isready -q -h localhost -p "$port" && break; sleep 2; done
  psql_admin "$port" postgres -v su_pw="$XV_SU_PW" -v do_pw="$XV_DOADMIN_PW" -v db="$db" <<'SQL'
SELECT 'CREATE ROLE xv_su' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xv_su') \gexec
SELECT 'CREATE ROLE xv_doadmin' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xv_doadmin') \gexec
ALTER ROLE xv_su LOGIN SUPERUSER PASSWORD :'su_pw';
-- DigitalOcean's doadmin profile (docs/staging-setup.md): not a superuser,
-- bypasses RLS, owns the tables, creates roles.
ALTER ROLE xv_doadmin LOGIN NOSUPERUSER BYPASSRLS CREATEROLE CREATEDB PASSWORD :'do_pw';
DROP DATABASE IF EXISTS :"db" WITH (FORCE);
CREATE DATABASE :"db" OWNER xv_su;
SQL
  # The PREVIOUS runner over the PREVIOUS migrations rebuilds the schema the
  # previous release ran on.
  (cd "$XV_PREV_ROOT/backend" && env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 \
    ORACLE_DB_HOST=localhost ORACLE_DB_PORT="$port" ORACLE_DB_NAME="$db" ORACLE_DB_SSL=disable \
    ORACLE_DB_ADMIN_USER=xv_su ORACLE_DB_ADMIN_PASSWORD="$XV_SU_PW" \
    ORACLE_MIGRATIONS_DIR=db/migrations ORACLE_SKIP_DOTENV=1 \
    ORACLE_DB_APP_PASSWORD="$XV_APP_PW" ORACLE_DB_PLATFORM_PASSWORD="$XV_PLATFORM_PW" \
    "$XV_PREV_PY" run_migrations.py) > "$log" 2>&1 \
    || { tail -n 40 "$log"; die 1 "the previous migrations did not apply to $db as a superuser"; }
  tail -n 2 "$log"
  # Hand everything to the stand-in, as on DigitalOcean where doadmin created
  # it all. PostgreSQL 16 gives a CREATEROLE creator ADMIN on the roles it
  # creates (not INHERIT, not SET); grant exactly that for every role a
  # migration created.
  #
  # Default privileges first: REASSIGN OWNED does not move them and DROP
  # OWNED deletes them, yet on DigitalOcean the ALTER DEFAULT PRIVILEGES in
  # 0001 and 0037 belong to doadmin, so a table a gap migration creates is
  # granted to oracle_app automatically. Without this copy the NEW app would
  # fail on such a table here and nowhere else. Schema-scoped entries only add
  # grants, so re-granting them is exact; a global entry (none today) also
  # removes defaults and is refused rather than approximated.
  psql_admin "$port" "$db" -v db="$db" <<'SQL'
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_default_acl
              WHERE defaclrole = 'xv_su'::regrole AND defaclnamespace = 0) THEN
    RAISE EXCEPTION 'cross-version: a global ALTER DEFAULT PRIVILEGES is not copied to the doadmin stand-in; extend _build_db';
  END IF;
END $$;
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE xv_doadmin IN SCHEMA %I GRANT %s ON %s TO %s',
              n.nspname, string_agg(a.privilege_type, ', ' ORDER BY a.privilege_type),
              CASE d.defaclobjtype WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES'
                   WHEN 'f' THEN 'FUNCTIONS' WHEN 'T' THEN 'TYPES' END,
              CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE quote_ident(g.rolname) END)
  FROM pg_default_acl d
  JOIN pg_namespace n ON n.oid = d.defaclnamespace
 CROSS JOIN LATERAL aclexplode(d.defaclacl) a
  LEFT JOIN pg_roles g ON g.oid = a.grantee
 WHERE d.defaclrole = 'xv_su'::regrole
 GROUP BY n.nspname, d.defaclobjtype, a.grantee, g.rolname \gexec
REASSIGN OWNED BY xv_su TO xv_doadmin;
DROP OWNED BY xv_su;
SELECT format('GRANT %I TO xv_doadmin WITH ADMIN TRUE, INHERIT FALSE, SET FALSE', rolname)
  FROM pg_roles
 WHERE rolname !~ '^pg_' AND rolname NOT IN ('postgres', 'xv_su', 'xv_doadmin') \gexec
COMMENT ON DATABASE :"db" IS 'neoh-environment=ci';
SQL
  note "$db on :$port: previous schema $(head_of "$XV_PREV_ROOT/backend/db/migrations"), owned by xv_doadmin"
}

_predeploy() { # port db migrations-dir — the staging PRE_DEPLOY job, as the doadmin stand-in
  local port=$1 db=$2 migdir=$3 log="$XV_LDIR/deploy-rehearsal-$2.log" rc=0
  # BEGIN PRE_DEPLOY ENV — infra/digitalocean/app.yaml jobs[migrate] envs and
  # run_command. ORACLE_DB_APP_PASSWORD is deliberately absent, as there.
  # backend/tests/test_cross_version_wiring.py compares the two.
  (cd "$XV_CUR_ROOT/backend" && env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 \
    ORACLE_EXPECTED_ENVIRONMENT=ci \
    ORACLE_DB_HOST=localhost \
    ORACLE_DB_PORT="$port" \
    ORACLE_DB_NAME="$db" \
    ORACLE_DB_SSL=disable \
    ORACLE_DB_ADMIN_USER=xv_doadmin \
    ORACLE_DB_ADMIN_PASSWORD="$XV_DOADMIN_PW" \
    ORACLE_DB_PLATFORM_PASSWORD="$XV_PLATFORM_PW" \
    ORACLE_MIGRATIONS_DIR="$migdir" \
    "$XV_NEW_PY" run_migrations.py --precheck-then-migrate) > "$log" 2>&1 || rc=$?
  # END PRE_DEPLOY ENV
  { grep -v "(recorded, skip)" "$log" || true; } | tail -n 25
  # The exit code is the verdict (3 refused, 4 drift, 1 an SQL error such as
  # 42501); the log is never grepped for words.
  exit "$rc"
}

_boot() { # name prev|new port db expected-head
  local name=$1 which=$2 port=$3 db=$4 head=$5 py root
  if [ "$which" = prev ]; then py="$XV_PREV_PY"; root="$XV_PREV_ROOT/backend"
  else py="$XV_NEW_PY"; root="$XV_CUR_ROOT/backend"; fi
  env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 \
    ORACLE_DB_PASSWORD="$XV_APP_PW" ORACLE_DB_PLATFORM_PASSWORD="$XV_PLATFORM_PW" \
    "$py" "$HARNESS_DIR/boot_probe.py" --name "$name" --app-dir "$root" --python "$py" \
      --port "$port" --db "$db" --expect-head "$head" \
      --out "$XV_LDIR/boot-$name.json" --log-dir "$XV_LDIR"
}

_suite() { # port db out-prefix [nodeid-list] — the PREVIOUS live suite, from the previous tree
  local port=$1 db=$2 prefix=$3 list=${4:-} rc=0 targets=() extra=()
  cd "$XV_PREV_ROOT/backend"
  if [ -n "${TMPDIR:-}" ]; then extra=(TMPDIR="$TMPDIR"); fi
  if [ -n "$list" ]; then mapfile -t targets < "$list"
  else shopt -s nullglob; targets=(tests/test_*_live.py); shopt -u nullglob; fi
  # test_migration_precheck.py is left out on purpose: an older release is
  # built to refuse a newer database (run_migrations.py evaluate_precheck).
  [ "${#targets[@]}" -gt 0 ] || { note "no previous live tests"; exit 5; }
  rm -f "$prefix.jsonl"
  env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 "${extra[@]}" \
    PYTHONPATH="$HARNESS_DIR" XV_PREVIOUS_BACKEND="$XV_PREV_ROOT/backend" XV_RESULTS="$prefix.jsonl" \
    ORACLE_LIVE_DB_ADMIN_DSN="postgresql://postgres:${XV_PG_SUPERUSER_PASSWORD}@localhost:${port}/${db}" \
    ORACLE_DB_HOST=localhost ORACLE_DB_PORT="$port" ORACLE_DB_NAME="$db" \
    ORACLE_DB_PASSWORD="$XV_APP_PW" ORACLE_DB_PLATFORM_PASSWORD="$XV_PLATFORM_PW" \
    ORACLE_DB_SSL=disable ORACLE_DB_SSLMODE=disable ORACLE_ENV=dev \
    ORACLE_SKIP_DOTENV=1 LITELLM_MODE=PRODUCTION \
    "$XV_PREV_PY" -m pytest -q -p no:cacheprovider -p xv_pytest_plugin --rootdir=. \
      --junitxml="$prefix.xml" "${targets[@]}" 2>&1 | tee "$prefix.log" || rc=$?
  exit "$rc"
}

# ── run ──────────────────────────────────────────────────────────────────

gen_secret() {
  local value
  value="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  [ "${#value}" -eq 32 ] || die 2 "could not generate a password"
  printf '%s' "$value"
}

LAST_RC=0
run_step() { # name internal-subcommand args... — records the exit code, never aborts
  local name=$1; shift
  LAST_RC=0
  echo "::group::cross-version $XV_LABEL: $name"
  "$BASH" "$SELF" "$@" || LAST_RC=$?
  echo "::endgroup::"
  printf '{"step": "%s", "exit": %d}\n' "$name" "$LAST_RC" >> "$XV_LDIR/steps.jsonl"
  [ "$LAST_RC" -eq 0 ] || note "$name exited $LAST_RC"
}

contract() { # label canary(0|1) — one complete run; prints nothing secret; returns evaluate's code
  # Called as `contract ... || rc=$?`, so bash ignores set -e in here: every
  # command that matters checks its own status.
  local label=$1 canary=$2 sfx="" newmig db_c db_r db_b prev_head new_head report rc=0
  local summary=()
  export XV_LABEL="$label" XV_LDIR="$XV_OUT/$label"
  rm -rf "$XV_LDIR" && mkdir -p "$XV_LDIR" || return 2
  newmig="$XV_CUR_ROOT/backend/db/migrations"
  report="$XV_OUT/cross-version-report.json"
  if [ "$canary" = 1 ]; then
    sfx="_k"; newmig="$XV_WORK/canary-migrations"; report="$XV_OUT/cross-version-canary-report.json"
    rm -rf "$newmig" && cp -a "$XV_CUR_ROOT/backend/db/migrations" "$newmig" || return 2
    printf -- '-- cross-version canary (audit 35-06): ADDITIVE to the classifier, breaks every old insert into leads\n%s\n' \
      "$CANARY_SQL" > "$newmig/$CANARY_FILE" || return 2
  fi
  db_c="xv_candidate$sfx"; db_r="xv_candidate${sfx}_r"; db_b="xv_control$sfx"
  python3 - "$XV_WORK/identify.json" "$XV_LDIR" "$label" "$canary" "$newmig" "$CANARY_FILE" \
    "${GITHUB_RUN_ID:-local}" "${GITHUB_RUN_ATTEMPT:-0}" "$XV_PREV_ROOT/backend/db/migrations" \
    "$XV_PGVERSION" <<'PY'
import json, os, sys
ident, ldir, label, canary, newmig, canary_file, run_id, attempt, prev_mig, pgver = sys.argv[1:]
data = json.load(open(ident))
gap = list(data["gap"])
if canary == "1":
    gap.append({"name": canary_file, "status": "A", "synthetic": True,
                "path": os.path.join(newmig, canary_file)})
json.dump(gap, open(os.path.join(ldir, "gap.json"), "w"), indent=2)
json.dump({"label": label, "canary": canary == "1", "previous_sha": data["previous_sha"],
           "current_sha": data["current_sha"], "run_id": run_id, "run_attempt": attempt,
           "pg_server_version": pgver, "prev_migrations": prev_mig, "new_migrations": newmig},
          open(os.path.join(ldir, "meta.json"), "w"), indent=2)
PY
  [ -s "$XV_LDIR/meta.json" ] && [ -s "$XV_LDIR/gap.json" ] || return 2
  printf '{"step": "venv", "exit": %d}\n' "$XV_VENV_RC" >> "$XV_LDIR/steps.jsonl" || return 2
  if [ "$XV_VENV_RC" -eq 0 ]; then
    run_step build_control _build_db "$CONTROL_PORT" "$db_b"
    local control_rc=$LAST_RC
    run_step build_candidate _build_db "$CANDIDATE_PORT" "$db_c"
    if [ "$control_rc" -eq 0 ] && [ "$LAST_RC" -eq 0 ]; then
      run_step deploy_rehearsal _predeploy "$CANDIDATE_PORT" "$db_c" "$newmig"
      if [ "$LAST_RC" -eq 0 ]; then
        prev_head="$(head_of "$XV_PREV_ROOT/backend/db/migrations")"
        new_head="$(head_of "$newmig")"
        run_step "boot_P@B" _boot "P@B" prev "$CONTROL_PORT" "$db_b" "$prev_head"
        run_step "boot_P@C" _boot "P@C" prev "$CANDIDATE_PORT" "$db_c" "$new_head"
        run_step "boot_N@C" _boot "N@C" new "$CANDIDATE_PORT" "$db_c" "$new_head"
        run_step suite _suite "$CANDIDATE_PORT" "$db_c" "$XV_LDIR/run1"
        python3 -c 'import json,sys
seen=[]
for line in open(sys.argv[1]):
    row=json.loads(line)
    if row.get("outcome")=="failed" and row["nodeid"] not in seen: seen.append(row["nodeid"])
print("\n".join(seen))' "$XV_LDIR/run1.jsonl" > "$XV_LDIR/failing.txt" 2>/dev/null || : > "$XV_LDIR/failing.txt"
        if [ -s "$XV_LDIR/failing.txt" ]; then
          note "$(grep -c . "$XV_LDIR/failing.txt") previous test(s) failed; attributing on a fresh candidate and on the control"
          run_step build_candidate_r _build_db "$CANDIDATE_PORT" "$db_r"
          if [ "$LAST_RC" -eq 0 ]; then
            run_step deploy_rehearsal_r _predeploy "$CANDIDATE_PORT" "$db_r" "$newmig"
            if [ "$LAST_RC" -eq 0 ]; then
              run_step rerun_candidate _suite "$CANDIDATE_PORT" "$db_r" "$XV_LDIR/rerun-candidate" "$XV_LDIR/failing.txt"
              run_step rerun_control _suite "$CONTROL_PORT" "$db_b" "$XV_LDIR/rerun-control" "$XV_LDIR/failing.txt"
            fi
          fi
        fi
      fi
    fi
  fi
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then summary=(--summary "$GITHUB_STEP_SUMMARY"); fi
  python3 "$HARNESS_DIR/evaluate.py" record --work "$XV_LDIR" --repo "$XV_CUR_ROOT" \
    --out "$report" "${summary[@]}" || rc=$?
  return "$rc"
}

scrub_outputs() { # replace every generated or configured password in the artifact files
  [ -d "${XV_OUT:-}" ] || return 0
  XV_SCRUB="${XV_SU_PW:-} ${XV_DOADMIN_PW:-} ${XV_APP_PW:-} ${XV_PLATFORM_PW:-} ${XV_PG_SUPERUSER_PASSWORD:-}" \
    python3 - "$XV_OUT" <<'PY' || true
import os, sys
secrets = [s for s in os.environ.get("XV_SCRUB", "").split() if len(s) >= 4]
for root, _dirs, files in os.walk(sys.argv[1]):
    for name in files:
        path = os.path.join(root, name)
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        clean = text
        for value in secrets:
            clean = clean.replace(value, "***")
        if clean != text:
            open(path, "w", encoding="utf-8").write(clean)
PY
}

cleanup() {
  local rc=$?
  scrub_outputs
  for tree in "$XV_WORK/prev" "$XV_WORK/cur"; do
    [ -d "$tree" ] && git -C "$XV_REPO" worktree remove --force "$tree" >/dev/null 2>&1 || true
  done
  exit "$rc"
}

run() {
  local mode canary prev cur same normal_rc=0 canary_rc=0 v state
  if [ "$EXPLICIT" = 1 ] || [ ! -f "$XV_WORK/identify.json" ]; then identify > /dev/null; fi
  state="$(python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
print(d["mode"], int(d["canary"]), d["previous_sha"] or "-", d["current_sha"])' "$XV_WORK/identify.json")" \
    || die 2 "unreadable $XV_WORK/identify.json"
  read -r mode canary prev cur <<< "$state"
  case "$mode" in
    gap|canary) ;;
    no_gap|no_previous) note "nothing to run (mode $mode)"; return 0 ;;
    *) die 2 "unknown mode '$mode' in identify.json" ;;
  esac
  [[ "${XV_PG_SUPERUSER_PASSWORD:-}" =~ ^[A-Za-z0-9._~-]{4,}$ ]] \
    || die 2 "XV_PG_SUPERUSER_PASSWORD (the clusters' postgres password) is unset or not URL-safe"
  # Fresh per run, never printed: the migration superuser, the doadmin
  # stand-in, and the two application logins.
  XV_SU_PW="$(gen_secret)"; XV_DOADMIN_PW="$(gen_secret)"
  XV_APP_PW="$(gen_secret)"; XV_PLATFORM_PW="$(gen_secret)"
  export XV_SU_PW XV_DOADMIN_PW XV_APP_PW XV_PLATFORM_PW
  if [ "${GITHUB_ACTIONS:-}" = true ]; then
    for v in "$XV_SU_PW" "$XV_DOADMIN_PW" "$XV_APP_PW" "$XV_PLATFORM_PW" "$XV_PG_SUPERUSER_PASSWORD"; do
      echo "::add-mask::$v"
    done
  fi
  trap cleanup EXIT
  export XV_PREV_ROOT="$XV_WORK/prev" XV_CUR_ROOT="$XV_REPO"
  git -C "$XV_REPO" worktree remove --force "$XV_PREV_ROOT" >/dev/null 2>&1 || rm -rf "$XV_PREV_ROOT"
  git -C "$XV_REPO" worktree add --detach "$XV_PREV_ROOT" "$prev" >/dev/null
  if [ "$cur" != "$(git -C "$XV_REPO" rev-parse HEAD)" ]; then
    XV_CUR_ROOT="$XV_WORK/cur"
    git -C "$XV_REPO" worktree remove --force "$XV_CUR_ROOT" >/dev/null 2>&1 || rm -rf "$XV_CUR_ROOT"
    git -C "$XV_REPO" worktree add --detach "$XV_CUR_ROOT" "$cur" >/dev/null
  fi
  same=0
  if cmp -s "$XV_PREV_ROOT/backend/requirements.txt" "$XV_CUR_ROOT/backend/requirements.txt" \
     && cmp -s "$XV_PREV_ROOT/backend/requirements-test.txt" "$XV_CUR_ROOT/backend/requirements-test.txt"; then
    same=1
  fi
  if [ "$same" = 1 ]; then
    export XV_PREV_PY="$XV_WORK/venv-shared/bin/python" XV_NEW_PY="$XV_WORK/venv-shared/bin/python"
  else
    export XV_PREV_PY="$XV_WORK/venv-prev/bin/python" XV_NEW_PY="$XV_WORK/venv-new/bin/python"
  fi
  export XV_LABEL=setup XV_LDIR="$XV_OUT/setup"
  mkdir -p "$XV_LDIR"
  run_step venv _venvs
  export XV_VENV_RC=$LAST_RC
  export XV_PGVERSION
  XV_PGVERSION="$(psql_admin "$CANDIDATE_PORT" postgres -Atc 'SHOW server_version' 2>/dev/null || echo unknown)"
  if [ "$mode" = gap ]; then contract gap 0 || normal_rc=$?; fi
  if [ "$canary" = 1 ]; then contract canary 1 || canary_rc=$?; fi
  note "result: gap run exit $normal_rc, canary exit $canary_rc"
  [ "$normal_rc" -ge "$canary_rc" ] && return "$normal_rc"
  return "$canary_rc"
}

# ── entry ────────────────────────────────────────────────────────────────

usage() { sed -n '2,30p' "$SELF"; }

COMMAND="${1:-}"; [ $# -gt 0 ] && shift
case "$COMMAND" in
  _venvs|_build_db|_predeploy|_boot|_suite)
    : "${XV_LDIR:?internal step run outside cross-version-contract.sh run}"
    "$COMMAND" "$@"; exit $? ;;
  identify|run) ;;
  -h|--help|help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

PREV_ARG=""; CUR_ARG=""; FORCE_CANARY=0; WORK_ARG=""; EXPLICIT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --previous) PREV_ARG="${2:?}"; EXPLICIT=1; shift 2 ;;
    --current) CUR_ARG="${2:?}"; EXPLICIT=1; shift 2 ;;
    --canary) FORCE_CANARY=1; EXPLICIT=1; shift ;;
    --work) WORK_ARG="${2:?}"; shift 2 ;;
    *) die 2 "unknown argument $1" ;;
  esac
done

export XV_REPO
XV_REPO="$(git rev-parse --show-toplevel)"
cd "$XV_REPO"
if [ -n "$WORK_ARG" ]; then XV_WORK="$WORK_ARG"
elif [ -n "${RUNNER_TEMP:-}" ]; then XV_WORK="$RUNNER_TEMP/xv"
else die 2 "--work <dir> is required outside GitHub Actions"; fi
mkdir -p "$XV_WORK"
XV_WORK="$(cd "$XV_WORK" && pwd)"
export XV_WORK XV_OUT="$XV_WORK/out"

case "$COMMAND" in
  identify) identify ;;
  run) run ;;
esac
