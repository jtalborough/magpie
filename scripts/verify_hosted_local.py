#!/usr/bin/env python3
"""Exercise the real hosted Magpie stack against disposable local Supabase data."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUPABASE_CLI_VERSION = "2.109.1"
SUPABASE = ["npx", "--yes", f"supabase@{SUPABASE_CLI_VERSION}"]
EXCLUDED_SUPABASE_SERVICES = (
    "studio,imgproxy,edge-runtime,realtime,logflare,vector,mailpit"
)
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600
SUPABASE_START_TIMEOUT_SECONDS = 300
IMAGE_BUILD_TIMEOUT_SECONDS = 1800
CLEANUP_TIMEOUT_SECONDS = 60
DOCKER_PREFLIGHT_IMAGE = "hello-world:latest"
DOCKER_INFO_TIMEOUT_SECONDS = 15
DOCKER_PULL_TIMEOUT_SECONDS = 180
DOCKER_RUN_TIMEOUT_SECONDS = 20
FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
UPGRADE_BASELINE_MIGRATION = "20260728143000"
PHOTO_BUCKET = "magpie-photos"
WORKSPACE_CASCADE_TABLES = (
    "workspace_members",
    "locations",
    "items",
    "inventory_entries",
    "photos",
    "labels",
    "hardware_bindings",
    "placement_rules",
    "size_rules",
    "agent_examples",
    "assistant_interactions",
    "active_guidance_states",
    "voice_attempts",
    "agent_credentials",
    "edge_devices",
    "edge_commands",
)


class SmokeFailure(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    check: bool = True,
    environment: dict[str, str] | None = None,
    timeout_seconds: float | None = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env={
                **os.environ,
                "SUPABASE_TELEMETRY": "false",
                "SUPABASE_TELEMETRY_DISABLED": "1",
                "DO_NOT_TRACK": "1",
                **(environment or {}),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        tail = "\n".join(output.strip().splitlines()[-20:])
        detail = f"\n{tail}" if tail else ""
        raise SmokeFailure(
            f"command timed out after {timeout_seconds:g}s: {' '.join(command)}{detail}"
        ) from exc
    if check and result.returncode:
        tail = "\n".join(result.stdout.strip().splitlines()[-20:])
        raise SmokeFailure(f"command failed: {' '.join(command)}\n{tail}")
    return result


def run_cleanup(command: list[str]) -> None:
    try:
        run(
            command,
            check=False,
            timeout_seconds=CLEANUP_TIMEOUT_SECONDS,
        )
    except SmokeFailure as exc:
        print(f"Hosted local smoke cleanup warning: {exc}", file=sys.stderr)


def release_identity() -> dict[str, str]:
    """Return the exact clean source identity that the candidate must report."""
    status = run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"]
    ).stdout.strip()
    if status:
        changed = len(status.splitlines())
        raise SmokeFailure(
            "release verification requires a clean checkout; "
            f"git reports {changed} changed or untracked path(s)"
        )

    commit = run(["git", "rev-parse", "HEAD"]).stdout.strip().lower()
    if not FULL_GIT_SHA.fullmatch(commit):
        raise SmokeFailure(f"git returned an invalid release commit: {commit!r}")
    branch = run(["git", "branch", "--show-current"]).stdout.strip() or "detached"
    built_at = run(["git", "show", "-s", "--format=%cI", "HEAD"]).stdout.strip()
    if not built_at:
        raise SmokeFailure("git did not return a commit timestamp for the release")
    return {
        "commit": commit,
        "branch": branch,
        "built_at": built_at,
    }


def verify_build_identity(
    base_url: str,
    expected: dict[str, str],
) -> dict[str, object]:
    """Prove the running container was built from this exact source commit."""
    _, build = request_json("GET", f"{base_url}/build")
    actual_commit = str(build.get("commit") or "").strip().lower()
    if actual_commit != expected["commit"]:
        raise SmokeFailure(
            "running container build identity does not match the candidate: "
            f"expected {expected['commit']}, received {actual_commit or 'missing'}"
        )
    if build.get("dirty") is not False:
        raise SmokeFailure("running container reports a dirty or unknown build")
    if build.get("source") != "container":
        raise SmokeFailure(
            "running service did not report container build provenance"
        )
    return build


def verify_docker_runtime() -> None:
    """Prove Docker can execute a new container before starting the hosted stack."""
    container_name = f"magpie-docker-preflight-{os.getpid()}"
    try:
        run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            timeout_seconds=DOCKER_INFO_TIMEOUT_SECONDS,
        )
        image = run(
            ["docker", "image", "inspect", DOCKER_PREFLIGHT_IMAGE],
            check=False,
            timeout_seconds=DOCKER_INFO_TIMEOUT_SECONDS,
        )
        if image.returncode:
            run(
                ["docker", "pull", DOCKER_PREFLIGHT_IMAGE],
                timeout_seconds=DOCKER_PULL_TIMEOUT_SECONDS,
            )
        try:
            run(
                [
                    "docker",
                    "run",
                    "--name",
                    container_name,
                    DOCKER_PREFLIGHT_IMAGE,
                ],
                timeout_seconds=DOCKER_RUN_TIMEOUT_SECONDS,
            )
        except SmokeFailure as exc:
            state_result = run(
                [
                    "docker",
                    "inspect",
                    container_name,
                    "--format",
                    "{{.State.Status}} {{.State.Error}}",
                ],
                check=False,
                timeout_seconds=DOCKER_INFO_TIMEOUT_SECONDS,
            )
            state = state_result.stdout.strip() or "unavailable"
            if state.split(maxsplit=1)[0].lower() == "created":
                diagnosis = (
                    "Docker created the preflight container but could not start it "
                    f"(state: {state}). Restart or repair the Docker Desktop runtime/VM; "
                    "successful image pulls and docker info do not prove containers can run."
                )
            else:
                diagnosis = (
                    "Docker could not execute the preflight container "
                    f"(state: {state}). Repair the Docker runtime before retrying."
                )
            raise SmokeFailure(f"{exc}\n{diagnosis}") from exc
    finally:
        run_cleanup(["docker", "rm", "-f", container_name])


def supabase_command(workdir: Path, *arguments: str) -> list[str]:
    return [*SUPABASE, "--workdir", str(workdir), *arguments]


def supabase_environment(workdir: Path = ROOT) -> dict[str, str]:
    result = run(supabase_command(workdir, "status", "-o", "env"), check=False)
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line.strip())
        if not match:
            continue
        raw = match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            raw = raw[1:-1]
        values[match.group(1)] = raw
    return values


def _free_local_ports(count: int) -> list[int]:
    sockets: list[socket.socket] = []
    try:
        for _ in range(count):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            sockets.append(listener)
        return [int(listener.getsockname()[1]) for listener in sockets]
    finally:
        for listener in sockets:
            listener.close()


def _replace_toml_setting(
    text: str,
    *,
    section: str,
    key: str,
    value: str,
) -> str:
    current_section = ""
    output: list[str] = []
    replaced = False
    for line in text.splitlines():
        section_match = re.fullmatch(r"\s*\[([^]]+)]\s*", line)
        if section_match:
            current_section = section_match.group(1)
        if current_section == section and re.match(rf"\s*{re.escape(key)}\s*=", line):
            output.append(f"{key} = {value}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        raise SmokeFailure(f"Supabase config omitted [{section}] {key}")
    return "\n".join(output) + "\n"


def prepare_upgrade_project(
    workdir: Path,
    *,
    source: Path = ROOT / "supabase",
    baseline: str = UPGRADE_BASELINE_MIGRATION,
) -> list[Path]:
    """Create an isolated project containing only the deployed schema baseline."""
    target = workdir / "supabase"
    migrations = target / "migrations"
    migrations.mkdir(parents=True, exist_ok=False)

    api_port, db_port, shadow_port = _free_local_ports(3)
    config = (source / "config.toml").read_text(encoding="utf-8")
    project_id = f"magpie-upgrade-{os.getpid()}-{secrets.token_hex(4)}"
    config, replacements = re.subn(
        r'(?m)^project_id\s*=\s*"[^"]*"\s*$',
        f'project_id = "{project_id}"',
        config,
        count=1,
    )
    if replacements != 1:
        raise SmokeFailure("Supabase config omitted project_id")
    config = _replace_toml_setting(
        config, section="api", key="port", value=str(api_port)
    )
    config = _replace_toml_setting(
        config, section="db", key="port", value=str(db_port)
    )
    config = _replace_toml_setting(
        config, section="db", key="shadow_port", value=str(shadow_port)
    )
    (target / "config.toml").write_text(config, encoding="utf-8")
    seed = source / "seed.sql"
    if seed.exists():
        shutil.copy2(seed, target / seed.name)

    pending: list[Path] = []
    candidates = sorted((source / "migrations").glob("*.sql"))
    if not any(path.name.startswith(f"{baseline}_") for path in candidates):
        raise SmokeFailure(f"upgrade baseline migration {baseline} was not found")
    for migration in candidates:
        version = migration.name.split("_", 1)[0]
        if version <= baseline:
            shutil.copy2(migration, migrations / migration.name)
        else:
            pending.append(migration)
    if not pending:
        raise SmokeFailure(f"no migrations follow upgrade baseline {baseline}")
    return pending


def promote_upgrade_migrations(workdir: Path, pending: list[Path]) -> None:
    migrations = workdir / "supabase" / "migrations"
    for migration in pending:
        shutil.copy2(migration, migrations / migration.name)


def request_json_value(
    method: str,
    url: str,
    *,
    payload: object | None = None,
    headers: dict[str, str] | None = None,
    expected: set[int] = {200},
) -> tuple[int, object]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    try:
        parsed = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        detail = raw[:1000].decode("utf-8", errors="replace")
        raise SmokeFailure(
            f"{method} {url} returned invalid JSON ({status}): {detail}"
        ) from exc
    if status not in expected:
        raise SmokeFailure(f"{method} {url} returned {status}: {parsed}")
    return status, parsed


def request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    expected: set[int] = {200},
) -> tuple[int, dict[str, object]]:
    status, parsed = request_json_value(
        method,
        url,
        payload=payload,
        headers=headers,
        expected=expected,
    )
    if not isinstance(parsed, dict):
        raise SmokeFailure(f"{method} {url} did not return a JSON object")
    return status, parsed


def request_bytes(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    expected: set[int] = {200},
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            response_headers = {
                key.lower(): value for key, value in response.headers.items()
            }
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_headers = {key.lower(): value for key, value in exc.headers.items()}
        body = exc.read()
    if status not in expected:
        detail = body[:500].decode("utf-8", errors="replace")
        raise SmokeFailure(f"{method} {url} returned {status}: {detail}")
    return status, response_headers, body


def verify_client_surfaces(
    base_url: str,
    headers: dict[str, str],
) -> None:
    surfaces = {
        "/": (
            b'id="desktopWorkspaceStyles"',
            b'initializeDesktopWorkspace()',
            b'window.magpieMobileRedirect',
        ),
        "/mobile": (
            b'id="command"',
            b'data-product-nav',
            b'place_configuration_ui_required',
            b'result.places_url',
            b'id="offlineQueue"',
            b'indexedDB.open',
            b'navigator.onLine',
            b'navigator.serviceWorker.register',
        ),
        "/storage?view=places": (
            b'id="storageSetupDialog"',
            b'id="storageSetupSubmit"',
            b'api("/storage/setup"',
        ),
        "/kiosk": (
            b'id="kioskMain"',
            b'id="kioskActions"',
            b'place_configuration_ui_required',
            b'voice_scope_ui_required',
        ),
        "/account": (
            b'id="deleteWorkspaceForm"',
            b'id="deleteWorkspaceConfirmation"',
            b'id="leaveWorkspaceButton"',
            b'src="/auth.js?v=',
        ),
    }
    for route, required_markers in surfaces.items():
        _, response_headers, body = request_bytes(
            "GET",
            f"{base_url}{route}",
            headers=headers,
        )
        if "text/html" not in response_headers.get("content-type", ""):
            raise SmokeFailure(f"hosted client surface {route} did not return HTML")
        missing = [
            marker.decode("utf-8", errors="replace")
            for marker in required_markers
            if marker not in body
        ]
        if missing:
            raise SmokeFailure(
                f"hosted client surface {route} is incomplete; missing {missing}"
            )


def verify_pwa_contract(base_url: str) -> None:
    """Prove the unauthenticated install shell works on iPhone and Android."""
    _, manifest = request_json("GET", f"{base_url}/manifest.webmanifest")
    if (
        manifest.get("name") != "Magpie Storage"
        or manifest.get("start_url") != "/mobile?source=pwa"
        or manifest.get("scope") != "/"
        or manifest.get("display") != "standalone"
    ):
        raise SmokeFailure("hosted PWA manifest has an invalid launch contract")

    icons = manifest.get("icons")
    if not isinstance(icons, list):
        raise SmokeFailure("hosted PWA manifest has no icon catalog")
    icon_contract = {
        (
            str(icon.get("src") or ""),
            str(icon.get("sizes") or ""),
            str(icon.get("purpose") or "any"),
        )
        for icon in icons
        if isinstance(icon, dict)
    }
    required_icons = {
        ("/app-icon-192.png", "192x192", "any"),
        ("/app-icon-512.png", "512x512", "any"),
        ("/app-icon-512.png", "512x512", "maskable"),
    }
    if not required_icons <= icon_contract:
        raise SmokeFailure("hosted PWA manifest is missing required install icons")

    shortcuts = manifest.get("shortcuts")
    if not isinstance(shortcuts, list):
        raise SmokeFailure("hosted PWA manifest has no product shortcuts")
    shortcut_urls = {
        str(shortcut.get("url") or "")
        for shortcut in shortcuts
        if isinstance(shortcut, dict)
    }
    if not {
        "/mobile?action=find&source=shortcut",
        "/mobile?action=put-away&source=shortcut",
        "/storage?view=inventory&source=shortcut",
        "/storage?view=places&create=top&source=shortcut",
    } <= shortcut_urls:
        raise SmokeFailure("hosted PWA manifest is missing product shortcuts")

    _, worker_headers, worker = request_bytes(
        "GET", f"{base_url}/service-worker.js"
    )
    if "javascript" not in worker_headers.get("content-type", ""):
        raise SmokeFailure("hosted service worker did not return JavaScript")
    cache_control = worker_headers.get("cache-control", "").lower()
    if "no-cache" not in cache_control or "no-store" not in cache_control:
        raise SmokeFailure("hosted service worker may be served stale")
    worker_markers = (
        b"const APP_SHELL =",
        b'"/mobile"',
        b'"/storage"',
        b'"/auth.js?v=',
        b'self.addEventListener("install"',
        b'self.addEventListener("activate"',
        b'self.addEventListener("fetch"',
        b"CACHE_SCAN_PAGE",
        b"skipWaiting",
        b"clients.claim",
    )
    if any(marker not in worker for marker in worker_markers):
        raise SmokeFailure(
            "hosted service worker is missing its offline shell contract"
        )

    for path in (
        "/app-icon-192.png",
        "/app-icon-512.png",
        "/apple-touch-icon.png",
    ):
        _, asset_headers, asset = request_bytes("GET", f"{base_url}{path}")
        if asset_headers.get("content-type") != "image/png" or not asset.startswith(
            b"\x89PNG\r\n\x1a\n"
        ):
            raise SmokeFailure(f"hosted install asset {path} is invalid")


def auth_headers(token: str, workspace_id: str = "") -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if workspace_id:
        headers["X-Magpie-Workspace"] = workspace_id
    return headers


def wait_for_health(base_url: str, container_name: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            request_json("GET", f"{base_url}/health")
            return
        except (OSError, SmokeFailure):
            time.sleep(1)
    logs = run(["docker", "logs", container_name], check=False).stdout
    raise SmokeFailure(f"Magpie did not become healthy\n{logs[-4000:]}")


def signup(api_url: str, publishable_key: str, email: str, password: str) -> tuple[str, str]:
    _, body = request_json(
        "POST",
        f"{api_url}/auth/v1/signup",
        payload={"email": email, "password": password},
        headers={"apikey": publishable_key},
    )
    user = body.get("user")
    token = str(body.get("access_token") or "")
    user_id = str(user.get("id") or "") if isinstance(user, dict) else ""
    if not user_id or not token:
        raise SmokeFailure("local Supabase signup did not return a user and access token")
    return user_id, token


def delete_user(api_url: str, service_key: str, user_id: str) -> None:
    if not user_id:
        return
    request_json(
        "DELETE",
        f"{api_url}/auth/v1/admin/users/{user_id}",
        headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
        expected={200, 404},
    )


def _service_rest_headers(service_key: str, *, prefer: str = "") -> dict[str, str]:
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _rest_rows(
    method: str,
    url: str,
    *,
    service_key: str,
    payload: object | None = None,
    expected: set[int] = {200},
    prefer: str = "",
) -> list[dict[str, object]]:
    _, body = request_json_value(
        method,
        url,
        payload=payload,
        headers=_service_rest_headers(service_key, prefer=prefer),
        expected=expected,
    )
    if not isinstance(body, list) or not all(isinstance(row, dict) for row in body):
        raise SmokeFailure(f"{method} {url} did not return a JSON row list")
    return body


def _rest_rows_after_schema_reload(
    method: str,
    url: str,
    *,
    service_key: str,
    payload: object | None = None,
    expected: set[int] = {200},
    prefer: str = "",
    timeout_seconds: float = 20,
) -> list[dict[str, object]]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return _rest_rows(
                method,
                url,
                service_key=service_key,
                payload=payload,
                expected=expected,
                prefer=prefer,
            )
        except SmokeFailure as exc:
            if "PGRST205" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def seed_upgrade_fixture(
    api_url: str,
    publishable_key: str,
    service_key: str,
) -> dict[str, str]:
    """Seed records using only columns available at the deployed baseline."""
    nonce = secrets.token_hex(6)
    user_id, _ = signup(
        api_url,
        publishable_key,
        f"upgrade-{nonce}@example.invalid",
        f"Upgrade-{nonce}-pass",
    )
    fixture = {
        "user_id": user_id,
        "workspace_id": str(uuid.uuid4()),
        "root_id": str(uuid.uuid4()),
        "bin_id": str(uuid.uuid4()),
        "item_id": str(uuid.uuid4()),
        "entry_id": str(uuid.uuid4()),
    }
    rest = f"{api_url}/rest/v1"
    try:
        for table, payload in (
            (
                "workspaces",
                {
                    "id": fixture["workspace_id"],
                    "owner_id": user_id,
                    "name": "Upgrade Path Fixture",
                },
            ),
            (
                "locations",
                {
                    "id": fixture["root_id"],
                    "workspace_id": fixture["workspace_id"],
                    "local_code": "upgrade-main-cabinet",
                    "type": "cabinet",
                    "label": "Upgrade Main Cabinet",
                    "capacity_class": "large",
                },
            ),
            (
                "locations",
                {
                    "id": fixture["bin_id"],
                    "workspace_id": fixture["workspace_id"],
                    "parent_id": fixture["root_id"],
                    "local_code": "upgrade-bin",
                    "type": "bin",
                    "label": "Upgrade Hardware Bin",
                    "capacity_class": "small",
                },
            ),
            (
                "items",
                {
                    "id": fixture["item_id"],
                    "workspace_id": fixture["workspace_id"],
                    "name": "Upgrade M4 T-nuts",
                    "category": "hardware",
                    "size_class": "tiny",
                    "home_location_id": fixture["bin_id"],
                    "last_seen_location_id": fixture["bin_id"],
                },
            ),
            (
                "inventory_entries",
                {
                    "id": fixture["entry_id"],
                    "workspace_id": fixture["workspace_id"],
                    "item_id": fixture["item_id"],
                    "location_id": fixture["bin_id"],
                    "quantity": 24,
                    "unit": "each",
                    "source": "manual",
                },
            ),
        ):
            _rest_rows(
                "POST",
                f"{rest}/{table}",
                service_key=service_key,
                payload=payload,
                expected={201},
                prefer="return=representation",
            )
    except Exception:
        delete_user(api_url, service_key, user_id)
        raise
    return fixture


def verify_upgrade_fixture(
    api_url: str,
    service_key: str,
    fixture: dict[str, str],
) -> None:
    """Prove baseline data and candidate destination semantics survived upgrade."""
    workspace_id = urllib.parse.quote(fixture["workspace_id"], safe="")
    rest = f"{api_url}/rest/v1"
    locations = _rest_rows(
        "GET",
        f"{rest}/locations?workspace_id=eq.{workspace_id}"
        "&select=id,parent_id,type,label,accepts_items",
        service_key=service_key,
    )
    by_id = {str(row.get("id")): row for row in locations}
    root = by_id.get(fixture["root_id"])
    destination = by_id.get(fixture["bin_id"])
    if root is None or destination is None:
        raise SmokeFailure("candidate migrations lost an existing location")
    if root.get("accepts_items") is not False:
        raise SmokeFailure("existing cabinet became an item destination during upgrade")
    if destination.get("accepts_items") is not True:
        raise SmokeFailure("existing bin stopped accepting items during upgrade")
    if destination.get("parent_id") != fixture["root_id"]:
        raise SmokeFailure("candidate migrations changed an existing location parent")

    items = _rest_rows(
        "GET",
        f"{rest}/items?workspace_id=eq.{workspace_id}"
        "&select=id,name,category,home_location_id,last_seen_location_id",
        service_key=service_key,
    )
    if items != [
        {
            "id": fixture["item_id"],
            "name": "Upgrade M4 T-nuts",
            "category": "hardware",
            "home_location_id": fixture["bin_id"],
            "last_seen_location_id": fixture["bin_id"],
        }
    ]:
        raise SmokeFailure("candidate migrations changed an existing item record")

    entries = _rest_rows(
        "GET",
        f"{rest}/inventory_entries?workspace_id=eq.{workspace_id}"
        "&select=id,item_id,location_id,quantity,unit",
        service_key=service_key,
    )
    if len(entries) != 1:
        raise SmokeFailure("candidate migrations lost existing inventory")
    entry = entries[0]
    if (
        entry.get("id") != fixture["entry_id"]
        or entry.get("item_id") != fixture["item_id"]
        or entry.get("location_id") != fixture["bin_id"]
        or float(entry.get("quantity") or 0) != 24
        or entry.get("unit") != "each"
    ):
        raise SmokeFailure("candidate migrations changed existing inventory placement")

    guidance = {
        "workspace_id": fixture["workspace_id"],
        "state": {"entries": [{"query": "M4 T-nuts", "color": "red"}], "next_id": 2},
    }
    rows = _rest_rows_after_schema_reload(
        "POST",
        f"{rest}/active_guidance_states",
        service_key=service_key,
        payload=guidance,
        expected={201},
        prefer="return=representation",
    )
    if len(rows) != 1 or rows[0].get("state") != guidance["state"]:
        raise SmokeFailure("upgraded schema could not persist active guidance state")


def ui_storage_setup(
    base_url: str,
    headers: dict[str, str],
    *,
    root_name: str,
    root_type: str,
    container_name: str,
) -> dict[str, object]:
    _, created = request_json(
        "POST",
        f"{base_url}/storage/setup",
        payload={
            "root_name": root_name,
            "root_type": root_type,
            "destination": {
                "label": container_name,
                "type": "box",
                "capacity_class": "medium",
            },
        },
        headers=headers,
    )
    root = created.get("root")
    destination = created.get("destination")
    if not isinstance(root, dict) or not root.get("id"):
        raise SmokeFailure("UI storage setup did not return its top-level place")
    if not isinstance(destination, dict) or destination.get("parent_id") != root["id"]:
        raise SmokeFailure("UI storage setup did not create its container")
    return created


def exercise_atomic_csv_path_import(
    base_url: str,
    headers: dict[str, str],
    *,
    nonce: str,
    request_fn=request_json,
) -> None:
    root_label = f"Imported Home {nonce}"
    leaf_label = f"Imported Parts {nonce}"
    csv_text = (
        "name,location_path,location_path_types,quantity,category\n"
        f"Imported T-nuts {nonce},{root_label} > Workshop > Parts cabinet > "
        f"{leaf_label},home > room > cabinet > bin,12,hardware\n"
    )
    _, preview = request_fn(
        "POST",
        f"{base_url}/import/inventory.csv",
        payload={"csv_text": csv_text, "dry_run": True},
        headers=headers,
    )
    missing = preview.get("missing_locations")
    if (
        preview.get("requires_location_creation") is not True
        or not isinstance(missing, list)
        or [row.get("type") for row in missing if isinstance(row, dict)]
        != ["home", "room", "cabinet", "bin"]
    ):
        raise SmokeFailure("CSV path import did not preview its typed storage graph")

    request_fn(
        "POST",
        f"{base_url}/import/inventory.csv",
        payload={"csv_text": csv_text},
        headers=headers,
        expected={409},
    )
    _, before_apply = request_fn(
        "GET",
        f"{base_url}/locations?include_archived=true",
        headers=headers,
    )
    if any(
        isinstance(row, dict) and row.get("label") == root_label
        for row in before_apply.get("locations", [])
    ):
        raise SmokeFailure("unreviewed CSV path import changed the storage graph")

    _, imported = request_fn(
        "POST",
        f"{base_url}/import/inventory.csv",
        payload={
            "csv_text": csv_text,
            "create_missing_locations": True,
        },
        headers=headers,
    )
    created = imported.get("created_locations")
    imported_rows = imported.get("rows")
    if (
        imported.get("imported") != 1
        or not isinstance(created, list)
        or len(created) != 4
        or not isinstance(imported_rows, list)
        or len(imported_rows) != 1
    ):
        raise SmokeFailure("reviewed CSV path import did not apply as one operation")
    destination_id = str(imported_rows[0].get("location_id") or "")
    _, destination = request_fn(
        "GET",
        f"{base_url}/locations/{urllib.parse.quote(destination_id, safe='')}",
        headers=headers,
    )
    location = destination.get("location")
    items = destination.get("items")
    if (
        not isinstance(location, dict)
        or location.get("label") != leaf_label
        or location.get("type") != "bin"
        or not isinstance(items, list)
        or not any(
            isinstance(item, dict)
            and item.get("name") == f"Imported T-nuts {nonce}"
            and float(item.get("quantity") or 0) == 12
            for item in items
        )
    ):
        raise SmokeFailure(
            "atomic CSV import did not persist its typed destination and item"
        )


def location_by_label(
    locations_payload: dict[str, object],
    label: str,
    *,
    parent_id: str | None = None,
) -> dict[str, object]:
    rows = locations_payload.get("locations")
    if not isinstance(rows, list):
        raise SmokeFailure("location catalog was missing its locations")
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("label") == label
        and (parent_id is None or row.get("parent_id") == parent_id)
    ]
    if len(matches) != 1:
        raise SmokeFailure(
            f"expected one location labeled {label!r} under {parent_id!r}; found {len(matches)}"
        )
    return matches[0]


def exercise_portable_workspace_restore(
    base_url: str,
    source_headers: dict[str, str],
    restore_headers: dict[str, str],
    *,
    request_fn=request_json,
) -> None:
    """Prove the operator-readable backup can restore a hosted workspace."""
    _, snapshot = request_fn(
        "GET",
        f"{base_url}/export/inventory.json",
        headers=source_headers,
    )
    collections = (
        "locations",
        "inventory",
        "placement_rules",
        "size_rules",
        "photos",
    )
    expected_counts = {
        key: len(snapshot.get(key, []))
        for key in collections
        if isinstance(snapshot.get(key), list)
    }
    if set(expected_counts) != set(collections):
        raise SmokeFailure("portable backup omitted required workspace collections")

    _, preview = request_fn(
        "POST",
        f"{base_url}/import/inventory.json",
        payload={"snapshot": snapshot, "dry_run": True},
        headers=restore_headers,
    )
    if preview.get("dry_run") is not True or preview.get("imported") != 0:
        raise SmokeFailure("portable backup restore did not remain read-only in dry-run")
    if any(preview.get(key) != count for key, count in expected_counts.items()):
        raise SmokeFailure("portable backup dry-run did not report the complete workspace")

    _, imported = request_fn(
        "POST",
        f"{base_url}/import/inventory.json",
        payload={"snapshot": snapshot},
        headers=restore_headers,
    )
    if imported.get("dry_run") is not False or any(
        imported.get(key) != count for key, count in expected_counts.items()
    ):
        raise SmokeFailure("portable backup restore did not import the complete workspace")

    _, restored = request_fn(
        "GET",
        f"{base_url}/export/inventory.json",
        headers=restore_headers,
    )

    def signatures(
        payload: dict[str, object],
        key: str,
        fields: tuple[str, ...],
    ) -> set[str]:
        rows = payload.get(key)
        if not isinstance(rows, list):
            raise SmokeFailure(f"restored workspace omitted {key}")
        return {
            json.dumps(
                {field: row.get(field) for field in fields},
                sort_keys=True,
                separators=(",", ":"),
            )
            for row in rows
            if isinstance(row, dict)
        }

    comparisons = {
        "locations": (
            "id",
            "parent_id",
            "type",
            "label",
            "accepts_items",
            "capacity_class",
            "purpose_tags",
            "dimensions",
            "active",
        ),
        "inventory": (
            "name",
            "description",
            "category",
            "aliases",
            "bin_id",
            "quantity",
            "unit",
            "needs_review",
            "review_reason",
            "size_class",
            "size_source",
            "size_confidence",
            "dimensions",
            "home_location_id",
            "last_seen_location_id",
            "active",
            "archived_at",
        ),
        "placement_rules": (
            "rule_text",
            "trigger_terms",
            "target_location_id",
            "source_command",
            "confidence",
            "active",
        ),
        "size_rules": (
            "rule_text",
            "trigger_terms",
            "size_class",
            "source_command",
            "confidence",
            "active",
        ),
        "photos": ("filename", "content_type", "data_base64"),
    }
    for key, fields in comparisons.items():
        if signatures(restored, key, fields) != signatures(snapshot, key, fields):
            raise SmokeFailure(f"portable backup restore changed {key}")

    def photo_links(payload: dict[str, object]) -> set[str]:
        photos = payload.get("photos")
        if not isinstance(photos, list):
            raise SmokeFailure("restored workspace omitted photos")
        photos_by_id = {
            int(row["id"]): json.dumps(
                {
                    "filename": row.get("filename"),
                    "content_type": row.get("content_type"),
                    "data_base64": row.get("data_base64"),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            for row in photos
            if isinstance(row, dict) and row.get("id") is not None
        }
        links: set[str] = set()
        for collection, identity_fields in (
            ("locations", ("id",)),
            ("inventory", ("name", "bin_id")),
        ):
            rows = payload.get(collection)
            if not isinstance(rows, list):
                raise SmokeFailure(f"restored workspace omitted {collection}")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                linked = []
                for raw_photo_id in row.get("photo_ids") or []:
                    photo = photos_by_id.get(int(raw_photo_id))
                    if photo is None:
                        raise SmokeFailure(
                            f"portable backup {collection} referenced a missing photo"
                        )
                    linked.append(photo)
                links.add(
                    json.dumps(
                        {
                            "collection": collection,
                            "identity": {
                                field: row.get(field) for field in identity_fields
                            },
                            "photos": sorted(linked),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
        return links

    if photo_links(restored) != photo_links(snapshot):
        raise SmokeFailure("portable backup restore changed photo associations")


def exercise_edge_controller(
    base_url: str,
    workspace_headers: dict[str, str],
    *,
    workspace_id: str,
    other_workspace_id: str,
    location_id: str,
    nonce: str,
    request_fn=request_json,
) -> None:
    device_id = f"smoke-cabinet-{nonce}"
    _, pairing = request_fn(
        "POST",
        f"{base_url}/workspace/devices/pairing",
        payload={"device_id": device_id, "scopes": ["commands"]},
        headers=workspace_headers,
    )
    pairing_code = str(pairing.get("pairing_code") or "")
    if (
        not pairing_code
        or pairing.get("shown_once") is not True
        or not str(pairing.get("machine_url") or "").startswith("https://")
        or "token" in pairing
    ):
        raise SmokeFailure("hosted edge pairing did not issue a one-use setup code")

    edge_path = f"{base_url}/edge/devices/{device_id}"
    edge_workspace = {"X-Magpie-Workspace": workspace_id}
    _, paired = request_fn(
        "POST",
        f"{edge_path}/pair",
        payload={"pairing_code": pairing_code},
        headers=edge_workspace,
    )
    token = str(paired.get("token") or "")
    if not token or paired.get("shown_once") is not True:
        raise SmokeFailure("hosted edge pairing did not return the device credential")
    request_fn(
        "POST",
        f"{edge_path}/pair",
        payload={"pairing_code": pairing_code},
        headers=edge_workspace,
        expected={401},
    )

    device_headers = {
        "Authorization": f"Bearer {token}",
        "X-Magpie-Workspace": workspace_id,
    }
    _, heartbeat = request_fn(
        "POST",
        f"{edge_path}/heartbeat",
        payload={"version": "hosted-smoke", "capabilities": ["set_pixels"]},
        headers=device_headers,
    )
    if heartbeat.get("ok") is not True:
        raise SmokeFailure("hosted edge device heartbeat was rejected")

    request_fn(
        "POST",
        f"{base_url}/locations/{urllib.parse.quote(location_id, safe='')}/update",
        payload={"light_target": device_id, "light_indexes": [0]},
        headers=workspace_headers,
    )
    _, device_catalog = request_fn(
        "GET", f"{base_url}/workspace/devices", headers=workspace_headers
    )
    devices = device_catalog.get("devices")
    matching_devices = [
        row
        for row in devices
        if isinstance(row, dict) and row.get("device_id") == device_id
    ] if isinstance(devices, list) else []
    if len(matching_devices) != 1:
        raise SmokeFailure("paired edge device was missing from the workspace")
    device = matching_devices[0]
    bound_locations = device.get("bound_locations")
    if (
        device.get("command_ready") is not True
        or not isinstance(bound_locations, list)
        or not any(
            isinstance(row, dict) and row.get("id") == location_id
            for row in bound_locations
        )
    ):
        raise SmokeFailure("paired edge device was not ready and bound to its location")

    _, light = request_fn(
        "POST",
        f"{base_url}/locations/{urllib.parse.quote(location_id, safe='')}/light",
        headers=workspace_headers,
    )
    if light.get("lit") is not True:
        raise SmokeFailure("hosted location did not enqueue its light command")
    _, claimed = request_fn(
        "POST",
        f"{edge_path}/commands/claim",
        headers=device_headers,
    )
    command = claimed.get("command")
    payload = command.get("payload") if isinstance(command, dict) else None
    if (
        not isinstance(command, dict)
        or command.get("kind") != "set_pixels"
        or not isinstance(payload, dict)
        or payload.get("pixel_count") != 1
        or not payload.get("pixels")
    ):
        raise SmokeFailure("paired edge device did not claim the expected pixel state")
    command_id = str(command.get("id") or "")
    if not command_id:
        raise SmokeFailure("claimed edge command did not include an id")
    _, completed = request_fn(
        "POST",
        f"{edge_path}/commands/{urllib.parse.quote(command_id, safe='')}/complete",
        payload={"succeeded": True},
        headers=device_headers,
    )
    completed_command = completed.get("command")
    if (
        not isinstance(completed_command, dict)
        or completed_command.get("status") != "succeeded"
    ):
        raise SmokeFailure("paired edge device did not complete its light command")

    request_fn(
        "POST",
        f"{edge_path}/commands/claim",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Magpie-Workspace": other_workspace_id,
        },
        expected={401},
    )
    _, revoked = request_fn(
        "DELETE",
        f"{base_url}/workspace/devices/{device_id}",
        headers=workspace_headers,
    )
    if revoked.get("ok") is not True:
        raise SmokeFailure("hosted edge device could not be revoked")
    request_fn(
        "POST",
        f"{edge_path}/heartbeat",
        payload={"version": "revoked"},
        headers=device_headers,
        expected={401},
    )


def exercise_workspace_lifecycle(
    base_url: str,
    api_url: str,
    service_key: str,
    *,
    first_user_id: str,
    first_token: str,
    first_workspace_id: str,
    first_workspace_name: str,
    restore_workspace_id: str,
    restore_workspace_name: str,
    second_token: str,
    second_email: str,
    second_workspace_id: str,
) -> None:
    first = auth_headers(first_token, first_workspace_id)
    second_in_first = auth_headers(second_token, first_workspace_id)
    second = auth_headers(second_token, second_workspace_id)

    _, invited = request_json(
        "POST",
        f"{base_url}/workspace/invitations",
        payload={
            "email": second_email,
            "role": "member",
            "redirect_url": f"{base_url}/account",
        },
        headers=first,
    )
    member = invited.get("member")
    if not isinstance(member, dict) or member.get("role") != "member":
        raise SmokeFailure("hosted workspace invitation did not add the member")
    _, member_workspace = request_json(
        "GET", f"{base_url}/workspace", headers=second_in_first
    )
    member_identity = member_workspace.get("identity")
    if (
        not isinstance(member_identity, dict)
        or member_identity.get("role") != "member"
        or member_identity.get("workspace_id") != first_workspace_id
    ):
        raise SmokeFailure("invited member could not select the shared workspace")
    request_json(
        "DELETE",
        f"{base_url}/workspace",
        payload={"confirmation": first_workspace_name},
        headers=second_in_first,
        expected={403},
    )
    _, left = request_json(
        "DELETE", f"{base_url}/workspace/membership", headers=second_in_first
    )
    left_member = left.get("member")
    if not isinstance(left_member, dict) or left_member.get("role") != "member":
        raise SmokeFailure("hosted workspace member could not leave")
    request_json(
        "GET",
        f"{base_url}/locations",
        headers=second_in_first,
        expected={403},
    )
    _, retained_second = request_json(
        "GET", f"{base_url}/workspace", headers=second
    )
    if retained_second.get("workspace", {}).get("id") != second_workspace_id:
        raise SmokeFailure("leaving one workspace changed another membership")

    encoded_workspace = urllib.parse.quote(first_workspace_id, safe="")
    photo_rows = _rest_rows(
        "GET",
        f"{api_url}/rest/v1/photos?workspace_id=eq.{encoded_workspace}"
        "&select=storage_path",
        service_key=service_key,
    )
    photo_paths = [
        str(row.get("storage_path") or "")
        for row in photo_rows
        if str(row.get("storage_path") or "")
    ]
    if not photo_paths:
        raise SmokeFailure("hosted workspace deletion had no photo object to verify")
    storage_headers = _service_rest_headers(service_key)
    for path in photo_paths:
        request_bytes(
            "GET",
            f"{api_url}/storage/v1/object/{PHOTO_BUCKET}/"
            f"{urllib.parse.quote(path, safe='/')}",
            headers=storage_headers,
        )

    request_json(
        "DELETE",
        f"{base_url}/workspace",
        payload={"confirmation": first_workspace_name.lower()},
        headers=first,
        expected={400},
    )
    _, deleted = request_json(
        "DELETE",
        f"{base_url}/workspace",
        payload={"confirmation": first_workspace_name},
        headers=first,
    )
    if (
        deleted.get("workspace", {}).get("id") != first_workspace_id
        or deleted.get("deleted_photo_objects") != len(photo_paths)
    ):
        raise SmokeFailure("hosted workspace deletion returned incomplete evidence")
    for path in photo_paths:
        request_bytes(
            "GET",
            f"{api_url}/storage/v1/object/{PHOTO_BUCKET}/"
            f"{urllib.parse.quote(path, safe='/')}",
            headers=storage_headers,
            expected={400, 404},
        )
    for table in WORKSPACE_CASCADE_TABLES:
        rows = _rest_rows(
            "GET",
            f"{api_url}/rest/v1/{table}?workspace_id=eq.{encoded_workspace}"
            "&select=workspace_id&limit=1",
            service_key=service_key,
        )
        if rows:
            raise SmokeFailure(
                f"hosted workspace deletion left rows in {table}"
            )
    workspace_rows = _rest_rows(
        "GET",
        f"{api_url}/rest/v1/workspaces?id=eq.{encoded_workspace}&select=id",
        service_key=service_key,
    )
    if workspace_rows:
        raise SmokeFailure("hosted workspace deletion left its workspace row")
    request_json(
        "GET",
        f"{base_url}/locations",
        headers=first,
        expected={403},
    )
    request_json("GET", f"{base_url}/workspace", headers=second)

    restore = auth_headers(first_token, restore_workspace_id)
    _, deleted_restore = request_json(
        "DELETE",
        f"{base_url}/workspace",
        payload={"confirmation": restore_workspace_name},
        headers=restore,
    )
    if deleted_restore.get("workspace", {}).get("id") != restore_workspace_id:
        raise SmokeFailure("hosted restored workspace could not be deleted")
    _, unassigned = request_json(
        "GET", f"{base_url}/client/bootstrap", headers=auth_headers(first_token)
    )
    if unassigned.get("workspace") is not None or unassigned.get("workspaces") != []:
        raise SmokeFailure("workspace deletion did not return the owner to setup")
    _, account = request_json(
        "DELETE", f"{base_url}/account", headers=auth_headers(first_token)
    )
    if account.get("account", {}).get("user_id") != first_user_id:
        raise SmokeFailure("hosted account deletion returned incomplete evidence")
    request_json(
        "GET",
        f"{base_url}/client/bootstrap",
        headers=auth_headers(first_token),
        expected={401},
    )


def exercise_physical_storage_checks(
    base_url: str,
    headers: dict[str, str],
) -> None:
    for _ in range(100):
        _, review = request_json(
            "GET", f"{base_url}/review/queue?limit=50", headers=headers
        )
        place_checks = review.get("place_checks")
        if not isinstance(place_checks, list):
            raise SmokeFailure("physical storage review queue was invalid")
        if not place_checks:
            return
        place_check = place_checks[0]
        if not isinstance(place_check, dict) or not place_check.get("location_id"):
            raise SmokeFailure("physical storage review queue returned an invalid place")
        location_id = urllib.parse.quote(str(place_check["location_id"]), safe="")
        _, audit = request_json(
            "GET", f"{base_url}/locations/{location_id}/audit", headers=headers
        )
        if audit.get("can_verify") is not True:
            raise SmokeFailure("hosted physical storage check exceeded the audit limit")
        placements = audit.get("placements")
        if not isinstance(placements, list):
            raise SmokeFailure("hosted physical storage audit returned invalid placements")
        request_json(
            "POST",
            f"{base_url}/locations/{location_id}/verify",
            payload={
                "revision": audit.get("revision"),
                "placements": [
                    {
                        "item_id": placement.get("item_id"),
                        "location_id": placement.get("location_id"),
                        "status": "present",
                    }
                    for placement in placements
                    if isinstance(placement, dict)
                ],
            },
            headers=headers,
        )
    raise SmokeFailure("hosted physical storage checks did not converge")


def exercise_hardware_intelligence(
    base_url: str,
    headers: dict[str, str],
    *,
    parent_id: str,
) -> None:
    locations: dict[str, str] = {}
    labels = ("Metric T-nuts", "Short Flat-head Screws", "Long Flat-head Screws")
    for label in labels:
        _, location = request_json(
            "POST",
            f"{base_url}/locations",
            payload={
                "type": "bin",
                "label": label,
                "parent_id": parent_id,
                "accepts_items": True,
                "capacity_class": "small",
            },
            headers=headers,
        )
        locations[label] = str(location["id"])

    example_names = {"M3 T-nuts", "M4 T-nuts", "M6 T-nuts"}
    for name in sorted(example_names):
        request_json(
            "POST",
            f"{base_url}/inventory",
            payload={
                "name": name,
                "bin_id": locations["Metric T-nuts"],
                "quantity": 10,
                "unit": "pieces",
            },
            headers=headers,
        )

    _, inventory_snapshot = request_json(
        "GET",
        f"{base_url}/inventory",
        headers=headers,
    )
    learned_examples = [
        item
        for item in inventory_snapshot.get("items", [])
        if isinstance(item, dict) and item.get("name") in example_names
    ]
    expected_home = locations["Metric T-nuts"]
    if len(learned_examples) != 3 or any(
        item.get("home_location_id") != expected_home for item in learned_examples
    ):
        raise SmokeFailure(
            "hosted inventory did not expose both related hardware homes: "
            f"items={learned_examples!r}"
        )

    family_command = "put away M5 T-nuts"
    _, family_proposal = request_json(
        "POST",
        f"{base_url}/assistant/chat",
        payload={"text": family_command, "source": "mobile"},
        headers=headers,
    )
    family_decision = family_proposal.get("decision")
    family_destination = (
        family_decision.get("destination")
        if isinstance(family_decision, dict)
        else None
    )
    family_placement = (
        family_decision.get("placement")
        if isinstance(family_decision, dict)
        else None
    )
    family_confirmation = (
        family_decision.get("confirmation")
        if isinstance(family_decision, dict)
        else None
    )
    family_explanation = (
        family_confirmation.get("explanation")
        if isinstance(family_confirmation, dict)
        else None
    )
    if (
        not isinstance(family_decision, dict)
        or family_decision.get("action") != "put_away"
        or not isinstance(family_destination, dict)
        or family_destination.get("id") != locations["Metric T-nuts"]
        or not isinstance(family_placement, dict)
        or "learned home from 3 similar items"
        not in family_placement.get("reasons", [])
        or not isinstance(family_explanation, dict)
        or "3 similar items"
        not in str(family_explanation.get("summary") or "")
    ):
        raise SmokeFailure(
            "hosted assistant did not reuse the learned home for related hardware: "
            f"action={family_decision.get('action') if isinstance(family_decision, dict) else None!r} "
            f"destination={family_destination.get('id') if isinstance(family_destination, dict) else None!r} "
            f"reasons={family_placement.get('reasons') if isinstance(family_placement, dict) else None!r} "
            f"explanation={family_explanation!r}"
        )
    request_json(
        "POST",
        f"{base_url}/assistant/cancel",
        payload={
            "command": family_command,
            "source_interaction_id": family_proposal.get("interaction_id"),
        },
        headers=headers,
    )

    screw_locations = {
        '#8 x 1/2" flush mount screws': locations["Short Flat-head Screws"],
        '#8 x 3/4" flush mount screws': locations["Long Flat-head Screws"],
    }
    for name, location_id in screw_locations.items():
        request_json(
            "POST",
            f"{base_url}/inventory",
            payload={
                "name": name,
                "bin_id": location_id,
                "quantity": 10,
                "unit": "pieces",
            },
            headers=headers,
        )

    _, ambiguous = request_json(
        "POST",
        f"{base_url}/assistant/chat",
        payload={"text": "put away #8 flush mount screws", "source": "mobile"},
        headers=headers,
    )
    ambiguous_decision = ambiguous.get("decision")
    candidates = (
        ambiguous_decision.get("identity_candidates")
        if isinstance(ambiguous_decision, dict)
        else None
    )
    candidate_names = (
        {
            str(candidate.get("name") or "")
            for candidate in candidates
            if isinstance(candidate, dict)
        }
        if isinstance(candidates, list)
        else set()
    )
    if (
        not isinstance(ambiguous_decision, dict)
        or ambiguous_decision.get("action") != "clarify_item_identity"
        or candidate_names != set(screw_locations)
    ):
        raise SmokeFailure(
            "hosted assistant did not clarify ambiguous hardware lengths"
        )

    exact_command = "put away #8 x 3/4 inch flush mount screw"
    _, exact = request_json(
        "POST",
        f"{base_url}/assistant/chat",
        payload={"text": exact_command, "source": "mobile"},
        headers=headers,
    )
    exact_decision = exact.get("decision")
    exact_destination = (
        exact_decision.get("destination")
        if isinstance(exact_decision, dict)
        else None
    )
    if (
        not isinstance(exact_decision, dict)
        or exact_decision.get("action") != "put_away"
        or exact_decision.get("existing_item") is not True
        or not isinstance(exact_destination, dict)
        or exact_destination.get("id") != locations["Long Flat-head Screws"]
    ):
        raise SmokeFailure(
            "hosted assistant did not resolve an exact hardware length"
        )
    request_json(
        "POST",
        f"{base_url}/assistant/cancel",
        payload={
            "command": exact_command,
            "source_interaction_id": exact.get("interaction_id"),
        },
        headers=headers,
    )


def exercise_tenants(
    base_url: str,
    api_url: str,
    publishable_key: str,
    service_key: str,
    *,
    migrate_legacy_fixture: Callable[[str], None] | None = None,
) -> None:
    nonce = secrets.token_hex(6)
    password = f"Magpie-smoke-{secrets.token_urlsafe(18)}"
    users: list[str] = []
    try:
        first_email = f"magpie-smoke-{nonce}-one@example.test"
        second_email = f"magpie-smoke-{nonce}-two@example.test"
        first_id, first_token = signup(
            api_url, publishable_key, first_email, password
        )
        users.append(first_id)
        second_id, second_token = signup(
            api_url, publishable_key, second_email, password
        )
        users.append(second_id)

        _, session = request_json(
            "GET", f"{base_url}/client/bootstrap", headers=auth_headers(first_token)
        )
        identity = session.get("identity")
        if not isinstance(identity, dict) or identity.get("role") != "authenticated":
            raise SmokeFailure("first-time user was not admitted to workspace setup")
        if session.get("workspace") is not None or session.get("workspaces") != []:
            raise SmokeFailure("first-time bootstrap returned an assigned workspace")
        capabilities = session.get("capabilities")
        if (
            not isinstance(capabilities, dict)
            or capabilities.get("schema") != "magpie.capabilities.v1"
        ):
            raise SmokeFailure("client bootstrap did not include capabilities")

        first_workspace_name = f"Hosted Smoke One {nonce}"
        second_workspace_name = f"Hosted Smoke Two {nonce}"
        _, first_workspace = request_json(
            "POST",
            f"{base_url}/workspaces",
            payload={"name": first_workspace_name},
            headers=auth_headers(first_token),
            expected={201},
        )
        _, second_workspace = request_json(
            "POST",
            f"{base_url}/workspaces",
            payload={"name": second_workspace_name},
            headers=auth_headers(second_token),
            expected={201},
        )
        first_workspace_id = str(first_workspace["workspace"]["id"])
        second_workspace_id = str(second_workspace["workspace"]["id"])
        first = auth_headers(first_token, first_workspace_id)
        second = auth_headers(second_token, second_workspace_id)
        verify_pwa_contract(base_url)
        verify_client_surfaces(base_url, first)

        if migrate_legacy_fixture is not None:
            migrate_legacy_fixture(first_workspace_id)
            _, legacy_catalog = request_json(
                "GET",
                f"{base_url}/locations?include_archived=true",
                headers=first,
            )
            legacy_locations = {
                str(row.get("id") or ""): row
                for row in legacy_catalog.get("locations", [])
                if isinstance(row, dict)
            }
            if (
                legacy_locations.get("legacy-main-cabinet", {}).get("active") is not True
                or legacy_locations.get("legacy-bin", {}).get("parent_id")
                != "legacy-main-cabinet"
                or legacy_locations.get(
                    "legacy-main-cabinet-duplicate", {}
                ).get("active")
                is not False
                or "legacy-undone-room" in legacy_locations
            ):
                raise SmokeFailure(
                    "legacy SQLite migration did not preserve the repaired storage graph"
                )
            _, legacy_inventory = request_json(
                "GET", f"{base_url}/inventory", headers=first
            )
            legacy_items = legacy_inventory.get("items")
            if (
                not isinstance(legacy_items, list)
                or not any(
                    isinstance(row, dict)
                    and row.get("name") == "Legacy Caliper"
                    and row.get("bin_id") == "legacy-bin"
                    for row in legacy_items
                )
            ):
                raise SmokeFailure(
                    "legacy SQLite migration did not preserve cabinet inventory"
                )

        _, bootstrap = request_json(
            "GET", f"{base_url}/client/bootstrap", headers=first
        )
        if bootstrap.get("workspace", {}).get("id") != first_workspace_id:
            raise SmokeFailure("client bootstrap did not select the requested workspace")
        if first_workspace_id not in {
            row.get("id") for row in bootstrap.get("workspaces", [])
        }:
            raise SmokeFailure("client bootstrap omitted the workspace catalog")

        exercise_atomic_csv_path_import(
            base_url,
            first,
            nonce=nonce,
        )

        garage_label = f"Smoke Garage {nonce}"
        attic_label = f"Smoke Attic {nonce}"
        _, locations_before_voice_setup = request_json(
            "GET", f"{base_url}/locations?include_archived=true", headers=first
        )
        before_voice_setup_ids = {
            str(row.get("id") or "")
            for row in locations_before_voice_setup.get("locations", [])
            if isinstance(row, dict)
        }
        _, voice_setup = request_json(
            "POST",
            f"{base_url}/assistant/chat",
            payload={
                "text": f"Create a storage place called {garage_label}",
                "source": "voice",
            },
            headers=first,
        )
        voice_setup_decision = voice_setup.get("decision")
        if (
            not isinstance(voice_setup_decision, dict)
            or voice_setup_decision.get("action")
            != "place_configuration_ui_required"
            or voice_setup_decision.get("places_url") != "/storage?view=places"
            or voice_setup_decision.get("plan") != []
        ):
            raise SmokeFailure("voice storage setup did not hand off to the visual UI")
        _, locations_after_voice_setup = request_json(
            "GET", f"{base_url}/locations?include_archived=true", headers=first
        )
        after_voice_setup_ids = {
            str(row.get("id") or "")
            for row in locations_after_voice_setup.get("locations", [])
            if isinstance(row, dict)
        }
        if after_voice_setup_ids != before_voice_setup_ids:
            raise SmokeFailure("voice storage setup changed the storage graph")

        garage_setup = ui_storage_setup(
            base_url,
            first,
            root_name=garage_label,
            root_type="room",
            container_name="Smoke Tool Box",
        )
        attic_setup = ui_storage_setup(
            base_url,
            first,
            root_name=attic_label,
            root_type="room",
            container_name="Smoke Jewelry Box",
        )
        garage_id = str(garage_setup["root"]["id"])
        attic_id = str(attic_setup["root"]["id"])
        _, location_catalog = request_json("GET", f"{base_url}/locations", headers=first)
        child = location_by_label(
            location_catalog, "Smoke Tool Box", parent_id=garage_id
        )
        jewelry_box = location_by_label(
            location_catalog, "Smoke Jewelry Box", parent_id=attic_id
        )
        child_id = str(child["id"])
        jewelry_box_id = str(jewelry_box["id"])
        _, roots_answer = request_json(
            "POST",
            f"{base_url}/assistant/chat",
            payload={"text": "What storage spaces do I have?", "source": "web"},
            headers=first,
        )
        roots_trace = roots_answer.get("trace")
        roots_entries = [
            entry
            for entry in roots_trace
            if isinstance(entry, dict) and entry.get("tool") == "list_storage_roots"
        ] if isinstance(roots_trace, list) else []
        root_results = roots_entries[0].get("result") if roots_entries else None
        returned_root_ids = {
            str(root.get("id") or "")
            for root in root_results
            if isinstance(root, dict)
        } if isinstance(root_results, list) else set()
        if not {garage_id, attic_id}.issubset(returned_root_ids):
            raise SmokeFailure("agent did not discover both hosted storage roots")

        _, label_answer = request_json(
            "POST",
            f"{base_url}/assistant/chat",
            payload={"text": f"labels for {attic_label}", "source": "web"},
            headers=first,
        )
        label_trace = label_answer.get("trace")
        label_entries = [
            entry
            for entry in label_trace
            if isinstance(entry, dict) and entry.get("tool") == "preview_label_sheet"
        ] if isinstance(label_trace, list) else []
        label_result = label_entries[0].get("result") if label_entries else None
        if (
            not isinstance(label_result, dict)
            or label_result.get("found") is not True
            or label_result.get("location_id") != attic_id
        ):
            raise SmokeFailure("agent did not preview labels for the hosted storage root")

        _, photo = request_json(
            "POST",
            f"{base_url}/photos",
            payload={
                "filename": "hosted-smoke.jpg",
                "content_type": "image/jpeg",
                "data_base64": base64.b64encode(b"hosted-smoke-image").decode("ascii"),
            },
            headers=first,
        )
        photo_id = int(photo["photo"]["id"])
        photo_put_command = "put away hosted smoke screwdriver from this photo"
        _, photo_proposal = request_json(
            "POST",
            f"{base_url}/assistant/chat",
            payload={
                "text": photo_put_command,
                "photo_ids": [photo_id],
                "current_location_id": child_id,
                "source": "mobile",
            },
            headers=first,
        )
        photo_decision = photo_proposal.get("decision")
        photo_plan = (
            photo_decision.get("plan")
            if isinstance(photo_decision, dict)
            else None
        )
        photo_destination = (
            photo_decision.get("destination")
            if isinstance(photo_decision, dict)
            else None
        )
        if (
            not isinstance(photo_decision, dict)
            or photo_decision.get("action") != "put_away"
            or not isinstance(photo_plan, list)
            or not photo_plan
            or not isinstance(photo_destination, dict)
            or photo_destination.get("id") != child_id
        ):
            raise SmokeFailure(
                "hosted assistant did not propose the text-and-photo put-away"
            )
        _, photo_confirmed = request_json(
            "POST",
            f"{base_url}/assistant/confirm",
            payload={
                "command": photo_put_command,
                "photo_ids": [photo_id],
                "source_interaction_id": photo_proposal.get("interaction_id"),
                "plan": photo_plan,
            },
            headers=first,
        )
        photo_applied = photo_confirmed.get("applied")
        item = (
            photo_applied[0]
            if isinstance(photo_applied, list) and photo_applied
            else None
        )
        attachment = photo_confirmed.get("photo_attachments")
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("item_id"), int)
            or not isinstance(attachment, dict)
            or attachment.get("item_links")
            != [{"photo_id": photo_id, "item_id": item["item_id"]}]
            or photo_confirmed.get("needs_review") is not True
        ):
            raise SmokeFailure(
                "hosted assistant confirmation did not preserve and flag its photo"
            )
        _, review_queue = request_json(
            "GET", f"{base_url}/inventory/review", headers=first
        )
        review_items = review_queue.get("items")
        review_item = (
            next(
                (
                    row
                    for row in review_items
                    if isinstance(row, dict)
                    and row.get("item_id") == item["item_id"]
                ),
                None,
            )
            if isinstance(review_items, list)
            else None
        )
        if not isinstance(review_item, dict) or review_item.get("photos") != [
            f"/photos/{photo_id}"
        ]:
            raise SmokeFailure(
                "hosted text-and-photo put-away did not enter the review queue"
            )
        _, reviewed = request_json(
            "POST",
            f"{base_url}/inventory/reviewed",
            payload={"item_id": item["item_id"], "bin_id": child_id},
            headers=first,
        )
        review_applied = reviewed.get("applied")
        if (
            reviewed.get("item_id") != item["item_id"]
            or reviewed.get("bin_id") != child_id
            or not isinstance(review_applied, list)
            or not any(
                isinstance(row, dict)
                and row.get("approved_review") == item["item_id"]
                for row in review_applied
            )
        ):
            raise SmokeFailure("hosted inventory review was not approved")
        _, cleared_review_queue = request_json(
            "GET", f"{base_url}/inventory/review", headers=first
        )
        if any(
            isinstance(row, dict) and row.get("item_id") == item["item_id"]
            for row in cleared_review_queue.get("items", [])
        ):
            raise SmokeFailure("approved hosted inventory remained in the review queue")
        _, contents = request_json(
            "GET",
            f"{base_url}/locations/{garage_id}/contents?limit=30",
            headers=first,
        )
        content_items = contents.get("items")
        if (
            contents.get("total") != 1
            or not isinstance(content_items, list)
            or len(content_items) != 1
            or content_items[0].get("photos") != [f"/photos/{photo_id}"]
        ):
            raise SmokeFailure("hosted paged contents did not hydrate its item and photo")
        _, filtered_contents = request_json(
            "GET",
            f"{base_url}/locations/{garage_id}/contents?limit=30&q=hosted%20smoke",
            headers=first,
        )
        filtered_items = filtered_contents.get("items")
        if (
            filtered_contents.get("total") != 1
            or not isinstance(filtered_items, list)
            or len(filtered_items) != 1
            or filtered_items[0].get("photos") != [f"/photos/{photo_id}"]
        ):
            raise SmokeFailure(
                "hosted filtered contents did not hydrate its item and photo"
            )
        _, global_search = request_json(
            "GET",
            f"{base_url}/search?q=hosted%20smoke",
            headers=first,
        )
        search_results = global_search.get("results")
        if (
            not isinstance(search_results, list)
            or len(search_results) != 1
            or search_results[0].get("item_id") != item.get("item_id")
            or search_results[0].get("photos") != [f"/photos/{photo_id}"]
        ):
            raise SmokeFailure(
                "hosted global search did not hydrate its item and photo"
            )

        item_id = int(item["item_id"])
        _, moved = request_json(
            "POST",
            f"{base_url}/inventory/move",
            payload={
                "item_id": item_id,
                "from_bin": child_id,
                "to_bin": jewelry_box_id,
            },
            headers=first,
        )
        if moved.get("to_bin") != jewelry_box_id:
            raise SmokeFailure("cross-root move did not report its destination")
        _, attic_contents = request_json(
            "GET", f"{base_url}/locations/{attic_id}/contents?limit=30", headers=first
        )
        if attic_contents.get("total") != 1:
            raise SmokeFailure("cross-root move was not visible from the destination root")
        _, undo = request_json("POST", f"{base_url}/assistant/undo", headers=first)
        undone = undo.get("undone")
        if (
            not undo.get("ok")
            or not isinstance(undone, list)
            or not undone
            or undone[0].get("moved_item") != item_id
            or undone[0].get("to_location_id") != child_id
        ):
            raise SmokeFailure("cross-root move was not undoable")
        _, restored_contents = request_json(
            "GET", f"{base_url}/locations/{garage_id}/contents?limit=30", headers=first
        )
        if restored_contents.get("total") != 1:
            raise SmokeFailure("move undo did not restore the source root")

        exercise_edge_controller(
            base_url,
            first,
            workspace_id=first_workspace_id,
            other_workspace_id=second_workspace_id,
            location_id=child_id,
            nonce=nonce,
        )

        rule_command = "I always keep hosted heirloom jewelry in the smoke jewelry box"
        _, learned = request_json(
            "POST",
            f"{base_url}/assistant/confirm",
            payload={
                "command": rule_command,
                "plan": [
                    {
                        "tool": "learn_rule",
                        "arguments": {
                            "rule_text": "Hosted heirloom jewelry goes in the smoke jewelry box.",
                            "trigger_terms": ["hosted", "heirloom", "jewelry"],
                            "target_location_id": jewelry_box_id,
                        },
                    }
                ],
            },
            headers=first,
        )
        if not learned.get("ok"):
            raise SmokeFailure("confirmed placement rule was not learned")
        _, jewelry_proposal = request_json(
            "POST",
            f"{base_url}/assistant/chat",
            payload={"text": "put away hosted heirloom jewelry", "source": "mobile"},
            headers=first,
        )
        decision = jewelry_proposal.get("decision")
        destination = decision.get("destination") if isinstance(decision, dict) else None
        adherence = (
            decision.get("learning", {}).get("rule_adherence")
            if isinstance(decision, dict) and isinstance(decision.get("learning"), dict)
            else None
        )
        if (
            not isinstance(decision, dict)
            or decision.get("action") != "put_away"
            or not isinstance(destination, dict)
            or destination.get("id") != jewelry_box_id
            or not isinstance(adherence, list)
            or not any(row.get("followed") is True for row in adherence if isinstance(row, dict))
        ):
            raise SmokeFailure("future put-away did not follow the learned placement rule")
        jewelry_plan = decision.get("plan") if isinstance(decision, dict) else None
        if not isinstance(jewelry_plan, list) or not jewelry_plan:
            raise SmokeFailure("rule-guided put-away did not produce a confirmable plan")
        confirmation = decision.get("confirmation")
        explanation = (
            confirmation.get("explanation")
            if isinstance(confirmation, dict)
            else None
        )
        if (
            not isinstance(explanation, dict)
            or not str(explanation.get("summary") or "").strip()
        ):
            raise SmokeFailure("rule-guided put-away did not explain its destination")
        _, jewelry_confirmed = request_json(
            "POST",
            f"{base_url}/assistant/confirm",
            payload={
                "command": "put away hosted heirloom jewelry",
                "source_interaction_id": jewelry_proposal.get("interaction_id"),
                "plan": jewelry_plan,
            },
            headers=first,
        )
        if not jewelry_confirmed.get("ok"):
            raise SmokeFailure("rule-guided put-away was not confirmed")
        _, adherence_summary = request_json(
            "GET", f"{base_url}/assistant/learning/adherence", headers=first
        )
        if int(adherence_summary.get("followed") or 0) < 1:
            raise SmokeFailure("learned placement adherence was not recorded")
        applied = jewelry_confirmed.get("applied")
        filed_item = applied[0] if isinstance(applied, list) and applied else None
        filed_item_id = (
            filed_item.get("item_id") if isinstance(filed_item, dict) else None
        )
        if not isinstance(filed_item_id, int):
            raise SmokeFailure("rule-guided put-away did not return its saved item")
        _, corrected = request_json(
            "POST",
            f"{base_url}/assistant/followup",
            payload={
                "text": f"actually put it in {child_id}",
                "context": {
                    "kind": "filed",
                    "item": {"name": "hosted heirloom jewelry"},
                    "saved_item_id": filed_item_id,
                    "destination": {"id": jewelry_box_id},
                },
            },
            headers=first,
        )
        if (
            corrected.get("action") != "learned_placement_correction"
            or corrected.get("from_location_id") != jewelry_box_id
            or corrected.get("to_location_id") != child_id
        ):
            raise SmokeFailure("filed-item destination correction was not applied")
        corrected_rule_id = corrected.get("rule_id")
        _, corrected_reuse = request_json(
            "POST",
            f"{base_url}/assistant/chat",
            payload={"text": "put away hosted heirloom jewelry", "source": "mobile"},
            headers=first,
        )
        corrected_decision = corrected_reuse.get("decision")
        corrected_destination = (
            corrected_decision.get("destination")
            if isinstance(corrected_decision, dict)
            else None
        )
        corrected_adherence = (
            corrected_decision.get("learning", {}).get("rule_adherence")
            if isinstance(corrected_decision, dict)
            and isinstance(corrected_decision.get("learning"), dict)
            else None
        )
        if (
            not isinstance(corrected_destination, dict)
            or corrected_destination.get("id") != child_id
            or not isinstance(corrected_adherence, list)
            or not any(
                isinstance(row, dict)
                and row.get("rule_id") == corrected_rule_id
                and row.get("followed") is True
                for row in corrected_adherence
            )
        ):
            raise SmokeFailure("future put-away did not retain the corrected placement rule")

        _, html_headers, labels_html = request_bytes(
            "GET", f"{base_url}/locations/{attic_id}/labels.html", headers=first
        )
        if (
            "text/html" not in html_headers.get("content-type", "")
            or b"<svg" not in labels_html
            or attic_label.encode("utf-8") not in labels_html
        ):
            raise SmokeFailure("location label sheet did not contain workspace QR labels")
        _, pdf_headers, labels_pdf = request_bytes(
            "GET", f"{base_url}/locations/{attic_id}/labels.pdf", headers=first
        )
        if (
            "application/pdf" not in pdf_headers.get("content-type", "")
            or not labels_pdf.startswith(b"%PDF")
        ):
            raise SmokeFailure("location label sheet was not exported as a PDF")
        _, item_pdf_headers, item_pdf = request_bytes(
            "GET", f"{base_url}/items/{item_id}/label.pdf", headers=first
        )
        if (
            "application/pdf" not in item_pdf_headers.get("content-type", "")
            or not item_pdf.startswith(b"%PDF")
        ):
            raise SmokeFailure("item label was not exported as a PDF")
        scan_url = (
            f"{base_url}/scan/{urllib.parse.quote(first_workspace_id, safe='')}"
            f"/l/{urllib.parse.quote(jewelry_box_id, safe='')}"
        )
        _, scan_headers, scan_html = request_bytes("GET", scan_url)
        if (
            "text/html" not in scan_headers.get("content-type", "")
            or b"magpie.auth.workspace.v1" not in scan_html
            or jewelry_box_id.encode("utf-8") not in scan_html
        ):
            raise SmokeFailure("workspace-qualified QR route did not open the storage shell")

        _, lifecycle_location = request_json(
            "POST",
            f"{base_url}/locations",
            payload={
                "type": "box",
                "label": f"Smoke Lifecycle Box {nonce}",
                "parent_id": attic_id,
            },
            headers=first,
        )
        lifecycle_id = str(lifecycle_location["id"])
        _, archived = request_json(
            "POST", f"{base_url}/locations/{lifecycle_id}/archive", headers=first
        )
        if lifecycle_id not in archived.get("archived_ids", []):
            raise SmokeFailure("location archive did not include the requested location")
        _, archived_catalog = request_json(
            "GET", f"{base_url}/locations?include_archived=true", headers=first
        )
        archived_row = location_by_label(
            archived_catalog, f"Smoke Lifecycle Box {nonce}", parent_id=attic_id
        )
        if archived_row.get("active") is not False:
            raise SmokeFailure("archived location remained active in the storage graph")
        _, restored = request_json(
            "POST", f"{base_url}/locations/{lifecycle_id}/restore", headers=first
        )
        if lifecycle_id not in restored.get("restored_ids", []):
            raise SmokeFailure("location restore did not include the requested location")
        _, active_catalog = request_json("GET", f"{base_url}/locations", headers=first)
        restored_row = location_by_label(
            active_catalog, f"Smoke Lifecycle Box {nonce}", parent_id=attic_id
        )
        if restored_row.get("active") is not True:
            raise SmokeFailure("restored location did not return to the active storage graph")

        restore_workspace_name = f"Hosted Restore {nonce}"
        _, restore_workspace = request_json(
            "POST",
            f"{base_url}/workspaces",
            payload={"name": restore_workspace_name},
            headers=auth_headers(first_token),
            expected={201},
        )
        restore_workspace_id = str(restore_workspace["workspace"]["id"])
        exercise_portable_workspace_restore(
            base_url,
            first,
            auth_headers(first_token, restore_workspace_id),
        )

        exercise_hardware_intelligence(
            base_url,
            first,
            parent_id=garage_id,
        )

        second_root_label = f"Tenant Two Shed {nonce}"
        request_json(
            "POST",
            f"{base_url}/locations",
            payload={"type": "room", "label": second_root_label},
            headers=second,
        )
        _, first_tree = request_json("GET", f"{base_url}/locations", headers=first)
        _, second_tree = request_json("GET", f"{base_url}/locations", headers=second)
        first_labels = {str(row["label"]) for row in first_tree["locations"]}
        second_labels = {str(row["label"]) for row in second_tree["locations"]}
        if not any(label.startswith("Smoke Garage") for label in first_labels):
            raise SmokeFailure("first workspace storage graph was not persisted")
        if (
            second_root_label in first_labels
            or garage_label in second_labels
            or attic_label in second_labels
        ):
            raise SmokeFailure("workspace storage graphs leaked into each other")

        request_json(
            "GET",
            f"{base_url}/locations",
            headers=auth_headers(first_token, second_workspace_id),
            expected={403},
        )
        request_json(
            "GET",
            f"{base_url}/locations",
            headers=auth_headers(second_token, first_workspace_id),
            expected={403},
        )
        _, chat = request_json(
            "POST",
            f"{base_url}/assistant/chat",
            payload={"text": "find hosted smoke screwdriver", "mode": "find"},
            headers=first,
        )
        if not chat.get("ok"):
            raise SmokeFailure("agent find did not complete in hosted mode")
        exercise_physical_storage_checks(base_url, first)
        _, readiness = request_json(
            "GET", f"{base_url}/product/readiness", headers=first
        )
        if readiness.get("schema") != "magpie.product_readiness.v8":
            raise SmokeFailure("hosted product readiness returned an unexpected schema")
        readiness_checks = {
            str(check.get("key") or ""): check
            for check in readiness.get("checks", [])
            if isinstance(check, dict)
        }
        for key in (
            "mobile_pwa",
            "whole_storage_agent",
            "physical_storage_integrity",
            "agent_storage_journeys",
            "agent_correction_journey",
            "workspace_management_journey",
        ):
            check = readiness_checks.get(key)
            if (
                not isinstance(check, dict)
                or check.get("required") is not True
                or check.get("passed") is not True
            ):
                raise SmokeFailure(
                    f"hosted product readiness did not prove required check {key}"
                )
        journeys = readiness.get("agent_storage_journeys")
        if not isinstance(journeys, dict) or not all(journeys.values()):
            raise SmokeFailure("hosted product readiness did not retain all agent journeys")
        correction = readiness.get("agent_correction_journey")
        if not isinstance(correction, dict) or not all(correction.values()):
            raise SmokeFailure("hosted product readiness did not retain correction proof")
        management = readiness.get("workspace_management_journey")
        if not isinstance(management, dict) or not all(management.values()):
            raise SmokeFailure(
                "hosted product readiness did not retain workspace management proof"
            )
        exercise_workspace_lifecycle(
            base_url,
            api_url,
            service_key,
            first_user_id=first_id,
            first_token=first_token,
            first_workspace_id=first_workspace_id,
            first_workspace_name=first_workspace_name,
            restore_workspace_id=restore_workspace_id,
            restore_workspace_name=restore_workspace_name,
            second_token=second_token,
            second_email=second_email,
            second_workspace_id=second_workspace_id,
        )
        users.remove(first_id)
    finally:
        for user_id in reversed(users):
            delete_user(api_url, service_key, user_id)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and verify a disposable multi-workspace Magpie deployment."
    )
    parser.add_argument("--image", default="magpie-hosted-smoke:local")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--port", type=int, default=8878)
    parser.add_argument("--keep-supabase", action="store_true")
    parser.add_argument(
        "--docker-preflight-only",
        action="store_true",
        help="Prove Docker can execute a new container, then exit without starting Supabase.",
    )
    args = parser.parse_args()

    started_supabase = False
    supabase_workdir = Path(tempfile.mkdtemp(prefix="magpie-supabase-upgrade-"))
    fixture_user_id = ""
    api_url = ""
    service_key = ""
    container_name = f"magpie-hosted-smoke-{os.getpid()}"
    try:
        print("Checking Docker container execution...", flush=True)
        verify_docker_runtime()
        if args.docker_preflight_only:
            print("Docker container execution is ready.")
            return 0
        identity = release_identity()
        print(
            f"Verifying clean candidate {identity['commit'][:12]} "
            f"({identity['branch']})...",
            flush=True,
        )
        pending_migrations = prepare_upgrade_project(supabase_workdir)
        print(
            f"Starting isolated Supabase at deployed migration "
            f"{UPGRADE_BASELINE_MIGRATION}...",
            flush=True,
        )
        started_supabase = True
        try:
            run(
                supabase_command(
                    supabase_workdir,
                    "start",
                    "-x",
                    EXCLUDED_SUPABASE_SERVICES,
                ),
                timeout_seconds=SUPABASE_START_TIMEOUT_SECONDS,
            )
        except SmokeFailure as exc:
            raise SmokeFailure(
                f"{exc}\n"
                "Local Supabase could not start. Verify that Docker can start "
                "a container backed by a named volume, then retry."
            ) from exc
        environment = supabase_environment(supabase_workdir)
        required = {"API_URL", "PUBLISHABLE_KEY"}
        missing = sorted(required - environment.keys())
        if missing:
            raise SmokeFailure(f"local Supabase did not provide: {', '.join(missing)}")
        service_key = environment.get("SECRET_KEY") or environment.get(
            "SERVICE_ROLE_KEY", ""
        )
        if not service_key:
            raise SmokeFailure("local Supabase did not provide a server secret key")
        api_url = environment["API_URL"].rstrip("/")

        print("Seeding deployed-schema upgrade fixture...")
        fixture = seed_upgrade_fixture(
            api_url,
            environment["PUBLISHABLE_KEY"],
            service_key,
        )
        fixture_user_id = fixture["user_id"]
        promote_upgrade_migrations(supabase_workdir, pending_migrations)
        print("Applying candidate migrations to the existing fixture...")
        run(
            supabase_command(
                supabase_workdir,
                "migration",
                "up",
                "--local",
                "--include-all",
            )
        )
        verify_upgrade_fixture(api_url, service_key, fixture)
        delete_user(api_url, service_key, fixture_user_id)
        fixture_user_id = ""
        print("In-place Supabase upgrade preserved the deployed fixture.")

        if not args.skip_build:
            print(
                f"Building {args.image} from clean commit "
                f"{identity['commit'][:12]}..."
            )
            run(
                [
                    "docker",
                    "build",
                    "--build-arg",
                    f"BUILD_COMMIT={identity['commit']}",
                    "--build-arg",
                    f"BUILD_BRANCH={identity['branch']}",
                    "--build-arg",
                    f"BUILT_AT={identity['built_at']}",
                    "-t",
                    args.image,
                    ".",
                ],
                timeout_seconds=IMAGE_BUILD_TIMEOUT_SECONDS,
            )

        container_api_url = re.sub(
            r"^http://(?:127\.0\.0\.1|localhost)",
            "http://host.docker.internal",
            api_url,
        )
        with tempfile.TemporaryDirectory(prefix="magpie-hosted-smoke-") as temp_dir:
            config_path = Path(temp_dir) / "runtime.json"
            config = json.loads(
                (ROOT / "config/container-supabase.example.json").read_text(encoding="utf-8")
            )
            config["storage"]["supabase_url"] = container_api_url
            config["auth"]["supabase_url"] = container_api_url
            config["edge"]["machine_url"] = "https://edge.magpie-smoke.invalid"
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

            common = [
                "--add-host",
                "host.docker.internal:host-gateway",
                "-e",
                "LED_CONFIG",
                "-e",
                "SUPABASE_PUBLISHABLE_KEY",
                "-e",
                "SUPABASE_SERVICE_ROLE_KEY",
                "-v",
                f"{config_path}:/app/config/runtime.json:ro",
            ]
            print("Checking the migrated Supabase repository contract...")
            container_environment = {
                "LED_CONFIG": "/app/config/runtime.json",
                "SUPABASE_PUBLISHABLE_KEY": environment["PUBLISHABLE_KEY"],
                "SUPABASE_SERVICE_ROLE_KEY": service_key,
            }
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    *common,
                    "--entrypoint",
                    "searchable-storage-supabase-check",
                    args.image,
                    "--config",
                    "/app/config/runtime.json",
                ],
                environment=container_environment,
            )

            def migrate_legacy_fixture(workspace_id: str) -> None:
                legacy_dir = Path(temp_dir) / "legacy"
                legacy_dir.mkdir()
                # The production image runs as UID 10001 and must create this
                # disposable SQLite fixture through the bind mount.
                legacy_dir.chmod(0o777)
                fixture_script = """
