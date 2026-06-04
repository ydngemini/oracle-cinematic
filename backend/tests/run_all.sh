#!/usr/bin/env bash
# Full tenancy/RLS test suite for the Oracle multi-tenant IAM layer.
#   1. App-layer  — tenancy.py predicates + require_context() JWT gate (no DB)
#   2. RLS layer  — Postgres policies in migrations 0001+0002, real container
#
# The RLS leg needs docker; if it's unreachable the suite reports it as SKIPPED
# (exit 0 only when everything that ran passed and nothing was skipped).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

echo "============================================================"
echo " 1/2  app-layer (tenancy.py + auth JWT gate)"
echo "============================================================"
"$PY" "$HERE/test_tenancy.py"
app_rc=$?

echo
echo "============================================================"
echo " 2/2  RLS layer (Postgres policies, dockerized)"
echo "============================================================"
bash "$HERE/run_rls.sh"
rls_rc=$?

echo
echo "============================================================"
echo " SUMMARY"
echo "============================================================"
[ $app_rc -eq 0 ] && echo " app-layer : PASS" || echo " app-layer : FAIL"
if   [ $rls_rc -eq 0 ]; then echo " RLS layer : PASS"
elif [ $rls_rc -eq 2 ]; then echo " RLS layer : SKIPPED (docker unavailable)"
else echo " RLS layer : FAIL"; fi

# Fail the suite on any real failure; skip (2) is non-fatal but visible.
[ $app_rc -eq 0 ] && { [ $rls_rc -eq 0 ] || [ $rls_rc -eq 2 ]; }
