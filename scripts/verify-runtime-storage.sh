#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/searchable-storage-led}"
CONFIG_PATH="${MAGPIE_CONFIG_FILE:-$APP_DIR/config/led-controller.json}"
ENV_PATH="${MAGPIE_ENV_FILE:-$APP_DIR/secrets.env}"

backend="$($APP_DIR/.venv/bin/python - "$CONFIG_PATH" <<'PY'
import sys

from searchable_storage_led.config import LedConfig

print(LedConfig.from_path(sys.argv[1]).storage.backend)
PY
)"

if [[ "$backend" != "supabase" ]]; then
  printf 'Storage preflight: sqlite (no remote schema check required)\n'
  exit 0
fi

if [[ -f "$ENV_PATH" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_PATH"
  set +a
fi

"$APP_DIR/.venv/bin/searchable-storage-supabase-check" --config "$CONFIG_PATH"
