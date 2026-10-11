#!/usr/bin/env bash
set -euo pipefail

PUBLIC_ORIGIN="${MAGPIE_PUBLIC_ORIGIN:-https://magpie.example.com}"
ORIGIN_MODE="${MAGPIE_ORIGIN_MODE:-public}"
CURL_BIN="${CURL_BIN:-curl}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ACCESS_CLIENT_ID="${CF_ACCESS_CLIENT_ID:-}"
ACCESS_CLIENT_SECRET="${CF_ACCESS_CLIENT_SECRET:-}"
EXPECTED_COMMIT="${MAGPIE_EXPECTED_COMMIT:-}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$PUBLIC_ORIGIN" != https://* ]]; then
  echo "MAGPIE_PUBLIC_ORIGIN must use HTTPS: $PUBLIC_ORIGIN" >&2
  exit 2
fi
if [[ "$ORIGIN_MODE" != "public" && "$ORIGIN_MODE" != "access" ]]; then
  echo "MAGPIE_ORIGIN_MODE must be public or access." >&2
  exit 2
fi
if [[ -n "$ACCESS_CLIENT_ID" && -z "$ACCESS_CLIENT_SECRET" ]] || \
   [[ -z "$ACCESS_CLIENT_ID" && -n "$ACCESS_CLIENT_SECRET" ]]; then
  echo "CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET must be set together." >&2
  exit 2
fi
if [[ "$ORIGIN_MODE" == "public" && -n "$ACCESS_CLIENT_ID" ]]; then
  echo "Public-origin checks must run without Cloudflare Access credentials." >&2
  exit 2
