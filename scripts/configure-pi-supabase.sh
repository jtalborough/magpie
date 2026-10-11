#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/searchable-storage-led}"
SERVICE_NAME="${SERVICE_NAME:-searchable-storage-led}"
CONFIG_PATH="${CONFIG_PATH:-$APP_DIR/config/led-controller.json}"
ENV_PATH="${ENV_PATH:-$APP_DIR/secrets.env}"
SOURCE_DB="${SOURCE_DB:-$APP_DIR/state/inventory.db}"
PUBLIC_BASE_URL="${MAGPIE_PUBLIC_BASE_URL:-https://magpie.example.com}"
export PUBLIC_BASE_URL

for name in SUPABASE_URL SUPABASE_WORKSPACE_ID SUPABASE_SERVICE_ROLE_KEY; do
  if [[ -z "${!name:-}" ]]; then
    echo "$name is required." >&2
    exit 2
  fi
done

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Magpie config was not found: $CONFIG_PATH" >&2
  exit 2
fi
if [[ ! -f "$SOURCE_DB" ]]; then
  echo "Magpie SQLite database was not found: $SOURCE_DB" >&2
  exit 2
fi

mkdir -p "$APP_DIR/config" "$APP_DIR/state"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
config_backup="$APP_DIR/config/led-controller.pre-supabase-$timestamp.json"
database_backup="$APP_DIR/state/inventory.pre-supabase-$timestamp.db"
candidate_config="$(mktemp "$APP_DIR/config/led-controller.supabase.XXXXXX.json")"
switched=false
service_stopped=false

rollback() {
  status=$?
  rm -f "$candidate_config"
  if [[ "$switched" == true && -f "$config_backup" ]]; then
    cp "$config_backup" "$CONFIG_PATH"
  fi
  if [[ "$service_stopped" == true ]]; then
    systemctl --user restart "$SERVICE_NAME.service" || true
  fi
  exit "$status"
}
trap rollback ERR INT TERM

cp "$CONFIG_PATH" "$config_backup"

python3 - "$CONFIG_PATH" "$candidate_config" <<'PY'
import json
import os
import sys
from pathlib import Path

source, destination = map(Path, sys.argv[1:])
config = json.loads(source.read_text(encoding="utf-8"))
config["storage"] = {
    "backend": "supabase",
    "supabase_url": os.environ["SUPABASE_URL"].rstrip("/"),
    "api_key_env": "SUPABASE_SERVICE_ROLE_KEY",
    "workspace_id": os.environ["SUPABASE_WORKSPACE_ID"],
    "photo_bucket": os.environ.get("SUPABASE_PHOTO_BUCKET", "magpie-photos"),
    "timeout_seconds": 10,
}
config.setdefault("zebra", {})["base_url"] = os.environ["PUBLIC_BASE_URL"].rstrip("/")
destination.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
PY

systemctl --user stop "$SERVICE_NAME.service"
service_stopped=true
cp "$SOURCE_DB" "$database_backup"

PUBLIC_BASE_URL="$PUBLIC_BASE_URL" \
  "$APP_DIR/.venv/bin/searchable-storage-migrate-supabase" \
  --config "$candidate_config" \
  --database "$SOURCE_DB" \
  --workspace-id "$SUPABASE_WORKSPACE_ID"

PUBLIC_BASE_URL="$PUBLIC_BASE_URL" \
  "$APP_DIR/.venv/bin/searchable-storage-supabase-check" \
  --config "$candidate_config"

python3 - "$ENV_PATH" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
key_name = "SUPABASE_SERVICE_ROLE_KEY"
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
lines = [line for line in lines if not line.startswith(f"{key_name}=")]
lines.append(f"{key_name}={os.environ[key_name]}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
path.chmod(0o600)
PY

mv "$candidate_config" "$CONFIG_PATH"
switched=true
systemctl --user restart "$SERVICE_NAME.service"

curl --fail --silent --show-error --retry 10 --retry-delay 2 --retry-connrefused \
  http://127.0.0.1:8765/health | python3 -c '
import json, sys
health = json.load(sys.stdin)
storage = health.get("storage") or {}
if storage.get("backend") != "supabase":
    raise SystemExit(f"Pi did not switch to Supabase: {storage}")
print("Supabase active for workspace {}".format(storage.get("workspace_id", "")))
'

switched=false
service_stopped=false
trap - ERR INT TERM
echo "Migrated Magpie to Supabase."
echo "SQLite backup: $database_backup"
echo "Config backup: $config_backup"
