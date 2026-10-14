#!/usr/bin/env bash
# Tenant-wall plans before/after 0141 (ledger L16-01, audit A10).
#
# EXPLAIN (ANALYZE, BUFFERS) of hot request queries on the LOCAL DEV DB, as the
# real request login (oracle_app_login; platform admin on oracle_platform_login),
# under the 0141 policies and — flipped back inside the same transaction — the
# old `app_is_platform_admin() OR tenant_id = app_current_tenant()` form.
# Everything runs in one transaction that is rolled back: a throwaway brokerage
# with 21,000 contacts is seeded, nothing is kept.
#
#   performance/rls_tenant_wall_plans.sh            # summary table
#   RAW=1 performance/rls_tenant_wall_plans.sh      # full plans
#
# Needs 0141 applied. SMALL/BIG name an existing small and large brokerage and
# one of each one's users (defaults: the dev DB's perf seeds). The leads query
# is capped at 5 s per run (the old form does not finish for a small brokerage).
# Local dev numbers only: one machine, shared with other work, no concurrency.
set -euo pipefail
export DOCKER_HOST="${DOCKER_HOST:-unix:///media/ydn/SYPHER_CORE2/runpod-docker-socket/docker.sock}"
DB_CONTAINER="${DB_CONTAINER:-oracle-db-1}"
SMALL="${SMALL:-d711f206-aa0d-5578-8bff-fb946cc221c0}"; SMALL_AGENT="${SMALL_AGENT:-perf-solo-03-a000@perf.invalid}"
BIG="${BIG:-2c0b4a3b-016d-5446-b9c5-c531dbb9b41f}";     BIG_AGENT="${BIG_AGENT:-perf-large-01-a002@perf.invalid}"
BIG_OWNER="${BIG_OWNER:-perf-large-01-a000@perf.invalid}"
psql_q() { docker exec -i "$DB_CONTAINER" psql -U postgres -d oracle -X -q "$@"; }

SMALL_CLIENT=$(psql_q -Atc "SELECT client_id FROM client_activities WHERE tenant_id='$SMALL' GROUP BY 1 ORDER BY count(*) DESC LIMIT 1")
BIG_CLIENT=$(psql_q -Atc "SELECT client_id FROM client_activities WHERE tenant_id='$BIG' GROUP BY 1 ORDER BY count(*) DESC LIMIT 1")

contexts=(
  "small agent|oracle_app_login|$SMALL|agent|$SMALL_AGENT|$SMALL_CLIENT"
  "big agent|oracle_app_login|$BIG|agent|$BIG_AGENT|$BIG_CLIENT"
  "big owner|oracle_app_login|$BIG|broker_owner|$BIG_OWNER|$BIG_CLIENT"
  "platform admin|oracle_platform_login|00000000-0000-0000-0000-000000000000|platform_admin|ops|$BIG_CLIENT"
  "forged admin GUC|oracle_app_login|$SMALL|platform_admin|forged|$SMALL_CLIENT"
)
PHONE="'ffff000000000000000000000000000000000000000000000000000000000000'"
E="EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON)"

