#!/usr/bin/env bash
# Promote an immutable Magpie image on a central-service host.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${MAGPIE_COMPOSE_FILE:-$ROOT_DIR/compose.hosted.yaml}"
CONFIG_FILE="${MAGPIE_CONFIG_FILE:-$ROOT_DIR/config/container-hosted.json}"
IMAGE="${MAGPIE_IMAGE:-}"
HOST_PORT="${MAGPIE_HOST_PORT:-8766}"
BASE_URL="${MAGPIE_LOCAL_BASE_URL:-http://127.0.0.1:$HOST_PORT}"
VERIFY_ATTEMPTS="${MAGPIE_VERIFY_ATTEMPTS:-30}"
VERIFY_INTERVAL_SECONDS="${MAGPIE_VERIFY_INTERVAL_SECONDS:-2}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
CURL_BIN="${CURL_BIN:-curl}"
REQUIRE_EDGE_ORIGIN="${MAGPIE_REQUIRE_EDGE_ORIGIN:-false}"
VERIFY_EXTERNAL_ORIGINS="${MAGPIE_VERIFY_EXTERNAL_ORIGINS:-false}"
ORIGIN_CHECK_SCRIPT="${MAGPIE_ORIGIN_CHECK_SCRIPT:-$ROOT_DIR/scripts/check-secure-origin.sh}"

fail() {
  echo "deploy-hosted: $*" >&2
  exit 1
}

[[ -n "$IMAGE" ]] || fail "MAGPIE_IMAGE is required"
[[ -f "$COMPOSE_FILE" ]] || fail "compose file not found: $COMPOSE_FILE"
[[ -f "$CONFIG_FILE" ]] || fail "runtime config not found: $CONFIG_FILE"
[[ -n "${SUPABASE_PUBLISHABLE_KEY:-}" ]] || fail "SUPABASE_PUBLISHABLE_KEY is required"
[[ -n "${SUPABASE_SERVICE_ROLE_KEY:-}" ]] || fail "SUPABASE_SERVICE_ROLE_KEY is required"
if ! PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
import os

from searchable_storage_led.supabase_keys import require_supabase_publishable_key

try:
    require_supabase_publishable_key(
        os.environ.get("SUPABASE_PUBLISHABLE_KEY", ""),
        setting="SUPABASE_PUBLISHABLE_KEY",
    )
except ValueError as exc:
    raise SystemExit(f"deploy-hosted: {exc}") from exc
PY
then
  exit 1
fi
[[ -n "${OPENAI_API_KEY:-}" || -n "${OPENROUTER_API_KEY:-}" || \
   -n "${CLOUDFLARE_AI_API_TOKEN:-}" ]] || \
  fail "an OpenAI, OpenRouter, or Cloudflare AI credential is required"
[[ -z "${CLOUDFLARE_AI_API_TOKEN:-}" || -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]] || \
  fail "CLOUDFLARE_ACCOUNT_ID is required with CLOUDFLARE_AI_API_TOKEN"
