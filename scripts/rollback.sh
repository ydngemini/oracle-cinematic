#!/usr/bin/env bash
#
# Neoh — application-only rollback to a previously deployed artifact.
#
# It redeploys a previous IMAGE DIGEST. It never rebuilds old source, and it
# never touches the database.
#
# The decision it enforces
# ────────────────────────
# Rolling the app back is only safe if the PREVIOUS release can run against the
# schema the CURRENT release left behind. That is not the same question as "did
# the migration work" — a migration can apply perfectly and still make rollback
# impossible, by dropping a column the old code still SELECTs or adding a NOT
# NULL the old code never writes.
#
#   additive migrations   → roll the app back, LEAVE the migration in place.
#                           This is the preferred pattern and should be the
#                           common case. Down-migrations are not attempted.
#
#   destructive migration → APPLICATION ROLLBACK UNSAFE. This script refuses.
#                           Recovery is a corrective forward migration, a PITR
#                           restore, or an emergency compatibility patch — see
#                           docs/disaster-recovery-state-map.md. Redeploying the
#                           old app onto an incompatible schema turns one broken
#                           release into a broken release AND a broken database.
#
# It will not restore a database. Ever. That decision belongs to a human
# holding the disaster-recovery runbook, not to a rollback script running
# unattended at 3am.
#
# Usage:
#   scripts/rollback.sh --to <release-manifest.json> [--from <current-manifest.json>] [--apply]
#
# Without --apply it prints exactly what it would do and changes nothing.
# That is the default on purpose.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

TO=""; FROM=""; APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --to)    TO="${2:-}"; shift 2 ;;
    --from)  FROM="${2:-}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

die()  { printf '\n  \033[31mERROR\033[0m  %s\n\n' "$*" >&2; exit 1; }
say()  { printf '  %s\n' "$*"; }
head2(){ printf '\n\033[1m%s\033[0m\n' "$*"; }

[ -n "$TO" ] || die "--to <release-manifest.json> is required.

  A rollback target is decided BEFORE an incident, not discovered during one.
  CI uploads a release manifest for every deploy; download the one for the
  release you want to return to."
[ -f "$TO" ] || die "no such manifest: $TO"

m() {
  local file="$1" key="$2"
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg k "$key" '.[$k] // empty' "$file" 2>/dev/null && return
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "
import json,sys
print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$file" "$key" 2>/dev/null && return
  fi
  sed -n "s/^[[:space:]]*\"${key}\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",]*\)\"\{0,1\},\{0,1\}[[:space:]]*$/\1/p" \
    "$file" | head -1
}

TARGET_SHA="$(m "$TO" release_id)"
TARGET_BACKEND="$(m "$TO" backend_digest)"
TARGET_FRONTEND="$(m "$TO" frontend_digest)"
TARGET_HEAD="$(m "$TO" migration_head)"

[ -n "$TARGET_SHA" ]      || die "could not read release_id from $TO — is it a release manifest?"
[ -n "$TARGET_BACKEND" ]  || die "the target manifest records no backend digest, so there is
  nothing immutable to roll back TO. Rebuilding old source would produce a
  different artifact and defeat the point."
[ -n "$TARGET_FRONTEND" ] || die "the target manifest records no frontend digest"

head2 "Rollback target"
say "release   $TARGET_SHA"
say "backend   $TARGET_BACKEND"
say "frontend  $TARGET_FRONTEND"
say "schema    $TARGET_HEAD (the release EXPECTED this head)"

# ── Is an application-only rollback safe? ──────────────────────────────────
#
# Every migration introduced AFTER the target release has to be additive. One
# destructive migration in the gap is enough: the old app cannot run against
# what it left behind.

head2 "Migration compatibility"

CURRENT_HEAD=""
[ -n "$FROM" ] && [ -f "$FROM" ] && CURRENT_HEAD="$(m "$FROM" migration_head)"
if [ -z "$CURRENT_HEAD" ]; then
  CURRENT_HEAD="$(ls "$REPO"/backend/db/migrations/*.sql 2>/dev/null | xargs -n1 basename | sort | tail -1)"
  say "current head taken from the working tree: $CURRENT_HEAD"
  say "(pass --from <current-manifest.json> to use the deployed release instead)"
fi

VERDICT_JSON="$(
  cd "$REPO/backend" && python3 - "$TARGET_HEAD" "$CURRENT_HEAD" <<'PY' 2>/dev/null
import json, pathlib, sys
sys.path.insert(0, ".")
from migration_safety import classify_files, rollback_is_safe

