#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/searchable-storage-led}"
ENV_PATH="${ENV_PATH:-$APP_DIR/secrets.env}"
SERVICE_NAME="${SERVICE_NAME:-magpie-cloudflared}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/.config/magpie}"
TOKEN_FILE="$CONFIG_DIR/cloudflared-token"
SERVICE_DIR="$HOME/.config/systemd/user"

if [[ -f "$ENV_PATH" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_PATH"
  set +a
fi

TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-${TUNNEL_TOKEN:-}}"
if [[ -z "$TOKEN" ]]; then
  echo "CLOUDFLARE_TUNNEL_TOKEN is required." >&2
  echo "Create a remotely-managed legacy POC tunnel and map an owner-only hostname to http://127.0.0.1:8765." >&2
  echo "Do not use the customer app hostname; production traffic belongs on the central service." >&2
  exit 2
fi

case "$(uname -m)" in
  aarch64|arm64) asset="cloudflared-linux-arm64" ;;
  x86_64|amd64) asset="cloudflared-linux-amd64" ;;
  armv7l|armhf) asset="cloudflared-linux-arm" ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 2 ;;
esac

mkdir -p "$BIN_DIR" "$CONFIG_DIR" "$SERVICE_DIR"
tmp_binary="$(mktemp)"
trap 'rm -f "$tmp_binary"' EXIT
curl --fail --location --silent --show-error \
  "https://github.com/cloudflare/cloudflared/releases/latest/download/$asset" \
  --output "$tmp_binary"
install -m 0755 "$tmp_binary" "$BIN_DIR/cloudflared"
printf '%s\n' "$TOKEN" > "$TOKEN_FILE"
chmod 0600 "$TOKEN_FILE"

cat > "$SERVICE_DIR/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Magpie secure Cloudflare Tunnel
After=network-online.target searchable-storage-led.service
Wants=network-online.target searchable-storage-led.service

[Service]
Type=notify
ExecStart=$BIN_DIR/cloudflared tunnel --no-autoupdate run --token-file $TOKEN_FILE
Restart=on-failure
RestartSec=5
TimeoutStartSec=0

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME.service"
systemctl --user restart "$SERVICE_NAME.service"

echo "Installed and started $SERVICE_NAME."
echo "This legacy POC hostname must target http://127.0.0.1:8765 and be protected by an owner-only Access policy."
echo "It must not be used for the public customer app or the outbound edge connection."
systemctl --user --no-pager --full status "$SERVICE_NAME.service"
