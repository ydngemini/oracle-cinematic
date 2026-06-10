#!/usr/bin/env bash
# Migrate Docker's data-root from /var/lib/docker (full 68G root disk) to
# SYPHER_CORE (ext4, overlay2-compatible). Run once with sudo:
#
#   sudo bash /media/ydn/SYPHER_CORE/Oracle/scripts/migrate-docker-root.sh
#
# Steps: stop stack + daemon → rsync data → point daemon.json at the new
# root → restart → VERIFY images/volumes survived → only then offer to
# delete the old directory (that's where the disk space comes back).
set -euo pipefail

TARGET="/media/ydn/SYPHER_CORE/docker-data"
OLD="/var/lib/docker"
ORACLE_DIR="/media/ydn/SYPHER_CORE/Oracle"

red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { red "Run with sudo."; exit 1; }

# ── Preflight ─────────────────────────────────────────────────────────────────
FSTYPE=$(findmnt -no FSTYPE --target "$TARGET" 2>/dev/null || findmnt -no FSTYPE /media/ydn/SYPHER_CORE)
[[ "$FSTYPE" == "ext4" || "$FSTYPE" == "xfs" ]] || { red "Target fs is $FSTYPE — overlay2 needs ext4/xfs. Aborting."; exit 1; }

SRC_KB=$(du -sk "$OLD" | cut -f1)
AVAIL_KB=$(df --output=avail /media/ydn/SYPHER_CORE | tail -1 | tr -d ' ')
(( AVAIL_KB > SRC_KB + 1048576 )) || { red "Not enough space on target ($((AVAIL_KB/1024))MB avail, need $((SRC_KB/1024))MB + 1GB headroom)."; exit 1; }
bold "Migrating $((SRC_KB/1024))MB from $OLD → $TARGET (fs: $FSTYPE, $((AVAIL_KB/1024/1024))GB free)"

# Snapshot inventory for post-migration verification.
BEFORE_IMAGES=$(docker images -q | sort -u | wc -l)
BEFORE_VOLUMES=$(docker volume ls -q | wc -l)
bold "Inventory before: $BEFORE_IMAGES images, $BEFORE_VOLUMES volumes"

# ── Stop everything ───────────────────────────────────────────────────────────
bold "Stopping Oracle stack..."
(cd "$ORACLE_DIR" && docker compose down) || true
bold "Stopping Docker daemon..."
systemctl stop docker.service docker.socket

# ── Copy data (preserves ownership, ACLs, xattrs, hardlinks, sparse) ──────────
bold "Copying data-root (this can take a few minutes)..."
mkdir -p "$TARGET"
rsync -aHAXS --delete "$OLD/" "$TARGET/"

# ── Point the daemon at the new root ─────────────────────────────────────────
bold "Updating /etc/docker/daemon.json..."
python3 - "$TARGET" <<'PY'
import json, sys, os
target = sys.argv[1]
path = "/etc/docker/daemon.json"
cfg = {}
if os.path.exists(path):
    with open(path) as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError:
            sys.exit(f"{path} exists but is not valid JSON — fix it manually first.")
cfg["data-root"] = target
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"  data-root -> {target}")
PY

# ── Restart and verify ────────────────────────────────────────────────────────
bold "Starting Docker daemon..."
systemctl start docker.service
sleep 3

NEW_ROOT=$(docker info --format '{{.DockerRootDir}}')
[[ "$NEW_ROOT" == "$TARGET" ]] || { red "Daemon reports root '$NEW_ROOT', expected '$TARGET'. NOT deleting old data."; exit 1; }

AFTER_IMAGES=$(docker images -q | sort -u | wc -l)
AFTER_VOLUMES=$(docker volume ls -q | wc -l)
bold "Inventory after:  $AFTER_IMAGES images, $AFTER_VOLUMES volumes"
if [[ "$AFTER_IMAGES" -ne "$BEFORE_IMAGES" || "$AFTER_VOLUMES" -ne "$BEFORE_VOLUMES" ]]; then
  red "Inventory mismatch — investigate before deleting $OLD. Old data is untouched."
  exit 1
fi
green "Verified: data-root is $NEW_ROOT, inventory matches."

# ── Restart the Oracle stack ──────────────────────────────────────────────────
bold "Restarting Oracle stack..."
(cd "$ORACLE_DIR" && docker compose up -d)
sleep 8
curl -sf --max-time 10 http://localhost:8000/health >/dev/null \
  && green "Backend /health OK on the new data-root." \
  || red "Backend /health not responding yet — check 'docker compose logs backend'."

# ── Reclaim the old directory (the actual point of all this) ──────────────────
echo
bold "Old data-root still occupies $((SRC_KB/1024))MB on the full root disk."
read -r -p "Delete $OLD now? [y/N] " answer
if [[ "$answer" =~ ^[Yy]$ ]]; then
  rm -rf "$OLD"
  green "Deleted. Root disk after:"
  df -h / | tail -1
else
  echo "Kept. Delete later with: sudo rm -rf $OLD"
fi

green "Migration complete."
