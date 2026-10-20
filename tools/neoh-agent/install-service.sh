#!/usr/bin/env bash
# Install Neoh as a systemd user service on an existing Ubuntu development box.
# Do not modify, reset, or stop the current Neoh development checkout.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd -P)"
SERVICE_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/neoh-agent"
UNIT="$SERVICE_DIR/neoh-agent.service"
ENV_FILE="$CONFIG_DIR/agent.env"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "Requires Ubuntu with systemd user services." >&2
  exit 1
fi
if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "No active systemd user manager. Log in as the normal development user." >&2
  exit 1
fi
case "$REPO" in
  *" "*)
    echo "Move the autonomous checkout to a path without spaces." >&2
    exit 1
    ;;
esac
if [[ "$(git -C "$REPO" branch --show-current)" == "main" ]]; then
  echo "Install from a separate autonomous checkout, not your active main branch." >&2
  exit 1
fi

PYTHON="$(command -v python3)"
if ! "$PYTHON" -c 'import claude_agent_sdk' >/dev/null 2>&1; then
  echo "Claude Agent SDK missing in $PYTHON. Activate the agent virtualenv first." >&2
  exit 1
fi

mkdir -p "$SERVICE_DIR" "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  (umask 077; echo '# Configure your Anthropic API key here, mode 0600' > "$ENV_FILE")
fi
chmod 600 "$ENV_FILE"
if ! grep -Eq '^ANTHROPIC_API_KEY=[^[:space:]#]+' "$ENV_FILE"; then
  echo "Configure ANTHROPIC_API_KEY in $ENV_FILE before starting." >&2
  exit 1
fi
if [[ -e "$UNIT" ]]; then
  echo "Existing $UNIT detected. Review it before replacing." >&2
  exit 1
fi

cat > "$UNIT" <<EOF
[Unit]
Description=Neoh autonomous development supervisor
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO
EnvironmentFile=$ENV_FILE
ExecStart=$PYTHON $REPO/tools/neoh-agent/continuous.py --daily-budget-usd 48 --budget-usd 12
Restart=on-failure
RestartSec=60
TimeoutStopSec=45
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now neoh-agent.service
echo "Service enabled. Check: systemctl --user status neoh-agent.service"
echo "Logs: journalctl --user -u neoh-agent.service -f"
echo "Stop: systemctl --user stop neoh-agent.service"
