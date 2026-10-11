#!/usr/bin/env bash
# Install the Cloudflare connector for Magpie's central customer and edge origins.
# Hostname routing remains in Cloudflare's remotely managed tunnel configuration.
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-magpie-central-cloudflared}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/.config/magpie-central}"
TOKEN_FILE="${TOKEN_FILE:-$CONFIG_DIR/cloudflared-token}"
SERVICE_DIR="${SERVICE_DIR:-$HOME/.config/systemd/user}"
MAGPIE_LOCAL_ORIGIN="${MAGPIE_LOCAL_ORIGIN:-http://127.0.0.1:8766}"
TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-${TUNNEL_TOKEN:-}}"

if [[ -z "$TOKEN" ]]; then
  echo "CLOUDFLARE_TUNNEL_TOKEN is required." >&2
  echo "Use the dedicated central-service tunnel token, not the legacy Pi tunnel token." >&2
  exit 2
fi

case "$MAGPIE_LOCAL_ORIGIN" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *)
    echo "MAGPIE_LOCAL_ORIGIN must be a loopback HTTP origin." >&2
    exit 2
    ;;
esac

health="$MAGPIE_LOCAL_ORIGIN/health"
if ! curl --fail --silent --show-error --max-time 10 "$health" >/dev/null; then
  echo "Central Magpie is not healthy at $health; tunnel installation stopped." >&2
  exit 1
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
Description=Magpie central Cloudflare Tunnel
After=network-online.target
Wants=network-online.target

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
systemctl --user is-active --quiet "$SERVICE_NAME.service"

echo "Installed and started $SERVICE_NAME."
echo "Cloudflare must route the public and edge hostnames to $MAGPIE_LOCAL_ORIGIN."
echo "The public hostname must not use Access; the edge hostname must use Service Auth."