queries() {  # $1 phase label
  for c in "${contexts[@]}"; do
    IFS='|' read -r label login tenant role agent client <<<"$c"
    setctx="SET LOCAL SESSION AUTHORIZATION $login; SELECT set_config('app.current_tenant','$tenant',true) a, set_config('app.current_role','$role',true) b, set_config('app.current_agent','$agent',true) c, set_config('app.record_scope','actor',true) d \\gset x_"
    cat <<SQL
\\echo ===== $1 / $label
$setctx
\\echo -- client count
$E SELECT count(*) FROM clients;
\\echo -- client page
$E SELECT id, full_name, lead_score FROM clients WHERE archived_at IS NULL ORDER BY lead_score DESC LIMIT 50;
\\echo -- contact phone-hash lookup
$E SELECT id FROM agent_contacts WHERE phone_lookup_hash = $PHONE AND deleted_at IS NULL;
\\echo -- child rows by client_id
$E SELECT id, kind, created_at FROM client_activities WHERE client_id = '$client' ORDER BY created_at DESC LIMIT 50;
\\echo -- client by id
$E SELECT id, full_name FROM clients WHERE id = 'ffffffff-0000-0000-0000-000000000001';
\\echo -- leads pipeline page
\\set ON_ERROR_STOP off
SAVEPOINT leads_page;
SET LOCAL statement_timeout = '5s';
$E SELECT id, motivation_score FROM leads ORDER BY motivation_score DESC, created_at DESC, id LIMIT 50;
ROLLBACK TO SAVEPOINT leads_page;
\\set ON_ERROR_STOP on
$setctx
\\echo -- client count, generic plan
SET LOCAL plan_cache_mode = force_generic_plan;
PREPARE wall_count AS SELECT count(*) FROM clients;
$E EXECUTE wall_count;
\\echo -- client page, generic plan
PREPARE wall_page(int) AS SELECT id, full_name, lead_score FROM clients WHERE archived_at IS NULL ORDER BY lead_score DESC LIMIT \$1;
$E EXECUTE wall_page(50);
\\echo -- phone-hash lookup, generic plan
PREPARE wall_phone(text) AS SELECT id FROM agent_contacts WHERE phone_lookup_hash = \$1 AND deleted_at IS NULL;
$E EXECUTE wall_phone($PHONE);
DEALLOCATE ALL;
SET LOCAL plan_cache_mode = auto;
\\echo -- per-row policy cost (seq scan forced)
SET LOCAL enable_indexscan = off; SET LOCAL enable_bitmapscan = off; SET LOCAL enable_indexonlyscan = off;
$E SELECT count(*) FROM clients;
SET LOCAL enable_indexscan = on; SET LOCAL enable_bitmapscan = on; SET LOCAL enable_indexonlyscan = on;
RESET SESSION AUTHORIZATION;
SQL
  done
}

sql=$(
  echo '\set ON_ERROR_STOP on'
  echo 'BEGIN;'
  cat <<'SQL'
INSERT INTO tenants (id, slug, name) VALUES ('16160000-0000-4000-8000-000000000001', 'l16-plans', 'L16 plans');
INSERT INTO agent_contacts (tenant_id, phone_lookup_hash, email_lookup_hash)
  SELECT '16160000-0000-4000-8000-000000000001', md5('p' || g) || md5('q' || g), md5('e' || g) || md5('f' || g)
    FROM generate_series(1, 21000) g;
SQL
  echo "INSERT INTO agent_contacts (tenant_id, phone_lookup_hash) SELECT '$SMALL', md5('s' || g) || md5('t' || g) FROM generate_series(1, 40) g;"
  echo 'ANALYZE agent_contacts; ANALYZE clients; ANALYZE client_activities;'
  queries "0141"
  cat <<'SQL'
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT c.relname, p.polname FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid
            WHERE pg_get_expr(p.polqual, p.polrelid) LIKE '%app_tenant_floor()%'
  LOOP EXECUTE format('ALTER POLICY %I ON %I USING (app_is_platform_admin() OR tenant_id = app_current_tenant())',
                      r.polname, r.relname); END LOOP; END $$;
SQL
  queries "pre-0141"
  echo 'ROLLBACK;'
)

out=$(printf '%s\n' "$sql" | psql_q 2>&1)
if [ "${RAW:-}" = 1 ]; then printf '%s\n' "$out"; exit 0; fi
printf '%s\n' "$out" | python3 -c '
import re, sys
res, order_q, order_c, phases = {}, [], [], []
phase = ctx = q = None; first = None
for line in sys.stdin:
    m = re.match(r"===== (.*) / (.*)", line)
    if m:
        phase, ctx = m.groups()
        if phase not in phases: phases.append(phase)
        if ctx not in order_c: order_c.append(ctx)
        q = None; continue
    m = re.match(r"-- (.*)", line)
    if m and phase:
        q = m.group(1).strip(); first = None
        if q not in order_q: order_q.append(q)
        continue
    if q is None: continue
    m = re.search(r"((?:Bitmap Heap|Bitmap Index|Index Only|Index|Seq) Scan)(?: using (\S+))? on (\w+)", line)
    if m and first is None and m.group(3) not in ("record_shares", "users") and " c " not in line:
        first = m.group(1).replace(" Scan", "") + (" " + m.group(2) if m.group(2) else "")
    m = re.search(r"Execution Time: ([\d.]+)", line)
    if m:
        node = first or "?"
        res[(phase, ctx, q)] = "%.1f ms, %s" % (float(m.group(1)), node)
for c in order_c:
    print(f"## {c}")
    for qq in order_q:
        miss = "> 5 s (cancelled)" if qq.startswith("leads") else "no plan"
        print(f"  {qq:34}", " | ".join(f"{p}: {res.get((p, c, qq), miss)}" for p in phases[::-1]))
'
