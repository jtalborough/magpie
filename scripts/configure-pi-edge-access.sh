#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/searchable-storage-led}"
ENV_PATH="${ENV_PATH:-$APP_DIR/secrets.env}"

for name in MAGPIE_CF_ACCESS_CLIENT_ID MAGPIE_CF_ACCESS_CLIENT_SECRET; do
  if [[ -z "${!name:-}" ]]; then
    echo "$name is required; edge machine access was not configured." >&2
    exit 2
  fi
done

python3 - "$ENV_PATH" <<'PY'
import os
import re
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
names = ("MAGPIE_CF_ACCESS_CLIENT_ID", "MAGPIE_CF_ACCESS_CLIENT_SECRET")
values = {name: os.environ[name].strip() for name in names}
if any(not re.fullmatch(r"[A-Za-z0-9._~+/=-]{8,512}", value) for value in values.values()):
    raise SystemExit("Cloudflare Access credentials are empty or contain invalid characters")

path.parent.mkdir(parents=True, exist_ok=True)
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
lines = [
    line
    for line in lines
    if not any(line.startswith(f"{name}=") for name in names)
]
lines.extend(f"{name}={values[name]}" for name in names)
with tempfile.NamedTemporaryFile(
    mode="w",
    encoding="utf-8",
    dir=path.parent,
    prefix=f".{path.name}.",
    delete=False,
) as handle:
    handle.write("\n".join(lines) + "\n")
    temporary = Path(handle.name)
temporary.chmod(0o600)
temporary.replace(path)
PY

echo "Configured Cloudflare Access machine authentication in $ENV_PATH."
