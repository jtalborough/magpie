#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/searchable-storage-led}"
CENTRAL_URL="${MAGPIE_CENTRAL_URL:-}"
DEVICE_ID="${MAGPIE_EDGE_DEVICE_ID:-main-cabinet}"
WORKSPACE_ID="${MAGPIE_EDGE_WORKSPACE_ID:-}"
PAIRING_CODE="${MAGPIE_EDGE_PAIRING_CODE:-}"
ACTIVATE_CENTRAL_EXPERIENCE="${MAGPIE_ACTIVATE_CENTRAL_EXPERIENCE:-false}"

usage() {
  cat <<'EOF'
Usage: connect-pi5-cabinet.sh --central-url URL --workspace-id ID [options]

Options:
  --device-id ID       Stable cabinet device ID (default: main-cabinet)
  --pairing-code CODE  Expiring code generated in Magpie Account
  --activate-central-experience
                         Point cabinet voice and kiosk at the central service
  --app-dir PATH       Installed Magpie directory
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --central-url) CENTRAL_URL="${2:-}"; shift 2 ;;
    --device-id) DEVICE_ID="${2:-}"; shift 2 ;;
    --workspace-id) WORKSPACE_ID="${2:-}"; shift 2 ;;
    --pairing-code) PAIRING_CODE="${2:-}"; shift 2 ;;
    --activate-central-experience) ACTIVATE_CENTRAL_EXPERIENCE=true; shift ;;
    --app-dir) APP_DIR="${2:-}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$CENTRAL_URL" || -z "$WORKSPACE_ID" ]]; then
  echo "--central-url and --workspace-id are required." >&2
  exit 2
fi

APP_DIR="$(cd "$APP_DIR" && pwd)"
ENV_PATH="${ENV_PATH:-$APP_DIR/secrets.env}"
PYTHON_BIN="${PYTHON_BIN:-$APP_DIR/.venv/bin/python}"
EDGE_CLI="${EDGE_CLI:-$APP_DIR/.venv/bin/searchable-storage-edge}"
EDGE_INSTALLER="${EDGE_INSTALLER:-$APP_DIR/scripts/install-pi5-edge-service.sh}"
VOICE_INSTALLER="${VOICE_INSTALLER:-$APP_DIR/scripts/install-pi5-voice-service.sh}"
KIOSK_INSTALLER="${KIOSK_INSTALLER:-$APP_DIR/scripts/install-pi5-kiosk.sh}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
CURL_BIN="${CURL_BIN:-curl}"
SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
BRIDGE_HEALTH_ATTEMPTS="${MAGPIE_BRIDGE_HEALTH_ATTEMPTS:-20}"
BRIDGE_HEALTH_SLEEP_SECONDS="${MAGPIE_BRIDGE_HEALTH_SLEEP_SECONDS:-1}"
ACTIVATION_BACKUP=""
ACTIVATION_ROLLBACK_ARMED=false

activation_services=(
  searchable-storage-voice.service
  searchable-storage-screen.service
  searchable-storage-kiosk.service
)

snapshot_activation_state() {
  ACTIVATION_BACKUP="$(mktemp -d "${TMPDIR:-/tmp}/magpie-cabinet-activation.XXXXXX")"
  mkdir -p "$ACTIVATION_BACKUP/units"
  for service in "${activation_services[@]}"; do
    unit="$SYSTEMD_USER_DIR/$service"
    if [[ -f "$unit" ]]; then
      cp -p "$unit" "$ACTIVATION_BACKUP/units/$service"
      touch "$ACTIVATION_BACKUP/$service.present"
    fi
    if "$SYSTEMCTL_BIN" --user is-enabled --quiet "$service" >/dev/null 2>&1; then
      touch "$ACTIVATION_BACKUP/$service.enabled"
    fi
    if "$SYSTEMCTL_BIN" --user is-active --quiet "$service" >/dev/null 2>&1; then
      touch "$ACTIVATION_BACKUP/$service.active"
    fi
  done
  ACTIVATION_ROLLBACK_ARMED=true
}

