#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/searchable-storage-led}"
APP_DIR="$(cd "$APP_DIR" && pwd)"
SERVICE_NAME="${SERVICE_NAME:-searchable-storage-edge}"
ENV_PATH="${ENV_PATH:-$APP_DIR/secrets.env}"
CENTRAL_URL="${MAGPIE_CENTRAL_URL:-}"
DEVICE_ID="${MAGPIE_EDGE_DEVICE_ID:-main-cabinet}"
WORKSPACE_ID="${MAGPIE_EDGE_WORKSPACE_ID:-}"

if [[ -z "$CENTRAL_URL" ]]; then
  echo "MAGPIE_CENTRAL_URL is required; the edge service was not installed." >&2
  exit 1
fi
if [[ ! -f "$ENV_PATH" ]] || ! grep -q '^MAGPIE_EDGE_TOKEN=' "$ENV_PATH"; then
  echo "MAGPIE_EDGE_TOKEN must be present in $ENV_PATH; the edge service was not installed." >&2
  exit 1
fi

mkdir -p "$HOME/.config/systemd/user"
cat >"$HOME/.config/systemd/user/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Magpie central-to-cabinet edge worker
After=network-online.target searchable-storage-led.service
Wants=network-online.target searchable-storage-led.service

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_PATH
Environment=MAGPIE_CENTRAL_URL=$CENTRAL_URL
Environment=MAGPIE_LOCAL_URL=http://127.0.0.1:8765
Environment=MAGPIE_EDGE_DEVICE_ID=$DEVICE_ID
Environment=MAGPIE_EDGE_WORKSPACE_ID=$WORKSPACE_ID
ExecStart=$APP_DIR/.venv/bin/python -m searchable_storage_led.edge_worker
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME.service"
systemctl --user status "$SERVICE_NAME.service" --no-pager
