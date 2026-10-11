#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/searchable-storage-led}"
APP_DIR="$(cd "$APP_DIR" && pwd)"
SERVICE_NAME="${SERVICE_NAME:-searchable-storage-voice}"
API_SERVICE_NAME="${API_SERVICE_NAME:-searchable-storage-led}"
ENV_PATH="${ENV_PATH:-$APP_DIR/secrets.env}"
INPUT_DEVICE="${MAGPIE_VOICE_INPUT_DEVICE:-plughw:2,0}"
INPUT_CHANNELS="${MAGPIE_VOICE_INPUT_CHANNELS:-2}"
INPUT_CHANNEL="${MAGPIE_VOICE_INPUT_CHANNEL:-1}"
OUTPUT_DEVICE="${MAGPIE_VOICE_OUTPUT_DEVICE:-plughw:CARD=vc4hdmi0,DEV=0}"
WAKE_WORD="${MAGPIE_WAKE_WORD:-magpie}"
WAKE_MIN_CONFIDENCE="${MAGPIE_LOCAL_WAKE_MIN_CONFIDENCE:-0.8}"
AUDIO_THRESHOLD="${MAGPIE_VOICE_AUDIO_THRESHOLD:-500}"
VAD_FRAME_THRESHOLD="${MAGPIE_VOICE_VAD_FRAME_THRESHOLD:-350}"
API_URL="${MAGPIE_VOICE_API_URL:-${MAGPIE_API_URL:-http://127.0.0.1:8765}}"
EDGE_DEVICE_ID="${MAGPIE_EDGE_DEVICE_ID:-main-cabinet}"
EDGE_WORKSPACE_ID="${MAGPIE_EDGE_WORKSPACE_ID:-}"

if ! command -v espeak-ng >/dev/null 2>&1 && ! command -v espeak >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq espeak-ng
  else
    echo "Warning: local speech fallback is unavailable; install espeak-ng." >&2
  fi
fi

MODEL_NAME="vosk-model-small-en-us-0.15"
MODEL_PATH="${MAGPIE_VOSK_MODEL_PATH:-$APP_DIR/state/$MODEL_NAME}"
MODEL_URL="https://alphacephei.com/vosk/models/$MODEL_NAME.zip"
MODEL_SHA256="30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498"

if [[ ! -f "$MODEL_PATH/conf/model.conf" ]]; then
  archive="$(mktemp "${TMPDIR:-/tmp}/magpie-vosk.XXXXXX.zip")"
  trap 'rm -f "$archive"' EXIT
  curl --fail --location --retry 3 --output "$archive" "$MODEL_URL"
  printf '%s  %s\n' "$MODEL_SHA256" "$archive" | sha256sum --check --status
  mkdir -p "$APP_DIR/state"
  APP_STATE="$APP_DIR/state" MODEL_ARCHIVE="$archive" python3 - <<'PY'
import os
from pathlib import Path
from zipfile import ZipFile

with ZipFile(os.environ["MODEL_ARCHIVE"]) as archive:
    archive.extractall(Path(os.environ["APP_STATE"]))
PY
fi
if [[ ! -f "$MODEL_PATH/conf/model.conf" ]]; then
  echo "Vosk wake model installation failed: $MODEL_PATH" >&2
  exit 1
fi

mkdir -p "$HOME/.config/systemd/user"

cat >"$HOME/.config/systemd/user/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Magpie cabinet voice listener
After=network-online.target $API_SERVICE_NAME.service
Wants=network-online.target $API_SERVICE_NAME.service

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=-$ENV_PATH
Environment=MAGPIE_API_URL=$API_URL
Environment=MAGPIE_EDGE_DEVICE_ID=$EDGE_DEVICE_ID
Environment=MAGPIE_EDGE_WORKSPACE_ID=$EDGE_WORKSPACE_ID
Environment=MAGPIE_VOICE_INPUT_DEVICE=$INPUT_DEVICE
Environment=MAGPIE_VOICE_INPUT_CHANNELS=$INPUT_CHANNELS
Environment=MAGPIE_VOICE_INPUT_CHANNEL=$INPUT_CHANNEL
Environment=MAGPIE_VOICE_OUTPUT_DEVICE=$OUTPUT_DEVICE
Environment=MAGPIE_WAKE_WORD=$WAKE_WORD
Environment=MAGPIE_LOCAL_WAKE_MIN_CONFIDENCE=$WAKE_MIN_CONFIDENCE
Environment=MAGPIE_VOICE_AUDIO_THRESHOLD=$AUDIO_THRESHOLD
Environment=MAGPIE_VOICE_VAD_FRAME_THRESHOLD=$VAD_FRAME_THRESHOLD
Environment=MAGPIE_VOSK_MODEL_PATH=$MODEL_PATH
ExecStartPre=-/usr/bin/amixer -q -c Array sset PCM,0 100% unmute
ExecStartPre=-/usr/bin/amixer -q -c Array sset PCM,1 100% unmute
ExecStart=$APP_DIR/.venv/bin/python -m searchable_storage_led.voice_daemon
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME.service"
systemctl --user restart "$SERVICE_NAME.service"

cat <<EOF
Installed $SERVICE_NAME.

Wake word:
  Hey $WAKE_WORD (offline Vosk grammar)

Minimum wake confidence:
  $WAKE_MIN_CONFIDENCE

Wake model:
  $MODEL_PATH

Input:
  $INPUT_DEVICE channel $INPUT_CHANNEL of $INPUT_CHANNELS

Output:
  $OUTPUT_DEVICE

Local speech fallback:
  $(command -v espeak-ng || command -v espeak || echo unavailable)

Speech start threshold:
  $VAD_FRAME_THRESHOLD RMS

Commands:
  systemctl --user status $SERVICE_NAME.service
  journalctl --user -u $SERVICE_NAME.service -f
  systemctl --user restart $SERVICE_NAME.service
EOF