# Invoked indirectly by the EXIT trap.
# shellcheck disable=SC2329
restore_activation_state() {
  local restored=true
  printf 'Central experience activation failed; restoring local voice and kiosk services.\n' >&2
  for service in "${activation_services[@]}"; do
    if "$SYSTEMCTL_BIN" --user is-active --quiet "$service"; then
      "$SYSTEMCTL_BIN" --user stop "$service" || restored=false
    fi
  done
  mkdir -p "$SYSTEMD_USER_DIR"
  for service in "${activation_services[@]}"; do
    unit="$SYSTEMD_USER_DIR/$service"
    if [[ -f "$ACTIVATION_BACKUP/$service.present" ]]; then
      cp -p "$ACTIVATION_BACKUP/units/$service" "$unit" || restored=false
    else
      rm -f "$unit" || restored=false
    fi
  done
  "$SYSTEMCTL_BIN" --user daemon-reload || restored=false
  for service in "${activation_services[@]}"; do
    if [[ -f "$ACTIVATION_BACKUP/$service.enabled" ]]; then
      "$SYSTEMCTL_BIN" --user enable "$service" || restored=false
      "$SYSTEMCTL_BIN" --user is-enabled --quiet "$service" || restored=false
    else
      "$SYSTEMCTL_BIN" --user disable "$service" >/dev/null 2>&1 || true
      if "$SYSTEMCTL_BIN" --user is-enabled --quiet "$service"; then
        restored=false
      fi
    fi
    if [[ -f "$ACTIVATION_BACKUP/$service.active" ]]; then
      "$SYSTEMCTL_BIN" --user restart "$service" || restored=false
      "$SYSTEMCTL_BIN" --user is-active --quiet "$service" || restored=false
    else
      if "$SYSTEMCTL_BIN" --user is-active --quiet "$service"; then
        "$SYSTEMCTL_BIN" --user stop "$service" || restored=false
      fi
      if "$SYSTEMCTL_BIN" --user is-active --quiet "$service"; then
        restored=false
      fi
    fi
  done
  if [[ "$restored" == "true" ]]; then
    printf 'Previous local voice and kiosk service state restored.\n' >&2
  else
    printf 'Rollback did not fully restore the previous cabinet service state; inspect user services immediately.\n' >&2
  fi
}

# Invoked indirectly by the EXIT trap.
# shellcheck disable=SC2329
cleanup_activation_transaction() {
  local status=$?
  trap - EXIT
  if [[ "$ACTIVATION_ROLLBACK_ARMED" == "true" && -n "$ACTIVATION_BACKUP" ]]; then
    restore_activation_state
  fi
  if [[ -n "$ACTIVATION_BACKUP" ]]; then
    rm -rf "$ACTIVATION_BACKUP"
  fi
  exit "$status"
}

trap cleanup_activation_transaction EXIT

required_paths=("$PYTHON_BIN" "$EDGE_CLI" "$EDGE_INSTALLER")
if [[ "$ACTIVATE_CENTRAL_EXPERIENCE" == "true" ]]; then
  required_paths+=("$VOICE_INSTALLER" "$KIOSK_INSTALLER")
fi
for path in "${required_paths[@]}"; do
  if [[ ! -x "$path" ]]; then
    echo "Required cabinet component is not executable: $path" >&2
    exit 1
  fi
done

"$PYTHON_BIN" - "$CENTRAL_URL" "$DEVICE_ID" "$ENV_PATH" "$PAIRING_CODE" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

import httpx

from searchable_storage_led.edge_worker import load_edge_machine_auth, validate_worker_urls
from searchable_storage_led.machine_auth import cloudflare_access_headers
from searchable_storage_led.screen_bridge import validate_screen_bridge

central_url, device_id, env_path, pairing_code = sys.argv[1:]
validate_worker_urls(central_url, "http://127.0.0.1:8765")
validate_screen_bridge(central_url, device_id, "cabinet-connector")
if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", device_id):
    raise SystemExit("device ID must contain lowercase letters, numbers, and hyphens")
path = Path(env_path)
client_id, client_secret = load_edge_machine_auth(path)
headers = cloudflare_access_headers(client_id, client_secret)
if os.environ.get("MAGPIE_SKIP_CENTRAL_PREFLIGHT") != "1":
    try:
        response = httpx.get(
            f"{central_url.rstrip('/')}/build",
            headers=headers,
            follow_redirects=False,
            timeout=15.0,
        )
        if response.is_redirect and not headers:
            raise SystemExit(
                "central service requires Cloudflare Access machine credentials; "
                "run scripts/configure-pi-edge-access.sh first"
            )
        response.raise_for_status()
        build = response.json()
        if not isinstance(build, dict) or not build.get("commit"):
            raise SystemExit("central /build response does not identify a deployed commit")
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise SystemExit(f"central service preflight failed: {exc}") from exc
has_token = path.exists() and any(
    line.startswith("MAGPIE_EDGE_TOKEN=") and line.partition("=")[2].strip()
    for line in path.read_text(encoding="utf-8").splitlines()
)
if not pairing_code.strip() and not has_token:
    raise SystemExit("a pairing code is required because this cabinet has no device credential")
PY

if [[ -n "$PAIRING_CODE" ]]; then
  "$EDGE_CLI" \
    --central-url "$CENTRAL_URL" \
    --device-id "$DEVICE_ID" \
    --workspace-id "$WORKSPACE_ID" \
    --pairing-code "$PAIRING_CODE" \
    --secrets-path "$ENV_PATH" \
    --pair-only
