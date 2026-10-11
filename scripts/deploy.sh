#!/usr/bin/env bash
# One-command redeploy to the Raspberry Pi.
#
#   scripts/deploy.sh            # deploy to the default host (pi5)
#   HOST=pi5 scripts/deploy.sh   # override the ssh host/alias
#
# Non-destructive: backs up the DB, rsyncs the source, reinstalls the package
# into the existing venv (no network, no dep changes), refreshes config, and
# restarts the service. Secrets (OPENROUTER_API_KEY) are NOT touched — set them
# once in $APP_DIR/secrets.env on the Pi.
set -euo pipefail

HOST="${HOST:-pi5}"
APP_DIR="${APP_DIR:-\$HOME/searchable-storage-led}"   # expanded on the Pi
SRC_DIR="${SRC_DIR:-\$HOME/searchable-storage-src}"    # expanded on the Pi
SERVICE="${SERVICE:-searchable-storage-led.service}"
PORT="${PORT:-8765}"

cd "$(dirname "$0")/.."

LOCAL_COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"
LOCAL_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
LOCAL_DIRTY="false"
if [[ -n "$(git status --porcelain 2>/dev/null || true)" ]]; then
  LOCAL_DIRTY="true"
fi
LOCAL_BUILT_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
LOCAL_VERSION="$(PYTHONPATH=src python3 - <<'PY'
from searchable_storage_led import __version__
print(__version__)
PY
)"

echo "==> Backing up the Pi database"
ssh "$HOST" "DB=$APP_DIR/state/inventory.db; [ -f \"\$DB\" ] && cp \"\$DB\" \"\$DB.bak-\$(date +%Y%m%d-%H%M%S)\" && echo backed up || echo '(no db yet)'"

echo "==> Rsyncing source to $HOST:$SRC_DIR"
ssh "$HOST" "mkdir -p $SRC_DIR"
rsync -az --delete --exclude __pycache__ --exclude '*.pyc' \
  src config scripts pyproject.toml README.md \
  "$HOST:$SRC_DIR/"

echo "==> Reinstalling package + refreshing config + restarting"
ssh "$HOST" \
  "APP_DIR=$APP_DIR SRC_DIR=$SRC_DIR SERVICE=$SERVICE COMMIT=$(printf '%q' "$LOCAL_COMMIT") BRANCH=$(printf '%q' "$LOCAL_BRANCH") DIRTY=$LOCAL_DIRTY BUILT_AT=$(printf '%q' "$LOCAL_BUILT_AT") VERSION=$(printf '%q' "$LOCAL_VERSION") bash -s" <<'REMOTE'
set -euo pipefail

if ! "$APP_DIR/.venv/bin/python" -c 'import mcp' >/dev/null 2>&1; then
  "$APP_DIR/.venv/bin/pip" install 'mcp==1.28.1' -q
fi
"$APP_DIR/.venv/bin/pip" install "$SRC_DIR" --no-deps --no-build-isolation -q
"$APP_DIR/.venv/bin/python" - <<'PY'
import json
import os
from pathlib import Path

app_dir = Path(os.environ["APP_DIR"])
payload = {
    "version": os.environ["VERSION"],
    "commit": os.environ["COMMIT"],
    "branch": os.environ["BRANCH"],
    "dirty": os.environ["DIRTY"] == "true",
    "built_at": os.environ["BUILT_AT"],
    "source": "scripts/deploy.sh",
}
(app_dir / "build.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
for pkg_dir in "$APP_DIR"/.venv/lib/python*/site-packages/searchable_storage_led; do
  [ -d "$pkg_dir" ] && cp "$APP_DIR/build.json" "$pkg_dir/build.json"
done
source_config="$SRC_DIR/config/led-controller.json"
installed_config="$APP_DIR/config/led-controller.json"
if [ -f "$installed_config" ]; then
  merged_config="$(mktemp "$APP_DIR/config/led-controller.deploy.XXXXXX.json")"
  "$APP_DIR/.venv/bin/python" -m searchable_storage_led.runtime_config \
    --source "$source_config" \
    --installed "$installed_config" \
    --output "$merged_config"
  mv "$merged_config" "$installed_config"
else
  cp "$source_config" "$installed_config"
fi
cp "$SRC_DIR/config/storage-layout.json" "$APP_DIR/config/"
if ls "$SRC_DIR"/config/*.md >/dev/null 2>&1; then cp "$SRC_DIR"/config/*.md "$APP_DIR/config/"; fi
systemctl --user restart "$SERVICE"
sleep 2
systemctl --user is-active "$SERVICE"
REMOTE

echo "==> Verifying"
ssh "$HOST" "for i in \$(seq 1 20); do curl -fsS http://127.0.0.1:$PORT/health >/dev/null 2>&1 && break; sleep 0.5; done
  curl -fsS http://127.0.0.1:$PORT/build \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(\"  build:\",d.get(\"label\") or d)'
  curl -fsS http://127.0.0.1:$PORT/assistant/status \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(\"  backend:\",d[\"effective_backend\"],\"| key:\",d[\"openai_key_present\"])'"
PI_IP="$(ssh "$HOST" "hostname -I | awk '{print \$1}'")"
echo "==> Done. http://$PI_IP:$PORT/  (also http://$HOST:$PORT/)"