target_head, current_head = sys.argv[1], sys.argv[2]
paths = sorted(pathlib.Path("db/migrations").glob("*.sql"))
# Everything strictly AFTER the target's head, up to and including the current.
gap = [p for p in paths if target_head < p.name <= current_head]
verdicts = classify_files(gap)
safe, blocking = rollback_is_safe(verdicts)
print(json.dumps({
    "count": len(gap),
    "safe": safe,
    "blocking": blocking,
    "files": [p.name for p in gap],
}))
PY
)"

[ -n "$VERDICT_JSON" ] || die "could not classify the migrations between the two releases.
  Refusing to guess: an unclassified gap is not a safe gap."

GAP_COUNT="$(printf '%s' "$VERDICT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["count"])')"
SAFE="$(printf '%s' "$VERDICT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["safe"])')"

if [ "$GAP_COUNT" = "0" ]; then
  say "no migrations were applied after $TARGET_SHA — nothing to be incompatible with"
else
  say "$GAP_COUNT migration(s) applied since the target release"
fi

if [ "$SAFE" != "True" ]; then
  printf '\n  \033[31m╔════════════════════════════════════════════════╗\033[0m\n'
  printf '  \033[31m║   APPLICATION ROLLBACK UNSAFE                  ║\033[0m\n'
  printf '  \033[31m╚════════════════════════════════════════════════╝\033[0m\n\n'
  printf '%s' "$VERDICT_JSON" | python3 -c '
import json,sys
for line in json.load(sys.stdin)["blocking"]:
    print("    " + line)'
  cat <<'EXPLAIN'

  The previous release cannot run against the schema now in place. Redeploying
  it would turn one broken release into a broken release AND a broken database.

  This script will not proceed. The recovery options are:

    1. CORRECTIVE FORWARD MIGRATION — usually fastest and safest. Fix forward
       with a new release rather than going backwards.
    2. EMERGENCY COMPATIBILITY PATCH — restore the dropped column/constraint so
       the old app can run, then roll back.
    3. POINT-IN-TIME RESTORE — operator-run, and it loses every write since.
       See docs/disaster-recovery-state-map.md. DigitalOcean's PITR window is
       7 days and a restore creates a NEW cluster that must be repointed.

  None of these is automated, deliberately. Each trades something different
  away, and the trade is a human's to make.

EXPLAIN
  exit 3
fi

say "all migrations in the gap are additive — the previous release can run"
say "the migrations STAY in place; no down-migration is attempted"

# ── Do it ──────────────────────────────────────────────────────────────────

head2 "Plan"
say "pin api + worker  -> neoh-backend@$TARGET_BACKEND"
say "pin web           -> neoh-frontend@$TARGET_FRONTEND"
say "database          -> UNTOUCHED"

SPEC_OUT="${ORACLE_ROLLBACK_SPEC:-/tmp/app.rollback.yaml}"
sed -e "s|__BACKEND_DIGEST__|$TARGET_BACKEND|g" \
    -e "s|__FRONTEND_DIGEST__|$TARGET_FRONTEND|g" \
    "$REPO/infra/digitalocean/app.yaml" > "$SPEC_OUT"

if grep -q "__.*_DIGEST__" "$SPEC_OUT"; then
  die "a digest placeholder survived substitution in $SPEC_OUT"
fi
say "spec written      -> $SPEC_OUT"

if [ "$APPLY" -ne 1 ]; then
  cat <<EOF

  Nothing has been changed. To apply:

      scripts/rollback.sh --to $TO --apply

  Requires DIGITALOCEAN_APP_ID and an authenticated doctl.

EOF
  exit 0
fi

: "${DIGITALOCEAN_APP_ID:?DIGITALOCEAN_APP_ID is required to apply a rollback}"
command -v doctl >/dev/null 2>&1 || die "doctl is not installed"

head2 "Applying"
# `doctl apps update`, not the rollback API. The rollback endpoint PINS the app
# — blocking every subsequent deploy until someone commits or reverts it — and
# that pin is a second incident waiting to happen when the fix-forward release
# is ready and will not deploy. Applying a digest-pinned spec reaches the same
# artifact and leaves the app deployable.
doctl apps update "$DIGITALOCEAN_APP_ID" --spec "$SPEC_OUT" --wait || \
  die "the rollback deploy failed. The previous release is still live."

head2 "Verify"
say "run the smoke test against the rolled-back release:"
say "    APP_URL=<url> EXPECTED_GIT_SHA=$TARGET_SHA infra/digitalocean/smoke-test.sh"
echo