fi

if [[ ! -f "$ENV_PATH" ]] || ! grep -q '^MAGPIE_EDGE_TOKEN=' "$ENV_PATH"; then
  echo "Cabinet pairing did not create MAGPIE_EDGE_TOKEN in $ENV_PATH." >&2
  exit 1
fi

APP_DIR="$APP_DIR" ENV_PATH="$ENV_PATH" \
MAGPIE_CENTRAL_URL="$CENTRAL_URL" \
MAGPIE_EDGE_DEVICE_ID="$DEVICE_ID" \
MAGPIE_EDGE_WORKSPACE_ID="$WORKSPACE_ID" \
  "$EDGE_INSTALLER"

services=(
  searchable-storage-led.service
  searchable-storage-edge.service
)

if [[ "$ACTIVATE_CENTRAL_EXPERIENCE" == "true" ]]; then
  snapshot_activation_state

  APP_DIR="$APP_DIR" ENV_PATH="$ENV_PATH" \
  MAGPIE_VOICE_API_URL="$CENTRAL_URL" \
  MAGPIE_EDGE_DEVICE_ID="$DEVICE_ID" \
  MAGPIE_EDGE_WORKSPACE_ID="$WORKSPACE_ID" \
    "$VOICE_INSTALLER"

  APP_DIR="$APP_DIR" ENV_PATH="$ENV_PATH" \
  MAGPIE_SCREEN_API_URL="$CENTRAL_URL" \
  MAGPIE_EDGE_DEVICE_ID="$DEVICE_ID" \
  MAGPIE_EDGE_WORKSPACE_ID="$WORKSPACE_ID" \
    "$KIOSK_INSTALLER"

  services+=(
    searchable-storage-voice.service
    searchable-storage-screen.service
    searchable-storage-kiosk.service
  )
fi

for service in "${services[@]}"; do
  "$SYSTEMCTL_BIN" --user is-active --quiet "$service"
done

if [[ "${MAGPIE_SKIP_CENTRAL_PREFLIGHT:-}" != "1" ]]; then
  "$PYTHON_BIN" - "$CENTRAL_URL" "$DEVICE_ID" "$WORKSPACE_ID" "$ENV_PATH" <<'PY'
import sys
import time
from pathlib import Path

import httpx

from searchable_storage_led.edge_worker import load_edge_machine_auth
from searchable_storage_led.machine_auth import cloudflare_access_headers

central_url, device_id, workspace_id, env_path = sys.argv[1:]
path = Path(env_path)
token = next(
    (
        line.partition("=")[2].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("MAGPIE_EDGE_TOKEN=")
    ),
    "",
)
if not token:
    raise SystemExit("cabinet device credential is missing after edge installation")
client_id, client_secret = load_edge_machine_auth(path)
headers = {
    "Authorization": f"Bearer {token}",
    "X-Magpie-Workspace": workspace_id,
    **cloudflare_access_headers(client_id, client_secret),
}
latest = {}
for _ in range(30):
    try:
        response = httpx.get(
            f"{central_url.rstrip('/')}/edge/devices/{device_id}/status",
            headers=headers,
            follow_redirects=False,
            timeout=10.0,
        )
        response.raise_for_status()
        latest = response.json().get("device") or {}
        if latest.get("command_ready"):
            print(
                "central edge verified:",
                f"device={device_id}",
                f"connection={latest.get('connection_state', 'unknown')}",
                f"version={latest.get('version', '')}",
            )
            break
    except (httpx.HTTPError, ValueError):
        latest = {}
    time.sleep(1)
else:
    raise SystemExit(
        "edge service is active locally, but the central service did not report "
        f"{device_id} command-ready; last status={latest or 'unavailable'}"
    )
PY
fi

if [[ "$ACTIVATE_CENTRAL_EXPERIENCE" != "true" ]]; then
  printf 'Cabinet edge connected to %s in workspace %s. Local voice and kiosk remain unchanged.\n' "$CENTRAL_URL" "$WORKSPACE_ID"
  printf 'After central inventory verification, rerun with --activate-central-experience and no pairing code.\n'
  exit 0
fi

for _ in $(seq 1 "$BRIDGE_HEALTH_ATTEMPTS"); do
  if "$CURL_BIN" -fsS --max-time 3 'http://127.0.0.1:8767/bridge/ready?require_voice=true' >/dev/null; then
    ACTIVATION_ROLLBACK_ARMED=false
    printf 'Cabinet connected to %s in workspace %s.\n' "$CENTRAL_URL" "$WORKSPACE_ID"
    exit 0
  fi
  sleep "$BRIDGE_HEALTH_SLEEP_SECONDS"
done

echo "Cabinet services are active, but central screen and voice readiness was not verified." >&2
exit 1
