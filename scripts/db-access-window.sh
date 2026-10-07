#!/usr/bin/env bash
#
# Neoh — open (or close) a temporary database-access window for THIS machine.
#
# The managed databases accept connections from the App Platform app only
# (DigitalOcean "trusted sources"). Migrations run inside DO as the app's
# PRE_DEPLOY job, so nothing routine needs more. An operator task that must
# reach a database directly — the demo preflight/reset, a backup or restore
# drill, a one-off investigation — opens a window for this machine's public IP,
# does the work, and closes it again.
#
#   scripts/db-access-window.sh open  <env>   # env: staging | production
#   scripts/db-access-window.sh close <env>
#   scripts/db-access-window.sh status <env>
#
# `close` removes every ip_addr rule (all operator windows, whatever IP they
# were opened for); the app rule is never touched. Close the window when you are done: an open
# window is the whole internet's way in from this IP.

set -euo pipefail

ACTION="${1:-}"; ENV="${2:-}"
case "$ENV" in
  staging)    PG="neoh-postgres-staging"; VALKEY="neoh-redis-staging" ;;
  production) PG="neoh-postgres";         VALKEY="neoh-redis" ;;
  *) echo "usage: $0 open|close|status staging|production" >&2; exit 2 ;;
esac

DOCTL=(env -u DOCKER_HOST doctl)
cluster_id() { "${DOCTL[@]}" databases list --format ID,Name --no-header | awk -v n="$1" '$2 == n {print $1}'; }
my_ip() { curl -fsS --max-time 10 https://api.ipify.org; }

IP="$(my_ip)"
[[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "could not determine this machine's public IPv4" >&2; exit 1; }

for name in "$PG" "$VALKEY"; do
  id="$(cluster_id "$name")"
  [ -n "$id" ] || { echo "no database cluster named $name" >&2; exit 1; }
  rules="$("${DOCTL[@]}" databases firewalls list "$id" -o json)"
  mine="$(jq -r --arg ip "$IP" '[.[]? | select(.type == "ip_addr" and .value == $ip) | .uuid] | join(" ")' <<<"$rules")"
  has_app="$(jq -r '[.[]? | select(.type == "app")] | length' <<<"$rules")"
  case "$ACTION" in
    open)
      if [ "$has_app" = 0 ]; then
        echo "$name: refusing — it has no app rule, so it is not locked down yet (open to all)" >&2
        exit 1
      fi
      if [ -n "$mine" ]; then
        echo "$name: already open for $IP"
      else
        "${DOCTL[@]}" databases firewalls append "$id" --rule "ip_addr:$IP" >/dev/null
        echo "$name: OPEN for $IP — close it when done: $0 close $ENV"
      fi ;;
    close)
      # EVERY ip_addr rule is an operator window (the policy is "the app, plus
      # temporary windows"), so close removes them all — including one left for
      # an address this machine no longer has (a mobile/DHCP connection changes
      # IP mid-task; matching only the current IP left the old window open).
      all="$(jq -r '[.[]? | select(.type == "ip_addr") | .uuid] | join(" ")' <<<"$rules")"
      for uuid in $all; do
        "${DOCTL[@]}" databases firewalls remove "$id" --uuid "$uuid" >/dev/null
      done
      echo "$name: every operator window closed (the app rule stays)" ;;
    status)
      jq -r --arg n "$name" '.[]? | "\($n): \(.type) \(.value)"' <<<"$rules" ;;
    *) echo "usage: $0 open|close|status staging|production" >&2; exit 2 ;;
  esac
done
