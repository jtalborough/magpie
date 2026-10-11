#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/searchable-storage-led}"
APP_DIR="$(cd "$APP_DIR" && pwd)"
SERVICE_NAME="${SERVICE_NAME:-searchable-storage-voice}"
ENV_PATH="${ENV_PATH:-$APP_DIR/secrets.env}"

if [[ -f "$ENV_PATH" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_PATH"
  set +a
fi

API_URL="${MAGPIE_API_URL:-http://127.0.0.1:8765}"
PYTHON_BIN="$APP_DIR/.venv/bin/python"
PRIMARY_STT="${MAGPIE_STT_MODEL:-gpt-4o-transcribe}"
FALLBACK_STT="${MAGPIE_STT_FALLBACK_MODEL:-gpt-4o-mini-transcribe}"
COUNTDOWN_SECONDS="${VOICE_ONCE_COUNTDOWN_SECONDS:-10}"
VOICE_ONCE_PHRASE="${VOICE_ONCE_PHRASE:-Magpie, find M4 T-nuts}"
VOICE_TEST_TRANSCRIPT="${VOICE_TEST_TRANSCRIPT:-}"
VOICE_TEST_SPEAK_RESPONSE="${VOICE_TEST_SPEAK_RESPONSE:-false}"

for optional_number in MAGPIE_VOICE_AUDIO_THRESHOLD MAGPIE_VOICE_CLIP_SECONDS; do
  if [[ -z "${!optional_number:-}" ]]; then
    unset "$optional_number"
  fi
done

restart_voice() {
  systemctl --user start "$SERVICE_NAME.service" >/dev/null 2>&1 || true
}

cleanup() {
  curl -fsS -X POST "$API_URL/clear" >/dev/null 2>&1 || true
  restart_voice
}
trap cleanup EXIT

echo "Stopping $SERVICE_NAME for isolated voice diagnostics."
systemctl --user stop "$SERVICE_NAME.service" >/dev/null 2>&1 || true
sleep 1

echo "VOICE_ONCE: primary STT=$PRIMARY_STT fallback STT=$FALLBACK_STT api=$API_URL"
voice_command=(
  "$PYTHON_BIN" -m searchable_storage_led.voice_daemon
  --api-url "$API_URL"
  --stt-model "$PRIMARY_STT"
  --stt-fallback-model "$FALLBACK_STT"
  --call-backend
  --json
)

if [[ -n "$VOICE_TEST_TRANSCRIPT" ]]; then
  echo "VOICE_ONCE: transcript diagnostic: $VOICE_TEST_TRANSCRIPT"
  voice_command+=(--transcript "$VOICE_TEST_TRANSCRIPT")
  if [[ "$VOICE_TEST_SPEAK_RESPONSE" == "true" ]]; then
    voice_command+=(--speak-response)
  fi
else
  echo "VOICE_ONCE: speak now: $VOICE_ONCE_PHRASE"
  if [[ "$COUNTDOWN_SECONDS" =~ ^[0-9]+$ ]] && (( COUNTDOWN_SECONDS > 0 )); then
    echo "VOICE_ONCE: recording starts in $COUNTDOWN_SECONDS seconds"
    while (( COUNTDOWN_SECONDS > 0 )); do
      echo "VOICE_ONCE: $COUNTDOWN_SECONDS"
      sleep 1
      COUNTDOWN_SECONDS=$((COUNTDOWN_SECONDS - 1))
    done
  fi
  voice_command+=(
    --once
    --compare-stt-model "$PRIMARY_STT"
    --compare-stt-model "$FALLBACK_STT"
  )
fi

result="$("${voice_command[@]}")"
echo "$result"

interaction_id="$(
  "$PYTHON_BIN" -c \
    'import json, sys; print((json.load(sys.stdin).get("response") or {}).get("interaction_id", ""))' \
    <<<"$result"
)"
if [[ -n "$interaction_id" ]]; then
  sleep 3
  screen="$(curl -fsS "$API_URL/screen/status")"
  echo "VOICE_ONCE: screen status"
  echo "$screen"
  screen_interaction="$(
    "$PYTHON_BIN" -c \
      'import json, sys; print((json.load(sys.stdin).get("screen") or {}).get("last_seen_interaction_id", 0))' \
      <<<"$screen"
  )"
  if (( screen_interaction < interaction_id )); then
    echo "VOICE_ONCE: screen did not observe interaction $interaction_id" >&2
    exit 1
  fi
fi

echo "VOICE_ONCE: recent attempts"
curl -fsS "$API_URL/voice/tuning?limit=3"
echo
echo "VOICE_ONCE: readiness"
curl -fsS "$API_URL/demo/readiness"
echo