from searchable_storage_led.locations import Location, LocationStore
from searchable_storage_led.store import InventoryStore

path = "/legacy/inventory.db"
inventory = InventoryStore(path)
locations = LocationStore(path)
locations.upsert(Location(
    id="legacy-main-cabinet-duplicate",
    type="room",
    label="Legacy Main Cabinet",
    accepts_items=False,
))
locations.archive_subtree("legacy-main-cabinet-duplicate")
locations.upsert(Location(
    id="legacy-main-cabinet",
    type="cabinet",
    label="Legacy Main Cabinet",
    accepts_items=False,
))
locations.upsert(Location(
    id="legacy-bin",
    type="bin",
    parent_id="legacy-main-cabinet",
    label="Legacy Tools",
))
locations.upsert(Location(
    id="legacy-undone-room",
    type="room",
    label="Legacy Undone Room",
    accepts_items=False,
))
locations.record_event(
    event_type="location_created",
    location_id="legacy-undone-room",
    note="room in",
)
locations.delete_subtree("legacy-undone-room")
item_id = inventory.upsert_item(name="Legacy Caliper", category="tools")
inventory.put_inventory(
    item_id=item_id,
    bin_id="legacy-bin",
    quantity=1,
    unit="each",
    normalize_bin=False,
)
""".strip()
                run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "-v",
                        f"{legacy_dir}:/legacy",
                        "--entrypoint",
                        "python",
                        args.image,
                        "-c",
                        fixture_script,
                    ]
                )
                run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--add-host",
                        "host.docker.internal:host-gateway",
                        "-e",
                        "LED_CONFIG",
                        "-e",
                        "SUPABASE_PUBLISHABLE_KEY",
                        "-e",
                        "SUPABASE_SERVICE_ROLE_KEY",
                        "-v",
                        f"{config_path}:/app/config/runtime.json:ro",
                        "-v",
                        f"{legacy_dir}:/legacy",
                        "--entrypoint",
                        "searchable-storage-migrate-supabase",
                        args.image,
                        "--config",
                        "/app/config/runtime.json",
                        "--database",
                        "/legacy/inventory.db",
                        "--workspace-id",
                        workspace_id,
                    ],
                    environment=container_environment,
                )

            run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    container_name,
                    "-p",
                    f"127.0.0.1:{args.port}:8765",
                    *common,
                    args.image,
                ],
                environment=container_environment,
            )
            base_url = f"http://127.0.0.1:{args.port}"
            wait_for_health(base_url, container_name)
            build = verify_build_identity(base_url, identity)
            print(
                f"Running container reports verified commit "
                f"{str(build['commit'])[:12]}."
            )
            print(
                "Exercising signup, agent setup, multi-root storage, photos, moves, "
                "learning, corrections, labels, lifecycle, search, secure edge routing, "
                "and isolation..."
            )
            exercise_tenants(
                base_url,
                api_url,
                environment["PUBLISHABLE_KEY"],
                service_key,
                migrate_legacy_fixture=migrate_legacy_fixture,
            )
        print("Hosted local smoke passed; disposable tenant data was removed.")
        return 0
    except (SmokeFailure, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"Hosted local smoke failed: {exc}", file=sys.stderr)
        return 1
    finally:
        run_cleanup(["docker", "rm", "-f", container_name])
        if fixture_user_id and api_url and service_key:
            try:
                delete_user(api_url, service_key, fixture_user_id)
            except (OSError, SmokeFailure):
                pass
        if started_supabase and not args.keep_supabase:
            run_cleanup(
                supabase_command(supabase_workdir, "stop", "--no-backup")
            )
        if args.keep_supabase:
            print(f"Kept local Supabase project at {supabase_workdir}.")
        else:
            shutil.rmtree(supabase_workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
