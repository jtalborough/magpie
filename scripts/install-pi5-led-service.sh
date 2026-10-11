#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/searchable-storage-led}"
APP_DIR="$(mkdir -p "$APP_DIR" && cd "$APP_DIR" && pwd)"
SERVICE_NAME="${SERVICE_NAME:-searchable-storage-led}"
CONFIG_PATH="$APP_DIR/config/led-controller.json"
ENV_PATH="$APP_DIR/secrets.env"
BUILD_PATH="$APP_DIR/build.json"

cd "$(dirname "$0")/.."

mkdir -p "$APP_DIR/scripts"
for companion_script in \
  connect-pi5-cabinet.sh \
  install-pi5-edge-service.sh \
  install-pi5-kiosk.sh \
  install-pi5-voice-service.sh; do
  cp "scripts/$companion_script" "$APP_DIR/scripts/$companion_script"
  chmod 755 "$APP_DIR/scripts/$companion_script"
done

python3 -m venv --system-site-packages "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
"$APP_DIR/.venv/bin/python" -m pip install ".[edge]"

COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
DIRTY="false"
if [[ -n "$(git status --porcelain 2>/dev/null || true)" ]]; then
  DIRTY="true"
fi
BUILT_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
VERSION="$("$APP_DIR/.venv/bin/python" - <<'PY'
from searchable_storage_led import __version__
print(__version__)
PY
)"
cat >"$BUILD_PATH" <<EOF
{
  "version": "$VERSION",
  "commit": "$COMMIT",
  "branch": "$BRANCH",
  "dirty": $DIRTY,
  "built_at": "$BUILT_AT",
  "source": "install-pi5-led-service"
}
EOF
for pkg_dir in "$APP_DIR"/.venv/lib/python*/site-packages/searchable_storage_led; do
  [ -d "$pkg_dir" ] && cp "$BUILD_PATH" "$pkg_dir/build.json"
done

mkdir -p "$APP_DIR/config"
for source_config in config/*.json; do
  target_config="$APP_DIR/config/$(basename "$source_config")"
  if [[ "$(basename "$source_config")" == "led-controller.json" && -f "$target_config" ]]; then
    merged_config="$(mktemp "$APP_DIR/config/led-controller.deploy.XXXXXX.json")"
    "$APP_DIR/.venv/bin/python" -m searchable_storage_led.runtime_config \
      --source "$source_config" \
      --installed "$target_config" \
      --output "$merged_config"
    mv "$merged_config" "$target_config"
  else
    cp "$source_config" "$target_config"
  fi
done
if ls config/*.md >/dev/null 2>&1; then
  cp config/*.md "$APP_DIR/config/"
fi
mkdir -p "$APP_DIR/state"
if [[ ! -f "$ENV_PATH" ]]; then
  cat >"$ENV_PATH" <<'EOF'
# Local secrets for searchable-storage-led.
# Keep this file out of git and do not paste keys into chat.
# OPENAI_API_KEY=sk-proj-...
EOF
  chmod 600 "$ENV_PATH"
else
  chmod 600 "$ENV_PATH"
fi

mkdir -p "$HOME/.config/systemd/user"
cat >"$HOME/.config/systemd/user/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Searchable Storage LED API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=LED_CONFIG=$CONFIG_PATH
Environment=BUILD_INFO_PATH=$BUILD_PATH
EnvironmentFile=-$ENV_PATH
ExecStart=$APP_DIR/.venv/bin/python -m searchable_storage_led.api
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME.service"
systemctl --user restart "$SERVICE_NAME.service"

restarted_companions=()
for companion in searchable-storage-voice searchable-storage-kiosk; do
  if systemctl --user is-enabled "$companion.service" >/dev/null 2>&1; then
    systemctl --user restart "$companion.service"
    restarted_companions+=("$companion")
  fi
done

cat <<EOF
Installed $SERVICE_NAME.

Restarted installed companions:
  ${restarted_companions[*]:-(none)}

API:
  http://$(hostname -I | awk '{print $1}'):8765/health
  http://$(hostname -I | awk '{print $1}'):8765/assistant/status
  http://$(hostname -I | awk '{print $1}'):8765/build

Secrets:
  $ENV_PATH
  Add OPENAI_API_KEY there, then restart the service.

Commands:
  systemctl --user status $SERVICE_NAME.service
  journalctl --user -u $SERVICE_NAME.service -f
  nano $ENV_PATH
  systemctl --user restart $SERVICE_NAME.service
  curl http://127.0.0.1:8765/assistant/status
  curl -X POST http://127.0.0.1:8765/strip -H 'content-type: application/json' -d '{"color":[255,0,0]}'
  curl -X POST http://127.0.0.1:8765/pixels/0 -H 'content-type: application/json' -d '{"color":[255,255,255],"clear_others":true}'
EOF