fi
if [[ -n "$EXPECTED_COMMIT" && ! "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "MAGPIE_EXPECTED_COMMIT must be an exact lowercase 40-character Git commit." >&2
  exit 2
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

request() {
  local path="$1"
  local headers="$2"
  local body="$3"
  local args=(
    --silent
    --show-error
    --max-time 30
    --dump-header "$headers"
    --output "$body"
    --write-out '%{http_code}'
  )
  if [[ -n "$ACCESS_CLIENT_ID" ]]; then
    args+=(
      --header "CF-Access-Client-Id: $ACCESS_CLIENT_ID"
      --header "CF-Access-Client-Secret: $ACCESS_CLIENT_SECRET"
    )
  fi
  "$CURL_BIN" "${args[@]}" "${PUBLIC_ORIGIN%/}$path"
}

validate_json() {
  local kind="$1"
  local path="$2"
  PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" - "$kind" "$path" "$EXPECTED_COMMIT" <<'PY'
import json
import sys

from searchable_storage_led.supabase_keys import is_supabase_publishable_key

kind, path, expected_commit = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    payload = json.load(handle)
if kind == "build":
    if not payload.get("commit") or not payload.get("label"):
        raise SystemExit("secure /build response is missing commit or label")
    if expected_commit and payload.get("commit") != expected_commit:
        raise SystemExit(
            "secure /build commit does not match the monitored release: "
            f"expected {expected_commit}, received {payload.get('commit')}"
        )
elif kind == "manifest":
    if payload.get("short_name") != "Magpie" or payload.get("display") != "standalone":
        raise SystemExit("secure manifest is not the installable Magpie PWA")
elif kind == "auth":
    if payload.get("schema") != "magpie.auth.config.v1":
        raise SystemExit("public /auth/config response has the wrong schema")
    if payload.get("mode") != "supabase" or payload.get("multi_workspace") is not True:
        raise SystemExit("public Magpie origin is not configured for customer workspaces")
    if not str(payload.get("supabase_url") or "").startswith("https://"):
        raise SystemExit("public Magpie origin is missing its Supabase URL")
    if not is_supabase_publishable_key(payload.get("publishable_key")):
        raise SystemExit("public Magpie origin has an invalid browser key")
elif kind == "central-health":
    storage = payload.get("storage") or {}
    auth = payload.get("auth") or {}
    edge = payload.get("edge") or {}
    failures = []
    if payload.get("ok") is not True:
        failures.append("health response is not ok")
    if payload.get("backend") != "none" or int(payload.get("pixel_count") or 0) != 0:
        failures.append("origin still exposes a hardware controller")
    if storage.get("backend") != "supabase" or storage.get("workspace_id") != "request-scoped":
        failures.append("storage is not request-scoped Supabase")
    if auth.get("mode") != "supabase" or auth.get("multi_workspace") is not True:
        failures.append("authentication is not multi-workspace Supabase")
    if auth.get("workspace_id") != "request-scoped":
        failures.append("authentication workspace is not request-scoped")
    if edge.get("mode") != "workspace" or edge.get("routing") != "location_light_target":
        failures.append("edge commands are not routed by workspace location")
    if failures:
        raise SystemExit("public origin is not the central Magpie service: " + "; ".join(failures))
else:
    raise SystemExit(f"unknown validation kind: {kind}")
PY
}

build_headers="$tmp_dir/build.headers"
build_body="$tmp_dir/build.body"
build_status="$(request "/build" "$build_headers" "$build_body")"

if [[ "$build_status" == "200" ]]; then
  validate_json build "$build_body"
  manifest_headers="$tmp_dir/manifest.headers"
  manifest_body="$tmp_dir/manifest.body"
  manifest_status="$(request "/manifest.webmanifest" "$manifest_headers" "$manifest_body")"
  if [[ "$manifest_status" != "200" ]]; then
    echo "Secure manifest returned HTTP $manifest_status." >&2
    exit 1
  fi
  validate_json manifest "$manifest_body"
  if [[ "$ORIGIN_MODE" == "public" ]]; then
    health_headers="$tmp_dir/health.headers"
    health_body="$tmp_dir/health.body"
    health_status="$(request "/health" "$health_headers" "$health_body")"
    if [[ "$health_status" != "200" ]]; then
      echo "Public central health returned HTTP $health_status." >&2
      exit 1
    fi
    validate_json central-health "$health_body"
    auth_headers="$tmp_dir/auth.headers"
    auth_body="$tmp_dir/auth.body"
    auth_status="$(request "/auth/config" "$auth_headers" "$auth_body")"
    if [[ "$auth_status" != "200" ]]; then
      echo "Public auth bootstrap returned HTTP $auth_status." >&2
      exit 1
    fi
    validate_json auth "$auth_body"
    echo "Public Magpie signup and PWA origin is healthy: $PUBLIC_ORIGIN"
    exit 0
  fi
  if [[ -z "$ACCESS_CLIENT_ID" ]]; then
    echo "Edge origin is reachable without a Cloudflare Access service token." >&2
    exit 1
  fi
  echo "Cloudflare Access edge origin is authenticated and healthy: $PUBLIC_ORIGIN"
  exit 0
fi

if [[ "$ORIGIN_MODE" == "public" ]]; then
  echo "Public Magpie origin returned HTTP $build_status; customer sign-in must not require Cloudflare Access." >&2
  exit 1
fi

if [[ -n "$ACCESS_CLIENT_ID" ]]; then
  echo "Authenticated secure origin returned HTTP $build_status." >&2
  exit 1
fi

case "$build_status" in
  301|302|303|307|308) ;;
  *)
    echo "Secure origin returned HTTP $build_status instead of Magpie or an Access challenge." >&2
    exit 1
    ;;
esac

location="$({ awk 'tolower($0) ~ /^location:/ {sub(/^[^:]*:[[:space:]]*/, ""); sub(/\r$/, ""); print; exit}' "$build_headers"; } || true)"
if [[ "$location" != *cloudflareaccess.com* && "$location" != *"/cdn-cgi/access/"* ]]; then
  echo "Redirect is not a Cloudflare Access challenge: ${location:-missing Location header}" >&2
  exit 1
fi

echo "Magpie edge origin is online and protected by Cloudflare Access: $PUBLIC_ORIGIN"
echo "Set CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET for an authenticated payload check."
