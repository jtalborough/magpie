#!/usr/bin/env bash
set -u

API_URL="${API_URL:-http://127.0.0.1:8765}"
MIC_TEST_PATH="${MIC_TEST_PATH:-/tmp/searchable-storage-mic-test.wav}"

section() {
  printf '\n== %s ==\n' "$1"
}

run() {
  printf '$ %s\n' "$*"
  "$@" 2>&1 || true
}

section "App"
run curl -fsS "$API_URL/health"
run curl -fsS "$API_URL/assistant/status"
run curl -fsS "$API_URL/screen/status"
run curl -fsS "$API_URL/voice/tuning?limit=3"
run curl -fsS "$API_URL/demo/readiness"

section "Display"
run ls -l /dev/dri
run loginctl list-sessions
if command -v wlr-randr >/dev/null 2>&1; then
  printf '$ wlr-randr\n'
  wlr_output="$(wlr-randr 2>&1 || true)"
  printf '%s\n' "$wlr_output"
  if printf '%s\n' "$wlr_output" | grep -Eiq 'NOOP|Headless'; then
    echo "DISPLAY_CHECK: no physical display output detected; compositor is using a headless/NOOP output."
  elif printf '%s\n' "$wlr_output" | grep -Eiq 'Enabled:[[:space:]]+yes'; then
    echo "DISPLAY_CHECK: display output appears enabled."
  else
    echo "DISPLAY_CHECK: display output is unclear; inspect wlr-randr output above."
  fi
fi
if command -v xrandr >/dev/null 2>&1; then
  printf '$ xrandr --query\n'
  xrandr_output="$(xrandr --query 2>&1 || true)"
  printf '%s\n' "$xrandr_output"
  if printf '%s\n' "$xrandr_output" | grep -Eiq ' connected'; then
    echo "XRANDR_CHECK: X11 display output appears connected."
  elif printf '%s\n' "$xrandr_output" | grep -Eiq "Can't open display"; then
    echo "XRANDR_CHECK: no X11 display is available; Wayland/wlr output is authoritative here."
  else
    echo "XRANDR_CHECK: X11 display output is unclear; inspect xrandr output above."
  fi
fi

section "Browser"
run command -v chromium-browser
run command -v chromium
sleep 3
run systemctl --user --no-pager --plain status searchable-storage-kiosk.service

section "Microphone"
run ls -l /dev/snd
run systemctl --user --no-pager --plain status searchable-storage-voice.service
run aplay -l
run aplay -L
if command -v arecord >/dev/null 2>&1; then
  run arecord -l
  if command -v pactl >/dev/null 2>&1; then
    run pactl list short sources
  fi

  if systemctl --user is-active --quiet searchable-storage-voice.service; then
    echo "Voice service is active and may hold the microphone device."
    echo "VOICE_CHECK: service is active; use /voice/tuning and /voice/calibration above for live mic evidence."
    echo "Stop searchable-storage-voice.service before raw arecord capture."
  else
    devices=("default")
    while IFS= read -r line; do
      if [[ "$line" =~ card[[:space:]]+([0-9]+):.*device[[:space:]]+([0-9]+): ]]; then
        devices+=("plughw:${BASH_REMATCH[1]},${BASH_REMATCH[2]}")
        devices+=("hw:${BASH_REMATCH[1]},${BASH_REMATCH[2]}")
      fi
    done < <(arecord -l 2>/dev/null || true)

    captured=""
    for device in "${devices[@]}"; do
      for rate in 16000 48000; do
        for channels in 1 2 4 6; do
          rm -f "$MIC_TEST_PATH"
          printf '$ arecord -D %s -r %s -c %s 2 second sample -> %s\n' \
            "$device" "$rate" "$channels" "$MIC_TEST_PATH"
          timeout 8s arecord -q -D "$device" -f S16_LE -r "$rate" -c "$channels" -d 2 \
            "$MIC_TEST_PATH" 2>&1 || true
          if [[ -s "$MIC_TEST_PATH" ]]; then
            captured="$device rate=$rate channels=$channels"
            break 3
          fi
        done
      done
    done

    if [[ -n "$captured" ]]; then
      echo "Captured microphone sample using $captured"
      ls -lh "$MIC_TEST_PATH"
    else
      echo "No microphone sample was captured."
    fi
  fi
else
  echo "arecord is not installed; install alsa-utils to test the mic."
fi

section "USB"
run lsusb
