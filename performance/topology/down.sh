#!/bin/sh
# Tear the perf topology down. -v so no anonymous volume is stranded (the DR
# drill filled a disk that way). The named media volume is removed too.
for n in oracle-perf-lb oracle-perf-web-1 oracle-perf-web-2 oracle-perf-worker oracle-perf-valkey oracle-perf-mock; do
  docker rm -fv "$n" >/dev/null 2>&1 && echo "removed $n"
done
docker volume rm oracle_perf_media >/dev/null 2>&1 || true
rm -f /tmp/neoh-perf.env
