#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="$(command -v python3)"
if ! command -v systemctl >/dev/null; then echo "systemd required" >&2; exit 1; fi
mkdir -p "$HOME/.config/systemd/user" "$HOME/.config/neoh-agent"
chmod 700 "$HOME/.config/neoh-agent"
ENV_FILE="$HOME/.config/neoh-agent/agent.env"
if [[ ! -e "$ENV_FILE" ]]; then (umask 077; echo '# ANTHROPIC_API_KEY=your-key-here' > "$ENV_FILE"); fi
chmod 600 "$ENV_FILE"
cat > "$HOME/.config/systemd/user/neoh-agent.service" <<EOF
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
TimeoutStopSec=35
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now neoh-agent.service
echo "Check: systemctl --user status neoh-agent.service"
echo "Logs: journalctl --user -u neoh-agent.service -f"
echo "Stop: systemctl --user stop neoh-agent.service"