[[ "$VERIFY_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || fail "MAGPIE_VERIFY_ATTEMPTS must be positive"
[[ "$REQUIRE_EDGE_ORIGIN" == "true" || "$REQUIRE_EDGE_ORIGIN" == "false" ]] || \
  fail "MAGPIE_REQUIRE_EDGE_ORIGIN must be true or false"
[[ "$VERIFY_EXTERNAL_ORIGINS" == "true" || "$VERIFY_EXTERNAL_ORIGINS" == "false" ]] || \
  fail "MAGPIE_VERIFY_EXTERNAL_ORIGINS must be true or false"
if [[ "$VERIFY_EXTERNAL_ORIGINS" == "true" ]]; then
  [[ "${MAGPIE_PUBLIC_ORIGIN:-}" == https://* ]] || \
    fail "MAGPIE_PUBLIC_ORIGIN is required for external verification"
  [[ -x "$ORIGIN_CHECK_SCRIPT" ]] || fail "origin verifier not executable: $ORIGIN_CHECK_SCRIPT"
  if [[ "$REQUIRE_EDGE_ORIGIN" == "true" ]]; then
    [[ "${MAGPIE_EDGE_ORIGIN:-}" == https://* ]] || \
      fail "MAGPIE_EDGE_ORIGIN is required for external verification"
    [[ -n "${CF_ACCESS_CLIENT_ID:-}" ]] || fail "CF_ACCESS_CLIENT_ID is required"
    [[ -n "${CF_ACCESS_CLIENT_SECRET:-}" ]] || fail "CF_ACCESS_CLIENT_SECRET is required"
  fi
fi

COMPOSE_FILE="$(cd "$(dirname "$COMPOSE_FILE")" && pwd)/$(basename "$COMPOSE_FILE")"
CONFIG_FILE="$(cd "$(dirname "$CONFIG_FILE")" && pwd)/$(basename "$CONFIG_FILE")"

derived_commit=""
if [[ "$IMAGE" =~ :sha-([0-9a-fA-F]{7,64})$ ]]; then
  derived_commit="${BASH_REMATCH[1]}"
elif [[ "$IMAGE" =~ @sha256:[0-9a-fA-F]{64}$ ]]; then
  :
elif [[ "${MAGPIE_ALLOW_MUTABLE_IMAGE:-false}" != "true" ]]; then
  fail "MAGPIE_IMAGE must use an immutable sha-* tag or sha256 digest"
fi
EXPECTED_COMMIT="${MAGPIE_EXPECTED_COMMIT:-$derived_commit}"

python3 - "$CONFIG_FILE" "$REQUIRE_EDGE_ORIGIN" <<'PY'
import json
import sys
from urllib.parse import urlsplit

path, require_edge_origin = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    config = json.load(handle)
errors = []
if config.get("backend") != "none" or int(config.get("pixel_count") or 0) != 0:
    errors.append("central service must be hardware-neutral")
if (config.get("storage") or {}).get("backend") != "supabase":
    errors.append("central service must use Supabase storage")
if (config.get("auth") or {}).get("mode") != "supabase":
    errors.append("central service must enable Supabase authentication")
storage = config.get("storage") or {}
auth = config.get("auth") or {}
storage_url = str(storage.get("supabase_url") or "")
auth_url = str(auth.get("supabase_url") or "")
if "PROJECT_REF" in storage_url or not storage_url.startswith("https://"):
    errors.append("storage Supabase URL is missing or still a placeholder")
if auth_url != storage_url:
    errors.append("storage and auth must use the same Supabase project")
if str(storage.get("workspace_id") or "") != str(auth.get("workspace_id") or ""):
    errors.append("storage and auth workspace IDs must match")
if auth.get("multi_workspace") is not True:
    errors.append("central service must use request-scoped multi-workspace auth")
if str(storage.get("workspace_id") or "") or str(auth.get("workspace_id") or ""):
    errors.append("central service workspace IDs must be request-scoped")
edge = config.get("edge") or {}
if edge.get("mode") != "workspace" or str(edge.get("target_device_id") or ""):
    errors.append("central service must route edge commands by workspace location target")
machine_url = str(edge.get("machine_url") or "")
parsed_machine_url = urlsplit(machine_url)
valid_machine_origin = bool(
    parsed_machine_url.scheme == "https"
    and parsed_machine_url.netloc
    and parsed_machine_url.path in {"", "/"}
    and not parsed_machine_url.query
    and not parsed_machine_url.fragment
)
if require_edge_origin == "true" and not valid_machine_origin:
    errors.append(
        "production deployment requires an HTTPS machine-only edge origin"
    )
if errors:
    raise SystemExit("deploy-hosted: " + "; ".join(errors))
PY

export MAGPIE_IMAGE="$IMAGE"
export MAGPIE_CONFIG_FILE="$CONFIG_FILE"
export MAGPIE_HOST_PORT="$HOST_PORT"
compose=("$DOCKER_BIN" compose -f "$COMPOSE_FILE")
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

previous_container="$("${compose[@]}" ps -q magpie 2>/dev/null || true)"
previous_image=""
previous_runtime_config="$work_dir/previous-runtime.json"
previous_runtime_config_available=false
if [[ -n "$previous_container" ]]; then
  previous_image="$(
    "$DOCKER_BIN" inspect --format '{{.Config.Image}}' "$previous_container" \
      2>/dev/null || true
  )"
  [[ -n "$previous_image" ]] || \
    fail "could not identify the currently running image before deployment"
  if ! "$DOCKER_BIN" cp \
    "$previous_container:/runtime/runtime.json" "$previous_runtime_config"; then
    echo "deploy-hosted: previous container uses the legacy runtime path" >&2
    if ! "$DOCKER_BIN" cp \
      "$previous_container:/app/config/runtime.json" "$previous_runtime_config"; then
      fail "could not snapshot previous runtime config before deployment"
    fi
  fi
  [[ -s "$previous_runtime_config" ]] || \
    fail "previous runtime config snapshot is empty"
  chmod 0600 "$previous_runtime_config"
  previous_runtime_config_available=true
fi

wait_for_readiness() {
  local output="$1"
  local attempt
  for ((attempt = 1; attempt <= VERIFY_ATTEMPTS; attempt++)); do
    if "$CURL_BIN" --fail --silent --show-error --max-time 10 \
      "$BASE_URL/ready" >"$output"; then
      return 0
    fi
    sleep "$VERIFY_INTERVAL_SECONDS"
  done
  return 1
}

verify_runtime() {
  local health_file="$work_dir/health.json"
  local build_file="$work_dir/build.json"
  local auth_file="$work_dir/auth-config.json"
  wait_for_readiness "$health_file" || return 1
  "$CURL_BIN" --fail --silent --show-error --max-time 10 \
    "$BASE_URL/build" >"$build_file" || return 1
  "$CURL_BIN" --fail --silent --show-error --max-time 10 \
    "$BASE_URL/auth/config" >"$auth_file" || return 1
  PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
    python3 - "$health_file" "$build_file" "$auth_file" "$EXPECTED_COMMIT" <<'PY'
import json
import sys

from searchable_storage_led.supabase_keys import is_supabase_publishable_key

health_path, build_path, auth_path, expected_commit = sys.argv[1:]
with open(health_path, encoding="utf-8") as handle:
    health = json.load(handle)
with open(build_path, encoding="utf-8") as handle:
    build = json.load(handle)
with open(auth_path, encoding="utf-8") as handle:
    auth_config = json.load(handle)
assistant = health.get("agent") or {}
health_auth = health.get("auth") or {}
runtime = health.get("runtime") or {}
checks = {
    "ready response": health.get("ok") is True and health.get("ready") is True,
    "storage probe": (health.get("storage_probe") or {}).get("ok") is True,
    "operational state": (
        health.get("operational_state_probe") or {}
    ).get("ok") is True,
    "hardware-neutral backend": health.get("backend") == "none",
    "zero central pixels": int(health.get("pixel_count") or 0) == 0,
    "Supabase storage": (health.get("storage") or {}).get("backend") == "supabase",
    "request-scoped storage": (health.get("storage") or {}).get("workspace_id") == "request-scoped",
    "Supabase authentication": health_auth.get("mode") == "supabase",
    "authentication enabled": health_auth.get("enabled") is True,
    "multi-workspace authentication": health_auth.get("multi_workspace") is True,
    "request-scoped authentication": health_auth.get("workspace_id") == "request-scoped",
    "workspace edge routing": (health.get("edge") or {}).get("mode") == "workspace",
    "central runtime role": runtime.get("role") == "central_service",
    "central runtime is hardware-neutral": runtime.get("hardware_owner") is False,
    "request-scoped runtime": runtime.get("workspace_scope") == "request",
    "workspace edge command queue": (
        runtime.get("edge_execution") == "workspace_queue"
        and runtime.get("edge_queue_enabled") is True
    ),
    "auth config schema": auth_config.get("schema") == "magpie.auth.config.v1",
    "browser authentication mode": auth_config.get("mode") == "supabase",
    "browser multi-workspace mode": auth_config.get("multi_workspace") is True,
    "browser Supabase origin": str(auth_config.get("supabase_url") or "").startswith(
        "https://"
    ),
    "browser client key": is_supabase_publishable_key(
        auth_config.get("publishable_key")
    ),
    "configured storage agent": assistant.get("ready") is True,
    "non-deterministic agent backend": assistant.get("effective_backend")
    not in {"deterministic", "openai_unconfigured"},
    "build commit": bool(build.get("commit")),
}
failed = [name for name, passed in checks.items() if not passed]
commit = str(build.get("commit") or "")
if expected_commit and not commit.lower().startswith(expected_commit.lower()):
    failed.append(f"expected commit {expected_commit}, received {commit or 'none'}")
if failed:
    raise SystemExit("deploy-hosted: verification failed: " + "; ".join(failed))
print("deployed build:", build.get("label") or commit)
print(
    "storage agent:",
    assistant.get("provider") or "unknown",
    assistant.get("model") or "unknown",
    assistant.get("api") or "unknown",
)
PY
}

verify_external_origins() {
  [[ "$VERIFY_EXTERNAL_ORIGINS" == "true" ]] || return 0
  env -u CF_ACCESS_CLIENT_ID -u CF_ACCESS_CLIENT_SECRET \
    MAGPIE_ORIGIN_MODE=public \
    MAGPIE_PUBLIC_ORIGIN="$MAGPIE_PUBLIC_ORIGIN" \
    "$ORIGIN_CHECK_SCRIPT" || return 1
  if [[ "$REQUIRE_EDGE_ORIGIN" == "true" ]]; then
    MAGPIE_ORIGIN_MODE=access \
      MAGPIE_PUBLIC_ORIGIN="$MAGPIE_EDGE_ORIGIN" \
      "$ORIGIN_CHECK_SCRIPT" || return 1
  fi
}

restore_previous_runtime_config() {
  [[ "$previous_runtime_config_available" == "true" ]] || return 0
  export MAGPIE_IMAGE="$previous_image"
  export MAGPIE_CONFIG_FILE="$previous_runtime_config"
  "${compose[@]}" run --rm magpie-data-init
}

rollback() {
  local reason="$1"
  echo "deploy-hosted: $reason; rolling back" >&2
  if [[ -n "$previous_image" ]]; then
    if ! restore_previous_runtime_config; then
      echo "deploy-hosted: previous runtime config could not be restored" >&2
      return 1
    fi
    "${compose[@]}" up -d --no-deps magpie
    if wait_for_readiness "$work_dir/rollback-readiness.json"; then
      echo "deploy-hosted: restored $previous_image" >&2
    else
      echo "deploy-hosted: rollback started but did not become healthy" >&2
    fi
  else
    "${compose[@]}" stop magpie >/dev/null 2>&1 || true
    echo "deploy-hosted: no previous image was available" >&2
  fi
}

echo "==> Pulling immutable image $IMAGE"
"${compose[@]}" pull magpie

echo "==> Preparing protected runtime config and operational storage"
if ! "${compose[@]}" run --rm magpie-data-init; then
  fail "runtime config and operational storage preparation failed"
fi

echo "==> Verifying Supabase schema and service access"
if ! "${compose[@]}" --profile verify run --rm magpie-preflight; then
  if ! restore_previous_runtime_config; then
    fail "Supabase preflight failed and previous runtime config could not be restored"
  fi
  fail "Supabase preflight failed; previous runtime config restored"
fi

echo "==> Promoting central service"
if ! "${compose[@]}" up -d --no-deps magpie; then
  rollback "container promotion failed"
  exit 1
fi
if ! verify_runtime; then
  rollback "new service failed runtime verification"
  exit 1
fi
if ! verify_external_origins; then
  rollback "new service failed external origin verification"
  exit 1
fi

echo "==> Hosted Magpie is healthy at $BASE_URL"
