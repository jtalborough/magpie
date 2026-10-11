#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/searchable-storage-led}"
APP_DIR="$(cd "$APP_DIR" && pwd)"
ENV_PATH="${ENV_PATH:-$APP_DIR/secrets.env}"
API_URL="${MAGPIE_API_URL:-http://127.0.0.1:8765}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -f "$ENV_PATH" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_PATH"
  set +a
fi

run() {
  echo
  echo "==> $*"
  "$@" || true
}

print_magpie_voice_env() {
  env | sort | grep -E '^(MAGPIE_(VOICE|STT|TTS|WAKE)_|MAGPIE_API_URL=)' || true
}

echo "MAGPIE_DIAGNOSTICS: host=$(hostname) app_dir=$APP_DIR api=$API_URL"
run curl -fsS "$API_URL/build"
run curl -fsS "$API_URL/screen/status"
run curl -fsS "$API_URL/voice/status"
run curl -fsS "$API_URL/voice/calibration?limit=50"
run curl -fsS "$API_URL/voice/tuning?limit=8"
run curl -fsS "$API_URL/demo/readiness"
run env LED_CONFIG="$APP_DIR/config/led-controller.json" \
  "$APP_DIR/.venv/bin/python" "$SCRIPT_DIR/diagnose-pi-storage.py"
run print_magpie_voice_env
run arecord -l
run aplay -l
run cat /proc/asound/cards
run aplay -L
run bash -c 'for path in /sys/class/drm/card*-HDMI-A-*/status; do [[ -e "$path" ]] && printf "%s: %s\n" "$path" "$(cat "$path")"; done'
run bash -c 'for path in /proc/asound/card*/eld*; do [[ -e "$path" ]] || continue; echo "--- $path"; cat "$path"; done'
run amixer -c 0
run amixer -c 1
run amixer -c 2
run pactl list short sources
run pactl list short sinks
run bash -c 'systemctl --user show searchable-storage-voice.service --property=Environment --value | tr " " "\n" | grep -E "^MAGPIE_VOICE_(AUDIO_THRESHOLD|VAD_FRAME_THRESHOLD)="'
run systemctl --user --no-pager --plain status searchable-storage-voice.service
run journalctl --user -u searchable-storage-voice.service --no-pager -n 160
