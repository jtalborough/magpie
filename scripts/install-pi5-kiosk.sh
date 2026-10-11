#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/searchable-storage-led}"
APP_DIR="$(cd "$APP_DIR" && pwd)"
SERVICE_NAME="${SERVICE_NAME:-searchable-storage-kiosk}"
API_SERVICE_NAME="${API_SERVICE_NAME:-searchable-storage-led}"
SCREEN_SERVICE_NAME="${SCREEN_SERVICE_NAME:-searchable-storage-screen}"
SCREEN_API_URL="${MAGPIE_SCREEN_API_URL:-}"
SCREEN_PORT="${MAGPIE_SCREEN_PORT:-8767}"
EDGE_DEVICE_ID="${MAGPIE_EDGE_DEVICE_ID:-main-cabinet}"
EDGE_WORKSPACE_ID="${MAGPIE_EDGE_WORKSPACE_ID:-}"
ENV_PATH="${ENV_PATH:-$APP_DIR/secrets.env}"
PROFILE_DIR="${PROFILE_DIR:-$HOME/.config/searchable-storage-kiosk/chromium}"
CHROMIUM_BIN="${CHROMIUM_BIN:-}"

if [[ -n "$SCREEN_API_URL" ]]; then
  if [[ ! -f "$ENV_PATH" ]] || ! grep -q '^MAGPIE_EDGE_TOKEN=' "$ENV_PATH"; then
    echo "MAGPIE_EDGE_TOKEN must be present in $ENV_PATH for a central kiosk." >&2
    exit 1
  fi
  "$APP_DIR/.venv/bin/python" - "$SCREEN_API_URL" "$EDGE_DEVICE_ID" <<'PY'
import sys
from searchable_storage_led.screen_bridge import validate_screen_bridge

validate_screen_bridge(sys.argv[1], sys.argv[2], "installer-check")
PY
  APP_URL="${APP_URL:-http://127.0.0.1:$SCREEN_PORT/kiosk}"
  READY_URL="http://127.0.0.1:$SCREEN_PORT/bridge/health"
  KIOSK_AFTER="graphical-session.target network-online.target $SCREEN_SERVICE_NAME.service"
  KIOSK_WANTS="network-online.target $SCREEN_SERVICE_NAME.service"
else
  APP_URL="${APP_URL:-http://127.0.0.1:8765/kiosk}"
  READY_URL="http://127.0.0.1:8765/health"
  KIOSK_AFTER="graphical-session.target $API_SERVICE_NAME.service"
  KIOSK_WANTS="$API_SERVICE_NAME.service"
fi

if [[ -z "$CHROMIUM_BIN" ]]; then
  CHROMIUM_BIN="$(
    command -v /usr/lib/chromium/chromium 2>/dev/null ||
      command -v chromium-browser ||
      command -v chromium ||
      true
  )"
fi

if [[ -z "$CHROMIUM_BIN" ]]; then
  cat >&2 <<'EOF'
Chromium was not found on the Pi.

Install it on Raspberry Pi OS, then rerun this script:
  sudo apt-get update
  sudo apt-get install -y chromium-browser

On some images the package is named chromium:
  sudo apt-get install -y chromium
EOF
  exit 1
fi

mkdir -p "$PROFILE_DIR"
mkdir -p "$HOME/.config/systemd/user"

if [[ -n "$SCREEN_API_URL" ]]; then
  cat >"$HOME/.config/systemd/user/$SCREEN_SERVICE_NAME.service" <<EOF
[Unit]
Description=Magpie secure central screen bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_PATH
Environment=MAGPIE_SCREEN_API_URL=$SCREEN_API_URL
Environment=MAGPIE_EDGE_DEVICE_ID=$EDGE_DEVICE_ID
Environment=MAGPIE_EDGE_WORKSPACE_ID=$EDGE_WORKSPACE_ID
Environment=MAGPIE_SCREEN_BIND_HOST=127.0.0.1
Environment=MAGPIE_SCREEN_PORT=$SCREEN_PORT
ExecStart=$APP_DIR/.venv/bin/python -m searchable_storage_led.screen_bridge
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
fi

# This profile is dedicated to the cabinet. Clear only its application caches
# so an explicit kiosk install always starts on the deployed UI build.
systemctl --user stop "$SERVICE_NAME.service" >/dev/null 2>&1 || true
rm -rf -- \
  "$PROFILE_DIR/Default/Cache" \
  "$PROFILE_DIR/Default/Code Cache" \
  "$PROFILE_DIR/Default/Service Worker" \
  "$PROFILE_DIR/ShaderCache"

cat >"$HOME/.config/systemd/user/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Magpie cabinet screen kiosk
After=$KIOSK_AFTER
Wants=$KIOSK_WANTS

[Service]
Type=simple
Environment=DISPLAY=:0
Environment=XDG_RUNTIME_DIR=%t
ExecStart=/usr/bin/env bash -lc 'until curl -fsS "$READY_URL" >/dev/null; do sleep 2; done; exec "$CHROMIUM_BIN" --user-data-dir="$PROFILE_DIR" --kiosk --start-fullscreen --noerrdialogs --disable-infobars --disable-session-crashed-bubble --no-first-run --autoplay-policy=no-user-gesture-required --use-fake-ui-for-media-stream --password-store=basic --disable-features=AutofillServerCommunication "$APP_URL"'
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
if [[ -n "$SCREEN_API_URL" ]]; then
  systemctl --user enable "$SCREEN_SERVICE_NAME.service"
  systemctl --user restart "$SCREEN_SERVICE_NAME.service"
fi
systemctl --user enable "$SERVICE_NAME.service"
systemctl --user restart "$SERVICE_NAME.service"

cat <<EOF
Installed $SERVICE_NAME.

Screen URL:
  $APP_URL

Browser:
  $CHROMIUM_BIN

Profile:
  $PROFILE_DIR

Screen source:
  ${SCREEN_API_URL:-local Pi service}

Commands:
  systemctl --user status $SERVICE_NAME.service
  journalctl --user -u $SERVICE_NAME.service -f
  systemctl --user restart $SERVICE_NAME.service
EOF
